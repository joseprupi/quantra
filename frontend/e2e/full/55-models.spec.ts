/**
 * Swaption models: Hull-White calibration from the Models page against a
 * created ATM-matrix vol surface + EUR curve environment; the
 * calibrate-swaption-vol tool via a SabrCalibrate surface; and a pinned
 * record of the Constant-surface calibration block.
 */
import { test, expect } from '@playwright/test'
import { fieldFor, gotoReady, selectByTextContains } from '../lib/ui'
import { waitForRealData } from '../lib/api'
import { uniq } from '../lib/stack'
import { createEurEnvironment, createSwaptionSurface } from '../lib/journeys'

test.beforeEach(async ({ request }) => {
  await waitForRealData(request)
})

async function fillModelForm(
  page: import('@playwright/test').Page,
  modelId: string,
  discName: string,
  volId: string,
) {
  await gotoReady(page, '/models/swaption')
  await page.getByRole('button', { name: 'New Model' }).click()
  await fieldFor(page, 'Model ID').fill(modelId)
  const discSel = page.locator(
    'xpath=//label[normalize-space(.)="Discounting Curve"]/following-sibling::div//select',
  )
  await discSel.nth(0).selectOption('__standalone__')
  await selectByTextContains(discSel.nth(1), discName)
  await selectByTextContains(fieldFor(page, 'Index'), 'EURIBOR_6M')
  await selectByTextContains(fieldFor(page, 'Swaption Vol Surface'), volId)
}

test('calibrate + store a Hull-White model on an ATM-matrix surface', async ({ page }) => {
  const tag = uniq('E2E hw')
  const { setName, discName } = await createEurEnvironment(page, tag)
  const volId = await createSwaptionSurface(page, setName, 'ATM Matrix')

  const modelId = uniq('e2e_hw_model')
  await fillModelForm(page, modelId, discName, volId)

  const respPromise = page.waitForResponse(
    (r) => r.url().includes('/v1/calibrate-swaption-model') && r.request().method() === 'POST',
    { timeout: 60_000 },
  )
  await page.getByRole('button', { name: 'Calibrate Hull-White Model' }).click()
  const resp = await respPromise
  const body = (await resp.json()) as Record<string, unknown>
  expect(resp.status(), `calibrate-model -> ${JSON.stringify(body).slice(0, 400)}`).toBe(200)

  // The calibrated model lands in the saved list with fitted a / sigma.
  await expect(page.getByText(new RegExp(`Calibrated ${modelId}`))).toBeVisible({
    timeout: 20_000,
  })
  await expect(page.getByText(modelId, { exact: true }).first()).toBeVisible()
})

// KNOWN GAP + error-quality issue (verified live): a CONSTANT surface has no
// calibration grid, so the Models page blocks calibration client-side — but
// with the MISLEADING message "Selected swaption vol surface grid exceeds
// curve horizon; choose longer curves or shorter expiries/tenors" (the code
// deliberately returns empty calibration axes for Constant and lets the
// grid-empty guard fire "uniformly"). No request is ever sent.
test('Constant-surface HW calibration is blocked with a misleading error (KNOWN GAP, pinned)', async ({
  page,
}) => {
  test.info().annotations.push({
    type: 'known-gap',
    description:
      'HW calibration on a Constant surface is blocked client-side with "grid exceeds curve horizon" (misleading — the real reason is Constant surfaces have no calibration grid). No request sent.',
  })
  const tag = uniq('E2E hwc')
  const { setName, discName } = await createEurEnvironment(page, tag)
  const volId = await createSwaptionSurface(page, setName)

  await fillModelForm(page, uniq('e2e_hw_const'), discName, volId)
  let requested = false
  page.on('request', (r) => {
    if (r.url().includes('/v1/calibrate-swaption-model')) requested = true
  })
  await page.getByRole('button', { name: 'Calibrate Hull-White Model' }).click()
  await expect(page.getByText(/exceeds curve horizon/)).toBeVisible({ timeout: 15_000 })
  expect(requested, 'no calibration request is sent for a Constant surface').toBe(false)
})

// DEFECT (verified live, deterministic — not a persist race, reproduced with
// multi-second settle time): switch a fresh surface's kind to "SABR
// Calibrate" and click its "▶ Calibrate now" button — the wire payload the
// portal builds is NOT a SabrCalibrateSpec, so the backend answers
//   400 {"code":"engine_invalid_request","error":"engine RPC failed:
//        INVALID_ARGUMENT (Endpoint /calibrate-swaption-vol only operates on
//        SabrCalibrate surfaces; vol_id '...' is not a
//        SwaptionSabrCalibrateSpec surface)"}
// i.e. the ONLY UI doorway to calibrate-swaption-vol rejects its own
// surface. Clean error, no crash — but the feature is unusable end-to-end.
test('calibrate-swaption-vol via a SabrCalibrate surface (KNOWN DEFECT, pinned)', async ({
  page,
}) => {
  test.info().annotations.push({
    type: 'defect',
    description:
      'Calibrate-vol unusable: a UI-created "SABR Calibrate" surface posts a non-SabrCalibrate payload -> 400 "vol_id ... is not a SwaptionSabrCalibrateSpec surface".',
  })
  const tag = uniq('E2E sabr')
  const { setName } = await createEurEnvironment(page, tag)
  await createSwaptionSurface(page, setName, 'SABR Calibrate')

  // The calibrate-vol tool is only reachable for SabrCalibrate surfaces.
  const calibrateBtn = page.getByRole('button', { name: /Calibrate now/i }).first()
  await expect(calibrateBtn, 'a Calibrate control exists for SabrCalibrate surfaces').toBeVisible({
    timeout: 15_000,
  })
  const respPromise = page.waitForResponse(
    (r) => r.url().includes('/v1/calibrate-swaption-vol') && r.request().method() === 'POST',
    { timeout: 60_000 },
  )
  await calibrateBtn.click()
  const resp = await respPromise
  const text = (await resp.text()).slice(0, 500)
  expect(resp.status(), `calibrate-swaption-vol -> ${text}`).toBe(400)
  expect(text).toContain('not a SwaptionSabrCalibrateSpec surface')
  // The UI shows the failure and stays alive.
  await expect(page.locator('header')).toBeVisible()
})
