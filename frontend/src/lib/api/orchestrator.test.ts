import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';

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
  orchestratorPost,
  priceBondsFixed,
  priceBondsFloating,
  priceCds,
  priceEquityOption,
  priceSwapIr,
  priceSwapsInflation,
  priceSwaption,
} from './orchestrator';

// Helpers

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const BASE_AS_OF = '2026-01-15';

// Matrix: 8 product / error rows

describe('orchestrator client — 8-row matrix', () => {
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

  // Row 1: priceSwapIr — success
  it('priceSwapIr returns ok:true with data on 200', async () => {
    const payload = { results: [{ npv: 1234.5, leg_npvs: [], extras: {} }] };
    fetchMock.mockResolvedValueOnce(jsonResponse(payload));

    const result = await priceSwapIr({ as_of: BASE_AS_OF, swap: { foo: 'bar' } });

    expect(result.ok).toBe(true);
    if (result.ok) expect(result.data).toEqual(payload);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/v1\/price\/swap\/ir$/);
    expect((init.headers as Record<string, string>)['Authorization']).toBe('Bearer test-token');
  });

  // Row 2: priceSwaption — success
  it('priceSwaption returns ok:true with data on 200', async () => {
    const payload = { results: [{ npv: 99.0, extras: {} }] };
    fetchMock.mockResolvedValueOnce(jsonResponse(payload));

    const result = await priceSwaption({ as_of: BASE_AS_OF, swaption: { type: 'payer' } });

    expect(result.ok).toBe(true);
    if (result.ok) expect(result.data).toEqual(payload);
  });

  // Row 3: priceBondsFixed — success
  it('priceBondsFixed returns ok:true with data on 200', async () => {
    const payload = { results: [{ npv: 102.5 }] };
    fetchMock.mockResolvedValueOnce(jsonResponse(payload));

    const result = await priceBondsFixed({ as_of: BASE_AS_OF, bond: { coupon: 0.05 } });

    expect(result.ok).toBe(true);
    if (result.ok) expect(result.data).toEqual(payload);
    const [url] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/v1\/price\/bonds\/fixed$/);
  });

  // Row 4: priceBondsFloating — success
  it('priceBondsFloating returns ok:true with data on 200', async () => {
    const payload = { results: [{ npv: 100.1 }] };
    fetchMock.mockResolvedValueOnce(jsonResponse(payload));

    const result = await priceBondsFloating({ as_of: BASE_AS_OF, bond: { spread: 0.01 } });

    expect(result.ok).toBe(true);
    if (result.ok) expect(result.data).toEqual(payload);
    const [url] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/v1\/price\/bonds\/floating$/);
  });

  // Row 5: priceCds — success
  it('priceCds returns ok:true with data on 200', async () => {
    const payload = { results: [{ npv: -500, fair_spread: 0.012 }] };
    fetchMock.mockResolvedValueOnce(jsonResponse(payload));

    const result = await priceCds({ as_of: BASE_AS_OF, cds: { notional: 1e6 } });

    expect(result.ok).toBe(true);
    if (result.ok) expect(result.data).toEqual(payload);
  });

  // Row 6: priceEquityOption — success
  it('priceEquityOption returns ok:true with data on 200', async () => {
    const payload = { results: [{ npv: 5.5, delta: 0.6 }] };
    fetchMock.mockResolvedValueOnce(jsonResponse(payload));

    const result = await priceEquityOption({ as_of: BASE_AS_OF, equity_option: { strike: 100 } });

    expect(result.ok).toBe(true);
    if (result.ok) expect(result.data).toEqual(payload);
    const [url] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/v1\/price\/equity-option$/);
  });

  // Row 7: priceSwapsInflation — success
  it('priceSwapsInflation returns ok:true with data on 200', async () => {
    const payload = { results: [{ npv: 0.0 }] };
    fetchMock.mockResolvedValueOnce(jsonResponse(payload));

    const result = await priceSwapsInflation({ as_of: BASE_AS_OF, swap: { swap_kind: 'ZC' } });

    expect(result.ok).toBe(true);
    if (result.ok) expect(result.data).toEqual(payload);
    const [url] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/v1\/price\/swaps\/inflation$/);
  });

  // Row 8: structured error envelope — branch on code, never on prose
  it('4xx error envelope surfaces code not prose', async () => {
    const envelope = {
      error: 'Swap not found. Please check the identifier.',
      code: 'swap_ir_not_found',
      request_id: 'req-abc',
    };
    fetchMock.mockResolvedValueOnce(jsonResponse(envelope, 404));

    const result = await priceSwapIr({ as_of: BASE_AS_OF, swap_id: 'no-such-id' });

    expect(result.ok).toBe(false);
    if (!result.ok) {
      // Branch on code, not prose — never inspect envelope.error in production
      expect(result.envelope.code).toBe('swap_ir_not_found');
      expect(result.httpStatus).toBe(404);
    }
  });
});

// correlation id + client-side structured logging

describe('orchestrator client — X-Request-Id + call logging', () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let infoSpy: MockInstance;
  let errorSpy: MockInstance;

  beforeEach(() => {
    getIdTokenMock.mockResolvedValue('test-token');
    authMock.currentUser = { getIdToken: getIdTokenMock };
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    infoSpy = vi.spyOn(console, 'info').mockImplementation(() => {});
    errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('sends an X-Request-ID header on every call and logs the call', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ results: [{ npv: 1 }] }));

    await priceSwapIr({ as_of: BASE_AS_OF, swap: {} });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(headers['X-Request-ID']).toBeTruthy();
    expect(headers['X-Request-ID'].length).toBeGreaterThan(8);

    // One structured success line carrying the same id + route + status.
    expect(infoSpy).toHaveBeenCalledTimes(1);
    const logged = infoSpy.mock.calls[0][1] as Record<string, unknown>;
    expect(logged.service).toBe('portal');
    expect(logged.event).toBe('orchestrator.call');
    expect(logged.route).toBe('/v1/price/swap/ir');
    expect(logged.http_status).toBe(200);
    expect(logged.request_id).toBe(headers['X-Request-ID']);
    expect(typeof logged.duration_ms).toBe('number');
  });

  it('surfaces the orchestrator-echoed request_id in the error envelope', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ error: 'nope', code: 'swap_ir_not_found', request_id: 'req-xyz' }, 404),
    );

    const result = await priceSwapIr({ as_of: BASE_AS_OF, swap_id: 'x' });

    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.envelope.request_id).toBe('req-xyz');
    // Failure logged at error level, branded with the code.
    expect(errorSpy).toHaveBeenCalledTimes(1);
    const logged = errorSpy.mock.calls[0][1] as Record<string, unknown>;
    expect(logged.event).toBe('orchestrator.call.failed');
    expect(logged.error).toBe('swap_ir_not_found');
    expect(logged.request_id).toBe('req-xyz');
  });

  it('falls back to the sent id when the error body omits request_id', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: 'nope', code: 'bad' }, 400));

    const result = await priceSwapIr({ as_of: BASE_AS_OF, swap_id: 'x' });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const sentId = (init.headers as Record<string, string>)['X-Request-ID'];
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.envelope.request_id).toBe(sentId);
  });

  it('quiet: suppresses the error log on a transport failure but still returns the result', async () => {
    // Cold-start provision probe path (Fix 2): a transport failure must NOT
    // surface as a first-paint console error when the call is made quietly.
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));

    const result = await orchestratorPost('/auth/provision', {}, { quiet: true });

    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.envelope.code).toBe('network_error');
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it('non-quiet: still logs the error on a transport failure (unchanged default)', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));

    await orchestratorPost('/auth/provision', {});

    expect(errorSpy).toHaveBeenCalledTimes(1);
    const logged = errorSpy.mock.calls[0][1] as Record<string, unknown>;
    expect(logged.event).toBe('orchestrator.call.failed');
    expect(logged.error).toBe('network_error');
  });
});

// Dev auth bypass: no Authorization header, no getIdToken call

describe('orchestrator client — dev auth bypass (VITE_DEV_AUTH_BYPASS)', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    getIdTokenMock.mockResolvedValue('test-token');
    authMock.currentUser = { getIdToken: getIdTokenMock };
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(console, 'info').mockImplementation(() => {});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it('ON: sends NO Authorization header and never calls getIdToken; X-Request-ID still present', async () => {
    vi.stubEnv('DEV', true);
    vi.stubEnv('VITE_DEV_AUTH_BYPASS', 'true');
    fetchMock.mockResolvedValueOnce(jsonResponse({ results: [{ npv: 1 }] }));

    const result = await priceSwapIr({ as_of: BASE_AS_OF, swap: {} });

    expect(result.ok).toBe(true);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(headers['Authorization']).toBeUndefined();
    expect(headers['X-Request-ID']).toBeTruthy();
    expect(getIdTokenMock).not.toHaveBeenCalled();
  });

  it('OFF: unchanged — sends the real Firebase Bearer token', async () => {
    vi.stubEnv('VITE_DEV_AUTH_BYPASS', 'false');
    fetchMock.mockResolvedValueOnce(jsonResponse({ results: [{ npv: 1 }] }));

    const result = await priceSwapIr({ as_of: BASE_AS_OF, swap: {} });

    expect(result.ok).toBe(true);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(headers['Authorization']).toBe('Bearer test-token');
    expect(getIdTokenMock).toHaveBeenCalled();
  });
});

