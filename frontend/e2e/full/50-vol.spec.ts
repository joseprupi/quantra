/**
 * Vol workbench: create a swaption vol surface (constant, then ATM matrix),
 * sample it against a coherent EUR curve set, verify the sampled grid, and
 * follow "View trace" into the Investigate page.
 *
 * NOTE (coverage finding): the real-data stack seeds NO vol surface and no
 * EUR curves — everything here is created through the UI first. Sampling
 * only works when the selected curve set's helpers reference the chosen
 * float index (that is how the engine learns the index); a fresh "New
 * Surface" auto-run with defaults errors "Unknown index id" (see the pinned
 * test at the bottom).
 */
import { test, expect, Page } from '@playwright/test'
import { fieldFor, gotoReady, selectByTextContains } from '../lib/ui'
import { waitForRealData } from '../lib/api'
import { uniq } from '../lib/stack'
import { createEurEnvironment, createSwaptionSurface } from '../lib/journeys'

test.beforeEach(async ({ request }) => {
  await waitForRealData(request)
})

interface SampleResult {
  results: Array<{
    n_expiries: number
    n_tenors: number
    n_strikes: number
    vols: number[]
    error: { error_message?: string } | null
  }>
}

async function configureSampler(page: Page, setName: string) {
  await selectByTextContains(fieldFor(page, 'Curve Set'), setName)
  await selectByTextContains(fieldFor(page, 'Swap Index ID'), 'EUR_SWAP_6M')
  await selectByTextContains(fieldFor(page, 'Float Index ID'), 'EURIBOR_6M')
}

async function runSample(page: Page): Promise<SampleResult> {
  const respPromise = page.waitForResponse(
    (r) => r.url().includes('/v1/vol-surfaces/sample') && r.request().method() === 'POST',
    { timeout: 60_000 },
  )
  await page.getByRole('button', { name: 'Sample → Sampler' }).click()
  const resp = await respPromise
  expect(resp.status(), 'sample endpoint status').toBe(200)
  return (await resp.json()) as SampleResult
}

test('constant swaption surface: create -> sample -> grid + trace -> investigate', async ({
  page,
}) => {
  const tag = uniq('E2E vol')
  const { setName } = await createEurEnvironment(page, tag)

  await gotoReady(page, '/vol-workbench')
  await page.getByRole('button', { name: 'New Surface' }).click()
  await expect(page.getByText(/Working on:/)).toBeVisible({ timeout: 15_000 })

  await configureSampler(page, setName)
  const sample = await runSample(page)
  const r = sample.results[0]
  expect(r.error, `sample error: ${JSON.stringify(r.error)}`).toBeNull()
  expect(r.n_expiries).toBeGreaterThan(2)
  expect(r.n_tenors).toBeGreaterThan(2)
  expect(r.n_strikes).toBeGreaterThan(2)
  const finite = r.vols.filter((v) => Number.isFinite(v))
  expect(finite.length).toBeGreaterThan(50)
  // A CONSTANT surface must sample flat at the configured constant vol (0.01
  // Normal is the New Surface default).
  for (const v of finite) {
    expect(Math.abs(v - 0.01)).toBeLessThan(1e-9)
  }

  // The sampler surface view renders (heatmap tab active by default).
  await expect(page.getByRole('button', { name: 'Re-sample' })).toBeVisible({ timeout: 15_000 })

  // View trace -> Investigate shows the vol_sample pipeline stages.
  await page.getByRole('link', { name: /View trace/i }).first().click()
  await expect(page).toHaveURL(/investigate\?request_id=/)
  await expect(page.getByTestId('trace-header')).toBeVisible({ timeout: 20_000 })
  await expect(page.getByTestId('trace-product')).toContainText(/vol/i)
  await expect
    .poll(async () => page.getByTestId('stage-card').count(), { timeout: 20_000 })
    .toBeGreaterThanOrEqual(3)
})

test('ATM matrix swaption surface: edit grid -> sample non-flat', async ({ page }) => {
  const tag = uniq('E2E volm')
  const { setName } = await createEurEnvironment(page, tag)

  // (A fresh ATM matrix has axes but an EMPTY grid — the journey fills a
  // non-flat vol ladder; sampling it as-is errors "AtmMatrix dimension
  // mismatch: grid (missing)".)
  await createSwaptionSurface(page, setName, 'ATM Matrix')

  const sample = await runSample(page)
  const r = sample.results[0]
  expect(r.error, `ATM-matrix sample error: ${JSON.stringify(r.error)}`).toBeNull()
  const finite = r.vols.filter((v) => Number.isFinite(v))
  expect(finite.length).toBeGreaterThan(50)
  const min = Math.min(...finite)
  const max = Math.max(...finite)
  expect(max - min, 'ATM-matrix surface should be non-flat').toBeGreaterThan(1e-4)
})

// KNOWN GAP (verified live): a freshly created surface auto-runs a sample
// with an auto-picked float index that no selected curve references — the
// engine then rejects it ("Unknown index id: ..."). The user has to pick a
// coherent curve set + float index pair by hand; nothing guides them.
test('fresh New Surface auto-sample errors with Unknown index id (KNOWN GAP, pinned)', async ({
  page,
}) => {
  test.info().annotations.push({
    type: 'known-gap',
    description:
      'Vol sampler auto-selects a float index / curve set pair that is not coherent; fresh-surface auto-run errors "Unknown index id" until the user manually aligns curve set + float index (curve helpers must reference the index).',
  })
  await gotoReady(page, '/vol-workbench')
  const respPromise = page.waitForResponse(
    (r) => r.url().includes('/v1/vol-surfaces/sample') && r.request().method() === 'POST',
    { timeout: 60_000 },
  )
  await page.getByRole('button', { name: 'New Surface' }).click()
  const resp = await respPromise
  const body = (await resp.json()) as SampleResult
  const err = body.results?.[0]?.error?.error_message ?? ''
  expect(err, 'auto-run fails on an unregistered index').toContain('Unknown index id')
})
