import { describe, expect, it } from 'vitest';

import { categoryForCode, mapEnvelopeToErrorInfo, suggestionForCode } from './errorEnvelope';
import type { ApiErrorEnvelope } from './types';

function env(over: Partial<ApiErrorEnvelope> = {}): ApiErrorEnvelope {
  return { error: 'real upstream reason', code: 'x', ...over };
}

describe('errorEnvelope — code → category (branch on code, never prose; inv. 9)', () => {
  it('infra codes map to fixed categories regardless of HTTP status', () => {
    expect(categoryForCode('unauthenticated', 200)).toBe('auth');
    expect(categoryForCode('network_error', 200)).toBe('network');
    expect(categoryForCode('engine_unavailable', 502)).toBe('unavailable');
    expect(categoryForCode('engine_timeout', 504)).toBe('unavailable');
  });

  it('suffix families cover every product without enumerating codes', () => {
    // *_not_found wins over a 500 status (code beats HTTP)
    expect(categoryForCode('swap_ir_not_found', 500)).toBe('not_found');
    expect(categoryForCode('swaption_vol_surface_not_found', 404)).toBe('not_found');
    expect(categoryForCode('cds_credit_curve_resolution_failed', 200)).toBe('validation');
    expect(categoryForCode('swap_inflation_index_resolution_failed', 200)).toBe('validation');
  });

  it('falls back to HTTP status for unrecognised codes', () => {
    expect(categoryForCode('weird', 422)).toBe('validation');
    expect(categoryForCode('weird', 404)).toBe('not_found');
    expect(categoryForCode('weird', 503)).toBe('server');
    expect(categoryForCode('weird', 401)).toBe('auth');
  });
});

describe('errorEnvelope — actionable suggestion', () => {
  it('gives exact guidance for infra codes', () => {
    expect(suggestionForCode('unauthenticated', 401)).toMatch(/sign in/i);
    expect(suggestionForCode('engine_unavailable', 502)).toMatch(/unreachable/i);
  });

  it('gives family guidance for not-found / resolution-failed', () => {
    expect(suggestionForCode('swaption_model_not_found', 404)).toMatch(/not found/i);
    expect(suggestionForCode('cds_credit_curve_resolution_failed', 422)).toMatch(/resolved/i);
  });
});

describe('errorEnvelope — mapEnvelopeToErrorInfo', () => {
  it('keeps the real upstream text in message and surfaces id + suggestion + code', () => {
    const info = mapEnvelopeToErrorInfo(
      'swap_ir_not_found',
      404,
      env({ error: 'Swap not found', code: 'swap_ir_not_found', request_id: 'req-1' }),
    );
    expect(info.message).toBe('Swap not found'); // real reason preserved
    expect(info.category).toBe('not_found');
    expect(info.codeName).toBe('swap_ir_not_found'); // stable code in the meta
    expect(info.requestId).toBe('req-1'); // support handle
    expect(info.suggestion).toBeTruthy();
  });

  it('tolerates a null request_id', () => {
    const info = mapEnvelopeToErrorInfo('network_error', 0, env({ code: 'network_error', request_id: null }));
    expect(info.requestId).toBeUndefined();
    expect(info.category).toBe('network');
  });
});
