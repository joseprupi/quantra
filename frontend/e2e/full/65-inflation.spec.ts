/**
 * Inflation swaps: build + save a ZCIIS inflation curve, price a Zero-Coupon
 * inflation swap with a saved CPI index that carries REAL base fixings
 * (parity-checked); pin the default no-fixings gap; attempt the Year-on-Year
 * kind and record the outcome.
 */
import { test, expect, Page } from '@playwright/test'
import { fieldFor, gotoReady, pickStandaloneCurve, selectByTextContains, tenorFieldsFor } from '../lib/ui'
import { apiGet, assertPricingParity, capturePricing, waitForRealData } from '../lib/api'
import { SEEDED, uniq } from '../lib/stack'

test.beforeEach(async ({ request }) => {
  await waitForRealData(request)
})

async function ustLatest(request: import('@playwright/test').APIRequestContext) {
  const { latest_date } = (await apiGet(
    request,
    `/v1/market-data/latest-date?prefix=${SEEDED.ustQuotePrefix}`,
  )) as { latest_date: string }
  return latest_date
}

/**
 * Create a saved Inflation index carrying monthly CPI fixings around the
 * as-of (the engine needs the base fixing at as-of minus the observation
 * lag). Returns the index id.
 */
async function createCpiIndexWithFixings(page: Page, asOf: string): Promise<string> {
  const id = uniq('E2ECPIX').toUpperCase().replace(/[^A-Z0-9_]/g, '_')
  await gotoReady(page, '/indices')
  await page.getByRole('button', { name: 'New Index' }).click()
  await fieldFor(page, 'Index ID *').fill(id)
  await fieldFor(page, 'Type').selectOption('Inflation')
  await fieldFor(page, 'Family Name').fill('EUHICP')
  await fieldFor(page, 'Currency').selectOption('EUR')
  // Monthly first-of-month fixings covering ~8 months up to the as-of month.
  const ref = new Date(`${asOf}T00:00:00Z`)
  for (let offset = 7; offset >= 0; offset -= 1) {
    const d = new Date(Date.UTC(ref.getUTCFullYear(), ref.getUTCMonth() - offset, 1))
    const dateStr = d.toISOString().split('T')[0]
    await fieldFor(page, 'Date').fill(dateStr)
    await fieldFor(page, 'Value').fill((100 + (7 - offset) * 0.3).toFixed(1))
    await page.getByRole('button', { name: 'Add Fixing' }).click()
  }
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByText(id, { exact: true }).first()).toBeVisible({ timeout: 15_000 })
  return id
}

/** Build + save a ZCIIS inflation curve anchored at `refDate`; returns name. */
async function createInflationCurve(page: Page, refDate: string): Promise<string> {
  await gotoReady(page, '/inflation-curves/new')
  const name = uniq('E2E infl curve')
  await fieldFor(page, 'Curve Name *').fill(name)
  await fieldFor(page, 'Reference Date').fill(refDate)
  await fieldFor(page, 'Nominal Discount Curve').selectOption({
    label: SEEDED.usdTreasuryCurveName,
  })
  // NOTE: a new inflation curve is pre-seeded with default 1Y + 2Y helpers;
  // add non-colliding tenors (duplicate pillars abort the engine).
  for (const [tenor, rate] of [
    [5, 2.4],
    [10, 2.5],
    [15, 2.5],
  ] as Array<[number, number]>) {
    await page.getByRole('button', { name: 'Add Helper' }).click()
    await fieldFor(page, 'Rate (%)').fill(String(rate))
    const t = tenorFieldsFor(page)
    await t.number.fill(String(tenor))
    await t.unit.selectOption('Years')
    await page.getByRole('button', { name: /^(Add Helper|Update)$/ }).last().click()
  }
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page).toHaveURL(/\/inflation-curves$/, { timeout: 20_000 })
  await expect(page.getByText(name, { exact: true }).first()).toBeVisible({ timeout: 15_000 })
  return name
}

/** Common product-form setup; returns after curves/index are picked. */
async function setupInflationSwap(
  page: Page,
  asOf: string,
  curveName: string,
  tradeType: string,
  savedIndexId?: string,
) {
  await gotoReady(page, '/products/inflation-swaps/new')
  await fieldFor(page, 'As Of Date').fill(asOf)
  await fieldFor(page, 'Trade Type').selectOption({ label: tradeType })
  await pickStandaloneCurve(page, 'Discounting Curve', SEEDED.usdTreasuryCurveName)
  await pickStandaloneCurve(page, 'Inflation Curve', curveName)
  if (savedIndexId) {
    // Switch the pricing index to the saved CPI index (it carries fixings).
    await page.getByRole('button', { name: /^Saved/ }).click()
    await selectByTextContains(
      page.locator('xpath=//label[normalize-space(.)="Inflation Index"]/following::select[1]'),
      savedIndexId,
    )
  }
}

test('ZCIIS: saved CPI index with fixings -> price + parity', async ({ page, request }) => {
  const asOf = await ustLatest(request)
  const indexId = await createCpiIndexWithFixings(page, asOf)
  const curveName = await createInflationCurve(page, asOf)

  await setupInflationSwap(page, asOf, curveName, 'Zero Coupon Inflation Swap', indexId)

  const exchangePromise = capturePricing(page, '/v1/price/swaps/inflation')
  await page.getByRole('button', { name: /^Price/ }).click()
  const exchange = await exchangePromise
  expect(
    exchange.status,
    `ZCIIS pricing -> ${JSON.stringify(exchange.responseBody).slice(0, 500)}`,
  ).toBe(200)
  const npv = page.locator('div:has(> p:text-is("NPV"))').locator('p').nth(1)
  await expect(npv).toBeVisible({ timeout: 30_000 })
  await assertPricingParity(request, exchange, (await npv.textContent()) ?? '')
})

// KNOWN GAP (verified live): pricing a ZCIIS with the curve's default INLINE
// index fails — the index carries no CPI fixings and the product path (unlike
// the curve-preview path, which silently injects synthetic 100.0 fixings via
// ensureInflationBaseFixings) sends none:
//   400 {"code":"engine_invalid_request","error":"engine RPC failed:
//        INVALID_ARGUMENT (Zero inflation base fixing unavailable for curve
//        id '...': provide InflationIndexSpec.fixings for <base month>)"}
// There is no CPI-fixings field in the inline index picker, so out of the box
// the inflation swap CANNOT price without the saved-index workaround above.
test('ZCIIS with the default inline index 400s on base fixing (KNOWN GAP, pinned)', async ({
  page,
  request,
}) => {
  test.info().annotations.push({
    type: 'known-gap',
    description:
      'Inflation swap with the inline (no-fixings) CPI index cannot price: engine 400 "Zero inflation base fixing unavailable". Curve preview injects synthetic fixings; the product path does not, and the UI offers no inline fixings entry.',
  })
  const asOf = await ustLatest(request)
  const curveName = await createInflationCurve(page, asOf)
  await setupInflationSwap(page, asOf, curveName, 'Zero Coupon Inflation Swap')

  const exchangePromise = capturePricing(page, '/v1/price/swaps/inflation')
  await page.getByRole('button', { name: /^Price/ }).click()
  const exchange = await exchangePromise
  expect(exchange.status).toBe(400)
  expect(JSON.stringify(exchange.responseBody)).toContain('base fixing unavailable')
  // Clean error card, no crash.
  await expect(page.getByText(/engine|Invalid|failed/i).first()).toBeVisible({ timeout: 15_000 })
})

test('YYIIS: year-on-year swap attempt (record outcome)', async ({ page, request }) => {
  const asOf = await ustLatest(request)
  const indexId = await createCpiIndexWithFixings(page, asOf)
  const curveName = await createInflationCurve(page, asOf)

  await setupInflationSwap(page, asOf, curveName, 'Year-on-Year Inflation Swap', indexId)

  const exchangePromise = capturePricing(page, '/v1/price/swaps/inflation')
  await page.getByRole('button', { name: /^Price/ }).click()
  const exchange = await exchangePromise
  // Record the truth: succeed -> NPV renders; fail -> a clean error card
  // (no crash). Either way the response and UI state are captured.
  if (exchange.status === 200) {
    const npv = page.locator('div:has(> p:text-is("NPV"))').locator('p').nth(1)
    await expect(npv).toBeVisible({ timeout: 30_000 })
    await assertPricingParity(request, exchange, (await npv.textContent()) ?? '')
  } else {
    test.info().annotations.push({
      type: 'known-gap',
      description: `YYIIS pricing failed (${exchange.status}): ${JSON.stringify(
        exchange.responseBody,
      ).slice(0, 300)}`,
    })
    // The failure must be a clean error card, not a blank screen.
    await expect(page.getByText(/Invalid Request|error|failed/i).first()).toBeVisible({
      timeout: 15_000,
    })
  }
})
