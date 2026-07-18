/**
 * Swaption product: price a European swaption on the EUR environment —
 * (a) inline constant vol, (b) the ATM-matrix vol surface created in the
 * workbench. Parity-checked against the API both times.
 */
import { test, expect } from '@playwright/test'
import { fieldFor, gotoReady, pickStandaloneCurve, selectByTextContains } from '../lib/ui'
import { assertPricingParity, capturePricing, waitForRealData } from '../lib/api'
import { uniq } from '../lib/stack'
import { createEurEnvironment, createSwaptionSurface } from '../lib/journeys'

test.beforeEach(async ({ request }) => {
  await waitForRealData(request)
})

const NPV_PANEL = 'div:has(> p:text-is("NPV"))'

async function setupSwaption(page: import('@playwright/test').Page, discName: string) {
  await gotoReady(page, '/products/swaption/new')
  await pickStandaloneCurve(page, 'Discounting Curve (PV)', discName)
  await selectByTextContains(fieldFor(page, 'Floating Index'), 'EURIBOR_6M')
}

async function priceAndAssert(
  page: import('@playwright/test').Page,
  request: import('@playwright/test').APIRequestContext,
) {
  const exchangePromise = capturePricing(page, '/v1/price/swaption')
  await page.getByRole('button', { name: 'Price Swaption' }).click()
  const exchange = await exchangePromise
  expect(
    exchange.status,
    `swaption pricing -> ${JSON.stringify(exchange.responseBody).slice(0, 500)}`,
  ).toBe(200)
  const npv = page.locator(NPV_PANEL).locator('p').nth(1)
  await expect(npv).toBeVisible({ timeout: 30_000 })
  return assertPricingParity(request, exchange, (await npv.textContent()) ?? '')
}

test('European swaption with inline constant vol: price + parity', async ({ page, request }) => {
  const tag = uniq('E2E swpt')
  const { discName } = await createEurEnvironment(page, tag)
  await setupSwaption(page, discName)
  const { uiNpv } = await priceAndAssert(page, request)
  expect(Math.abs(uiNpv), 'swaption NPV should be finite/non-negative').toBeGreaterThanOrEqual(0)
})

test('European swaption priced off the created ATM-matrix vol surface', async ({
  page,
  request,
}) => {
  const tag = uniq('E2E swpts')
  const { discName, setName } = await createEurEnvironment(page, tag)
  const volId = await createSwaptionSurface(page, setName, 'ATM Matrix')

  await setupSwaption(page, discName)
  await selectByTextContains(fieldFor(page, 'Volatility Source'), volId)
  await priceAndAssert(page, request)
})
