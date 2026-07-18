import { test, expect, Page } from '@playwright/test'
import { FIREBASE_API_KEY, ORCHESTRATOR_ORIGIN, buildFakeUser } from './helpers'

// Constants


// Seed fixtures

const fakeUser = buildFakeUser()

// Nominal discount curve, role=discount. Spans past the 5Y ZCIIS
// maturity (deposit 1Y + swap 2Y/5Y/10Y) — without the long
// tail the bootstrapped TermStructure raises "past max curve time" at
// the swap's last cashflow.
const discountCurve = {
  id: 'e2e-nominal',
  name: 'E2E Nominal',
  currency: 'EUR',
  role: 'discount',
  day_counter: 'Actual365Fixed',
  interpolator: 'LogLinear',
  bootstrap_trait: 'Discount',
  reference_date: '2025-01-15',
  points: [
    { point_type: 'DepositHelper', point: { tenor: { n: 1, unit: 'Years' }, quote_id: 'EUR.IRS.1Y' } },
    { point_type: 'SwapHelper',    point: { tenor: { n: 2, unit: 'Years' }, quote_id: 'EUR.IRS.2Y' } },
    { point_type: 'SwapHelper',    point: { tenor: { n: 5, unit: 'Years' }, quote_id: 'EUR.IRS.5Y' } },
    { point_type: 'SwapHelper',    point: { tenor: { n: 10, unit: 'Years' }, quote_id: 'EUR.IRS.10Y' } },
  ],
  createdAt: '',
  updatedAt: '',
}

const inflationCurve = {
  id: 'e2e-hicp',
  name: 'E2E HICP',
  currency: 'EUR',
  role: 'inflation',
  day_counter: 'Actual365Fixed',
  interpolator: 'Linear',
  bootstrap_trait: 'ZeroRate',
  reference_date: '2025-01-15',
  inflation_curve: {
    kind: 'ZeroInflation',
    index: {
      id: 'EUHICP',
      family_name: 'EU HICP',
      currency: 'EUR',
      calendar: 'TARGET',
      day_counter: 'Actual365Fixed',
      frequency: 'Monthly',
      availability_lag: { n: 2, unit: 'Months' },
      observation_lag: { n: 3, unit: 'Months' },
      interpolated: true,
      revised: false,
      kind: 'ZeroInflation',
      fixings: [
        { date: '2024-10-01', value: 100.0 },
        { date: '2024-11-01', value: 100.2 },
        { date: '2024-12-01', value: 100.4 },
      ],
    },
    calendar: 'TARGET',
    business_day_convention: 'ModifiedFollowing',
    day_counter: 'Actual365Fixed',
    interpolator: 'Linear',
    allow_extrapolation: true,
    bootstrap_accuracy: 1.0e-12,
    points: [
      {
        point_type: 'ZeroCouponInflationSwapHelper',
        point: {
          tenor: { n: 5, unit: 'Years' },
          quote_id: 'EUR.HICP.5Y',
          swap_observation_lag: { n: 3, unit: 'Months' },
          calendar: 'TARGET',
          payment_convention: 'ModifiedFollowing',
          day_counter: 'Actual365Fixed',
          observation_interpolation: 'AsIndex',
        },
      },
    ],
  },
  points: [],
  createdAt: '',
  updatedAt: '',
}

// Saved inflation-index spec (so the picker has something to pre-fill).
const inflationIndex = {
  id: 'EUHICP',
  family_name: 'EU HICP',
  currency: 'EUR',
  calendar: 'TARGET',
  day_counter: 'Actual365Fixed',
  frequency: 'Monthly',
  availability_lag: { n: 2, unit: 'Months' },
  observation_lag: { n: 3, unit: 'Months' },
  interpolated: true,
  revised: false,
  kind: 'ZeroInflation',
  fixings: [
    { date: '2024-10-01', value: 100.0 },
    { date: '2024-11-01', value: 100.2 },
    { date: '2024-12-01', value: 100.4 },
  ],
}

// Quote book — the save path (buildRequest → resolveCurveArrayQuoteIds /
// buildInflationCurveSpec) resolves the curves' quote_ids client-side against
// this book (the price path resolves server-side; save still resolves locally). Without it
// the save throws "Unknown quote id" before any server persist. "previous"
// mode + a 2025-01-15 series resolves for the 2026-05-30 as-of.
const quoteBook = [
  { id: 'EUR.IRS.1Y', kind: 'Rate', series: [{ date: '2025-01-15', value: 0.025 }] },
  { id: 'EUR.IRS.2Y', kind: 'Rate', series: [{ date: '2025-01-15', value: 0.026 }] },
  { id: 'EUR.IRS.5Y', kind: 'Rate', series: [{ date: '2025-01-15', value: 0.028 }] },
  { id: 'EUR.IRS.10Y', kind: 'Rate', series: [{ date: '2025-01-15', value: 0.030 }] },
  { id: 'EUR.HICP.5Y', kind: 'Rate', series: [{ date: '2025-01-15', value: 0.021 }] },
]

// Helpers

async function seedAll(page: Page) {
  await page.addInitScript(
    ({
      user,
      apiKey,
      curves,
      indices,
      quotes,
    }: {
      user: unknown
      apiKey: string
      curves: unknown[]
      indices: unknown[]
      quotes: unknown[]
    }) => {
      localStorage.setItem('quantra_curves', JSON.stringify(curves))
      localStorage.setItem('quantra_inflation_indices', JSON.stringify(indices))
      localStorage.setItem('quantra_quote_book', JSON.stringify(quotes))

      const req = indexedDB.open('firebaseLocalStorageDb', 1)
      req.onupgradeneeded = (ev: IDBVersionChangeEvent) => {
        const db = (ev.target as IDBOpenDBRequest).result
        if (!db.objectStoreNames.contains('firebaseLocalStorage')) {
          db.createObjectStore('firebaseLocalStorage', { keyPath: 'fbase_key' })
        }
      }
      req.onsuccess = (ev: Event) => {
        const db = (ev.target as IDBOpenDBRequest).result
        const tx = db.transaction('firebaseLocalStorage', 'readwrite')
        tx.objectStore('firebaseLocalStorage').put({
          fbase_key: `firebase:authUser:${apiKey}:[DEFAULT]`,
          value: user,
        })
      }
    },
    {
      user: fakeUser,
      apiKey: FIREBASE_API_KEY,
      curves: [discountCurve, inflationCurve],
      indices: [inflationIndex],
      quotes: quoteBook,
    },
  )
}

// Tests

test.describe('Inflation Swap — D104 orchestrator integration (O20 retired)', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/firestore.googleapis.com/**', r => r.abort())
    await page.route('**/identitytoolkit.googleapis.com/**', r => r.abort())
    await page.route('**/securetoken.googleapis.com/**', r => r.abort())
    // Hard-block the legacy backend so any accidental fallback to
    // ``api.quantra.io`` is visible (no fallback).
    await page.route('**/api.quantra.io/**', r => r.abort())
    await page.route('**/market.quantra.io/**', route =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) }),
    )
    await seedAll(page)
  })

  // Test A: happy path — orchestrator 200 → Pricing Results panel visible.
  // Also asserts the wire body matches the inline contract: flat ``swap`` body
  // (swap_kind + swaps[].zero_coupon_inflation_swap.* at the top of
  // ``swap``), top-level role-tagged ``curves`` (nominal + inflation),
  // top-level ``inflation_index`` with body, ``as_of`` at envelope root;
  // no ``pricing.*`` nesting.
  test('A: orchestrator 200 → Pricing Results visible (Thin-A body asserted)', async ({ page }) => {
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/swaps/inflation`, route => {
      const body = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>
      expect(body.as_of).toBeTruthy()
      expect(body.pricing).toBeUndefined()
      const swap = body.swap as Record<string, unknown>
      expect(swap).toBeDefined()
      expect(swap.swap_kind).toEqual(expect.any(String))
      expect(Array.isArray(swap.swaps)).toBe(true)
      const curves = body.curves as Array<Record<string, unknown>>
      expect(curves.length).toBeGreaterThanOrEqual(2)
      expect((curves[0].body as Record<string, unknown>).role).toBe('nominal')
      expect((curves[1].body as Record<string, unknown>).role).toBe('inflation')
      // Guard: nominal curve spans past 5Y maturity.
      const maxNominalTenor = (curves[0].points as Array<Record<string, unknown>>).reduce((max, wrap) => {
        const t = (wrap.point as Record<string, unknown>).tenor as { n: number; unit: string } | undefined
        if (!t || t.unit !== 'Years') return max
        return Math.max(max, t.n)
      }, 0)
      expect(maxNominalTenor).toBeGreaterThanOrEqual(5)
      const idx = body.inflation_index as Record<string, unknown>
      expect(idx).toBeDefined()
      expect(idx.index_id).toEqual(expect.any(String))
      expect(idx.body).toBeDefined()
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          pricing_history_id: null,
          assembled_request: {},
          result: {
            npv: -39671.61,
            fair_rate: 0.0300,
            fair_spread: null,
            fixed_leg_bps: 12.5,
            fixed_leg_npv: -1234.5,
            inflation_leg_npv: 1234.5,
            yoy_leg_bps: null,
            yoy_leg_npv: null,
            swap_kind: 'zero_coupon',
            extras: {},
          },
        }),
      })
    })

    await page.goto('/products/inflation-swaps/new')

    // Select discount (nominal) curve in the Discounting Curve picker
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-nominal"]') })
      .selectOption('e2e-nominal')

    // Select inflation curve in the Inflation Curve picker
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-hicp"]') })
      .selectOption('e2e-hicp')

    const priceBtn = page.getByRole('button', { name: /Price.*Zero Coupon.*Swap/ })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    await expect(page.getByText('Pricing Results')).toBeVisible({ timeout: 15_000 })
  })

  // Test B: coded error — swap_inflation_index_not_found 404 → error card; no Pricing Results panel
  test('B: swap_inflation_index_not_found 404 → Not Found error card; Pricing Results absent', async ({ page }) => {
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/swaps/inflation`, route =>
      route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'Inflation index not found.',
          code: 'swap_inflation_index_not_found',
          request_id: 'req-e2e-inflation-b',
        }),
      }),
    )

    await page.goto('/products/inflation-swaps/new')

    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-nominal"]') })
      .selectOption('e2e-nominal')
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-hicp"]') })
      .selectOption('e2e-hicp')

    const priceBtn = page.getByRole('button', { name: /Price.*Zero Coupon.*Swap/ })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    // Branch on code, not prose (invariant 9)
    await expect(page.getByText('Not Found', { exact: true })).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText('Pricing Results')).not.toBeVisible()
  })

  // Test C: Save persists the leaf→root graph
  // (curves → index → swaps_inflation) and stamps the appGraph, then Price
  // flips to the by-reference arm ({ swap_id, as_of }, no inline
  // payloads). Inflation has NO curve_set — a curve-sets POST fails loudly.
  test('C: save persists curves→index→swaps_inflation leaf→root, then prices by-reference', async ({
    page,
  }) => {
    const SWAP_UUID = 'cccccccc-1111-2222-3333-777777777777'
    const crudOrder: string[] = []
    let curveSeq = 0

    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/curves`, route => {
      crudOrder.push('curves')
      const body = JSON.parse(route.request().postData() ?? '{}') as { name?: string; body?: { role?: string } }
      const seq = ++curveSeq
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          id: `curve-uuid-${seq}`,
          name: body.name ?? `curve-${seq}`,
          points: [],
          body: body.body ?? {},
          created_at: '2026-05-30T00:00:00Z',
          updated_at: '2026-05-30T00:00:00Z',
        }),
      })
    })
    // Inflation reads pricing.curves directly — NO curve_set. Fail loudly if one is POSTed.
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/curve-sets`, route => {
      crudOrder.push('curve-sets')
      return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'unexpected curve_set', code: 'unexpected' }) })
    })
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/indices`, route => {
      crudOrder.push('indices')
      const body = JSON.parse(route.request().postData() ?? '{}') as { kind?: string; body?: Record<string, unknown> }
      expect(body.kind).toBe('Inflation')
      // The engine string id rides inside the app.indices body so the by-ref
      // pricing.inflation_index_id scalar resolves back to it.
      expect((body.body as Record<string, unknown>)?.id).toBe('EUHICP')
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'index-uuid-e2e',
          name: 'EU HICP',
          kind: 'Inflation',
          body: body.body ?? {},
          created_at: '2026-05-30T00:00:00Z',
          updated_at: '2026-05-30T00:00:00Z',
        }),
      })
    })
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/swaps/inflation`, route => {
      crudOrder.push('swaps/inflation')
      // The saved trade pins the refs the swaps_inflation assembler reads: a
      // role-tagged pricing.curves list (nominal first; inflation second),
      // pricing.inflation_index_id, and the top-level swap_kind. No
      // curve_set_id / discount_curve_id.
      const body = JSON.parse(route.request().postData() ?? '{}') as {
        request?: {
          swap_kind?: string
          pricing?: {
            curves?: Array<{ curve_id?: string; role?: string }>
            inflation_index_id?: string
            curve_set_id?: string
            discount_curve_id?: string
          }
        }
      }
      const pricing = body.request?.pricing
      expect(body.request?.swap_kind).toBe('zero_coupon')
      expect(pricing?.curves?.[0]?.curve_id).toBe('curve-uuid-1')
      expect(pricing?.curves?.[0]?.role).toBe('nominal')
      expect(pricing?.curves?.[1]?.curve_id).toBe('curve-uuid-2')
      expect(pricing?.curves?.[1]?.role).toBe('inflation')
      expect(pricing?.inflation_index_id).toBe('index-uuid-e2e')
      expect(pricing?.curve_set_id).toBeUndefined()
      expect(pricing?.discount_curve_id).toBeUndefined()
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          id: SWAP_UUID,
          name: 'inflation-swap',
          request: body.request ?? {},
          created_at: '2026-05-30T00:00:00Z',
          updated_at: '2026-05-30T00:00:00Z',
        }),
      })
    })

    let pricedBody: Record<string, unknown> | null = null
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/swaps/inflation`, route => {
      pricedBody = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          pricing_history_id: null,
          assembled_request: {},
          result: {
            npv: -39907.0,
            fair_rate: 0.0300,
            fair_spread: null,
            fixed_leg_bps: 12.5,
            fixed_leg_npv: -1234.5,
            inflation_leg_npv: 1234.5,
            yoy_leg_bps: null,
            yoy_leg_npv: null,
            swap_kind: 'zero_coupon',
            extras: {},
          },
        }),
      })
    })

    await page.goto('/products/inflation-swaps/new')

    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-nominal"]') })
      .selectOption('e2e-nominal')
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-hicp"]') })
      .selectOption('e2e-hicp')

    // Save — drives the leaf→root persist.
    const saveBtn = page.getByRole('button', { name: 'Save', exact: true })
    await expect(saveBtn).toBeEnabled({ timeout: 10_000 })
    await saveBtn.click()

    // Success banner mentions the server-side reference (appGraph stamped).
    await expect(page.getByText(/Referenced server-side/)).toBeVisible({ timeout: 15_000 })
    // Leaf→root order: curves (nominal) → curves (inflation) → index → swaps_inflation.
    expect(crudOrder).toEqual(['curves', 'curves', 'indices', 'swaps/inflation'])

    // Now price — the saved swap_id flips the call to the by-reference by-ref arm.
    const priceBtn = page.getByRole('button', { name: /Price.*Zero Coupon.*Swap/ })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    await expect(page.getByText('Pricing Results')).toBeVisible({ timeout: 15_000 })

    // By-reference body: swap_id pinned to the saved UUID; NO inline payloads.
    expect(pricedBody).not.toBeNull()
    const body = pricedBody as unknown as Record<string, unknown>
    expect(body.swap_id).toBe(SWAP_UUID)
    expect(body.as_of).toBeTruthy()
    expect(body.swap).toBeUndefined()
    expect(body.curves).toBeUndefined()
    expect(body.inflation_index).toBeUndefined()
  })
})
