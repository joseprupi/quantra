import { describe, it, expect } from 'vitest';
import {
  providerLabel,
  isImported,
  isLivePublicCurve,
  isSyntheticLegacy,
} from './provenance';

describe('provenance — provider attribution', () => {
  it('maps official public feeds to their publishing institution (case-insensitive)', () => {
    expect(providerLabel('boe')).toBe('Bank of England');
    expect(providerLabel('BOE')).toBe('Bank of England');
    expect(providerLabel('boe_ois')).toBe('Bank of England');
    expect(providerLabel('gilt')).toBe('Bank of England');
    expect(providerLabel('treasury')).toBe('US Treasury');
    expect(providerLabel('TREASURY')).toBe('US Treasury');
    expect(providerLabel('FRED')).toBe('FRED');
    expect(providerLabel('fred')).toBe('FRED');
    expect(providerLabel('ECB')).toBe('ECB');
    expect(providerLabel('ecb')).toBe('ECB');
  });

  it('labels user-supplied data as Imported', () => {
    expect(providerLabel('manual')).toBe('Imported');
    expect(providerLabel('csv')).toBe('Imported');
  });

  it('capitalizes unknown sources rather than dropping them', () => {
    expect(providerLabel('bloomberg')).toBe('Bloomberg');
    expect(providerLabel(null)).toBe('—');
    expect(providerLabel(undefined)).toBe('—');
  });

  it('treats only manual/csv as imported (FRED/ECB are real public data)', () => {
    expect(isImported('manual')).toBe(true);
    expect(isImported('csv')).toBe(true);
    expect(isImported('CSV')).toBe(true);
    expect(isImported('fred')).toBe(false);
    expect(isImported('ecb')).toBe(false);
    expect(isImported('treasury')).toBe(false);
    expect(isImported('boe')).toBe(false);
    expect(isImported(null)).toBe(false);
  });

  it('labels legacy synthetic rows distinctly, never as an official feed', () => {
    expect(providerLabel('synthetic')).toBe('Synthetic (legacy)');
    expect(providerLabel('SYNTHETIC')).toBe('Synthetic (legacy)');
    expect(isSyntheticLegacy('synthetic')).toBe(true);
    expect(isSyntheticLegacy('Synthetic')).toBe(true);
    expect(isSyntheticLegacy('fred')).toBe(false);
    expect(isSyntheticLegacy('manual')).toBe(false);
    expect(isSyntheticLegacy(null)).toBe(false);
    // synthetic is not the user-import category either
    expect(isImported('synthetic')).toBe(false);
  });

  it('recognises real public curves by name/description', () => {
    expect(isLivePublicCurve({ name: 'GBP SONIA OIS' })).toBe(true);
    expect(isLivePublicCurve({ description: 'US Treasury par curve' })).toBe(true);
    expect(isLivePublicCurve({ name: 'BoE curve' })).toBe(true);
    expect(isLivePublicCurve({ name: 'EUR ESTR' })).toBe(false);
  });
});
