/**
 * App chrome: navigation crawl, honesty banner, About panel, Feedback link,
 * Settings backup export. Read-only journeys (safe to run in parallel with
 * everything else).
 */
import { test, expect } from '@playwright/test'
import { gotoReady } from '../lib/ui'
import { waitForRealData } from '../lib/api'

const TOP_LEVEL_PAGES: Array<{ path: string; marker: string | RegExp }> = [
  { path: '/', marker: /Quantra|Products|Pricing/i },
  { path: '/quote-book', marker: /Quote Book/i },
  { path: '/indices', marker: /Indices/i },
  { path: '/market-data/timeseries', marker: /Time Series/i },
  { path: '/market-data/import', marker: /Import/i },
  { path: '/calendar', marker: /Calendar/i },
  { path: '/yield-curves', marker: /Yield Curves/i },
  { path: '/inflation-curves', marker: /Inflation Curves/i },
  { path: '/credit-curves', marker: /Credit Curves/i },
  { path: '/curve-sets', marker: /Curve Sets/i },
  { path: '/vol-workbench', marker: /Vol|Surface/i },
  { path: '/models/swaption', marker: /Model/i },
  { path: '/products/fixed-rate-bond', marker: /Fixed Rate Bond/i },
  { path: '/products/floating-rate-bond', marker: /Floating Rate Bond/i },
  { path: '/products/ir-swap', marker: /Swap/i },
  { path: '/products/inflation-swaps', marker: /Inflation Swap/i },
  { path: '/products/swaption', marker: /Swaption/i },
  { path: '/products/cds', marker: /CDS/i },
  { path: '/products/equity-options', marker: /Equity Option/i },
  { path: '/settings', marker: /Settings/i },
]

test.describe('chrome & navigation', () => {
  test('every top-level page renders (no blank/error screen)', async ({ page }) => {
    const consoleErrors: string[] = []
    page.on('pageerror', (e) => consoleErrors.push(String(e)))
    for (const { path, marker } of TOP_LEVEL_PAGES) {
      await gotoReady(page, path)
      await expect(
        page.locator('main, [class*="max-w"]').first(),
        `${path} should render content`,
      ).toBeVisible()
      await expect(page.getByText(marker).first(), `${path} marker`).toBeVisible({
        timeout: 15_000,
      })
    }
    expect(consoleErrors, `uncaught page errors during crawl:\n${consoleErrors.join('\n')}`).toEqual([])
  })

  test('honesty banner shows the live public-data copy once real data ingested', async ({
    page,
    request,
  }) => {
    await waitForRealData(request)
    await gotoReady(page, '/')
    const banner = page.getByTestId('self-hosted-banner')
    await expect(banner).toBeVisible()
    await expect(banner).toContainText('Live public market data', { timeout: 15_000 })
    await expect(banner).toContainText('Bank of England')
  })

  test('About panel shows web client / backend / engine versions', async ({ page }) => {
    await gotoReady(page, '/')
    await page.getByRole('button', { name: 'About Quantra' }).click()
    const panel = page.getByRole('dialog').or(page.locator('[aria-label="About Quantra"]')).last()
    await expect(panel.getByText(/Web client/i)).toBeVisible()
    await expect(panel.getByText(/Backend/i).first()).toBeVisible()
    await expect(panel.getByText(/Pricing engine/i)).toBeVisible()
    // Versions resolve (GET /v1/version) — "Unavailable" would be a defect.
    await expect(panel.getByText('Unavailable')).toHaveCount(0, { timeout: 15_000 })
  })

  test('Feedback link points at the proposal form', async ({ page }) => {
    await gotoReady(page, '/')
    const link = page.getByRole('link', { name: 'Feedback' })
    await expect(link).toBeVisible()
    await expect(link).toHaveAttribute('href', /quantra\.io\/propose/)
  })

  test('Settings: backup export downloads a JSON with the expected shape', async ({ page }) => {
    await gotoReady(page, '/settings')
    const downloadPromise = page.waitForEvent('download', { timeout: 30_000 })
    await page.getByRole('button', { name: 'Export', exact: true }).click()
    const download = await downloadPromise
    expect(download.suggestedFilename()).toMatch(/\.json$/)
    const stream = await download.createReadStream()
    const chunks: Buffer[] = []
    for await (const chunk of stream) chunks.push(chunk as Buffer)
    const parsed = JSON.parse(Buffer.concat(chunks).toString('utf-8')) as Record<string, unknown>
    expect(Array.isArray(parsed.curves), 'backup carries curves[]').toBe(true)
    expect(Array.isArray(parsed.indices), 'backup carries indices[]').toBe(true)
  })
})
