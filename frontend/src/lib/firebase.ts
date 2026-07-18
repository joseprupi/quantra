// Firebase configuration and initialization
import { initializeApp } from 'firebase/app';
import { 
  getAuth, 
  GoogleAuthProvider, 
  signInWithPopup, 
  signInWithEmailAndPassword,
  signOut, 
  onAuthStateChanged,
  connectAuthEmulator,
  type User 
} from 'firebase/auth';
import {
  initializeFirestore,
  doc,
  setDoc,
  getDoc,
  updateDoc,
  serverTimestamp,
  connectFirestoreEmulator,
} from 'firebase/firestore';
import { isDevAuthBypass } from './devAuth';

// Firebase configuration from environment variables
const envConfig = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY,
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID,
  storageBucket: import.meta.env.VITE_FIREBASE_STORAGE_BUCKET,
  messagingSenderId: import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID,
  appId: import.meta.env.VITE_FIREBASE_APP_ID,
  measurementId: import.meta.env.VITE_FIREBASE_MEASUREMENT_ID,
};

// Validate config — mode-gated on `isDevAuthBypass()`. The self-hosted bundle
// ships WITHOUT Firebase config and never contacts Firebase (it runs under the
// dev-auth bypass), but `getAuth()` throws `auth/invalid-api-key` on an empty
// apiKey, which would crash the whole app at import time.
//   • bypass ON  + keys missing → degrade to inert placeholders so init
//     succeeds offline (no real Firebase call is ever made under bypass).
//   • bypass OFF + keys missing → a MISCONFIGURED hosted build: fail LOUDLY at
//     import so the misconfig is visible instead of login being quietly dead.
//   • bypass OFF + keys present → unchanged real Firebase.
const requiredKeys = ['apiKey', 'authDomain', 'projectId'] as const;
const missingKeys = requiredKeys.filter((key) => !envConfig[key]);
if (missingKeys.length > 0) {
  if (isDevAuthBypass()) {
    console.warn(
      `Firebase config missing (${missingKeys.join(', ')}); using inert ` +
        'placeholders. Expected under the self-hosted dev-auth bypass; a real ' +
        'Firebase login is unavailable in this mode.',
    );
  } else {
    // Hosted build (bypass off) with a required key absent: a real misconfig.
    // Throw loudly so it surfaces at boot rather than failing silently at login.
    throw new Error(
      `Firebase misconfigured: missing required config (${missingKeys.join(', ')}). ` +
        'The hosted build needs VITE_FIREBASE_API_KEY, VITE_FIREBASE_AUTH_DOMAIN ' +
        'and VITE_FIREBASE_PROJECT_ID. (Self-hosted / dev-auth-bypass builds are ' +
        'exempt — they run without Firebase.)',
    );
  }
}
const firebaseConfig = {
  ...envConfig,
  apiKey: envConfig.apiKey || 'self-hosted-no-firebase',
  authDomain: envConfig.authDomain || 'self-hosted.invalid',
  projectId: envConfig.projectId || 'self-hosted',
};

// Initialize Firebase
const app = initializeApp(firebaseConfig);
export const auth = getAuth(app);
export const db = initializeFirestore(app, {
  // More robust over VPN / proxied networks where QUIC/WebChannel can flap.
  experimentalAutoDetectLongPolling: true,
});
// Connect to emulators in development
if (import.meta.env.DEV && import.meta.env.VITE_USE_EMULATORS === 'true') {
  connectAuthEmulator(auth, 'http://localhost:9099');
  connectFirestoreEmulator(db, 'localhost', 8081);
  console.log('Connected to Firebase emulators');
}

// Auth providers
const googleProvider = new GoogleAuthProvider();
googleProvider.setCustomParameters({
  prompt: 'select_account'
});

// ============================================================================
// Auth Functions
// ============================================================================

export const signInWithGoogle = async () => {
  try {
    const result = await signInWithPopup(auth, googleProvider);
    return result.user;
  } catch (error) {
    console.error('Google sign-in error:', error);
    throw error;
  }
};

export const signInWithEmail = async (email: string, password: string) => {
  try {
    const result = await signInWithEmailAndPassword(auth, email.trim(), password);
    return result.user;
  } catch (error) {
    console.error('Email sign-in error:', error);
    throw error;
  }
};

export const logOut = () => signOut(auth);

// ============================================================================
// User Profile Functions
// ============================================================================

export async function createOrUpdateUserProfile(user: User): Promise<void> {
  const userRef = doc(db, 'users', user.uid);
  const userSnap = await getDoc(userRef);
  
  if (!userSnap.exists()) {
    // Create new user profile
    await setDoc(userRef, {
      email: user.email,
      displayName: user.displayName,
      photoURL: user.photoURL,
      createdAt: serverTimestamp(),
      tier: 'free',
    });
  } else {
    // Update last login
    await updateDoc(userRef, {
      lastLogin: serverTimestamp(),
    });
  }
}

// Re-export auth observer
export { onAuthStateChanged };
export type { User };
