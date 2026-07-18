// Shared e2e constants and fixtures.

/**
 * Deliberately FAKE Firebase API key. The e2e specs are hermetic: every
 * Firebase network call (identitytoolkit / securetoken / firestore) is
 * aborted, so the key is never sent anywhere real — it only has to be the
 * SAME string in the seeded IndexedDB auth record and in the app's own
 * Firebase config. To run the specs, start the dev server with
 * `VITE_FIREBASE_API_KEY` set to this same value (or use the dev-auth
 * bypass, which skips Firebase entirely).
 */
export const FIREBASE_API_KEY = 'test-only-not-a-real-key'

/** Where the specs expect the orchestrator API (route-intercepted or real). */
export const ORCHESTRATOR_ORIGIN = 'http://localhost:8080'

/**
 * A pre-authenticated Firebase user blob, shaped like the record the
 * Firebase Auth SDK persists in IndexedDB (`firebaseLocalStorage`). Seeding
 * it lets a spec start "logged in" without any network auth flow.
 */
export function buildFakeUser() {
  return {
    uid: 'e2e-user',
    email: 'test@e2e.example.com',
    displayName: 'E2E Tester',
    emailVerified: true,
    isAnonymous: false,
    providerData: [{ providerId: 'google.com', uid: 'e2e-user' }],
    stsTokenManager: {
      refreshToken: 'fake-refresh-token',
      accessToken: 'fake-access-token',
      expirationTime: 9_999_999_999_999,
    },
    createdAt: '1700000000000',
    lastLoginAt: '1700000000000',
    apiKey: FIREBASE_API_KEY,
    appName: '[DEFAULT]',
  }
}
