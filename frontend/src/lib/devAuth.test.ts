import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The runtime dev-auth-bypass signal (self-hosted / demo `config.js`) is the
// only thing that survives into a production build. We drive it directly so the
// tests can simulate a production bundle running with dev-bypass ON vs a hosted
// production bundle with it OFF.
const { isRuntimeDevAuthBypassMock } = vi.hoisted(() => ({
  isRuntimeDevAuthBypassMock: vi.fn<[], boolean>(),
}));

vi.mock('./runtimeConfig', () => ({
  isRuntimeDevAuthBypass: isRuntimeDevAuthBypassMock,
}));

import { isDevTooling, isDevAuthBypass } from './devAuth';

describe('isDevTooling — gates the pricing-trace / Investigate tooling', () => {
  beforeEach(() => {
    isRuntimeDevAuthBypassMock.mockReset();
    isRuntimeDevAuthBypassMock.mockReturnValue(false);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('TRUE in a dev build (import.meta.env.DEV) even without any bypass', () => {
    vi.stubEnv('DEV', true);
    isRuntimeDevAuthBypassMock.mockReturnValue(false);
    expect(isDevTooling()).toBe(true);
  });

  it('TRUE in a production build when the self-hosted runtime dev-bypass is ON', () => {
    // Simulate the self-hosted / demo bundle: a production build (DEV false)
    // whose injected config.js sets devAuthBypass: true.
    vi.stubEnv('DEV', false);
    isRuntimeDevAuthBypassMock.mockReturnValue(true);

    // Guard: this is exactly the dev-bypass signal.
    expect(isDevAuthBypass()).toBe(true);
    // ...and the trace/Investigate tooling is therefore enabled.
    expect(isDevTooling()).toBe(true);
  });

  it('FALSE in a plain hosted production build (DEV false, no runtime bypass)', () => {
    vi.stubEnv('DEV', false);
    isRuntimeDevAuthBypassMock.mockReturnValue(false);

    expect(isDevAuthBypass()).toBe(false);
    expect(isDevTooling()).toBe(false);
  });
});
