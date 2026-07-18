// CurveSetSelector — select a curve set, then pick a curve by role for pricing
// Replaces the old CurveSelector in bond/swap pricers
import { useState, useEffect } from 'react';
import { CurveSet, Curve, CurveRole } from '../../lib/types';
import { ensureCurveSetsLoaded, getSavedCurveSets, resolveCurveSetRefs } from '../../lib/storage/curveSets';
import { ensureCurvesLoaded, getSavedCurves } from '../../lib/storage/curves';

interface CurveSetSelectorProps {
  label: string;
  curveRole: CurveRole;           // which role to filter (discount / forward)
  curveSetId: string;
  curveId: string;
  onChangeCurveSet: (csId: string, cs?: CurveSet | null) => void;
  onChangeCurve: (curveId: string, curve: Curve | null) => void;
  className?: string;
}

export default function CurveSetSelector({
  label, curveRole, curveSetId, curveId,
  onChangeCurveSet, onChangeCurve, className,
}: CurveSetSelectorProps) {
  const [curveSets, setCurveSets] = useState<CurveSet[]>([]);
  const [standaloneCurves, setStandaloneCurves] = useState<Curve[]>([]);
  const [selectedCs, setSelectedCs] = useState<CurveSet | null>(null);

  // Auto-resolve the selected ids into full objects. Re-runs whenever the
  // parent's ids change — NOT just on mount — because product detail pages
  // load their saved record asynchronously and only then set curveSetId /
  // curveId. The old mount-only effect had already run by that point, so a
  // reloaded product showed its curve as selected while the parent never
  // received the resolved Curve object (observed live: a reloaded saved bond
  // kept its Price button disabled until the curve was re-picked by hand).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      // Stores are normally preloaded by the app bootstrap; ensure anyway so
      // a cold cache can never make resolution silently miss.
      await Promise.all([ensureCurvesLoaded(), ensureCurveSetsLoaded()]);
      if (cancelled) return;
      const sets = getSavedCurveSets();
      setCurveSets(sets);
      setStandaloneCurves(getSavedCurves());

      if (curveSetId) {
        const cs = sets.find(s => s.id === curveSetId);
        if (cs) {
          setSelectedCs(cs);
          onChangeCurveSet(curveSetId, cs);
          if (curveId) {
            const c = resolveCurveSetRefs(cs).find(ref => ref.curve?.id === curveId)?.curve || null;
            if (c) onChangeCurve(curveId, c);
          }
        }
      } else if (curveId) {
        // Legacy: standalone curve selected
        const c = getSavedCurves().find(c => c.id === curveId);
        if (c) onChangeCurve(curveId, c);
      }
    })();
    return () => {
      cancelled = true;
    };
    // Deliberately keyed on the ids only: the onChange* callbacks are
    // page-inline lambdas (new identity every render) — keying on them
    // would re-run this on every parent render.
  }, [curveSetId, curveId]);

  const handleCurveSetChange = (csId: string) => {
    if (csId === '__standalone__') {
      setSelectedCs(null);
      onChangeCurveSet('', null);
      onChangeCurve('', null);
      return;
    }
    const cs = curveSets.find(s => s.id === csId) || null;
    setSelectedCs(cs);
    onChangeCurveSet(csId, cs);
    // Auto-select first matching curve
    if (cs) {
      const matching = resolveCurveSetRefs(cs)
        .filter(ref => ref.role === curveRole)
        .map(ref => ref.curve)
        .filter((curve): curve is Curve => curve !== null);
      if (matching.length === 1) {
        onChangeCurve(matching[0].id, matching[0]);
      } else {
        onChangeCurve('', null);
      }
    } else {
      onChangeCurve('', null);
    }
  };

  const handleCurveChange = (cId: string) => {
    if (selectedCs) {
      const c = resolveCurveSetRefs(selectedCs).find(ref => ref.curve?.id === cId)?.curve || null;
      onChangeCurve(cId, c);
    } else {
      // Standalone
      const c = standaloneCurves.find(c => c.id === cId) || null;
      onChangeCurve(cId, c);
    }
  };

  const availableCurves = selectedCs
    ? resolveCurveSetRefs(selectedCs)
        .filter(ref => ref.role === curveRole)
        .map(ref => ref.curve)
        .filter((curve): curve is Curve => curve !== null)
    : standaloneCurves.filter(c => c.role === curveRole);

  return (
    <div className={className}>
      <label className="block text-xs text-[#737373] mb-1.5 font-medium">{label}</label>
      <div className="space-y-2">
        {/* Curve Set selector */}
        <select
          value={curveSetId || (selectedCs ? '' : '__standalone__')}
          onChange={e => handleCurveSetChange(e.target.value)}
          className="w-full px-3 py-2 bg-white border border-[#d4d4d4] rounded-lg text-sm text-[#0a0a0a] focus:outline-none focus:border-[#8a6a2f] transition-colors"
        >
          <option value="">Select a curve set...</option>
          {curveSets.map(cs => (
            <option key={cs.id} value={cs.id}>
              {cs.name} ({cs.currency}) — {(cs.curve_refs || []).length} references
            </option>
          ))}
          {standaloneCurves.length > 0 && (
            <option value="__standalone__">— Standalone curves —</option>
          )}
        </select>

        {/* Curve selector within set */}
        {(selectedCs || curveSetId === '') && (
          <select
            value={curveId}
            onChange={e => handleCurveChange(e.target.value)}
            className="w-full px-3 py-2 bg-white border border-[#d4d4d4] rounded-lg text-sm text-[#0a0a0a] focus:outline-none focus:border-[#8a6a2f] transition-colors"
          >
            <option value="">
              {selectedCs
                ? `Select ${curveRole} curve...`
                : 'Select a curve...'
              }
            </option>
            {availableCurves.map(c => (
              <option key={c.id} value={c.id}>
                {c.name} ({c.role})
              </option>
            ))}
          </select>
        )}
      </div>
      {curveSets.length === 0 && standaloneCurves.length === 0 && (
        <p className="text-xs text-[#a3a3a3] mt-1">
          No curves available. <a href="/yield-curves" className="text-[#8a6a2f] hover:underline">Create a standalone curve</a>
        </p>
      )}
    </div>
  );
}
