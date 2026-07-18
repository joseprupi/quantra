/**
 * Calendar tools: month view (business/holiday classification via the
 * backend calendar routes) + range load. There is no "advance" tool page in
 * the UI — the backend /v1/calendar/advance route has no portal surface
 * (recorded as a coverage note, not a defect).
 */
import { test, expect } from '@playwright/test'
import { fieldFor, gotoReady } from '../lib/ui'

test.describe('calendar', () => {
  test('month view loads business/holiday counts for a real calendar', async ({ page }) => {
    const respPromise = page.waitForResponse(
      (r) => r.url().includes('/v1/calendar/') && r.status() === 200,
      { timeout: 30_000 },
    )
    await gotoReady(page, '/calendar')
    await respPromise
    // The month summary shows non-zero business days.
    await expect(page.getByText(/Business \d+ · Holidays \d+/).first()).toBeVisible({ timeout: 20_000 })
    const summary = (await page.getByText(/Business \d+ · Holidays \d+/).first().textContent()) ?? ''
    const business = parseInt(summary.match(/Business (\d+)/)?.[1] ?? '0', 10)
    expect(business, 'a real month has business days').toBeGreaterThan(15)

    // Switching calendar re-queries and still renders.
    await fieldFor(page, 'Calendar').selectOption('UnitedKingdom')
    await expect(page.getByText(/Business \d+ · Holidays \d+/).first()).toBeVisible({ timeout: 20_000 })
  })

  test('range tools: load business days + holidays for a fixed range', async ({ page }) => {
    await gotoReady(page, '/calendar')
    await fieldFor(page, 'Start date').fill('2026-01-01')
    await fieldFor(page, 'End date').fill('2026-03-31')
    await page.getByRole('button', { name: 'Load Range', exact: true }).click()
    // Q1-2026 has 90 days; TARGET-ish calendars ~62-64 business days.
    await expect(page.getByText(/Days 90 · Business \d+ · Holidays \d+/)).toBeVisible({
      timeout: 30_000,
    })
    const text = (await page.getByText(/Days 90 · Business/).first().textContent()) ?? ''
    const business = parseInt(text.match(/Business (\d+)/)?.[1] ?? '0', 10)
    expect(business).toBeGreaterThan(55)
    expect(business).toBeLessThan(70)
  })
})
