/**
 * IR Swap product journeys — the flagship real-data path:
 * a GBP swap priced off the REAL Bank of England SONIA OIS curve
 * (Vanilla kind + SONIA float index; the portal's OIS/Basis/CMS kinds are a
 * documented capability gap — see the last test).
 *
 * Covers: create -> price -> NPV renders -> parity vs direct API ->
 * input sensitivity (rate change flips NPV) -> View trace -> Investigate ->
 * save -> reload from list -> reprice. Plus a vanilla swap whose float leg
 * uses a freshly created CUSTOM IBOR index.
 */
import { test, expect } from '@playwright/test'
import {
  fieldFor,
  gotoReady,
  parseUiNumber,
  pickStandaloneCurve,
  selectByTextContains,
} from '../lib/ui'
import { apiGet, assertPricingParity, capturePricing, waitForRealData } from '../lib/api'
import { SEEDED, uniq } from '../lib/stack'
import { createIborIndex } from '../lib/journeys'

test.beforeEach(async ({ request }) => {
  await waitForRealData(request)
})

const NPV_PANEL = 'div:has(> p:text-is("NPV"))'

async function readNpv(page: import('@playwright/test').Page): Promise<string> {
  const npv = page.locator(NPV_PANEL).locator('p').nth(1)
  await expect(npv).toBeVisible({ timeout: 30_000 })
  return (await npv.textContent()) ?? ''
}

/** Fill the shared GBP-swap form: real BoE curve + SONIA float index. */
async function setupGbpVanillaSonia(page: import('@playwright/test').Page, ratePct: string) {
  await gotoReady(page, '/products/ir-swap/new')
  await pickStandaloneCurve(page, 'Discounting Curve (PV)', SEEDED.gbpOisCurveName)
  // Vanilla is the default kind; the float leg projects SONIA off the same
  // curve (single-curve mode is on by default).
  await selectByTextContains(fieldFor(page, 'Index'), SEEDED.soniaIndexId)
  await fieldFor(page, 'Rate (%)').fill(ratePct)
}

test.describe('IR swap — GBP off the real BoE SONIA curve', () => {
  test('price + parity + rate sensitivity + trace -> investigate', async ({ page, request }) => {
    await setupGbpVanillaSonia(page, '2')
    let exchangePromise = capturePricing(page, '/v1/price/swap/ir')
    await page.getByRole('button', { name: 'Price', exact: true }).click()
    const exchange1 = await exchangePromise
    const npvText1 = await readNpv(page)
    const { uiNpv: npv1 } = await assertPricingParity(request, exchange1, npvText1)
    expect(Math.abs(npv1), 'NPV should be materially non-zero at 2% fixed').toBeGreaterThan(1_000)

    // The wire body is the inline Thin-A shape with the MD-referencing curve:
    // top-level curves whose points keep unresolved quote_ids (server resolves).
    const curves = exchange1.requestBody.curves as Array<Record<string, unknown>>
    expect(Array.isArray(curves) && curves.length > 0, 'wire body carries top-level curves').toBe(
      true,
    )

    // Input sensitivity: 6% fixed must flip the NPV sign (BoE curve ~4%).
    await fieldFor(page, 'Rate (%)').fill('6')
    exchangePromise = capturePricing(page, '/v1/price/swap/ir')
    await page.getByRole('button', { name: 'Price', exact: true }).click()
    await exchangePromise
    const npv2 = parseUiNumber(await readNpv(page))
    expect(npv2, 'NPV must move with the fixed rate').not.toEqual(npv1)
    expect(npv1 * npv2, 'NPV must flip sign between 2% and 6% fixed').toBeLessThan(0)

    // Trace: the results panel offers "View trace" -> Investigate renders stages.
    const traceLink = page.getByRole('link', { name: /View trace/i })
    await expect(traceLink).toBeVisible()
    await traceLink.click()
    await expect(page).toHaveURL(/investigate\?request_id=/)
    await expect(page.getByTestId('trace-header')).toBeVisible({ timeout: 20_000 })
    const stages = page.getByTestId('stage-card')
    await expect
      .poll(async () => stages.count(), { timeout: 20_000, message: 'trace stages render' })
      .toBeGreaterThanOrEqual(3)
    await expect(page.getByTestId('trace-product')).toContainText(/swap/i)
  })

  // FIXED (was a pinned HIGH defect): the product save-graph used to persist
  // the swap's wire curves under the FIXED name "discount" (the wire curve
  // id) — the first-ever product save created a live curve row literally
  // named "discount", EVERY later save's POST /v1/curves 409'd with
  // name_conflict, and in one observed session-state a user's own unrelated
  // curve named "discount" was silently overwritten. The save-graph now
  // derives a unique product-owned name per persisted curve and re-saves
  // PATCH by remembered UUID with a name-less body (see
  // src/lib/api/saveGraphNaming.ts). Two tests: the save/reload journey, and
  // the strengthened clobber test below (formerly the annotated 409 pin).
  test('save -> reload from a fresh browser -> reprice parity', async ({
    page,
    request,
    browser,
  }) => {
    const base = process.env.E2E_PORTAL_URL || 'http://localhost:5173'
    const name = uniq('E2E GBP swap')
    await setupGbpVanillaSonia(page, '3')

    await page.getByLabel('Product name').fill(name)
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(page.getByText(/Saved/).first()).toBeVisible({ timeout: 20_000 })

    // Reload from the saved list in a FRESH browser context — the swap must
    // come back from the SERVER, not this session's localStorage.
    const freshContext = await browser.newContext()
    const fresh = await freshContext.newPage()
    try {
      await fresh.goto(`${base}/products/ir-swap`)
      await expect(fresh.locator('header')).toBeVisible({ timeout: 20_000 })
      await fresh.getByText(name, { exact: true }).first().click({ timeout: 20_000 })
      await expect(fresh).toHaveURL(/\/products\/ir-swap\/(?!new)/)

      const exchangePromise = capturePricing(fresh, '/v1/price/swap/ir')
      await fresh.getByRole('button', { name: 'Price', exact: true }).click()
      const exchange = await exchangePromise
      const npv = fresh.locator(NPV_PANEL).locator('p').nth(1)
      await expect(npv).toBeVisible({ timeout: 30_000 })
      await assertPricingParity(request, exchange, (await npv.textContent()) ?? '')
    } finally {
      await freshContext.close()
    }
  })

  test('save-graph curve identity: repeated saves never 409 and never touch a user curve named "discount"', async ({
    page,
    request,
  }) => {
    // Strengthened regression test for the fixed clobber/collision defect:
    //   1. a user curve literally named "discount" pre-exists,
    //   2. TWO products are saved TWICE each (create arm + PATCH-by-id arm),
    //   3. no request ever 409s, no POST /v1/curves carries a constant role
    //      name, every toast is honest, and the user's curve row is
    //      byte-identical afterwards.
    const base = process.env.E2E_PORTAL_URL || 'http://localhost:5173'

    // The user's own unrelated curve named "discount" (idempotent across
    // runs: reuse the existing row when a previous run created it).
    await request.post(`${base}/v1/curves`, {
      data: { name: 'discount', currency: 'GBP', body: { role: 'discount' } },
    })
    const listed = (await apiGet(request, '/v1/curves?limit=200')) as {
      items: Array<{ id: string; name: string }>
    }
    const userCurveId = listed.items.find((c) => c.name === 'discount')?.id
    expect(userCurveId, 'user curve named "discount" exists').toBeTruthy()
    const before = JSON.stringify(await apiGet(request, `/v1/curves/${userCurveId}`))

    // Wire-level tripwires for the whole journey.
    const conflicts: string[] = []
    page.on('response', (r) => {
      if (r.status() === 409) conflicts.push(`${r.request().method()} ${r.url()}`)
    })
    const postedCurveNames: string[] = []
    page.on('request', (r) => {
      if (r.method() === 'POST' && r.url().includes('/v1/curves')) {
        try {
          const body = r.postDataJSON() as { name?: string }
          if (body?.name !== undefined) postedCurveNames.push(body.name)
        } catch {
          /* non-JSON */
        }
      }
    })

    const failureBanner = page.getByText(/server persist failed/)

    // Product A: GBP swap — save (create arm) then re-save (PATCH-by-id arm).
    await setupGbpVanillaSonia(page, '3')
    await page.getByLabel('Product name').fill(uniq('E2E identity swap'))
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(page.getByText(/Referenced server-side/)).toBeVisible({ timeout: 20_000 })
    await expect(page).toHaveURL(/\/products\/ir-swap\/(?!new)/)
    await expect(failureBanner).toHaveCount(0)
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(page.getByText(/Updated server copy/)).toBeVisible({ timeout: 20_000 })
    await expect(failureBanner).toHaveCount(0)

    // Product B: USD fixed bond — save twice as well.
    await gotoReady(page, '/products/fixed-rate-bond/new')
    await pickStandaloneCurve(page, 'Discount Curve', SEEDED.usdTreasuryCurveName)
    await page.getByLabel('Product name').fill(uniq('E2E identity bond'))
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(page.getByText(/Referenced server-side/)).toBeVisible({ timeout: 20_000 })
    await expect(page).toHaveURL(/\/products\/fixed-rate-bond\/(?!new)/)
    await expect(failureBanner).toHaveCount(0)
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(page.getByText(/Updated server copy/)).toBeVisible({ timeout: 20_000 })
    await expect(failureBanner).toHaveCount(0)

    // No collisions anywhere; persisted curve names are product-derived,
    // never a bare role constant.
    expect(conflicts, 'no 409 during any save').toEqual([])
    expect(postedCurveNames.length, 'the saves did persist curves').toBeGreaterThanOrEqual(2)
    for (const name of postedCurveNames) {
      expect(name, 'POST /v1/curves must never use a constant role name').not.toBe('discount')
      expect(name).not.toBe('forward')
    }

    // The user's unrelated curve is byte-identical (same row, same
    // updated_at — it was never PATCHed, restored or renamed).
    const after = JSON.stringify(await apiGet(request, `/v1/curves/${userCurveId}`))
    expect(after, 'user curve named "discount" untouched byte-for-byte').toBe(before)
  })
})

test.describe('IR swap — vanilla with a CUSTOM IBOR index on the float leg', () => {
  test('create custom GBP 6M IBOR index and price a vanilla swap with it', async ({
    page,
    request,
  }) => {
    const indexId = uniq('E2EGBP6M').toUpperCase().replace(/[^A-Z0-9_]/g, '_')
    await createIborIndex(page, {
      id: indexId,
      family: 'GBPLibor',
      tenorNumber: 6,
      tenorUnit: 'Months',
      calendar: 'UnitedKingdom',
      dayCounter: 'Actual365Fixed',
    })

    await gotoReady(page, '/products/ir-swap/new')
    await pickStandaloneCurve(page, 'Discounting Curve (PV)', SEEDED.gbpOisCurveName)
    await selectByTextContains(fieldFor(page, 'Index'), indexId)
    await fieldFor(page, 'Rate (%)').fill('4')

    const exchangePromise = capturePricing(page, '/v1/price/swap/ir')
    await page.getByRole('button', { name: 'Price', exact: true }).click()
    const exchange = await exchangePromise
    expect(
      exchange.status,
      `pricing with custom index ${indexId} -> ${JSON.stringify(exchange.responseBody).slice(0, 400)}`,
    ).toBe(200)
    const npvText = await readNpv(page)
    await assertPricingParity(request, exchange, npvText)
  })
})

test.describe('IR swap — non-vanilla kinds (documented capability gap)', () => {
  // KNOWN GAP, verified live: the portal's OIS / Basis / CMS swap kinds still
  // ship the legacy fat request wrapped under ``swap:`` and the backend
  // rejects it (422 "Inline mode (``swap`` set) requires a non-empty
  // ``curves`` list."). The source comments call the visible failure
  // intentional until the non-vanilla translation work lands
  // (src/pages/products/IrSwap.tsx buildRequest). This test pins the CURRENT
  // behavior: a clean 422 error card, no crash, no fake numbers.
  test('OIS kind: 422 error card rendered (legacy fat body, by design)', async ({ page }) => {
    test.info().annotations.push({
      type: 'known-gap',
      description:
        'OIS/Basis/CMS swap kinds cannot price (legacy fat wire shape → 422 validation_error); Vanilla+SONIA is the supported GBP OIS-curve path.',
    })
    await gotoReady(page, '/products/ir-swap/new')
    await pickStandaloneCurve(page, 'Discounting Curve (PV)', SEEDED.gbpOisCurveName)
    await fieldFor(page, 'Swap Kind').selectOption('OIS')
    await selectByTextContains(fieldFor(page, 'Overnight Index'), SEEDED.soniaIndexId)
    const exchangePromise = capturePricing(page, '/v1/price/swap/ir')
    await page.getByRole('button', { name: 'Price', exact: true }).click()
    const exchange = await exchangePromise
    expect(exchange.status).toBe(422)
    await expect(page.getByText('Invalid Request').first()).toBeVisible({ timeout: 15_000 })
    // No stale NPV panel is shown alongside the error.
    await expect(page.locator(NPV_PANEL)).toHaveCount(0)
  })
})
