/**
 * Bonds: fixed-rate bond off the REAL USD Treasury curve (price + parity +
 * save/reload), floating-rate bond with a saved IBOR index, plus a pinned
 * defect on the DEFAULT As-Of (BoE-vs-Treasury latest-date mismatch).
 */
import { test, expect } from '@playwright/test'
import { fieldFor, gotoReady, pickStandaloneCurve, selectByTextContains } from '../lib/ui'
import { apiGet, assertPricingParity, capturePricing, waitForRealData } from '../lib/api'
import { SEEDED, uniq } from '../lib/stack'
import { createIborIndex } from '../lib/journeys'

test.beforeEach(async ({ request }) => {
  await waitForRealData(request)
})

async function ustLatestDate(request: import('@playwright/test').APIRequestContext) {
  const body = (await apiGet(
    request,
    `/v1/market-data/latest-date?prefix=${SEEDED.ustQuotePrefix}`,
  )) as { latest_date: string | null }
  expect(body.latest_date, 'UST data ingested').toBeTruthy()
  return body.latest_date as string
}

test.describe('fixed-rate bond — real USD Treasury curve', () => {
  test('price + parity + save + reload from a fresh browser', async ({
    page,
    request,
    browser,
  }) => {
    const base = process.env.E2E_PORTAL_URL || 'http://localhost:5173'
    const asOf = await ustLatestDate(request)
    await gotoReady(page, '/products/fixed-rate-bond/new')
    await pickStandaloneCurve(page, 'Discount Curve', SEEDED.usdTreasuryCurveName)
    // Align the pricing As-Of with the Treasury curve's (auto-rolled)
    // reference date — see the pinned default-As-Of defect below.
    await fieldFor(page, 'As Of Date').fill(asOf)

    const exchangePromise = capturePricing(page, '/v1/price/bonds/fixed')
    await page.getByRole('button', { name: 'Price Bond', exact: true }).click()
    const exchange = await exchangePromise
    expect(
      exchange.status,
      `bond pricing -> ${JSON.stringify(exchange.responseBody).slice(0, 400)}`,
    ).toBe(200)
    const npv = page.locator('div:has(> p:text-is("NPV"))').locator('p').nth(1)
    await expect(npv).toBeVisible({ timeout: 30_000 })
    await assertPricingParity(request, exchange, (await npv.textContent()) ?? '')
    // Clean/dirty price render too.
    await expect(page.getByText('Clean Price')).toBeVisible()
    await expect(page.getByText('Dirty Price')).toBeVisible()

    // Save + fresh-context reload + reprice.
    const name = uniq('E2E UST bond')
    await page.getByLabel('Product name').fill(name)
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(page.getByText(/Saved/).first()).toBeVisible({ timeout: 20_000 })

    const ctx = await browser.newContext()
    const fresh = await ctx.newPage()
    try {
      await fresh.goto(`${base}/products/fixed-rate-bond`)
      await expect(fresh.locator('header')).toBeVisible({ timeout: 20_000 })
      await fresh.getByText(name, { exact: true }).first().click({ timeout: 20_000 })
      await fresh.locator('xpath=//label[normalize-space(.)="As Of Date"]/following::input[1]').fill(asOf)
      // FIXED (was a pinned intermittent defect): the reloaded bond editor
      // used to show the discount curve as selected while the Price button
      // stayed DISABLED forever — CurveSetSelector only auto-resolved ids on
      // mount (before the saved record loaded) so the parent never received
      // the Curve object, and the save-graph 409 meant no appGraph.bondId
      // fallback either. Both halves are fixed (id-keyed selector resolution
      // + working save-graph), so the button must now be enabled WITHOUT
      // re-picking the curve.
      const priceBtn = fresh.getByRole('button', { name: 'Price Bond', exact: true })
      await expect(priceBtn, 'reloaded saved bond is priceable without re-picking the curve').toBeEnabled({
        timeout: 20_000,
      })
      const repricePromise = capturePricing(fresh, '/v1/price/bonds/fixed')
      await priceBtn.click()
      const reprice = await repricePromise
      const freshNpv = fresh.locator('div:has(> p:text-is("NPV"))').locator('p').nth(1)
      await expect(freshNpv).toBeVisible({ timeout: 30_000 })
      await assertPricingParity(request, reprice, (await freshNpv.textContent()) ?? '')
    } finally {
      await ctx.close()
    }
  })

  // ERROR CONTRACT (was a pinned defect): the app-wide default As-Of comes
  // from the BoE (GBP) latest-date, but the USD Treasury curve auto-rolls its
  // reference_date to the TREASURY latest-date. Whenever the two feeds are
  // not on the same business day (observed: BoE 2026-07-16 vs UST
  // 2026-07-17), clicking "Price Bond" with all defaults used to die with an
  // opaque
  //   502 {"code":"engine_upstream_error",
  //        "error":"engine RPC failed: ABORTED (negative time (-0.0027...) given)"}
  // The backend now pre-flights `as_of < curve.reference_date` and returns a
  // typed, actionable 422 `pricing_as_of_before_curve_date` naming the as-of,
  // the curve + its reference date, and what to do. This journey pins that
  // error contract (it auto-skips when the two feeds coincide).
  test('DEFAULT As-Of before the USD curve date yields the typed 422 (not an opaque 502)', async ({
    page,
    request,
  }) => {
    const boe = (await apiGet(
      request,
      `/v1/market-data/latest-date?prefix=${SEEDED.boeQuotePrefix}`,
    )) as { latest_date: string | null }
    const ust = await ustLatestDate(request)
    test.skip(boe.latest_date === ust, 'BoE and UST latest dates coincide today — mismatch not reproducible')
    await gotoReady(page, '/products/fixed-rate-bond/new')
    await pickStandaloneCurve(page, 'Discount Curve', SEEDED.usdTreasuryCurveName)
    const exchangePromise = capturePricing(page, '/v1/price/bonds/fixed')
    await page.getByRole('button', { name: 'Price Bond', exact: true }).click()
    const exchange = await exchangePromise
    const body = exchange.responseBody as {
      code?: string
      error?: string
      details?: Array<{ curve?: string; reference_date?: string; as_of?: string }>
    }
    expect(
      exchange.status,
      `default-As-Of bond pricing -> ${JSON.stringify(exchange.responseBody).slice(0, 400)}`,
    ).toBe(422)
    expect(body.code).toBe('pricing_as_of_before_curve_date')
    // The message is actionable: names both dates + tells the user what to do.
    expect(String(body.error ?? '')).toContain('predates the reference date')
    expect(String(body.error ?? '')).toContain(ust)
    // Structured details name the offending curve + its reference date. The
    // portal ships the curve INLINE with `name: curve.id` (see
    // FixedRateBond.tsx toThinACurve), so the identifying string here is the
    // backend curve id, not the display name — accept either.
    const entry = (body.details ?? [])[0] ?? {}
    expect(String(entry.curve ?? '')).not.toBe('')
    expect(entry.reference_date).toBe(ust)
  })
})

test.describe('floating-rate bond — USD Treasury curve + saved IBOR index', () => {
  test('price + parity (forward-start FRN, USD_LIBOR_6M)', async ({ page, request }) => {
    test.info().annotations.push({
      type: 'known-gap',
      description:
        'A DEFAULT (spot-start) floating bond 422s with missing_required_fixing (historical index fixings are not yet market data — O47(b) class). The error is clean and actionable; this journey prices a forward-start FRN instead.',
    })
    // USD_LIBOR_6M is in the backend's known-IBOR catalog; create the saved
    // index if a previous run has not already.
    await gotoReady(page, '/indices')
    if ((await page.getByText('USD_LIBOR_6M', { exact: true }).count()) === 0) {
      await createIborIndex(page, {
        id: 'USD_LIBOR_6M',
        family: 'USDLibor',
        tenorNumber: 6,
        tenorUnit: 'Months',
        calendar: 'UnitedStates',
        dayCounter: 'Actual360',
      })
    }

    const asOf = await ustLatestDate(request)
    await gotoReady(page, '/products/floating-rate-bond/new')
    await pickStandaloneCurve(page, 'Discounting Curve (PV)', SEEDED.usdTreasuryCurveName)
    await fieldFor(page, 'As Of Date').fill(asOf)
    await selectByTextContains(fieldFor(page, 'IBOR Index'), 'USD_LIBOR_6M')
    // Forward-start: a spot-start FRN needs a historical fixing (see the
    // annotation above); push issue/effective past the As-Of.
    await fieldFor(page, 'Issue Date').fill('2026-09-01')
    await fieldFor(page, 'Effective Date').fill('2026-09-01')
    await fieldFor(page, 'Maturity Date').fill('2031-09-01')

    const exchangePromise = capturePricing(page, '/v1/price/bonds/floating')
    await page.getByRole('button', { name: 'Price Bond', exact: true }).click()
    const exchange = await exchangePromise
    expect(
      exchange.status,
      `floating bond pricing -> ${JSON.stringify(exchange.responseBody).slice(0, 500)}`,
    ).toBe(200)
    const npv = page.locator('div:has(> p:text-is("NPV"))').locator('p').nth(1)
    await expect(npv).toBeVisible({ timeout: 30_000 })
    await assertPricingParity(request, exchange, (await npv.textContent()) ?? '')
  })
})
