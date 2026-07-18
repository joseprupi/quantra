/**
 * Audit-trail journeys — the History panel over the entity versioning API.
 *
 * Create a GBP swap -> save -> amend the notional with a reason -> the
 * History tab lists create+amend with actor and reason -> the diff view shows
 * the notional old->new -> restore v1 (issues the entity PATCH with
 * X-Change-Reason "restored to v1") -> the timeline gains a new version ->
 * a by-reference price stamps trade_version onto the pricing-history row.
 */
import { test, expect, Page } from '@playwright/test'
import { fieldFor, gotoReady, selectByTextContains, pickStandaloneCurve } from '../lib/ui'
import { apiGet, capturePricing, waitForRealData } from '../lib/api'
import { SEEDED, uniq } from '../lib/stack'

test.beforeEach(async ({ request }) => {
  await waitForRealData(request)
})

const NOTIONAL_BEFORE = '5000000'
const NOTIONAL_AFTER = '6000000'

async function setupGbpVanillaSonia(page: Page, ratePct: string) {
  await gotoReady(page, '/products/ir-swap/new')
  await pickStandaloneCurve(page, 'Discounting Curve (PV)', SEEDED.gbpOisCurveName)
  await selectByTextContains(fieldFor(page, 'Index'), SEEDED.soniaIndexId)
  await fieldFor(page, 'Rate (%)').fill(ratePct)
}

test.describe('audit history — create, amend with reason, diff, restore, priced version', () => {
  test('full audit journey on an IR swap', async ({ page, request }) => {
    test.setTimeout(180_000)
    const swapName = uniq('audit-swap')

    // ---- create + first save (version 1: create) -------------------------
    await setupGbpVanillaSonia(page, '2')
    await fieldFor(page, 'Notional').fill(NOTIONAL_BEFORE)
    await page.getByLabel('Product name').fill(swapName)

    const createRespPromise = page.waitForResponse(
      (r) => /\/v1\/swaps\/ir$/.test(new URL(r.url()).pathname) && r.request().method() === 'POST',
      { timeout: 30_000 },
    )
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    const createResp = await createRespPromise
    expect(createResp.status(), 'swap create persists').toBe(201)
    const swapId = ((await createResp.json()) as { id: string }).id
    expect(swapId).toBeTruthy()
    await expect(page.getByText(/Saved ".*Referenced server-side/)).toBeVisible({
      timeout: 20_000,
    })

    // ---- amend the notional WITH a reason (version 2: amend) -------------
    await fieldFor(page, 'Notional').fill(NOTIONAL_AFTER)
    await page.getByLabel('Reason for change').fill('notional corrected')
    const patchPromise = page.waitForRequest(
      (r) => r.url().includes(`/v1/swaps/ir/${swapId}`) && r.method() === 'PATCH',
      { timeout: 30_000 },
    )
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    const patchReq = await patchPromise
    expect(
      patchReq.headers()['x-change-reason'],
      'the reason rides the save as X-Change-Reason',
    ).toBe('notional corrected')
    await expect(page.getByText(/Saved ".*Updated server copy/)).toBeVisible({ timeout: 20_000 })
    // A successful save clears the reason input.
    await expect(page.getByLabel('Reason for change')).toHaveValue('')

    // ---- History tab: create + amend, actor + reason ---------------------
    await page.getByRole('button', { name: /History/ }).click()
    const rows = page.getByTestId('history-row')
    await expect(rows).toHaveCount(2, { timeout: 20_000 })

    const amendRow = page.locator('[data-testid="history-row"][data-version="2"]')
    await expect(amendRow.getByTestId('history-chip')).toHaveText('amend')
    await expect(amendRow.getByTestId('history-reason')).toContainText('notional corrected')
    await expect(amendRow.getByTestId('history-actor')).toContainText('dev')

    const createRow = page.locator('[data-testid="history-row"][data-version="1"]')
    await expect(createRow.getByTestId('history-chip')).toHaveText('create')
    await expect(createRow.getByTestId('history-actor')).toContainText('dev')

    // Cross-check the timeline against the versions API itself.
    const versions = (await apiGet(request, `/v1/swaps/ir/${swapId}/versions`)) as {
      items: Array<{ version_no: number; change_type: string; change_reason: string | null }>
    }
    expect(versions.items.map((v) => [v.version_no, v.change_type])).toEqual([
      [2, 'amend'],
      [1, 'create'],
    ])
    expect(versions.items[0].change_reason).toBe('notional corrected')

    // ---- diff view: notional old -> new ----------------------------------
    await createRow.click()
    await amendRow.click()
    const diff = page.getByTestId('history-diff')
    await expect(diff).toBeVisible()
    const notionalRow = diff.locator(
      '[data-testid="history-diff-row"][data-path="request.notional"]',
    )
    await expect(notionalRow).toBeVisible({ timeout: 20_000 })
    await expect(notionalRow).toHaveAttribute('data-kind', 'changed')
    await expect(notionalRow).toContainText(NOTIONAL_BEFORE)
    await expect(notionalRow).toContainText(NOTIONAL_AFTER)

    // ---- restore v1 (a NEW version appears) ------------------------------
    await amendRow.click() // deselect v2 -> v1 alone -> snapshot view
    await expect(page.getByTestId('history-snapshot')).toBeVisible({ timeout: 20_000 })
    await page.getByTestId('history-restore').click()
    await expect(page.getByTestId('history-notice')).toContainText('Restored to v1', {
      timeout: 20_000,
    })
    await expect(rows).toHaveCount(3, { timeout: 20_000 })
    const restoredRow = page.locator('[data-testid="history-row"][data-version="3"]')
    await expect(restoredRow.getByTestId('history-reason')).toContainText('restored to v1')

    // The server row really is back to the v1 notional.
    const head = (await apiGet(request, `/v1/swaps/ir/${swapId}`)) as {
      request: { notional: number }
    }
    expect(head.request.notional).toBe(Number(NOTIONAL_BEFORE))

    // ---- by-reference price carries trade_version in pricing history -----
    // Pin the pricing As-Of to the latest REAL ingested date: the page's
    // global As-Of rolls forward asynchronously and can transiently sit on
    // the pre-ingest fallback under parallel load, which would 422 quote
    // resolution and flake this journey.
    const latest = (await apiGet(request, '/v1/market-data/latest-date')) as {
      latest_date: string
    }
    await fieldFor(page, 'As Of Date').fill(latest.latest_date)
    const exchangePromise = capturePricing(page, '/v1/price/swap/ir')
    await page.getByRole('button', { name: 'Price', exact: true }).click()
    const exchange = await exchangePromise
    expect(exchange.status, 'saved swap prices by-reference').toBe(200)
    expect(exchange.requestBody.swap_id, 'by-reference body').toBe(swapId)

    const history = (await apiGet(request, '/v1/pricing-history?limit=25')) as {
      items: Array<{
        product_id: string | null
        trade_entity_type: string | null
        trade_entity_id: string | null
        trade_version: number | null
      }>
    }
    const entry = history.items.find((h) => h.trade_entity_id === swapId)
    expect(entry, 'pricing-history row for the priced swap exists').toBeTruthy()
    expect(entry!.trade_entity_type).toBe('swaps_ir')
    expect(entry!.trade_version, 'the priced entity version is stamped').toBe(3)
  })
})
