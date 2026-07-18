/**
 * Curve sets: create a set, add discount + forward references to saved
 * curves, save, and verify membership round-trips from the list.
 */
import { test, expect } from '@playwright/test'
import { fieldFor, gotoReady, selectByTextContains } from '../lib/ui'
import { waitForRealData } from '../lib/api'
import { SEEDED, uniq } from '../lib/stack'
import { fillYieldCurveForm, saveCurve } from '../lib/journeys'

test.beforeEach(async ({ request }) => {
  await waitForRealData(request)
})

test('create a curve set with discount + forward members', async ({ page }) => {
  // A forward-role curve to reference (the seeded curves are discount-role).
  const fwdName = uniq('E2E fwd curve')
  await gotoReady(page, '/yield-curves/new')
  await fillYieldCurveForm(page, {
    name: fwdName,
    currency: 'GBP',
    deposits: [{ ratePct: 4.0, tenorNumber: 6, tenorUnit: 'Months' }],
  })
  // Role: forward (the role select is on the curve settings panel).
  await fieldFor(page, 'Curve Family').selectOption({ label: 'Forward (IBOR)' })
  await saveCurve(page, fwdName)

  // New curve set.
  const setName = uniq('E2E curve set')
  await gotoReady(page, '/curve-sets')
  await page.getByRole('button', { name: /New Curve Set/ }).click()
  await page.waitForURL(/\/curve-sets\/[^/]+$/, { timeout: 30_000 })
  await fieldFor(page, 'Name').fill(setName)
  await fieldFor(page, 'Currency').selectOption('GBP')

  // Add a discount reference -> pick the REAL seeded GBP curve.
  await page.getByRole('button', { name: '+ Discount' }).click()
  await selectByTextContains(fieldFor(page, 'Standalone Curve'), SEEDED.gbpOisCurveName)

  // Add a forward reference -> our forward curve.
  await page.getByRole('button', { name: '+ Forward' }).click()
  await selectByTextContains(fieldFor(page, 'Standalone Curve'), fwdName)

  // The editor AUTOSAVES every change (no Save button).
  // Membership round-trips: reopen from the list; both refs present.
  await gotoReady(page, '/curve-sets')
  await page.getByText(setName, { exact: true }).first().click()
  await expect(page.getByText(SEEDED.gbpOisCurveName).first()).toBeVisible({ timeout: 20_000 })
  await expect(page.getByText(fwdName).first()).toBeVisible()
})
