/**
 * Yield / inflation curves: inline-rate curve with deposit+swap helpers +
 * engine bootstrap preview; MD quote-referencing curve off the REAL BoE OIS
 * series (OIS helpers + SONIA); edit + delete; inflation (ZCIIS) curve +
 * preview; pinned capability gaps for custom indices in curve preview.
 */
import { test, expect } from '@playwright/test'
import { fieldFor, gotoReady, tenorFieldsFor } from '../lib/ui'
import { waitForRealData } from '../lib/api'
import { SEEDED, uniq } from '../lib/stack'
import {
  bootstrapPreview,
  createIborIndex,
  fillYieldCurveForm,
  saveCurve,
} from '../lib/journeys'

test.beforeEach(async ({ request }) => {
  await waitForRealData(request)
})

/** Create the saved EURIBOR_6M index if missing (a KNOWN backend catalog id). */
async function ensureEuribor6m(page: import('@playwright/test').Page) {
  await gotoReady(page, '/indices')
  if ((await page.getByText('EURIBOR_6M', { exact: true }).count()) > 0) return
  await createIborIndex(page, {
    id: 'EURIBOR_6M',
    family: 'Euribor',
    tenorNumber: 6,
    tenorUnit: 'Months',
    calendar: 'TARGET',
    dayCounter: 'Actual360',
  })
}

test.describe('yield curves', () => {
  test('inline deposit-strip curve bootstraps a plausible grid and saves', async ({ page }) => {
    const name = uniq('E2E inline curve')
    await fillYieldCurveForm(page, {
      name,
      currency: 'EUR',
      deposits: [
        { ratePct: 3.0, tenorNumber: 3, tenorUnit: 'Months' },
        { ratePct: 3.1, tenorNumber: 6, tenorUnit: 'Months' },
        { ratePct: 3.2, tenorNumber: 1, tenorUnit: 'Years' },
        { ratePct: 3.4, tenorNumber: 2, tenorUnit: 'Years' },
      ],
    })

    // Bootstrap preview via the orchestrator + engine (before saving —
    // saving navigates back to the list).
    const preview = await bootstrapPreview(page)
    expect(preview.__status, `curve-preview: ${JSON.stringify(preview).slice(0, 400)}`).toBe(200)
    const zeros = extractZeros(preview)
    expect(zeros.length, 'grid has points').toBeGreaterThan(3)
    for (const z of zeros) {
      expect(z).toBeGreaterThan(0)
      expect(z).toBeLessThan(0.1)
    }

    // Save persists and lands on the list with the curve visible.
    await saveCurve(page, name)
  })

  // FIXED (was a pinned defect): /v1/curve-preview could not register ANY
  // IBOR float-leg index — _build_preview_indices() consulted only
  // _KNOWN_OVERNIGHT_INDICES while the pricing path also had
  // _KNOWN_IBOR_INDICES. The preview arm now consults the same IBOR catalog
  // (same resolution order as pricing), so a SwapHelper curve referencing a
  // catalog id like EURIBOR_6M previews normally.
  test('inline curve with EURIBOR_6M swap helpers previews', async ({
    page,
  }) => {
    await ensureEuribor6m(page)

    const name = uniq('E2E ibor curve')
    await fillYieldCurveForm(page, {
      name,
      currency: 'EUR',
      deposits: [
        { ratePct: 3.0, tenorNumber: 6, tenorUnit: 'Months' },
        { ratePct: 3.2, tenorNumber: 1, tenorUnit: 'Years' },
      ],
      swaps: [
        { ratePct: 3.5, tenorNumber: 5, floatIndexId: 'EURIBOR_6M' },
        { ratePct: 3.8, tenorNumber: 10, floatIndexId: 'EURIBOR_6M' },
      ],
    })
    const preview = await bootstrapPreview(page)
    expect(preview.__status, `curve-preview: ${JSON.stringify(preview).slice(0, 400)}`).toBe(200)
    const zeros = extractZeros(preview)
    expect(zeros.length, 'grid has points').toBeGreaterThan(3)
  })

  test('MD quote-referencing curve off the REAL BoE OIS series (OIS helpers + SONIA) bootstraps', async ({
    page,
  }) => {
    const name = uniq('E2E MD curve')
    await fillYieldCurveForm(page, {
      name,
      currency: 'GBP',
      deposits: [{ quoteId: 'GBP.RATES.BOE.OIS.6M.PAR', tenorNumber: 6, tenorUnit: 'Months' }],
      oisHelpers: [
        {
          quoteId: 'GBP.RATES.BOE.OIS.5Y.PAR',
          tenorNumber: 5,
          overnightIndexId: SEEDED.soniaIndexId,
          calendar: 'UnitedKingdom',
        },
        {
          quoteId: 'GBP.RATES.BOE.OIS.10Y.PAR',
          tenorNumber: 10,
          overnightIndexId: SEEDED.soniaIndexId,
          calendar: 'UnitedKingdom',
        },
      ],
    })

    const preview = await bootstrapPreview(page)
    expect(preview.__status, `curve-preview: ${JSON.stringify(preview).slice(0, 400)}`).toBe(200)
    const zeros = extractZeros(preview)
    expect(zeros.length).toBeGreaterThan(3)
    // Real GBP OIS zeros are ~3.5–5.5%; assert a sane band.
    for (const z of zeros) {
      expect(z).toBeGreaterThan(0.005)
      expect(z).toBeLessThan(0.1)
    }
    await saveCurve(page, name)
  })

  test('edit (rename) and delete a curve from the list', async ({ page }) => {
    const name = uniq('E2E edit curve')
    const renamed = `${name} RENAMED`
    await fillYieldCurveForm(page, {
      name,
      currency: 'EUR',
      deposits: [{ ratePct: 2.5, tenorNumber: 1, tenorUnit: 'Years' }],
    })
    await saveCurve(page, name)

    // Edit: open from the list, rename, save.
    await page.getByText(name, { exact: true }).first().click()
    await fieldFor(page, 'Curve Name *').fill(renamed)
    await saveCurve(page, renamed)

    // Delete from the list (native confirm accepted).
    page.on('dialog', (d) => void d.accept())
    const card = page
      .locator('div')
      .filter({ hasText: renamed })
      .filter({ has: page.getByTitle('Delete') })
      .last()
    await card.getByTitle('Delete').first().click()
    await expect(page.getByText(renamed, { exact: true })).toHaveCount(0, { timeout: 15_000 })
  })

  // KNOWN GAP (verified live): a saved CUSTOM index (arbitrary id) cannot be
  // used by curve-preview — the portal ships only the index ID inside the
  // helper and the orchestrator only registers ids from its built-in catalog
  // (ESTR/SOFR/SONIA + EURIBOR_*/USD_LIBOR_*/GBP_LIBOR_*):
  //   422 "Curve helper references index id 'X' but no IndexDef could be
  //        registered for it ..."
  // Inconsistent with product pricing, which DOES ship saved index defs and
  // prices custom indices fine (see 60-swap custom-index journey).
  test('custom-index curve preview 422s (KNOWN GAP, pinned)', async ({ page }) => {
    test.info().annotations.push({
      type: 'known-gap',
      description:
        'Curve preview cannot use custom saved indices (only backend-catalog ids); product pricing can. 422 index-not-registered.',
    })
    const indexId = uniq('E2ECUSTOM').toUpperCase().replace(/[^A-Z0-9_]/g, '_')
    await createIborIndex(page, { id: indexId, family: 'Euribor', tenorNumber: 6, tenorUnit: 'Months' })
    await fillYieldCurveForm(page, {
      name: uniq('E2E custom-idx curve'),
      currency: 'EUR',
      deposits: [{ ratePct: 3.0, tenorNumber: 6, tenorUnit: 'Months' }],
      swaps: [{ ratePct: 3.5, tenorNumber: 5, floatIndexId: indexId }],
    })
    const preview = await bootstrapPreview(page)
    expect(preview.__status).toBe(422)
    expect(String(preview.error ?? '')).toContain('no IndexDef could be registered')
  })
})

test.describe('inflation curves', () => {
  test('ZCIIS inflation curve (inline index + nominal curve) previews', async ({ page }) => {
    await gotoReady(page, '/inflation-curves/new')
    const name = uniq('E2E ZCIIS curve')
    await fieldFor(page, 'Curve Name *').fill(name)
    // The Inflation Index panel defaults to a complete inline EUHICP index —
    // use it as-is. Pick the nominal discount curve (any seeded discount).
    await fieldFor(page, 'Nominal Discount Curve').selectOption({
      label: SEEDED.usdTreasuryCurveName,
    })

    // Two ZCIIS helpers (inline rates, tenor maturity mode).
    for (const [tenor, rate] of [
      [2, 2.2],
      [5, 2.4],
    ] as Array<[number, number]>) {
      await page.getByRole('button', { name: 'Add Helper' }).click()
      await fieldFor(page, 'Rate (%)').fill(String(rate))
      const t = tenorFieldsFor(page)
      await t.number.fill(String(tenor))
      await t.unit.selectOption('Years')
      await page.getByRole('button', { name: /^(Add Helper|Update)$/ }).last().click()
    }

    const preview = await bootstrapPreview(page)
    expect(
      preview.__status,
      `inflation curve-preview: ${JSON.stringify(preview).slice(0, 500)}`,
    ).toBe(200)
  })
})

/**
 * Pull zero rates out of the preview response
 * ({results: [{grid_dates, series: [{measure, values}], error}]}).
 * Throws when the engine reported a per-curve error.
 */
function extractZeros(preview: Record<string, unknown>): number[] {
  const results = preview.results as
    | Array<{
        series?: Array<{ measure?: string; values?: number[] }>
        error?: { error_message?: string } | null
      }>
    | undefined
  const first = results?.[0]
  if (!first) return []
  if (first.error) {
    throw new Error(`curve-preview engine error: ${JSON.stringify(first.error)}`)
  }
  const series = first.series ?? []
  const zero = series.find((s) => /zero/i.test(s.measure ?? '')) ?? series[0]
  return (zero?.values ?? []).filter((v) => Number.isFinite(v))
}
