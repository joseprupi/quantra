import { useState, useRef, useEffect } from 'react';
import { useAuth } from '../hooks/useAuth';
import { useLocation, useNavigate, Link } from 'react-router-dom';
import AsOfDatePicker from './AsOfDatePicker';
import AboutPanel from './AboutPanel';

export default function Header() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [productsOpen, setProductsOpen] = useState(false);
  const [marketDataOpen, setMarketDataOpen] = useState(false);
  const [curvesOpen, setCurvesOpen] = useState(false);
  const [modelsOpen, setModelsOpen] = useState(false);
  const [aboutOpen, setAboutOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const marketDataRef = useRef<HTMLDivElement>(null);
  const curvesRef = useRef<HTMLDivElement>(null);
  const modelsRef = useRef<HTMLDivElement>(null);

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setProductsOpen(false);
      }
      if (marketDataRef.current && !marketDataRef.current.contains(e.target as Node)) {
        setMarketDataOpen(false);
      }
      if (curvesRef.current && !curvesRef.current.contains(e.target as Node)) {
        setCurvesOpen(false);
      }
      if (modelsRef.current && !modelsRef.current.contains(e.target as Node)) {
        setModelsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const isActive = (path: string) => {
    if (path === '/') return location.pathname === '/';
    return location.pathname.startsWith(path);
  };

  const navLinkClass = (path: string) => `text-sm font-medium transition-colors ${isActive(path)
      ? 'text-[#8a6a2f]'
      : 'text-[#525252] hover:text-[#0a0a0a]'
    }`;

  const productGroups = [
    {
      name: 'Rates',
      items: [
        { path: '/products/ir-swap', name: 'Swaps' },
        { path: '/products/swaption', name: 'Swaptions' },
      ],
    },
    {
      name: 'Inflation',
      items: [
        { path: '/products/inflation-swaps', name: 'Inflation Swaps' },
      ],
    },
    {
      name: 'Credit',
      items: [
        { path: '/products/cds', name: 'CDS' },
      ],
    },
    {
      name: 'Equity',
      items: [
        { path: '/products/equity-options', name: 'Equity Options' },
      ],
    },
    {
      name: 'Bonds',
      items: [
        { path: '/products/fixed-rate-bond', name: 'Fixed Rate Bond' },
        { path: '/products/floating-rate-bond', name: 'Floating Rate Bond' },
      ],
    },
  ];

  return (
    <header className="fixed top-0 left-0 right-0 z-50 px-6 py-4 flex justify-between items-center bg-[#fafafa]/80 backdrop-blur-xl border-b border-[#e5e5e5]">
      {/* Logo */}
      <Link to="/" className="flex items-center">
        <svg width="130" height="32" viewBox="0 0 220 48" xmlns="http://www.w3.org/2000/svg">
          <text x="0" y="35" fontSize="34" fontFamily="Outfit, system-ui, sans-serif" fontWeight="700" fill="#f4f7fc">q</text>
          <text x="21" y="35" fontSize="34" fontFamily="Outfit, system-ui, sans-serif" fontWeight="600" fill="#8a6a2f" letterSpacing="-0.5">uantra</text>
        </svg>
      </Link>

      {/* Nav links */}
      <div className="flex items-center gap-6">
        {/* Main nav */}
        <nav className="hidden sm:flex items-center gap-5">
          <Link
            to="/"
            className={`text-sm font-medium transition-colors ${location.pathname === '/'
              ? 'text-[#8a6a2f]'
              : 'text-[#525252] hover:text-[#0a0a0a]'
              }`}
          >
            Home
          </Link>

          {/* Market Data dropdown */}
          <div ref={marketDataRef} className="relative">
            <button
              onClick={() => { setMarketDataOpen(!marketDataOpen); setProductsOpen(false); setCurvesOpen(false); setModelsOpen(false); }}
              className={`flex items-center gap-1 text-sm font-medium transition-colors ${isActive('/indices') || isActive('/quote-book') || isActive('/market-data/timeseries') || isActive('/market-data/import')
                  ? 'text-[#8a6a2f]'
                  : 'text-[#525252] hover:text-[#0a0a0a]'
                }`}
            >
              Market Data
              <svg className={`w-4 h-4 transition-transform ${marketDataOpen ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
              </svg>
            </button>
            {marketDataOpen && (
              <div className="absolute top-full left-0 mt-2 w-52 bg-white border border-[#e5e5e5] rounded-lg shadow-lg py-1 z-50">
                <Link to="/quote-book" onClick={() => setMarketDataOpen(false)} className={`block px-4 py-2 text-sm transition-colors ${isActive('/quote-book') ? 'bg-[#f5f5f5] text-[#8a6a2f]' : 'text-[#525252] hover:bg-[#f5f5f5] hover:text-[#0a0a0a]'}`}>
                  Quote Book
                </Link>
                <Link to="/indices" onClick={() => setMarketDataOpen(false)} className={`block px-4 py-2 text-sm transition-colors ${isActive('/indices') ? 'bg-[#f5f5f5] text-[#8a6a2f]' : 'text-[#525252] hover:bg-[#f5f5f5] hover:text-[#0a0a0a]'}`}>
                  Indices
                </Link>
                <Link to="/market-data/timeseries" onClick={() => setMarketDataOpen(false)} className={`block px-4 py-2 text-sm transition-colors ${isActive('/market-data/timeseries') ? 'bg-[#f5f5f5] text-[#8a6a2f]' : 'text-[#525252] hover:bg-[#f5f5f5] hover:text-[#0a0a0a]'}`}>
                  Time Series Lab
                </Link>
                <div className="my-1 border-t border-[#e5e5e5]" />
                <Link to="/market-data/import" onClick={() => setMarketDataOpen(false)} className={`block px-4 py-2 text-sm transition-colors ${isActive('/market-data/import') ? 'bg-[#f5f5f5] text-[#8a6a2f]' : 'text-[#525252] hover:bg-[#f5f5f5] hover:text-[#0a0a0a]'}`}>
                  Import
                </Link>
              </div>
            )}
          </div>

          <Link to="/calendar" className={navLinkClass('/calendar')}>
            Calendar
          </Link>

          {/* Curves dropdown */}
          <div ref={curvesRef} className="relative">
            <button
              onClick={() => { setCurvesOpen(!curvesOpen); setMarketDataOpen(false); setProductsOpen(false); setModelsOpen(false); }}
              className={`flex items-center gap-1 text-sm font-medium transition-colors ${isActive('/yield-curves') || isActive('/inflation-curves') || isActive('/curves') || isActive('/curve-sets') || isActive('/credit-curves')
                  ? 'text-[#8a6a2f]'
                  : 'text-[#525252] hover:text-[#0a0a0a]'
                }`}
            >
              Curves
              <svg className={`w-4 h-4 transition-transform ${curvesOpen ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
              </svg>
            </button>
            {curvesOpen && (
              <div className="absolute top-full left-0 mt-2 w-56 bg-white border border-[#e5e5e5] rounded-lg shadow-lg py-1 z-50">
                <Link to="/yield-curves" onClick={() => setCurvesOpen(false)} className={`block px-4 py-2 text-sm transition-colors ${isActive('/yield-curves') ? 'bg-[#f5f5f5] text-[#8a6a2f]' : 'text-[#525252] hover:bg-[#f5f5f5] hover:text-[#0a0a0a]'}`}>
                  Yield Curves
                </Link>
                <Link to="/inflation-curves" onClick={() => setCurvesOpen(false)} className={`block px-4 py-2 text-sm transition-colors ${isActive('/inflation-curves') ? 'bg-[#f5f5f5] text-[#8a6a2f]' : 'text-[#525252] hover:bg-[#f5f5f5] hover:text-[#0a0a0a]'}`}>
                  Inflation Curves
                </Link>
                <Link to="/credit-curves" onClick={() => setCurvesOpen(false)} className={`block px-4 py-2 text-sm transition-colors ${isActive('/credit-curves') ? 'bg-[#f5f5f5] text-[#8a6a2f]' : 'text-[#525252] hover:bg-[#f5f5f5] hover:text-[#0a0a0a]'}`}>
                  Credit Curves
                </Link>
                <div className="my-1 border-t border-[#e5e5e5]" />
                <div className="px-4 py-1 text-[10px] font-semibold uppercase tracking-wide text-[#a3a3a3]">
                  Pricing Context
                </div>
                <Link to="/curve-sets" onClick={() => setCurvesOpen(false)} className={`block px-4 py-2 text-sm transition-colors ${isActive('/curve-sets') ? 'bg-[#f5f5f5] text-[#8a6a2f]' : 'text-[#525252] hover:bg-[#f5f5f5] hover:text-[#0a0a0a]'}`}>
                  Curve Sets
                </Link>
              </div>
            )}
          </div>

          <Link to="/vol-workbench" className={navLinkClass('/vol-workbench')}>
            Volatilities
          </Link>

          {/* Models dropdown */}
          <div ref={modelsRef} className="relative">
            <button
              onClick={() => { setModelsOpen(!modelsOpen); setCurvesOpen(false); setMarketDataOpen(false); setProductsOpen(false); }}
              className={`flex items-center gap-1 text-sm font-medium transition-colors ${isActive('/models')
                  ? 'text-[#8a6a2f]'
                  : 'text-[#525252] hover:text-[#0a0a0a]'
                }`}
            >
              Models
              <svg className={`w-4 h-4 transition-transform ${modelsOpen ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
              </svg>
            </button>
            {modelsOpen && (
              <div className="absolute top-full left-0 mt-2 w-56 bg-white border border-[#e5e5e5] rounded-lg shadow-lg py-1 z-50">
                <Link to="/models/swaption" onClick={() => setModelsOpen(false)} className={`block px-4 py-2 text-sm transition-colors ${isActive('/models/swaption') ? 'bg-[#f5f5f5] text-[#8a6a2f]' : 'text-[#525252] hover:bg-[#f5f5f5] hover:text-[#0a0a0a]'}`}>
                  Swaption HW Calibration
                </Link>
              </div>
            )}
          </div>

          {/* Products dropdown */}
          <div ref={dropdownRef} className="relative">
            <button
              onClick={() => { setProductsOpen(!productsOpen); setMarketDataOpen(false); setCurvesOpen(false); setModelsOpen(false); }}
              className={`flex items-center gap-1 text-sm font-medium transition-colors ${isActive('/products')
                  ? 'text-[#8a6a2f]'
                  : 'text-[#525252] hover:text-[#0a0a0a]'
                }`}
            >
              Products
              <svg
                className={`w-4 h-4 transition-transform ${productsOpen ? 'rotate-180' : ''}`}
                fill="none" viewBox="0 0 24 24" stroke="currentColor"
              >
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
              </svg>
            </button>

            {productsOpen && (
              <div className="absolute top-full left-0 mt-2 w-64 bg-white border border-[#e5e5e5] rounded-lg shadow-lg py-1 z-50">
                {productGroups.map((group, index) => (
                  <div key={group.name}>
                    {index > 0 && <div className="my-1 border-t border-[#e5e5e5]" />}
                    <div className="px-4 py-1 text-[10px] font-semibold uppercase tracking-wide text-[#a3a3a3]">
                      {group.name}
                    </div>
                    {group.items.map(product => (
                      <Link
                        key={product.path}
                        to={product.path}
                        onClick={() => setProductsOpen(false)}
                        className={`block px-4 py-2 text-sm transition-colors ${isActive(product.path)
                            ? 'bg-[#f5f5f5] text-[#8a6a2f]'
                            : 'text-[#525252] hover:bg-[#f5f5f5] hover:text-[#0a0a0a]'
                          }`}
                      >
                        {product.name}
                      </Link>
                    ))}
                  </div>
                ))}
              </div>
            )}
          </div>
        </nav>

        <div className="w-px h-5 bg-[#e5e5e5] hidden sm:block" />

        {/* Global As-Of Date */}
        <div className="hidden sm:block">
          <AsOfDatePicker />
        </div>

        <div className="w-px h-5 bg-[#e5e5e5] hidden sm:block" />

        <a
          href="https://quantra.io/docs/portal"
          target="_blank"
          rel="noopener noreferrer"
          className="hidden sm:block text-sm font-medium text-[#525252] hover:text-[#0a0a0a] transition-colors"
        >
          Docs
        </a>
        <a
          href="https://github.com/joseprupi/quantraserver"
          target="_blank"
          rel="noopener noreferrer"
          className="hidden sm:block text-sm font-medium text-[#525252] hover:text-[#0a0a0a] transition-colors"
        >
          GitHub
        </a>
        <a
          href="https://quantra.io/propose"
          target="_blank"
          rel="noopener noreferrer"
          className="hidden sm:block text-sm font-medium text-[#525252] hover:text-[#0a0a0a] transition-colors"
          title="Suggest a feature"
        >
          Feedback
        </a>

        {/* About / version info */}
        <button
          onClick={() => setAboutOpen(true)}
          className="p-2 text-[#737373] hover:text-[#0a0a0a] hover:bg-[#f5f5f5] rounded-lg transition-colors"
          title="About Quantra"
          aria-label="About Quantra"
        >
          <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
        </button>

        <div className="w-px h-5 bg-[#e5e5e5] hidden sm:block" />

        {/* User/auth actions */}
        <div className="flex items-center gap-3">
          <Link
            to="/settings"
            className="p-2 text-[#737373] hover:text-[#0a0a0a] hover:bg-[#f5f5f5] rounded-lg transition-colors"
            title="Settings"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
          </Link>
          {user ? (
            <>
              <div className="flex items-center gap-2">
                {user.photoURL ? (
                  <img src={user.photoURL} alt="" className="w-8 h-8 rounded-full" />
                ) : (
                  <div className="w-8 h-8 rounded-full bg-[#8a6a2f] flex items-center justify-center text-white text-sm font-medium">
                    {user.displayName?.[0] || user.email?.[0] || '?'}
                  </div>
                )}
                <span className="hidden sm:block text-sm text-[#525252] max-w-[150px] truncate">{user.email}</span>
              </div>
              <button
                onClick={logout}
                className="p-2 text-[#737373] hover:text-[#0a0a0a] hover:bg-[#f5f5f5] rounded-lg transition-colors"
                title="Sign out"
              >
                <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
                </svg>
              </button>
            </>
          ) : (
            <button
              onClick={() => navigate('/login')}
              className="px-3 py-1.5 text-sm font-medium text-white bg-[#0a0a0a] rounded-lg hover:bg-[#262626] transition-colors"
              title="Sign in"
            >
              Sign in
            </button>
          )}
        </div>
      </div>

      {aboutOpen && <AboutPanel onClose={() => setAboutOpen(false)} />}
    </header>
  );
}
