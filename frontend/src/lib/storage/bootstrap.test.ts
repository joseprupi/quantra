import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { OrchestratorResult } from '../api/types';

// Mocks (hoisted)
// Isolate the bootstrap orchestration from firebase/network by mocking the
// orchestrator client and every storage preload it fans out to.

const { orchestratorPostMock } = vi.hoisted(() => ({ orchestratorPostMock: vi.fn() }));
vi.mock('../api/orchestrator', () => ({ orchestratorPost: orchestratorPostMock }));

const ensureMock = () => vi.fn().mockResolvedValue([]);
const { ensures } = vi.hoisted(() => ({
  ensures: {
    curves: vi.fn(),
    curveSets: vi.fn(),
    indices: vi.fn(),
    creditCurves: vi.fn(),
    volSurfaces: vi.fn(),
    swaptionModels: vi.fn(),
  },
}));
vi.mock('./curves', () => ({ ensureCurvesLoaded: ensures.curves, refreshCurves: vi.fn() }));
vi.mock('./curveSets', () => ({ ensureCurveSetsLoaded: ensures.curveSets, refreshCurveSets: vi.fn() }));
vi.mock('./indices', () => ({ ensureIndicesLoaded: ensures.indices, refreshIndices: vi.fn() }));
vi.mock('./creditCurves', () => ({ ensureCreditCurvesLoaded: ensures.creditCurves, refreshCreditCurves: vi.fn() }));
vi.mock('./volSurfaces', () => ({ ensureVolSurfacesLoaded: ensures.volSurfaces, refreshVolSurfaces: vi.fn() }));
vi.mock('./swaptionModels', () => ({ ensureSwaptionModelsLoaded: ensures.swaptionModels, refreshSwaptionModels: vi.fn() }));

const ok = (): OrchestratorResult<{ uid: string }> => ({ ok: true, data: { uid: 'dev-user' }, duration_ms: 1 });
const fail = (code: string, httpStatus: number): OrchestratorResult<{ uid: string }> => ({
  ok: false,
  envelope: { error: code, code },
  httpStatus,
  duration_ms: 1,
});

async function loadBootstrap() {
  vi.resetModules();
  return import('./bootstrap');
}

describe('bootstrap — cold-start provision retry (Fix 2)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    orchestratorPostMock.mockReset();
    for (const k of Object.keys(ensures) as Array<keyof typeof ensures>) {
      ensures[k] = ensureMock();
    }
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('retries provision on a transport failure, then fires preloads once it succeeds', async () => {
    orchestratorPostMock
      .mockResolvedValueOnce(fail('network_error', 0))
      .mockResolvedValueOnce(fail('network_error', 0))
      .mockResolvedValueOnce(ok());

    const { bootstrapEntityStores } = await loadBootstrap();
    const p = bootstrapEntityStores();
    await vi.runAllTimersAsync();
    await p;

    expect(orchestratorPostMock).toHaveBeenCalledTimes(3);
    // Probe attempts are quiet so cold-start transients do not spam the console.
    expect(orchestratorPostMock).toHaveBeenCalledWith('/auth/provision', {}, { quiet: true });
  });

  it('does not retry a non-transient failure (e.g. 401), but still runs preloads', async () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {});
    orchestratorPostMock.mockResolvedValue(fail('unauthenticated', 401));

    const { bootstrapEntityStores } = await loadBootstrap();
    const p = bootstrapEntityStores();
    await vi.runAllTimersAsync();
    await p;

    expect(orchestratorPostMock).toHaveBeenCalledTimes(1);
    expect(err).toHaveBeenCalled();
    err.mockRestore();
  });

  it('gives up after a bounded number of attempts when the backend stays cold', async () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {});
    orchestratorPostMock.mockResolvedValue(fail('network_error', 0));

    const { bootstrapEntityStores, PROVISION_MAX_ATTEMPTS } = await loadBootstrap();
    const p = bootstrapEntityStores();
    await vi.runAllTimersAsync();
    await p;

    // Bounded: exactly PROVISION_MAX_ATTEMPTS tries, then it proceeds (never hangs).
    expect(orchestratorPostMock).toHaveBeenCalledTimes(PROVISION_MAX_ATTEMPTS);
    expect(err).toHaveBeenCalled();
    err.mockRestore();
  });

  it('succeeds on the first try when the backend is already warm', async () => {
    orchestratorPostMock.mockResolvedValue(ok());

    const { bootstrapEntityStores } = await loadBootstrap();
    const p = bootstrapEntityStores();
    await vi.runAllTimersAsync();
    await p;

    expect(orchestratorPostMock).toHaveBeenCalledTimes(1);
  });
});
