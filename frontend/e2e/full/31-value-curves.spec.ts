/**
 * Value-based curves ("Interpolate given values", engine Interpolated*
 * families): the zero family works END TO END on the pinned engine 0.2.0
 * (auto-dispatched ZeroRatePoint) and ships UNGATED — paste-table entry,
 * preview grid, save, curve set, IR swap priced discounting on the value
 * curve, quote-referenced rows, and both error paths (client-side unsorted
 * pillars; server-side unresolvable quote id mapped onto the row).
 *
 * DiscountFactorPoint / ForwardRatePoint need engine >= 0.5.0 (the union
 * members do not exist in 0.2.0) — their SUCCESS journeys are gated on the
 * running engine's version; the ungated DF test proves the request shape and
 * the clean D54 error render on older engines instead.
 */
import { test, expect } from '@playwright/test'
import { fieldFor, gotoReady, pickStandaloneCurve, selectByTextContains } from '../lib/ui'
import { assertPricingParity, capturePricing, waitForRealData } from '../lib/api'
import { PORTAL_URL, SEEDED, uniq, uniqCanonicalId } from '../lib/stack'
import { fillValueCurveForm, saveCurve, valueCurvePreview } from '../lib/journeys'

test.beforeEach(async ({ request }) => {
  await waitForRealData(request)
})

/** Latest real-data business date — the coherent reference/As-Of date. */
async function latestDate(request: import('@playwright/test').APIRequestContext): Promise<string> {
  const res = await request.get(`${PORTAL_URL}/v1/market-data/latest-date`)
  const body = (await res.json()) as { latest_date?: string }
  expect(body.latest_date, 'stack has a latest market-data date').toBeTruthy()
  return body.latest_date as string
}

/** Engine version from /v1/version (semver-ish "0.2.0"). */
async function engineVersion(request: import('@playwright/test').APIRequestContext): Promise<string> {
  const res = await request.get(`${PORTAL_URL}/v1/version`)
  const body = (await res.json()) as { engine?: { version?: string } }
  return body.engine?.version ?? '0.0.0'
}

function versionGte(version: string, wanted: string): boolean {
  const a = version.split('.').map((n) => parseInt(n, 10) || 0)
  const b = wanted.split('.').map((n) => parseInt(n, 10) || 0)
  for (let i = 0; i < 3; i++) {
    if ((a[i] ?? 0) > (b[i] ?? 0)) return true
    if ((a[i] ?? 0) < (b[i] ?? 0)) return false
  }
  return true
}

function extractSeries(body: Record<string, unknown>, measure: string): number[] {
  const results = (body.results ?? []) as Array<{
    series?: Array<{ measure?: string; values?: number[] }>
  }>
  const series = results[0]?.series?.find((s) => s.measure === measure || (!s.measure && measure === 'DF'))
  return series?.values ?? []
}

test.describe('value curves — zero family (works on engine 0.2.0, UNGATED)', () => {
  test('paste -> preview grid -> save -> curve set -> price an IR swap discounting on it', async ({
    page,
    request,
  }) => {
    const refDate = await latestDate(request)
    const curveName = uniq('E2E zero value curve')
    const setName = uniq('E2E value set')

    // 1. Create through the PASTE workflow (the primary path: reproduce a
    //    published curve from a two-column block; percent entry for zeros).
    await fillValueCurveForm(page, {
      name: curveName,
      currency: 'GBP',
      referenceDate: refDate,
      pasteTable: '6M\t3.90\n1Y, 3.95\n2Y 4.00\n5Y 4.10\n10Y 4.25\n30Y 4.35',
    })
    // 6 pasted rows + the auto-prepended reference-date anchor row (the
    // interpolated curve is ANCHORED at its first pillar).
    await expect(page.getByTestId('value-point-row')).toHaveCount(7)

    // 2. Preview via the orchestrator + engine: non-trivial zero grid close
    //    to the pasted levels, and the wire trait is InterpolatedZero with
    //    ZeroRatePoint rows carrying the curve-wide conventions.
    const preview = await valueCurvePreview(page)
    expect(preview.status, `curve-preview: ${JSON.stringify(preview.body).slice(0, 400)}`).toBe(200)
    const zeros = extractSeries(preview.body, 'ZERO')
    expect(zeros.length, 'grid has points').toBeGreaterThan(3)
    for (const z of zeros) {
      expect(z).toBeGreaterThan(0.03)
      expect(z).toBeLessThan(0.06)
    }
    const wireCurve = ((preview.requestBody.pricing as Record<string, unknown>)?.curves as Array<
      Record<string, unknown>
    >)?.[0]
    expect(wireCurve?.bootstrap_trait).toBe('InterpolatedZero')
    const wirePoints = wireCurve?.points as Array<{ point_type: string; point: Record<string, unknown> }>
    expect(wirePoints).toHaveLength(7)
    expect(wirePoints.every((p) => p.point_type === 'ZeroRatePoint')).toBe(true)
    expect(wirePoints[0].point).toMatchObject({
      date: refDate,
      zero_rate: 0.039,
      compounding: 'Continuous',
      frequency: 'Annual',
    })
    expect(wirePoints[1].point).toMatchObject({
      tenor: { n: 6, unit: 'Months' },
      zero_rate: 0.039,
    })

    // 3. Save persists through the shared path and lands on the list.
    await saveCurve(page, curveName)

    // 4. The value curve is selectable in a curve-set slot like any other.
    await gotoReady(page, '/curve-sets')
    await page.getByRole('button', { name: /New Curve Set/ }).click()
    await page.waitForURL(/\/curve-sets\/[^/]+$/, { timeout: 30_000 })
    await fieldFor(page, 'Name').fill(setName)
    await fieldFor(page, 'Currency').selectOption('GBP')
    await page.getByRole('button', { name: '+ Discount' }).click()
    await selectByTextContains(fieldFor(page, 'Standalone Curve'), curveName)
    await page.waitForTimeout(1000)

    // 5. Price a GBP swap DISCOUNTING on the value curve (single-curve mode
    //    projects SONIA off it) — NPV renders with UI==wire==replay parity.
    await gotoReady(page, '/products/ir-swap/new')
    await page.getByLabel('Product name').fill(uniq('E2E value swap'))
    await pickStandaloneCurve(page, 'Discounting Curve (PV)', curveName)
    await selectByTextContains(fieldFor(page, 'Index'), SEEDED.soniaIndexId)
    await fieldFor(page, 'Rate (%)').fill('2')
    const exchangePromise = capturePricing(page, '/v1/price/swap/ir')
    await page.getByRole('button', { name: 'Price', exact: true }).click()
    const exchange = await exchangePromise
    expect(
      exchange.status,
      `pricing on the value curve -> ${JSON.stringify(exchange.responseBody).slice(0, 400)}`,
    ).toBe(200)
    const npvPanel = page.locator('div:has(> p:text-is("NPV"))').locator('p').nth(1)
    await expect(npvPanel).toBeVisible({ timeout: 30_000 })
    const npvText = (await npvPanel.textContent()) ?? ''
    const { uiNpv } = await assertPricingParity(request, exchange, npvText)
    expect(Math.abs(uiNpv), 'NPV materially non-zero at 2% fixed vs ~4% zeros').toBeGreaterThan(1_000)
  })

  test('quote-referenced row resolves through the MD walker at As-Of', async ({ page, request }) => {
    const refDate = await latestDate(request)
    const curveName = uniq('E2E zero quote curve')

    await fillValueCurveForm(page, {
      name: curveName,
      currency: 'GBP',
      referenceDate: refDate,
      pasteTable: '1Y 4.0\n10Y 4.2',
    })

    // Add a row sourced from the REAL BoE OIS catalog (same MD dropdown the
    // instrument editor uses; value shown resolved at the global As-Of).
    await page.getByRole('button', { name: '+ Add row' }).click()
    const rows = page.getByTestId('value-point-row')
    await expect(rows).toHaveCount(4) // anchor + 2 pasted + the new row
    await rows.nth(3).getByLabel('Pillar 4').fill('5Y')
    await rows.nth(3).getByLabel('Pillar 4').press('Enter')
    await rows.nth(3).getByLabel('Source 4').selectOption('quote')
    const quoteSelect = rows.nth(3).getByLabel('Quote 4')
    await selectByTextContains(quoteSelect, `${SEEDED.boeQuotePrefix}5Y`)
    // Sort so the 5Y row sits between 1Y and 10Y.
    await page.getByRole('button', { name: 'Sort by pillar' }).click()

    const preview = await valueCurvePreview(page)
    expect(preview.status, `preview: ${JSON.stringify(preview.body).slice(0, 400)}`).toBe(200)
    const zeros = extractSeries(preview.body, 'ZERO')
    expect(zeros.length).toBeGreaterThan(3)
    // The quote-referenced row went out UNRESOLVED (server resolves it).
    const wireCurve = ((preview.requestBody.pricing as Record<string, unknown>)?.curves as Array<
      Record<string, unknown>
    >)?.[0]
    const wirePoints = wireCurve?.points as Array<{ point: Record<string, unknown> }>
    const quotePoint = wirePoints.find((p) => p.point.quote_id)
    expect(quotePoint?.point.quote_id).toContain(SEEDED.boeQuotePrefix)
    expect(quotePoint?.point.zero_rate).toBeUndefined()
  })

  test('unsorted pillars render a per-row error and preview is blocked client-side', async ({
    page,
    request,
  }) => {
    const refDate = await latestDate(request)
    await fillValueCurveForm(page, {
      name: uniq('E2E unsorted'),
      currency: 'GBP',
      referenceDate: refDate,
      pasteTable: '6M 3.0\n5Y 3.2',
    })

    // Break the ordering: pull the 5Y row before the 6M one.
    const rows = page.getByTestId('value-point-row')
    await expect(rows).toHaveCount(3) // anchor + 6M + 5Y
    await rows.nth(2).getByLabel('Pillar 3').fill('3M')
    await rows.nth(2).getByLabel('Pillar 3').press('Enter')

    // The offending ROW carries a friendly message (mirrors the backend's
    // pillars_not_sorted 422 without a round-trip).
    await expect(
      page.getByTestId('value-point-row-error').filter({ hasText: 'strictly increasing' }).first(),
    ).toBeVisible()

    // Preview refuses to round-trip while rows are invalid.
    await page.getByRole('button', { name: 'Preview', exact: true }).click()
    await expect(page.getByText('Fix the highlighted rows before previewing.')).toBeVisible()

    // Sort fixes it; the row error clears.
    await page.getByRole('button', { name: 'Sort by pillar' }).click()
    await expect(page.getByTestId('value-point-row-error')).toHaveCount(0)
  })

  test('unresolvable quote id: server 422 maps onto the offending row', async ({
    page,
    request,
  }) => {
    const refDate = await latestDate(request)
    // A REAL catalog series with NO values: valid id, unresolvable at As-Of.
    const emptySeriesId = uniqCanonicalId('7Y')
    const created = await request.post(`${PORTAL_URL}/v1/market-data/series`, {
      data: {
        canonical_id: emptySeriesId,
        asset_class: 'RATES',
        instrument: 'IRS',
        currency: 'USD',
        field: 'PAR',
      },
    })
    expect(created.ok(), `series create -> ${created.status()}`).toBe(true)

    await fillValueCurveForm(page, {
      name: uniq('E2E bad quote'),
      currency: 'USD',
      referenceDate: refDate,
      pasteTable: '1Y 4.0\n10Y 4.2',
    })
    await page.getByRole('button', { name: '+ Add row' }).click()
    const rows = page.getByTestId('value-point-row')
    await expect(rows).toHaveCount(4) // anchor + 2 pasted + the new row
    await rows.nth(3).getByLabel('Pillar 4').fill('5Y')
    await rows.nth(3).getByLabel('Pillar 4').press('Enter')
    await rows.nth(3).getByLabel('Source 4').selectOption('quote')
    await selectByTextContains(rows.nth(3).getByLabel('Quote 4'), emptySeriesId)
    await page.getByRole('button', { name: 'Sort by pillar' }).click()

    const preview = await valueCurvePreview(page)
    expect(preview.status, 'unresolvable quote must 422').toBe(422)
    // The friendly per-row message lands on the quote row.
    await expect(page.getByTestId('value-point-row-error')).toContainText(/could not be resolved/)
  })
})

test.describe('value curves — DF / Fwd families (engine >= 0.5.0)', () => {
  test('DF quantity: pinned reference row, request shape, clean error render pre-0.5.0', async ({
    page,
    request,
  }) => {
    const refDate = await latestDate(request)
    const engine = await engineVersion(request)

    await fillValueCurveForm(page, {
      name: uniq('E2E df curve'),
      currency: 'GBP',
      referenceDate: refDate,
      quantity: 'Discount factors',
      pasteTable: '1Y 0.96\n5Y 0.82\n10Y 0.66',
    })
    // Paste auto-prepends the pinned DF=1.0 row at the reference date.
    const rows = page.getByTestId('value-point-row')
    await expect(rows).toHaveCount(4)
    await expect(rows.nth(0)).toContainText('(reference)')

    const preview = await valueCurvePreview(page)
    // Request shape is right regardless of the engine's verdict.
    const wireCurve = ((preview.requestBody.pricing as Record<string, unknown>)?.curves as Array<
      Record<string, unknown>
    >)?.[0]
    expect(wireCurve?.bootstrap_trait).toBe('InterpolatedDiscount')
    const wirePoints = wireCurve?.points as Array<{ point_type: string; point: Record<string, unknown> }>
    expect(wirePoints[0]).toMatchObject({
      point_type: 'DiscountFactorPoint',
      point: { date: refDate, discount_factor: 1 },
    })
    expect(wirePoints[1].point.discount_factor).toBe(0.96)

    if (versionGte(engine, '0.5.0')) {
      expect(preview.status, `DF preview on ${engine}`).toBe(200)
      expect(extractSeries(preview.body, 'DF').length).toBeGreaterThan(3)
    } else {
      // Pre-0.5.0 the engine rejects the unknown union member — the D54
      // envelope must render as a clean error, NOT crash the page.
      expect(preview.status, `DF preview must be rejected on engine ${engine}`).toBeGreaterThanOrEqual(400)
      await expect(page.locator('main')).toBeVisible()
      await expect(rows.nth(0)).toBeVisible() // table intact
      await expect(page.locator('.bg-red-50').first()).toBeVisible() // error banner rendered
    }
  })

  test('DF curve previews a discount-factor grid (engine >= 0.5.0 only)', async ({ page, request }) => {
    const engine = await engineVersion(request)
    test.skip(!versionGte(engine, '0.5.0'), `DiscountFactorPoint needs engine >= 0.5.0 (running ${engine})`)

    const refDate = await latestDate(request)
    await fillValueCurveForm(page, {
      name: uniq('E2E df grid'),
      currency: 'GBP',
      referenceDate: refDate,
      quantity: 'Discount factors',
      pasteTable: '1Y 0.96\n5Y 0.82\n10Y 0.66',
    })
    const preview = await valueCurvePreview(page)
    expect(preview.status, JSON.stringify(preview.body).slice(0, 400)).toBe(200)
    const dfs = extractSeries(preview.body, 'DF')
    expect(dfs.length).toBeGreaterThan(3)
    for (const df of dfs) {
      expect(df).toBeGreaterThan(0)
      expect(df).toBeLessThanOrEqual(1)
    }
  })

  test('forward-rate curve previews (engine >= 0.5.0 only)', async ({ page, request }) => {
    const engine = await engineVersion(request)
    test.skip(!versionGte(engine, '0.5.0'), `ForwardRatePoint needs engine >= 0.5.0 (running ${engine})`)

    const refDate = await latestDate(request)
    await fillValueCurveForm(page, {
      name: uniq('E2E fwd curve'),
      currency: 'GBP',
      referenceDate: refDate,
      quantity: 'Forward rates',
      pasteTable: '6M 3.9\n2Y 4.1\n10Y 4.4',
    })
    const preview = await valueCurvePreview(page)
    expect(preview.status, JSON.stringify(preview.body).slice(0, 400)).toBe(200)
    const fwds = extractSeries(preview.body, 'FWD')
    expect(fwds.length).toBeGreaterThan(3)
  })
})
