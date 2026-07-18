import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Firebase mock (must be hoisted before any module that imports firebase)

const { authMock, getIdTokenMock } = vi.hoisted(() => {
  const getIdToken = vi.fn<[], Promise<string>>();
  return {
    getIdTokenMock: getIdToken,
    authMock: {
      currentUser: { getIdToken } as { getIdToken: () => Promise<string> } | null,
    },
  };
});

vi.mock('../firebase', () => ({ auth: authMock }));

import { curves, getVersion, listVersions, restoreEntityVersion, swapsIr } from './crud';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

type Call = [string, RequestInit];

function lastCall(fetchMock: ReturnType<typeof vi.fn>): Call {
  const calls = fetchMock.mock.calls;
  return calls[calls.length - 1] as Call;
}

function headerOf(init: RequestInit, name: string): string | undefined {
  return (init.headers as Record<string, string>)[name];
}

const SUMMARY = {
  version_no: 3,
  change_type: 'amend',
  change_reason: 'notional corrected',
  changed_by_uid: 'dev-user',
  changed_by_email: 'dev@quantra.local',
  changed_at: '2026-07-19T20:14:01Z',
  request_id: 'ef7e6d0f-0000-0000-0000-000000000000',
};

describe('entity versions API + X-Change-Reason plumbing', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    getIdTokenMock.mockResolvedValue('test-token');
    authMock.currentUser = { getIdToken: getIdTokenMock };
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('listVersions GETs {entityPath}/{id}/versions and returns the newest-first items', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ items: [SUMMARY] }));

    const result = await listVersions('/v1/swaps/ir', 'uuid-1');

    expect(result.ok).toBe(true);
    if (result.ok) expect(result.data.items?.[0]).toEqual(SUMMARY);
    const [url, init] = lastCall(fetchMock);
    expect(url).toMatch(/\/v1\/swaps\/ir\/uuid-1\/versions$/);
    expect(init.method).toBe('GET');
  });

  it('getVersion GETs .../versions/{n} and carries the payload snapshot', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ ...SUMMARY, payload: { name: 'my swap', request: { notional: 5e6 } } }),
    );

    const result = await getVersion('/v1/swaps/ir', 'uuid-1', 3);

    expect(result.ok).toBe(true);
    if (result.ok) expect(result.data.payload).toEqual({ name: 'my swap', request: { notional: 5e6 } });
    const [url] = lastCall(fetchMock);
    expect(url).toMatch(/\/v1\/swaps\/ir\/uuid-1\/versions\/3$/);
  });

  it('a foreign/unknown id surfaces the structured 404 envelope', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ error: 'Not found', code: 'not_found' }, 404),
    );

    const result = await listVersions('/v1/curves', 'someone-elses-id');

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.envelope.code).toBe('not_found');
      expect(result.httpStatus).toBe(404);
    }
  });

  // X-Change-Reason on the mutating verbs

  it('create sends X-Change-Reason when a changeReason is supplied', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 'uuid-1' }, 201));

    await swapsIr.create({ name: 'sw', request: {} }, { changeReason: 'initial booking' });

    const [, init] = lastCall(fetchMock);
    expect(headerOf(init, 'X-Change-Reason')).toBe('initial booking');
  });

  it('patch sends X-Change-Reason alongside the body', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 'uuid-1' }));

    await swapsIr.patch('uuid-1', { request: { notional: 6e6 } }, { changeReason: 'notional corrected' });

    const [, init] = lastCall(fetchMock);
    expect(init.method).toBe('PATCH');
    expect(headerOf(init, 'X-Change-Reason')).toBe('notional corrected');
  });

  it('no changeReason (or blank) → header absent, request otherwise unchanged', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 'uuid-1' }));
    await swapsIr.patch('uuid-1', { name: 'renamed' });
    let [, init] = lastCall(fetchMock);
    expect(headerOf(init, 'X-Change-Reason')).toBeUndefined();

    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 'uuid-1' }));
    await swapsIr.patch('uuid-1', { name: 'renamed' }, { changeReason: '   ' });
    [, init] = lastCall(fetchMock);
    expect(headerOf(init, 'X-Change-Reason')).toBeUndefined();
  });

  it('delete and restore accept the reason too', async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await curves.delete('uuid-1', { changeReason: 'obsolete' });
    let [, init] = lastCall(fetchMock);
    expect(init.method).toBe('DELETE');
    expect(headerOf(init, 'X-Change-Reason')).toBe('obsolete');

    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 'uuid-1' }));
    await curves.restore('uuid-1', { changeReason: 'oops' });
    [, init] = lastCall(fetchMock);
    expect((init.method as string) + '').toBe('POST');
    expect(headerOf(init, 'X-Change-Reason')).toBe('oops');
  });

  it('restoreEntityVersion PATCHes the entity with the snapshot body and "restored to v{n}"', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 'uuid-1' }));

    await restoreEntityVersion('/v1/swaps/ir', 'uuid-1', { name: 'sw', request: { notional: 5e6 } }, 1);

    const [url, init] = lastCall(fetchMock);
    expect(url).toMatch(/\/v1\/swaps\/ir\/uuid-1$/);
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(init.body as string)).toEqual({ name: 'sw', request: { notional: 5e6 } });
    expect(headerOf(init, 'X-Change-Reason')).toBe('restored to v1');
  });
});
