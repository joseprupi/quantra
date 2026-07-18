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

import {
  bondsFixed,
  bondsFloating,
  cds,
  creditCurves,
  curveSets,
  curves,
  equityOptions,
  indices,
  swapsInflation,
  swapsIr,
  swaptionModels,
  swaptions,
  volSurfaces,
} from './crud';
import type { ListParams } from './crud';

// Helpers

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function noContentResponse(): Response {
  return new Response(null, { status: 204 });
}

type Call = [string, RequestInit];

function lastCall(fetchMock: ReturnType<typeof vi.fn>): Call {
  const calls = fetchMock.mock.calls;
  return calls[calls.length - 1] as Call;
}

function headerOf(init: RequestInit, name: string): string | undefined {
  return (init.headers as Record<string, string>)[name];
}

describe('crud client wrappers', () => {
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

  // Request shape + verb/method/path mapping

  it('create POSTs to the base path with the body and Bearer auth', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 'uuid-1', name: 'USD-OIS' }, 201));

    const result = await curves.create({ name: 'USD-OIS', currency: 'USD' });

    expect(result.ok).toBe(true);
    const [url, init] = lastCall(fetchMock);
    expect(url).toMatch(/\/v1\/curves$/);
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ name: 'USD-OIS', currency: 'USD' });
    expect(headerOf(init, 'Authorization')).toBe('Bearer test-token');
  });

  it('get reads /{id} with GET and no body', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 'uuid-1' }));

    await curves.get('uuid-1');

    const [url, init] = lastCall(fetchMock);
    expect(url).toMatch(/\/v1\/curves\/uuid-1$/);
    expect(init.method).toBe('GET');
    expect(init.body).toBeUndefined();
  });

  it('list serialises limit/offset as query params', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ items: [], page: {} }));

    await curves.list({ limit: 25, offset: 50 });

    const [url, init] = lastCall(fetchMock);
    expect(url).toMatch(/\/v1\/curves\?limit=25&offset=50$/);
    expect(init.method).toBe('GET');
  });

  it('list with no params hits the bare path', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ items: [], page: {} }));

    await curves.list();

    const [url] = lastCall(fetchMock);
    expect(url).toMatch(/\/v1\/curves$/);
  });

  it('patch PATCHes /{id} with the partial body', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 'uuid-1', name: 'renamed' }));

    await curves.patch('uuid-1', { name: 'renamed' });

    const [url, init] = lastCall(fetchMock);
    expect(url).toMatch(/\/v1\/curves\/uuid-1$/);
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(init.body as string)).toEqual({ name: 'renamed' });
  });

  it('delete DELETEs /{id} and resolves ok on 204 with no body', async () => {
    fetchMock.mockResolvedValueOnce(noContentResponse());

    const result = await curves.delete('uuid-1');

    expect(result.ok).toBe(true);
    if (result.ok) expect(result.data).toBeUndefined();
    const [url, init] = lastCall(fetchMock);
    expect(url).toMatch(/\/v1\/curves\/uuid-1$/);
    expect(init.method).toBe('DELETE');
  });

  it('restore POSTs to /{id}:restore', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 'uuid-1' }));

    await curves.restore('uuid-1');

    const [url, init] = lastCall(fetchMock);
    expect(url).toMatch(/\/v1\/curves\/uuid-1:restore$/);
    expect(init.method).toBe('POST');
  });

  // If-Match defaults OFF

  it('patch does NOT send If-Match by default', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 'uuid-1' }));

    await curves.patch('uuid-1', { name: 'x' });

    const [, init] = lastCall(fetchMock);
    expect(headerOf(init, 'If-Match')).toBeUndefined();
  });

  it('patch sends If-Match only when ifMatch is supplied', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 'uuid-1' }));

    await curves.patch('uuid-1', { name: 'x' }, { ifMatch: '2026-05-28T00:00:00Z' });

    const [, init] = lastCall(fetchMock);
    expect(headerOf(init, 'If-Match')).toBe('2026-05-28T00:00:00Z');
  });

  // Error mapping branches on code, never prose

  it('404 surfaces the envelope code, not the prose', async () => {
    const envelope = {
      error: 'Curve not found. Please check the identifier.',
      code: 'curve_not_found',
      request_id: 'req-1',
    };
    fetchMock.mockResolvedValueOnce(jsonResponse(envelope, 404));

    const result = await curves.get('missing');

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.envelope.code).toBe('curve_not_found');
      expect(result.httpStatus).toBe(404);
    }
  });

  it('409 name_conflict on restore surfaces code', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ error: 'A live curve already uses that name.', code: 'name_conflict' }, 409),
    );

    const result = await curves.restore('uuid-1');

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.envelope.code).toBe('name_conflict');
      expect(result.httpStatus).toBe(409);
    }
  });

  it('non-JSON error body falls back to a synthesised http_<status> envelope', async () => {
    fetchMock.mockResolvedValueOnce(new Response('upstream down', { status: 503 }));

    const result = await curves.list();

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.envelope.code).toBe('http_503');
      expect(result.httpStatus).toBe(503);
    }
  });

  it('missing auth maps to unauthenticated without calling fetch', async () => {
    authMock.currentUser = null;

    const result = await curves.create({ name: 'x' });

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.envelope.code).toBe('unauthenticated');
      expect(result.httpStatus).toBe(401);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  // Path correctness across every named-entity slot

  it.each<[string, { list: (p?: ListParams) => Promise<unknown> }, RegExp]>([
    ['indices', indices, /\/v1\/indices$/],
    ['curves', curves, /\/v1\/curves$/],
    ['curveSets', curveSets, /\/v1\/curve-sets$/],
    ['creditCurves', creditCurves, /\/v1\/credit-curves$/],
    ['volSurfaces', volSurfaces, /\/v1\/vol-surfaces$/],
    ['swaptionModels', swaptionModels, /\/v1\/swaption-models$/],
    ['swapsIr', swapsIr, /\/v1\/swaps\/ir$/],
    ['swapsInflation', swapsInflation, /\/v1\/swaps\/inflation$/],
    ['swaptions', swaptions, /\/v1\/swaptions$/],
    ['bondsFixed', bondsFixed, /\/v1\/bonds\/fixed$/],
    ['bondsFloating', bondsFloating, /\/v1\/bonds\/floating$/],
    ['cds', cds, /\/v1\/cds$/],
    ['equityOptions', equityOptions, /\/v1\/equity-options$/],
  ])('%s.list targets the correct prefix', async (_name, client, re) => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ items: [], page: {} }));

    await client.list();

    const [url] = lastCall(fetchMock);
    expect(url).toMatch(re);
  });
});
