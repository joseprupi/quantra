// React hook for Firebase authentication
import { useState, useEffect, useCallback } from 'react';
import { 
  auth, 
  signInWithGoogle, 
  signInWithEmail,
  logOut, 
  createOrUpdateUserProfile,
  onAuthStateChanged,
  type User
} from '../lib/firebase';
import { isDevAuthBypass, DEV_USER } from '../lib/devAuth';

function friendlyAuthError(error: unknown): string {
  if (!(error instanceof Error)) return 'Login failed. Please try again.';
  const msg = error.message;
  if (msg.includes('popup-closed')) return 'Sign-in popup was closed.';
  if (msg.includes('network')) return 'Network error. Check your connection.';
  if (
    msg.includes('invalid-credential') ||
    msg.includes('wrong-password') ||
    msg.includes('user-not-found') ||
    msg.includes('invalid-email')
  ) return 'Invalid email or password.';
  if (msg.includes('too-many-requests')) return 'Too many attempts. Try again in a few minutes.';
  if (msg.includes('user-disabled')) return 'This account has been disabled.';
  return msg;
}

export interface AuthState {
  user: User | null;
  loading: boolean;
  error: string | null;
}

export function useAuth() {
  // Dev bypass (mirrors the backend): expose a fixed `dev-user` mock so the app
  // is authenticated immediately and the Google `signInWithPopup` path — which
  // throws `auth/unauthorized-domain` on localhost — never runs.
  const bypass = isDevAuthBypass();

  const [state, setState] = useState<AuthState>({
    user: bypass ? DEV_USER : null,
    loading: !bypass,
    error: null,
  });

  // Listen to auth state changes
  useEffect(() => {
    if (bypass) {
      // No Firebase listener under bypass; the mock user is already set.
      return;
    }
    const unsubscribe = onAuthStateChanged(auth, async (user) => {
      if (user) {
        // Create/update user profile in Firestore
        try {
          await createOrUpdateUserProfile(user);
        } catch (error) {
          console.error('Failed to update user profile:', error);
        }
      }
      
      setState({
        user,
        loading: false,
        error: null,
      });
    });

    return () => unsubscribe();
  }, [bypass]);

  // Sign in with Google
  const login = useCallback(async (): Promise<User | null> => {
    // Under bypass there is no real sign-in; hand back the mock user so callers
    // (e.g. IrSwap.handlePrice) proceed straight to pricing.
    if (bypass) {
      setState(prev => ({ ...prev, user: DEV_USER, loading: false, error: null }));
      return DEV_USER;
    }
    setState(prev => ({ ...prev, loading: true, error: null }));

    try {
      const signedInUser = await signInWithGoogle();
      return signedInUser;
    } catch (error) {
      console.error('Login failed:', error);
      setState(prev => ({
        ...prev,
        loading: false,
        error: friendlyAuthError(error),
      }));
      return null;
    }
  }, [bypass]);

  // Sign in with email + password
  const loginWithPassword = useCallback(async (email: string, password: string): Promise<User | null> => {
    if (!email || !password) {
      setState(prev => ({ ...prev, error: 'Email and password are required.' }));
      return null;
    }
    setState(prev => ({ ...prev, loading: true, error: null }));

    try {
      const signedInUser = await signInWithEmail(email, password);
      return signedInUser;
    } catch (error) {
      console.error('Email login failed:', error);
      setState(prev => ({
        ...prev,
        loading: false,
        error: friendlyAuthError(error),
      }));
      return null;
    }
  }, []);

  // Sign out
  const logout = useCallback(async () => {
    setState(prev => ({ ...prev, loading: true, error: null }));
    
    try {
      await logOut();
      // Auth state listener will handle the rest
    } catch (error) {
      console.error('Logout failed:', error);
      
      setState(prev => ({
        ...prev,
        loading: false,
        error: error instanceof Error ? error.message : 'Logout failed',
      }));
    }
  }, []);

  // Clear error
  const clearError = useCallback(() => {
    setState(prev => ({ ...prev, error: null }));
  }, []);

  return {
    user: state.user,
    loading: state.loading,
    error: state.error,
    isAuthenticated: !!state.user,
    login,
    loginWithPassword,
    logout,
    clearError,
  };
}
