import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';

// Firebase mock (hoisted before useAuth imports it)

const { onAuthStateChangedMock, signInWithGoogleMock } = vi.hoisted(() => ({
  onAuthStateChangedMock: vi.fn(),
  signInWithGoogleMock: vi.fn(),
}));

vi.mock('../lib/firebase', () => ({
  auth: {},
  signInWithGoogle: signInWithGoogleMock,
  signInWithEmail: vi.fn(),
  logOut: vi.fn(),
  createOrUpdateUserProfile: vi.fn(),
  onAuthStateChanged: onAuthStateChangedMock,
}));

import { useAuth } from './useAuth';

describe('useAuth — dev auth bypass', () => {
  beforeEach(() => {
    onAuthStateChangedMock.mockReturnValue(() => {});
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.clearAllMocks();
  });

  it('ON: exposes the dev-user mock and never wires the Firebase listener', async () => {
    vi.stubEnv('DEV', true);
    vi.stubEnv('VITE_DEV_AUTH_BYPASS', 'true');

    const { result } = renderHook(() => useAuth());

    expect(result.current.user).toMatchObject({ uid: 'dev-user', email: 'dev@quantra.local' });
    expect(result.current.isAuthenticated).toBe(true);
    expect(result.current.loading).toBe(false);
    expect(onAuthStateChangedMock).not.toHaveBeenCalled();

    // login() resolves the mock user and never invokes signInWithGoogle.
    const u = await result.current.login();
    expect(u).toMatchObject({ uid: 'dev-user' });
    expect(signInWithGoogleMock).not.toHaveBeenCalled();
  });

  it('OFF: unchanged — wires the Firebase listener and starts unauthenticated', async () => {
    vi.stubEnv('VITE_DEV_AUTH_BYPASS', 'false');

    const { result } = renderHook(() => useAuth());

    await waitFor(() => expect(onAuthStateChangedMock).toHaveBeenCalled());
    expect(result.current.user).toBeNull();
    expect(result.current.isAuthenticated).toBe(false);
  });
});
