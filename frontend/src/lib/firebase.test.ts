import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Firebase SDK mocks (hoisted)
// firebase.ts calls initializeApp / getAuth / initializeFirestore at import
// time. Mock the SDK so importing the module is side-effect-free and we can
// assert what config it was initialised with.

const { initializeAppMock, getAuthMock, initializeFirestoreMock } = vi.hoisted(() => ({
  initializeAppMock: vi.fn((config: Record<string, string>) => ({ __app: true, config })),
  getAuthMock: vi.fn(() => ({ __auth: true })),
  initializeFirestoreMock: vi.fn(() => ({ __db: true })),
}));

vi.mock('firebase/app', () => ({ initializeApp: initializeAppMock }));
vi.mock('firebase/auth', () => ({
  getAuth: getAuthMock,
  GoogleAuthProvider: class {
    setCustomParameters() {}
  },
  signInWithPopup: vi.fn(),
  signInWithEmailAndPassword: vi.fn(),
  signOut: vi.fn(),
  onAuthStateChanged: vi.fn(),
  connectAuthEmulator: vi.fn(),
}));
vi.mock('firebase/firestore', () => ({
  initializeFirestore: initializeFirestoreMock,
  doc: vi.fn(),
  setDoc: vi.fn(),
  getDoc: vi.fn(),
  updateDoc: vi.fn(),
  serverTimestamp: vi.fn(),
  connectFirestoreEmulator: vi.fn(),
}));

// The single mode signal firebase.ts gates on.
const { isDevAuthBypassMock } = vi.hoisted(() => ({ isDevAuthBypassMock: vi.fn() }));
vi.mock('./devAuth', () => ({
  isDevAuthBypass: isDevAuthBypassMock,
  DEV_USER: {},
  isDevTooling: () => false,
}));

const REQUIRED_ENV = {
  VITE_FIREBASE_API_KEY: 'real-api-key',
  VITE_FIREBASE_AUTH_DOMAIN: 'real.firebaseapp.com',
  VITE_FIREBASE_PROJECT_ID: 'real-project',
};

function clearFirebaseEnv() {
  vi.stubEnv('VITE_FIREBASE_API_KEY', '');
  vi.stubEnv('VITE_FIREBASE_AUTH_DOMAIN', '');
  vi.stubEnv('VITE_FIREBASE_PROJECT_ID', '');
}

describe('firebase.ts — mode-gated missing-config handling (Fix 1)', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
  });
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('bypass ON + no config ⇒ inert placeholders, module boots (no throw)', async () => {
    isDevAuthBypassMock.mockReturnValue(true);
    clearFirebaseEnv();

    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    await expect(import('./firebase')).resolves.toBeDefined();

    expect(initializeAppMock).toHaveBeenCalledTimes(1);
    const cfg = initializeAppMock.mock.calls[0][0];
    expect(cfg.apiKey).toBe('self-hosted-no-firebase');
    expect(cfg.authDomain).toBe('self-hosted.invalid');
    expect(cfg.projectId).toBe('self-hosted');
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });

  it('bypass OFF + no config ⇒ loud throw at import (pre-75a83d4f behaviour)', async () => {
    isDevAuthBypassMock.mockReturnValue(false);
    clearFirebaseEnv();

    await expect(import('./firebase')).rejects.toThrow(/Firebase misconfigured/);
    // Must NOT silently degrade to placeholders and boot.
    expect(initializeAppMock).not.toHaveBeenCalled();
  });

  it('bypass OFF + real config ⇒ unchanged real Firebase (no throw, real keys)', async () => {
    isDevAuthBypassMock.mockReturnValue(false);
    vi.stubEnv('VITE_FIREBASE_API_KEY', REQUIRED_ENV.VITE_FIREBASE_API_KEY);
    vi.stubEnv('VITE_FIREBASE_AUTH_DOMAIN', REQUIRED_ENV.VITE_FIREBASE_AUTH_DOMAIN);
    vi.stubEnv('VITE_FIREBASE_PROJECT_ID', REQUIRED_ENV.VITE_FIREBASE_PROJECT_ID);

    await expect(import('./firebase')).resolves.toBeDefined();

    expect(initializeAppMock).toHaveBeenCalledTimes(1);
    const cfg = initializeAppMock.mock.calls[0][0];
    expect(cfg.apiKey).toBe('real-api-key');
    expect(cfg.authDomain).toBe('real.firebaseapp.com');
    expect(cfg.projectId).toBe('real-project');
  });
});
