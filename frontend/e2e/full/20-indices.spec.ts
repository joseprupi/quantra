/**
 * Indices: seeded real overnight indices visible; custom IBOR index CRUD.
 * (Using the custom index in a curve and in a swap float leg is covered by
 * 30-curves and 60-swap.)
 */
import { test, expect } from '@playwright/test'
import { gotoReady, fieldFor } from '../lib/ui'
import { SEEDED, uniq } from '../lib/stack'
import { createIborIndex } from '../lib/journeys'

test.describe('indices', () => {
  test('seeded overnight indices (SONIA / SOFR) are listed', async ({ page }) => {
    await gotoReady(page, '/indices')
    await expect(page.getByText(SEEDED.soniaIndexId, { exact: true }).first()).toBeVisible({
      timeout: 15_000,
    })
    await expect(page.getByText(SEEDED.sofrIndexId, { exact: true }).first()).toBeVisible()
  })

  test('create a custom IBOR index -> edit -> delete', async ({ page }) => {
    const id = uniq('E2EIBOR').toUpperCase().replace(/[^A-Z0-9_]/g, '_')
    await createIborIndex(page, {
      id,
      family: 'Euribor',
      tenorNumber: 3,
      tenorUnit: 'Months',
      calendar: 'TARGET',
      dayCounter: 'Actual360',
    })

    // Edit: change the description, save, still listed.
    const rowFor = (needle: string) =>
      page
        .locator('div')
        .filter({ hasText: needle })
        .filter({ has: page.getByTitle('Delete') })
        .last()
    await rowFor(id).getByTitle('Edit').first().click()
    await expect(page.getByText('Edit Index')).toBeVisible()
    await fieldFor(page, 'Description').fill('e2e edited')
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(page.getByText(id, { exact: true }).first()).toBeVisible({ timeout: 15_000 })

    // Delete (native confirm) — the index disappears.
    page.on('dialog', (d) => void d.accept())
    // Find the row containing our id and click its Delete button.
    await rowFor(id).getByTitle('Delete').first().click()
    await expect(page.getByText(id, { exact: true })).toHaveCount(0, { timeout: 15_000 })
  })

  test('custom index persists server-side (fresh browser sees it)', async ({ page, browser }) => {
    const id = uniq('E2EPERSIST').toUpperCase().replace(/[^A-Z0-9_]/g, '_')
    await createIborIndex(page, { id, family: 'USDLibor', tenorNumber: 6, tenorUnit: 'Months' })
    const ctx = await browser.newContext()
    const fresh = await ctx.newPage()
    try {
      await fresh.goto(`${process.env.E2E_PORTAL_URL || 'http://localhost:5173'}/indices`)
      await expect(fresh.getByText(id, { exact: true }).first()).toBeVisible({ timeout: 20_000 })
    } finally {
      await ctx.close()
    }
  })
})
