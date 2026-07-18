/**
 * CDS: create a saved credit curve (flat hazard) -> price a CDS against the
 * REAL USD Treasury discount curve with it -> parity + save/reload.
 */
import { test, expect } from '@playwright/test'
import { fieldFor, gotoReady, pickStandaloneCurve, selectByTextContains } from '../lib/ui'
import { apiGet, assertPricingParity, capturePricing, waitForRealData } from '../lib/api'
import { SEEDED, uniq } from '../lib/stack'

test.beforeEach(async ({ request }) => {
  await waitForRealData(request)
})

test('credit curve (flat hazard) -> CDS price + parity', async ({ page, request }) => {
  // 1. Saved credit curve with a flat hazard rate.
  const curveId = uniq('e2e_credit').replace(/[^a-z0-9_]/g, '_')
  await gotoReady(page, '/credit-curves/new')
  await fieldFor(page, 'ID *').fill(curveId)
  await fieldFor(page, 'Currency').selectOption('USD')
  await fieldFor(page, 'Name').fill(`E2E credit ${curveId}`)
  await page.getByRole('button', { name: 'Flat hazard rate' }).click()
  await fieldFor(page, 'Recovery Rate').fill('0.4')
  await fieldFor(page, 'Flat Hazard Rate').fill('0.02')
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  // Editor navigates back to the credit-curve list on success.
  await expect(page).toHaveURL(/credit-curves/, { timeout: 20_000 })
  await gotoReady(page, '/credit-curves')
  await expect(page.getByText(curveId).first()).toBeVisible({ timeout: 15_000 })

  // 2. CDS against the real USD Treasury discount curve + that credit curve.
  const { latest_date: asOf } = (await apiGet(
    request,
    `/v1/market-data/latest-date?prefix=${SEEDED.ustQuotePrefix}`,
  )) as { latest_date: string }
  await gotoReady(page, '/products/cds/new')
  await fieldFor(page, 'As Of Date').fill(asOf)
  await pickStandaloneCurve(page, 'Discount Curve', SEEDED.usdTreasuryCurveName)
  await fieldFor(page, 'Curve Source').selectOption({ label: 'Use saved credit curve' })
  await selectByTextContains(fieldFor(page, 'Saved Credit Curve'), curveId)

  const exchangePromise = capturePricing(page, '/v1/price/cds')
  await page.getByRole('button', { name: 'Price CDS' }).click()
  const exchange = await exchangePromise
  expect(
    exchange.status,
    `CDS pricing -> ${JSON.stringify(exchange.responseBody).slice(0, 500)}`,
  ).toBe(200)
  const npv = page.locator('div:has(> p:text-is("NPV"))').locator('p').nth(1)
  await expect(npv).toBeVisible({ timeout: 30_000 })
  const { uiNpv } = await assertPricingParity(request, exchange, (await npv.textContent()) ?? '')
  expect(Math.abs(uiNpv), 'CDS NPV must be materially non-zero (real hazard + spread)').toBeGreaterThan(1)
  // Leg NPVs render.
  await expect(page.getByText('Default Leg NPV:')).toBeVisible()
  await expect(page.getByText('Premium Leg NPV:')).toBeVisible()
})
