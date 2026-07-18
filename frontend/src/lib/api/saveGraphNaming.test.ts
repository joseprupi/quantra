import { describe, expect, it } from 'vitest';

import {
  deriveGraphCurveName,
  graphCurveCreateBody,
  graphCurvePatchBody,
} from './saveGraphNaming';

describe('deriveGraphCurveName', () => {
  it('embeds the product name and the role', () => {
    const name = deriveGraphCurveName('My GBP Swap', 'discount');
    expect(name).toContain('My GBP Swap');
    expect(name).toContain('discount');
  });

  it('is never the bare role constant', () => {
    for (const role of ['discount', 'forward', 'projection', 'dividend', 'nominal', 'inflation']) {
      expect(deriveGraphCurveName('P', role)).not.toBe(role);
    }
  });

  it('is unique across calls even for identical inputs', () => {
    const seen = new Set<string>();
    for (let i = 0; i < 50; i++) {
      seen.add(deriveGraphCurveName('Same Product', 'discount'));
    }
    expect(seen.size).toBe(50);
  });

  it('clips very long product names but keeps the role + suffix', () => {
    const long = 'x'.repeat(500);
    const name = deriveGraphCurveName(long, 'discount');
    expect(name.length).toBeLessThan(120);
    expect(name).toContain('discount');
  });

  it('falls back to sane parts for empty inputs', () => {
    const name = deriveGraphCurveName('', '');
    expect(name).toContain('product');
    expect(name).toContain('curve');
  });
});

describe('graphCurveCreateBody', () => {
  it('replaces the wire name (a role constant) with the derived unique name', () => {
    const wire = { name: 'discount', currency: 'GBP', points: [{ point_type: 'SwapHelper' }] };
    const body = graphCurveCreateBody(wire, 'My GBP Swap', 'discount');
    expect(body.name).not.toBe('discount');
    expect(body.name).toContain('My GBP Swap');
    // Everything else passes through untouched.
    expect(body.currency).toBe('GBP');
    expect(body.points).toEqual([{ point_type: 'SwapHelper' }]);
    // The input is not mutated.
    expect(wire.name).toBe('discount');
  });
});

describe('graphCurvePatchBody', () => {
  it('strips the name entirely so a by-id re-save never renames the row', () => {
    const wire = {
      name: 'discount',
      currency: 'GBP',
      reference_date: '2026-07-17',
      points: [{ point_type: 'SwapHelper' }],
      body: { role: 'discount' },
    };
    const patch = graphCurvePatchBody(wire);
    expect('name' in patch).toBe(false);
    expect(patch).toEqual({
      currency: 'GBP',
      reference_date: '2026-07-17',
      points: [{ point_type: 'SwapHelper' }],
      body: { role: 'discount' },
    });
  });
});
