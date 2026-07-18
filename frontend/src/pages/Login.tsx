import { useState } from 'react';
import { useAuth } from '../hooks/useAuth';

export default function Login() {
  const { login, loginWithPassword, loading, error, clearError } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');

  const handlePasswordSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    void loginWithPassword(email, password);
  };
  
  return (
    <div className="min-h-screen bg-[#fafafa] flex flex-col">
      <div className="flex-1 flex items-center justify-center p-4">
        <div className="w-full max-w-md">
          {/* Logo */}
          <div className="text-center mb-8">
            <svg width="180" height="48" viewBox="0 0 220 48" xmlns="http://www.w3.org/2000/svg" className="mx-auto mb-6">
              <text x="30" y="38" fontSize="40" fontFamily="Outfit, system-ui, sans-serif" fontWeight="700" fill="#f4f7fc">q</text>
              <text x="56" y="38" fontSize="40" fontFamily="Outfit, system-ui, sans-serif" fontWeight="600" fill="#8a6a2f" letterSpacing="-0.5">uantra</text>
            </svg>
            <p className="text-[#737373] text-lg">Open-source derivatives pricing platform</p>
          </div>
          
          {/* Login card */}
          <div className="bg-white border border-[#e5e5e5] rounded-xl p-8 shadow-sm">
            {/* Error */}
            {error && (
              <div className="mb-6 p-4 bg-red-50 border border-red-200 rounded-lg">
                <div className="flex items-start justify-between">
                  <p className="text-sm text-red-600">{error}</p>
                  <button onClick={clearError} className="text-red-400 hover:text-red-600 ml-2">✕</button>
                </div>
              </div>
            )}
            
            {/* Google sign-in */}
            <button
              onClick={login}
              disabled={loading}
              className="w-full flex items-center justify-center gap-3 px-6 py-3.5 bg-white hover:bg-[#f5f5f5] text-[#0a0a0a] font-medium rounded-lg border border-[#d4d4d4] transition-all disabled:opacity-50 shadow-sm"
            >
              {loading ? (
                <div className="w-5 h-5 border-2 border-[#d4d4d4] border-t-[#0a0a0a] rounded-full animate-spin" />
              ) : (
                <>
                  <svg className="w-5 h-5" viewBox="0 0 24 24">
                    <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
                    <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
                    <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>
                    <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
                  </svg>
                  Sign in with Google
                </>
              )}
            </button>

            {/* Divider */}
            <div className="my-6 flex items-center gap-3">
              <div className="flex-1 h-px bg-[#e5e5e5]" />
              <span className="text-xs uppercase tracking-wider text-[#a3a3a3]">or</span>
              <div className="flex-1 h-px bg-[#e5e5e5]" />
            </div>

            {/* Email + password */}
            <form onSubmit={handlePasswordSubmit} className="space-y-3">
              <div>
                <label htmlFor="email" className="block text-xs font-medium text-[#525252] mb-1">Email</label>
                <input
                  id="email"
                  type="email"
                  autoComplete="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  disabled={loading}
                  className="w-full px-3 py-2.5 bg-white border border-[#d4d4d4] rounded-lg text-[#0a0a0a] text-sm focus:outline-none focus:ring-2 focus:ring-[#8a6a2f]/40 focus:border-[#8a6a2f] disabled:opacity-50"
                  placeholder="you@bank.com"
                  required
                />
              </div>
              <div>
                <label htmlFor="password" className="block text-xs font-medium text-[#525252] mb-1">Password</label>
                <input
                  id="password"
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  disabled={loading}
                  className="w-full px-3 py-2.5 bg-white border border-[#d4d4d4] rounded-lg text-[#0a0a0a] text-sm focus:outline-none focus:ring-2 focus:ring-[#8a6a2f]/40 focus:border-[#8a6a2f] disabled:opacity-50"
                  placeholder="••••••••"
                  required
                />
              </div>
              <button
                type="submit"
                disabled={loading || !email || !password}
                className="w-full flex items-center justify-center gap-2 px-6 py-3 bg-[#0a0a0a] hover:bg-[#262626] text-white font-medium rounded-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {loading ? (
                  <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                ) : (
                  'Sign in'
                )}
              </button>
            </form>

            {/* Tier info */}
            <div className="mt-6 pt-6 border-t border-[#e5e5e5]">
              <div className="flex items-center justify-center gap-2 text-sm text-[#737373]">
                <span className="w-2 h-2 rounded-full bg-[#8a6a2f] animate-pulse"></span>
                Free tier available
              </div>
            </div>
          </div>
          
          {/* Products */}
          <div className="mt-8 grid grid-cols-4 gap-3">
            {['Bonds', 'Swaps', 'FRAs', 'Options'].map(product => (
              <div key={product} className="p-3 rounded-lg bg-white border border-[#e5e5e5] text-center">
                <p className="text-xs text-[#737373]">{product}</p>
              </div>
            ))}
          </div>
        </div>
      </div>
      
      {/* Footer */}
      <footer className="p-4 text-center text-xs text-[#a3a3a3] border-t border-[#e5e5e5]">
        © 2025 Quantra. Built with QuantLib.
      </footer>
    </div>
  );
}
