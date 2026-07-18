/**
 * Equity options: create an EquityBlack vol surface + discount/dividend
 * curves, price a call option, parity-check.
 */
import { test, expect } from '@playwright/test'
import { fieldFor, gotoReady, pickStandaloneCurve, selectByTextContains } from '../lib/ui'
import { assertPricingParity, capturePricing, waitForRealData } from '../lib/api'
import { uniq } from '../lib/stack'
import { fillYieldCurveForm, saveCurve } from '../lib/journeys'

test.beforeEach(async ({ request }) => {
  await waitForRealData(request)
})

test('equity option: create curves + Black vol surface -> price + parity', async ({
  page,
  request,
}) => {
  const tag = uniq('E2E eq')

  // Discount + dividend curves (deposit strips; dividend = forward role).
  const discName = `${tag} disc`
  await fillYieldCurveForm(page, {
    name: discName,
    currency: 'USD',
    deposits: [
      { ratePct: 4.0, tenorNumber: 6, tenorUnit: 'Months' },
      { ratePct: 4.2, tenorNumber: 2, tenorUnit: 'Years' },
    ],
  })
  await saveCurve(page, discName)

  const divName = `${tag} div`
  await fillYieldCurveForm(page, {
    name: divName,
    currency: 'USD',
    deposits: [
      { ratePct: 1.0, tenorNumber: 6, tenorUnit: 'Months' },
      { ratePct: 1.0, tenorNumber: 2, tenorUnit: 'Years' },
    ],
  })
  await fieldFor(page, 'Curve Family').selectOption({ label: 'Forward (IBOR)' })
  await saveCurve(page, divName)

  // EquityBlack constant vol surface (0.2 default) from the workbench:
  // open any surface first (edit view), switch the type filter to
  // EquityBlack, then "New Surface" creates one OF THAT TYPE.
  await gotoReady(page, '/vol-workbench')
  await page.getByRole('button', { name: 'New Surface' }).click()
  await expect(page.getByText(/Working on:/)).toBeVisible({ timeout: 15_000 })
  await fieldFor(page, 'Surface Type').selectOption('EquityBlack')
  // "New Surface" lives on the LIST view and creates a surface of the
  // currently selected type.
  await page.getByRole('button', { name: 'Back to Surfaces' }).click()
  await page.getByRole('button', { name: 'New Surface' }).click()
  await expect(page.getByText(/Working on:/)).toBeVisible({ timeout: 15_000 })
  const working = (await page.getByText(/Working on:/).textContent()) ?? ''
  const volId = working.replace('Working on:', '').trim()
  expect(volId, 'an EquityBlack surface id').toMatch(/^vol_/)

  // Price the option.
  await gotoReady(page, '/products/equity-options/new')
  await pickStandaloneCurve(page, 'Discount Curve', discName)
  await pickStandaloneCurve(page, 'Dividend / Repo Curve', divName)
  await selectByTextContains(fieldFor(page, 'Vol Surface'), volId)
  await fieldFor(page, 'Strike').fill('100')
  await fieldFor(page, 'Spot').fill('105')

  const exchangePromise = capturePricing(page, '/v1/price/equity-option')
  await page.getByRole('button', { name: /^Price/ }).click()
  const exchange = await exchangePromise
  expect(
    exchange.status,
    `equity option pricing -> ${JSON.stringify(exchange.responseBody).slice(0, 500)}`,
  ).toBe(200)
  const npv = page.locator('div:has(> p:text-is("NPV"))').locator('p').nth(1)
  await expect(npv).toBeVisible({ timeout: 30_000 })
  const { uiNpv } = await assertPricingParity(request, exchange, (await npv.textContent()) ?? '')
  // An ITM call on spot 105 / strike 100 must be worth at least intrinsic-ish.
  expect(uiNpv).toBeGreaterThan(1)
})
