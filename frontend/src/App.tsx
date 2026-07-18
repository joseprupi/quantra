import { useEffect, useState } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { useAuth } from './hooks/useAuth';
import { bootstrapEntityStores } from './lib/storage/bootstrap';
import SelfHostedBanner from './components/SelfHostedBanner';
import Login from './pages/Login';
import Home from './pages/Home';
import Settings from './pages/Settings';
import CurveBuilder from './pages/curves/CurveBuilder';
import CurvesList from './pages/curves/CurvesList';
import IndicesPage from './pages/market-data/IndicesPage';
import QuoteBook from './pages/market-data/QuoteBook';
import VolWorkbench from './pages/market-data/VolWorkbench';
import CreditCurves from './pages/market-data/CreditCurves';
import CreditCurveEditor from './pages/market-data/CreditCurveEditor';
import TimeSeriesLab from './pages/market-data/TimeSeriesLab';
import MarketDataImport from './pages/market-data/MarketDataImport';
import CalendarApp from './pages/calendar/CalendarApp';
import SwaptionModels from './pages/models/SwaptionModels';
import CurveSetList from './pages/curve-sets/CurveSetList';
import CurveSetEditor from './pages/curve-sets/CurveSetEditor';
import FixedRateBondList from './pages/products/FixedRateBondList';
import FixedRateBondPricer from './pages/products/FixedRateBond';
import FloatingRateBondList from './pages/products/FloatingRateBondList';
import FloatingRateBondPricer from './pages/products/FloatingRateBond';
import IrSwap from './pages/products/IrSwap';
import IrSwapList from './pages/products/IrSwapList';
import InflationSwap from './pages/products/InflationSwap';
import InflationSwapList from './pages/products/InflationSwapList';
import Swaption from './pages/products/Swaption';
import SwaptionList from './pages/products/SwaptionList';
import Cds from './pages/products/Cds';
import CdsList from './pages/products/CdsList';
import EquityOptions from './pages/products/EquityOptions';
import EquityOptionsList from './pages/products/EquityOptionsList';
import Investigate from './pages/Investigate';
import { isDevTooling, isDevAuthBypass } from './lib/devAuth';
import { applyRealDataAsOfDefault } from './lib/asOfDate';

// Loading spinner component
function LoadingScreen() {
  return (
    <div className="min-h-screen bg-[#fafafa] flex items-center justify-center">
      <div className="text-center">
        <div className="w-10 h-10 border-2 border-[#e5e5e5] border-t-[#8a6a2f] rounded-full animate-spin mx-auto mb-4" />
        <p className="text-[#737373]">Loading...</p>
      </div>
    </div>
  );
}

// Protected route wrapper. Beyond the auth gate it also runs the one-time
// entity-store bootstrap: provision the user's backend account row and
// preload the backend-backed caches so synchronous entity reads see real data.
function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { loading } = useAuth();
  const [entitiesReady, setEntitiesReady] = useState(false);

  useEffect(() => {
    if (loading) return;
    let cancelled = false;
    bootstrapEntityStores().then(() => {
      if (!cancelled) setEntitiesReady(true);
    });
    return () => {
      cancelled = true;
    };
  }, [loading]);

  if (loading || !entitiesReady) {
    return <LoadingScreen />;
  }

  return <>{children}</>;
}

// Public route wrapper: bounce already-authenticated users to home.
// (Anonymous visitors are still allowed to browse the rest of the app.)
function PublicRoute({ children }: { children: React.ReactNode }) {
  const { loading, isAuthenticated } = useAuth();

  if (loading) {
    return <LoadingScreen />;
  }
  if (isAuthenticated) {
    return <Navigate to="/" replace />;
  }
  return <>{children}</>;
}

export default function App() {
  // Default the pricing As-Of to the latest REAL-data date (the auto-rolling
  // BoE SONIA OIS reference_date) so a self-hosted bundle prices a real GBP
  // swap out of the box. Only under the self-hosted / dev-bypass signal (real
  // public feeds live there); no-op elsewhere and whenever the user has already
  // chosen an As-Of or no real data exists yet. The field stays user-editable.
  useEffect(() => {
    if (!isDevAuthBypass()) return;
    void applyRealDataAsOfDefault();
  }, []);

  return (
    <BrowserRouter>
      {/* Honesty banner — visible only under the self-hosted /
          dev-auth-bypass signal; renders nothing in the hosted build. */}
      <SelfHostedBanner />
      <Routes>
        <Route
          path="/login"
          element={
            <PublicRoute>
              <Login />
            </PublicRoute>
          }
        />
        <Route
          path="/"
          element={
            <ProtectedRoute>
              <Home />
            </ProtectedRoute>
          }
        />
        {/* The legacy "Market Data -> Quotes" app was dropped.
            Keep /market-data as a redirect so old links land on the read-only
            Quote Book instead of erroring. */}
        <Route path="/market-data" element={<Navigate to="/quote-book" replace />} />
        {/* Indices route */}
        <Route
          path="/indices"
          element={
            <ProtectedRoute>
              <IndicesPage />
            </ProtectedRoute>
          }
        />
        {/* Quote Book route */}
        <Route
          path="/quote-book"
          element={
            <ProtectedRoute>
              <QuoteBook />
            </ProtectedRoute>
          }
        />
        <Route
          path="/vol-workbench"
          element={
            <ProtectedRoute>
              <VolWorkbench />
            </ProtectedRoute>
          }
        />
        <Route
          path="/market-data/timeseries"
          element={
            <ProtectedRoute>
              <TimeSeriesLab />
            </ProtectedRoute>
          }
        />
        {/* Market Data → Import: user-supplied quote import. */}
        <Route
          path="/market-data/import"
          element={
            <ProtectedRoute>
              <MarketDataImport />
            </ProtectedRoute>
          }
        />
        {/* Credit Curves route */}
        <Route
          path="/credit-curves"
          element={
            <ProtectedRoute>
              <CreditCurves />
            </ProtectedRoute>
          }
        />
        <Route
          path="/credit-curves/new"
          element={
            <ProtectedRoute>
              <CreditCurveEditor />
            </ProtectedRoute>
          }
        />
        <Route
          path="/credit-curves/:id"
          element={
            <ProtectedRoute>
              <CreditCurveEditor />
            </ProtectedRoute>
          }
        />
        <Route
          path="/calendar"
          element={
            <ProtectedRoute>
              <CalendarApp />
            </ProtectedRoute>
          }
        />
        <Route
          path="/models/swaption"
          element={
            <ProtectedRoute>
              <SwaptionModels />
            </ProtectedRoute>
          }
        />
        {/* Yield curve routes */}
        <Route
          path="/yield-curves"
          element={
            <ProtectedRoute>
              <CurvesList />
            </ProtectedRoute>
          }
        />
        <Route
          path="/yield-curves/new"
          element={
            <ProtectedRoute>
              <CurveBuilder />
            </ProtectedRoute>
          }
        />
        <Route
          path="/yield-curves/:id"
          element={
            <ProtectedRoute>
              <CurveBuilder />
            </ProtectedRoute>
          }
        />
        {/* Inflation curve routes */}
        <Route
          path="/inflation-curves"
          element={
            <ProtectedRoute>
              <CurvesList />
            </ProtectedRoute>
          }
        />
        <Route
          path="/inflation-curves/new"
          element={
            <ProtectedRoute>
              <CurveBuilder />
            </ProtectedRoute>
          }
        />
        <Route
          path="/inflation-curves/:id"
          element={
            <ProtectedRoute>
              <CurveBuilder />
            </ProtectedRoute>
          }
        />
        {/* Legacy curves aliases */}
        <Route
          path="/curves"
          element={<Navigate to="/yield-curves" replace />}
        />
        <Route
          path="/curves/new"
          element={<Navigate to="/yield-curves/new" replace />}
        />
        <Route
          path="/curves/:id"
          element={
            <ProtectedRoute>
              <CurveBuilder />
            </ProtectedRoute>
          }
        />
        {/* Curve Set routes */}
        <Route
          path="/curve-sets"
          element={
            <ProtectedRoute>
              <CurveSetList />
            </ProtectedRoute>
          }
        />
        <Route
          path="/curve-sets/:id"
          element={
            <ProtectedRoute>
              <CurveSetEditor />
            </ProtectedRoute>
          }
        />
        {/* Product routes — Fixed Rate Bond */}
        <Route
          path="/products/fixed-rate-bond"
          element={
            <ProtectedRoute>
              <FixedRateBondList />
            </ProtectedRoute>
          }
        />
        <Route
          path="/products/fixed-rate-bond/new"
          element={
            <ProtectedRoute>
              <FixedRateBondPricer />
            </ProtectedRoute>
          }
        />
        <Route
          path="/products/fixed-rate-bond/:id"
          element={
            <ProtectedRoute>
              <FixedRateBondPricer />
            </ProtectedRoute>
          }
        />
        {/* Product routes — Floating Rate Bond */}
        <Route
          path="/products/floating-rate-bond"
          element={
            <ProtectedRoute>
              <FloatingRateBondList />
            </ProtectedRoute>
          }
        />
        <Route
          path="/products/floating-rate-bond/new"
          element={
            <ProtectedRoute>
              <FloatingRateBondPricer />
            </ProtectedRoute>
          }
        />
        <Route
          path="/products/floating-rate-bond/:id"
          element={
            <ProtectedRoute>
              <FloatingRateBondPricer />
            </ProtectedRoute>
          }
        />
        {/* Product routes — IR Swap */}
        <Route
          path="/products/ir-swap"
          element={
            <ProtectedRoute>
              <IrSwapList />
            </ProtectedRoute>
          }
        />
        <Route
          path="/products/ir-swap/new"
          element={
            <ProtectedRoute>
              <IrSwap />
            </ProtectedRoute>
          }
        />
        <Route
          path="/products/ir-swap/:id"
          element={
            <ProtectedRoute>
              <IrSwap />
            </ProtectedRoute>
          }
        />
        {/* Product routes — Inflation Swaps */}
        <Route
          path="/products/inflation-swaps"
          element={
            <ProtectedRoute>
              <InflationSwapList />
            </ProtectedRoute>
          }
        />
        <Route
          path="/products/inflation-swaps/new"
          element={
            <ProtectedRoute>
              <InflationSwap />
            </ProtectedRoute>
          }
        />
        <Route
          path="/products/inflation-swaps/:id"
          element={
            <ProtectedRoute>
              <InflationSwap />
            </ProtectedRoute>
          }
        />
        {/* Product routes — Swaption */}
        <Route
          path="/products/swaption"
          element={
            <ProtectedRoute>
              <SwaptionList />
            </ProtectedRoute>
          }
        />
        <Route
          path="/products/swaption/new"
          element={
            <ProtectedRoute>
              <Swaption />
            </ProtectedRoute>
          }
        />
        <Route
          path="/products/swaption/:id"
          element={
            <ProtectedRoute>
              <Swaption />
            </ProtectedRoute>
          }
        />
        {/* Product routes — CDS */}
        <Route
          path="/products/cds"
          element={
            <ProtectedRoute>
              <CdsList />
            </ProtectedRoute>
          }
        />
        <Route
          path="/products/cds/new"
          element={
            <ProtectedRoute>
              <Cds />
            </ProtectedRoute>
          }
        />
        <Route
          path="/products/cds/:id"
          element={
            <ProtectedRoute>
              <Cds />
            </ProtectedRoute>
          }
        />
        {/* Product routes — Equity Options */}
        <Route
          path="/products/equity-options"
          element={
            <ProtectedRoute>
              <EquityOptionsList />
            </ProtectedRoute>
          }
        />
        <Route
          path="/products/equity-options/new"
          element={
            <ProtectedRoute>
              <EquityOptions />
            </ProtectedRoute>
          }
        />
        <Route
          path="/products/equity-options/:id"
          element={
            <ProtectedRoute>
              <EquityOptions />
            </ProtectedRoute>
          }
        />
        <Route
          path="/settings"
          element={
            <ProtectedRoute>
              <Settings />
            </ProtectedRoute>
          }
        />
        {/* Dev-only pricing-trace investigation page. Gated on a dev
            build so it never ships to prod users; in a prod build the path
            falls through to the catch-all redirect below. */}
        {isDevTooling() && (
          <Route
            path="/investigate"
            element={
              <ProtectedRoute>
                <Investigate />
              </ProtectedRoute>
            }
          />
        )}
        {/* Redirect any unknown routes to home */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
