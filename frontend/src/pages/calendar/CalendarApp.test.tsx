import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import '@testing-library/jest-dom/vitest';

// Hoisted mocks

const { orchestratorPostMock } = vi.hoisted(() => ({
  orchestratorPostMock: vi.fn(),
}));

// The calendar feature was cut off the legacy `quantraApi` cloud client
// onto the orchestrator: month + range loads now POST `/v1/calendar/business-days`
// and `/v1/calendar/holidays`. This test pins that wiring.
vi.mock('../../lib/api/orchestrator', () => ({
  orchestratorPost: orchestratorPostMock,
}));

vi.mock('../../components/Header', () => ({ default: () => null }));

import CalendarApp from './CalendarApp';

function renderCalendar() {
  return render(
    <MemoryRouter>
      <CalendarApp />
    </MemoryRouter>,
  );
}

function okDates(dates: string[]) {
  return { ok: true as const, data: { dates, count: dates.length }, duration_ms: 1 };
}

describe('CalendarApp — orchestrator wiring', () => {
  beforeEach(() => {
    orchestratorPostMock.mockReset();
    orchestratorPostMock.mockImplementation((path: string) =>
      Promise.resolve(
        path === '/v1/calendar/business-days'
          ? okDates(['2026-02-02', '2026-02-03'])
          : okDates(['2026-02-16']),
      ),
    );
  });

  afterEach(() => cleanup());

  it('loads month data via the /v1/calendar/* orchestrator routes on mount', async () => {
    renderCalendar();
    await waitFor(() => {
      expect(orchestratorPostMock).toHaveBeenCalledWith(
        '/v1/calendar/business-days',
        expect.objectContaining({ include_start: true, include_end: true }),
      );
      expect(orchestratorPostMock).toHaveBeenCalledWith(
        '/v1/calendar/holidays',
        expect.objectContaining({ include_weekends: expect.any(Boolean) }),
      );
    });
    // No legacy "Not authenticated" path — calls resolve and month counts render.
    await waitFor(() =>
      expect(screen.getByText(/Business 2 · Holidays 1/)).toBeInTheDocument(),
    );
  });

  it('renders range business days + holidays returned by the orchestrator', async () => {
    renderCalendar();
    fireEvent.click(screen.getByRole('button', { name: /Load Range/i }));
    await waitFor(() => {
      expect(screen.getByText('2026-02-02')).toBeInTheDocument();
      expect(screen.getByText('2026-02-16')).toBeInTheDocument();
    });
  });

  it('surfaces the error envelope error prose when a calendar call fails', async () => {
    orchestratorPostMock.mockResolvedValue({
      ok: false as const,
      envelope: { error: 'unknown calendar: Narnia', code: 'invalid_argument' },
      httpStatus: 422,
      duration_ms: 1,
    });
    renderCalendar();
    await waitFor(() =>
      expect(screen.getByText(/unknown calendar: Narnia/)).toBeInTheDocument(),
    );
  });
});
