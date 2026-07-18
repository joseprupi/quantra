/**
 * Market data: Quote Book (real series + provider chips + series CRUD +
 * inline manual value), CSV import, Time Series Lab chart, real-data As-Of
 * default.
 */
import { test, expect } from '@playwright/test'
import { gotoReady } from '../lib/ui'
import { waitForRealData } from '../lib/api'
import { SEEDED, uniqCanonicalId } from '../lib/stack'

test.beforeEach(async ({ request }) => {
  await waitForRealData(request)
})

test.describe('quote book', () => {
  test('lists the real BoE OIS + UST series with provider source chips', async ({ page }) => {
    await gotoReady(page, '/quote-book')
    // Real BoE OIS series present
    await expect(page.getByText(`${SEEDED.boeQuotePrefix}10Y.PAR`).first()).toBeVisible({
      timeout: 20_000,
    })
    await expect(page.getByText(`${SEEDED.ustQuotePrefix}10Y.YIELD`).first()).toBeVisible()
    // Provider chips carry the connector source names
    await expect(page.getByText('Bank of England').first()).toBeVisible()
    await expect(page.getByText('US Treasury').first()).toBeVisible()
  })

  test('series CRUD: create -> add manual value -> history shows it -> edit -> delete', async ({
    page,
  }) => {
    const id = uniqCanonicalId('5Y')
    await gotoReady(page, '/quote-book')

    // CREATE
    await page.getByRole('button', { name: '+ New series' }).click()
    const dialog = page.getByRole('dialog', { name: 'New series' })
    await dialog.getByLabel('canonical_id').fill(id)
    await dialog.getByLabel('asset_class').fill('RATES')
    await dialog.getByLabel('currency').fill('USD')
    await dialog.getByRole('button', { name: 'Create series' }).click()
    await expect(dialog).toHaveCount(0)
    await expect(page.getByText(id, { exact: true })).toBeVisible({ timeout: 15_000 })

    // ADD VALUE inline (row expand -> + Add value)
    await page.getByText(id, { exact: true }).click()
    await page.getByLabel('add value date').fill('2026-01-02')
    await page.getByLabel('add value amount').fill('0.0456')
    await page.getByRole('button', { name: '+ Add value' }).click()
    await expect(page.getByText('4.5600%').first()).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText('Imported', { exact: true }).first()).toBeVisible()

    // EDIT (description)
    await page.getByLabel(`edit ${id}`).click()
    const editDialog = page.getByRole('dialog', { name: `Edit series` })
    await editDialog.getByLabel('description').fill('e2e edited description')
    await editDialog.getByRole('button', { name: 'Save changes' }).click()
    await expect(editDialog).toHaveCount(0)

    // DELETE (with values warning)
    await page.getByLabel(`delete ${id}`).click()
    const delDialog = page.getByRole('dialog', { name: 'Delete series' })
    await expect(delDialog.getByText(/permanently deletes/)).toBeVisible()
    await delDialog.getByLabel('confirm delete').click()
    await expect(delDialog).toHaveCount(0)
    await expect(page.getByText(id, { exact: true })).toHaveCount(0, { timeout: 15_000 })
  })
})

test.describe('market data import', () => {
  test('CSV import: unknown series rejected per-row, known series lands', async ({ page }) => {
    const id = uniqCanonicalId('1Y')

    // Create the target series first (import rejects unknown canonical ids).
    await gotoReady(page, '/quote-book')
    await page.getByRole('button', { name: '+ New series' }).click()
    const dialog = page.getByRole('dialog', { name: 'New series' })
    await dialog.getByLabel('canonical_id').fill(id)
    await dialog.getByLabel('asset_class').fill('RATES')
    await dialog.getByLabel('currency').fill('USD')
    await dialog.getByRole('button', { name: 'Create series' }).click()
    await expect(dialog).toHaveCount(0)

    await gotoReady(page, '/market-data/import')
    await page.getByRole('button', { name: 'CSV upload' }).click()
    const csv = [
      'canonical_id,as_of,value',
      `${id},2026-01-05,0.0333`,
      `${id},2026-01-06,0.0334`,
      `USD.ZZQQWW.9Y,2026-01-05,0.9`,
    ].join('\n')
    await page.getByLabel('CSV file').setInputFiles({
      name: 'e2e-import.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from(csv, 'utf-8'),
    })
    await page.getByRole('button', { name: 'Upload CSV' }).click()

    // Partial import: 2 in, 1 rejected with a per-row reason.
    await expect(page.getByText(/Partial import|Import complete/)).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText('Imported', { exact: false }).first()).toBeVisible()
    await expect(page.getByText(/does not exist|unknown series/i).first()).toBeVisible()

    // The imported value is visible in the Quote Book with a csv source chip.
    await gotoReady(page, '/quote-book')
    await page.getByText(id, { exact: true }).click()
    await expect(page.getByText('3.3400%').first()).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText('Imported', { exact: true }).first()).toBeVisible()

    // Clean up the series.
    await page.getByLabel(`delete ${id}`).click()
    await page.getByRole('dialog', { name: 'Delete series' }).getByLabel('confirm delete').click()
  })

  test('manual entry: add a value to a REAL series via the import screen', async ({ page }) => {
    const seriesId = `${SEEDED.boeQuotePrefix}10Y.PAR`
    await gotoReady(page, '/market-data/import')
    await page.getByLabel('canonical_id row 1').selectOption(seriesId)
    await page.getByLabel('as_of row 1').fill('2020-01-02')
    await page.getByLabel('value row 1').fill('0.0123')
    await page.getByRole('button', { name: /Import|Submit/ }).last().click()
    await expect(page.getByText(/Import complete/)).toBeVisible({ timeout: 30_000 })
  })
})

test.describe('time series lab', () => {
  test('renders a chart for a real UST yield series', async ({ page }) => {
    await gotoReady(page, '/market-data/timeseries')
    await page.getByPlaceholder('Search series...').fill('USD.RATES.UST.OFFICIAL.10Y')
    const row = page.getByText(`${SEEDED.ustQuotePrefix}10Y.YIELD`).first()
    await expect(row).toBeVisible({ timeout: 20_000 })
    await row.click()
    // An ECharts canvas appears with the series plotted.
    await expect(page.locator('canvas').first()).toBeVisible({ timeout: 20_000 })
  })
})

test.describe('real-data As-Of default', () => {
  test('pricing form As-Of defaults to the latest real BoE date', async ({ page, request }) => {
    const res = await request.get(
      `${process.env.E2E_PORTAL_URL || 'http://localhost:5173'}/v1/market-data/latest-date?prefix=${SEEDED.boeQuotePrefix}`,
    )
    const { latest_date } = (await res.json()) as { latest_date: string | null }
    expect(latest_date, 'real BoE data ingested').toBeTruthy()
    await gotoReady(page, '/products/ir-swap/new')
    const asOf = page.locator(
      'xpath=//label[normalize-space(.)="As Of Date"]/following::input[1]',
    )
    await expect(asOf).toHaveValue(latest_date as string, { timeout: 20_000 })
  })
})
