import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import '@testing-library/jest-dom/vitest';

// Mock the crud API surface the panel talks to.
const { listVersionsMock, getVersionMock, restoreEntityVersionMock } = vi.hoisted(() => ({
  listVersionsMock: vi.fn(),
  getVersionMock: vi.fn(),
  restoreEntityVersionMock: vi.fn(),
}));

vi.mock('../../lib/api/crud', () => ({
  listVersions: listVersionsMock,
  getVersion: getVersionMock,
  restoreEntityVersion: restoreEntityVersionMock,
}));

import HistoryPanel, { relativeTime } from './HistoryPanel';

const V1 = {
  version_no: 1,
  change_type: 'create',
  change_reason: null,
  changed_by_uid: 'dev-user',
  changed_by_email: 'dev@quantra.local',
  changed_at: '2026-07-19T20:10:00Z',
  request_id: 'aaaaaaaa-1111-2222-3333-444444444444',
};

const V2 = {
  version_no: 2,
  change_type: 'amend',
  change_reason: 'notional corrected',
  changed_by_uid: 'dev-user',
  changed_by_email: 'dev@quantra.local',
  changed_at: '2026-07-19T20:14:01Z',
  request_id: 'bbbbbbbb-1111-2222-3333-444444444444',
};

const PAYLOAD_V1 = {
  id: 'uuid-1',
  owner_uid: 'dev-user',
  name: 'my swap',
  request: { notional: 5000000, fixed_rate: 0.02 },
  created_at: '2026-07-19T20:10:00Z',
  updated_at: '2026-07-19T20:10:00Z',
  deleted_at: null,
};

const PAYLOAD_V2 = {
  ...PAYLOAD_V1,
  request: { notional: 6000000, fixed_rate: 0.02 },
  updated_at: '2026-07-19T20:14:01Z',
};

function ok<T>(data: T) {
  return { ok: true as const, data, duration_ms: 1, requestId: 'rid' };
}

function fail(code: string, httpStatus = 404) {
  return {
    ok: false as const,
    envelope: { error: 'boom', code },
    httpStatus,
    duration_ms: 1,
    requestId: 'rid',
  };
}

async function openPanel() {
  render(<HistoryPanel entityPath="/v1/swaps/ir" entityId="uuid-1" />);
  await userEvent.click(screen.getByRole('button', { name: /History/ }));
}

beforeEach(() => {
  listVersionsMock.mockResolvedValue(ok({ items: [V2, V1] }));
  getVersionMock.mockImplementation((_path: string, _id: string, n: number) =>
    Promise.resolve(ok(n === 1 ? { ...V1, payload: PAYLOAD_V1 } : { ...V2, payload: PAYLOAD_V2 })),
  );
  restoreEntityVersionMock.mockResolvedValue(ok({ id: 'uuid-1' }));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('HistoryPanel — timeline', () => {
  it('renders the timeline newest-first with badge, chip, actor, time and reason', async () => {
    await openPanel();

    const rows = await screen.findAllByTestId('history-row');
    expect(rows).toHaveLength(2);
    expect(listVersionsMock).toHaveBeenCalledWith('/v1/swaps/ir', 'uuid-1');

    // Newest (v2, amend) first.
    const first = within(rows[0]);
    expect(first.getByText('v2')).toBeInTheDocument();
    expect(first.getByTestId('history-chip')).toHaveTextContent('amend');
    expect(first.getByTestId('history-actor')).toHaveTextContent('dev@quantra.local');
    expect(first.getByTestId('history-reason')).toHaveTextContent('notional corrected');
    // Truncated request id (first 8 chars).
    expect(first.getByText(/bbbbbbbb…/)).toBeInTheDocument();

    const second = within(rows[1]);
    expect(second.getByText('v1')).toBeInTheDocument();
    expect(second.getByTestId('history-chip')).toHaveTextContent('create');
    // v1 has no reason.
    expect(second.queryByTestId('history-reason')).not.toBeInTheDocument();
  });

  it('falls back to the uid when the email is missing', async () => {
    listVersionsMock.mockResolvedValue(ok({ items: [{ ...V1, changed_by_email: null }] }));
    await openPanel();

    const row = await screen.findByTestId('history-row');
    expect(within(row).getByTestId('history-actor')).toHaveTextContent('dev-user');
  });

  it('surfaces the structured error on a failed load', async () => {
    listVersionsMock.mockResolvedValue(fail('not_found'));
    await openPanel();

    expect(await screen.findByTestId('history-error')).toHaveTextContent('not_found');
  });
});

describe('HistoryPanel — snapshot view', () => {
  it('clicking one version fetches and renders the read-only snapshot', async () => {
    await openPanel();
    const rows = await screen.findAllByTestId('history-row');
    await userEvent.click(rows[1]); // v1

    const snapshot = await screen.findByTestId('history-snapshot');
    expect(getVersionMock).toHaveBeenCalledWith('/v1/swaps/ir', 'uuid-1', 1);
    expect(within(snapshot).getByText('request.notional')).toBeInTheDocument();
    expect(within(snapshot).getByText('5000000')).toBeInTheDocument();
  });
});

describe('HistoryPanel — diff view', () => {
  it('selecting two versions shows only changed keys, old → new', async () => {
    await openPanel();
    const rows = await screen.findAllByTestId('history-row');
    await userEvent.click(rows[0]); // v2
    await userEvent.click(rows[1]); // v1

    const diff = await screen.findByTestId('history-diff');
    await waitFor(() => {
      expect(within(diff).getAllByTestId('history-diff-row')).toHaveLength(1);
    });
    const row = within(diff).getByTestId('history-diff-row');
    expect(row).toHaveAttribute('data-kind', 'changed');
    expect(row).toHaveAttribute('data-path', 'request.notional');
    expect(within(row).getByText('5000000')).toBeInTheDocument();
    expect(within(row).getByText('6000000')).toBeInTheDocument();
    // fixed_rate did not change — not shown.
    expect(within(diff).queryByText('request.fixed_rate')).not.toBeInTheDocument();
  });

  it('shows added and removed keys distinctly', async () => {
    getVersionMock.mockImplementation((_p: string, _i: string, n: number) =>
      Promise.resolve(
        ok(
          n === 1
            ? { ...V1, payload: { name: 'sw', request: { old_field: 'x' } } }
            : { ...V2, payload: { name: 'sw', request: { new_field: { nested: 1 } } } },
        ),
      ),
    );
    await openPanel();
    const rows = await screen.findAllByTestId('history-row');
    await userEvent.click(rows[0]);
    await userEvent.click(rows[1]);

    const diff = await screen.findByTestId('history-diff');
    await waitFor(() => {
      expect(within(diff).getAllByTestId('history-diff-row')).toHaveLength(2);
    });
    const kinds = within(diff)
      .getAllByTestId('history-diff-row')
      .map(r => [r.getAttribute('data-path'), r.getAttribute('data-kind')]);
    expect(kinds).toContainEqual(['request.old_field', 'removed']);
    expect(kinds).toContainEqual(['request.new_field.nested', 'added']);
  });
});

describe('HistoryPanel — restore', () => {
  it('restore sends the entity PATCH with the snapshot-derived editable body and refreshes', async () => {
    await openPanel();
    const rows = await screen.findAllByTestId('history-row');
    await userEvent.click(rows[1]); // v1 snapshot
    await screen.findByTestId('history-snapshot');

    listVersionsMock.mockClear();
    await userEvent.click(screen.getByTestId('history-restore'));

    await waitFor(() => {
      expect(restoreEntityVersionMock).toHaveBeenCalledWith(
        '/v1/swaps/ir',
        'uuid-1',
        // ONLY the editable keys — no id / owner_uid / timestamps.
        { name: 'my swap', request: { notional: 5000000, fixed_rate: 0.02 } },
        1,
      );
    });
    // Timeline refreshed after the restore.
    await waitFor(() => expect(listVersionsMock).toHaveBeenCalled());
    expect(await screen.findByTestId('history-notice')).toHaveTextContent('Restored to v1');
  });

  it('a failed restore surfaces the envelope code and does not refresh', async () => {
    restoreEntityVersionMock.mockResolvedValue(fail('validation_error', 422));
    await openPanel();
    const rows = await screen.findAllByTestId('history-row');
    await userEvent.click(rows[1]);
    await screen.findByTestId('history-snapshot');

    listVersionsMock.mockClear();
    await userEvent.click(screen.getByTestId('history-restore'));

    expect(await screen.findByTestId('history-notice')).toHaveTextContent('validation_error');
    expect(listVersionsMock).not.toHaveBeenCalled();
  });
});

describe('relativeTime', () => {
  it('buckets seconds/minutes/hours/days', () => {
    const now = new Date('2026-07-19T12:00:00Z');
    expect(relativeTime('2026-07-19T11:59:30Z', now)).toBe('just now');
    expect(relativeTime('2026-07-19T11:45:00Z', now)).toBe('15m ago');
    expect(relativeTime('2026-07-19T09:00:00Z', now)).toBe('3h ago');
    expect(relativeTime('2026-07-12T12:00:00Z', now)).toBe('7d ago');
    expect(relativeTime('not-a-date', now)).toBe('not-a-date');
  });
});
