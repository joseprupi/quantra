/**
 * Swaption models: Hull-White calibration from the Models page against a
 * created ATM-matrix vol surface + EUR curve environment; SABR smile
 * calibration end to end via a SabrCalibrate surface (create -> fill smile
 * -> Calibrate now -> per-node parameters rendered); and a pinned record of
 * the Constant-surface calibration block.
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

// SABR smile calibration end to end (formerly a pinned defect). Two root
// causes fixed: (backend) the orchestrator's swaption-vol translator had no
// SwaptionSabrCalibrateSpec branch, so the engine received a Constant
// surface and answered 400 "vol_id ... is not a SwaptionSabrCalibrateSpec
// surface"; (portal) an unfilled cube's NaN cells became null after the
// save/reload JSON round trip and wired silently as 0 vols. Now: create an
// EUR environment, create a SABR Calibrate surface, shrink the node grid,
// fill a realistic smile, Calibrate now -> 200 with per-node parameters.
test('calibrate-swaption-vol via a SabrCalibrate surface end to end', async ({ page }) => {
  const tag = uniq('E2E sabr')
  const { setName } = await createEurEnvironment(page, tag)
  await createSwaptionSurface(page, setName, 'SABR Calibrate')

  // Shrink the default axes to a 2x2 node grid (keeps the journey fast and
  // well inside the 30Y curve horizon). Each axis list renders one row per
  // period with a Remove button; keep only the wanted labels.
  const trimAxis = async (axisLabel: string, keep: string[]) => {
    const rows = page.locator(
      `xpath=//label[normalize-space(.)="${axisLabel}"]/following-sibling::div//div[contains(@class,"justify-between")]`,
    )
    // Remove rows until only the kept labels remain.
    for (let guard = 0; guard < 30; guard++) {
      const texts = await rows.locator('span').allTextContents()
      const dropIdx = texts.findIndex((t) => !keep.includes(t.trim()))
      if (dropIdx === -1) break
      await rows.nth(dropIdx).getByTitle('Remove').click()
    }
    expect((await rows.count()), `${axisLabel} trimmed to ${keep.join(',')}`).toBe(keep.length)
  }
  await trimAxis('Surface expiries', ['1Y', '2Y'])
  await trimAxis('Surface tenors', ['2Y', '5Y'])

  // Fill the market-vol cube: one flat table, rows = expiry x tenor nodes,
  // cols = the 5 default strike spreads (-100bp..+100bp). A gentle smile:
  // wings above ATM, slight per-node level shift.
  const volTable = page.locator('table[aria-label^="SABR market vol cube editor"]')
  await expect(volTable).toBeVisible({ timeout: 15_000 })
  const cells = volTable.locator('tbody input')
  await expect(cells).toHaveCount(20)
  const smile = [0.38, 0.34, 0.32, 0.335, 0.365]
  for (let row = 0; row < 4; row++) {
    for (let col = 0; col < 5; col++) {
      await cells.nth(row * 5 + col).fill((smile[col] + row * 0.005).toFixed(3))
    }
  }

  const respPromise = page.waitForResponse(
    (r) => r.url().includes('/v1/calibrate-swaption-vol') && r.request().method() === 'POST',
    { timeout: 60_000 },
  )
  await page.getByRole('button', { name: /Calibrate now/i }).click()
  const resp = await respPromise
  const body = (await resp.json()) as {
    diagnostics?: {
      alpha_per_node?: number[]
      beta_per_node?: number[]
      calibration?: { overall_rmse?: number; converged?: boolean }
    }
  }
  expect(resp.status(), `calibrate-swaption-vol -> ${JSON.stringify(body).slice(0, 400)}`).toBe(200)

  // Response carries the full per-node diagnostics for the 4 nodes.
  const diag = body.diagnostics
  expect(diag?.alpha_per_node, 'alpha per node present').toHaveLength(4)
  for (const a of diag?.alpha_per_node ?? []) {
    expect(a, 'alpha plausible').toBeGreaterThan(0)
    expect(a).toBeLessThan(5)
  }
  // beta was fixed at 0.5 (the default options block).
  for (const b of diag?.beta_per_node ?? []) expect(b).toBeCloseTo(0.5, 6)

  // The UI renders the diagnostics: per-node table + overall stats.
  await expect(page.getByText('Calibration diagnostics')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByText('Per-node calibrated parameters')).toBeVisible()
  const perNode = page.locator('table[aria-label="Per-node SABR calibration results"]')
  await expect(perNode.locator('tbody tr')).toHaveCount(4)
  // First node row shows expiry/tenor labels and a plausible alpha value.
  const firstRowCells = await perNode.locator('tbody tr').first().locator('td').allTextContents()
  expect(firstRowCells[0]).toBe('1Y')
  expect(firstRowCells[1]).toBe('2Y')
  const alphaCell = Number(firstRowCells[4])
  expect(Number.isFinite(alphaCell), `alpha cell renders a number (got "${firstRowCells[4]}")`).toBe(true)
  expect(alphaCell).toBeGreaterThan(0)
  await expect(page.getByText(/Overall RMSE/)).toBeVisible()
})
