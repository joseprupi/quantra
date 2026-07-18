import { test, expect, Page } from '@playwright/test'
import { FIREBASE_API_KEY, ORCHESTRATOR_ORIGIN, buildFakeUser } from './helpers'

// Constants


// Seed fixtures

const fakeUser = buildFakeUser()

const discountCurve = {
  id: 'e2e-discount',
  name: 'E2E Discount',
  currency: 'EUR',
  role: 'discount',
  day_counter: 'Actual365Fixed',
  interpolator: 'LogLinear',
  bootstrap_trait: 'Discount',
  reference_date: '2026-01-15',
  points: [],
  createdAt: '',
  updatedAt: '',
}

// Required for FloatingRateBond: resolveIndexDefs looks up EURIBOR_6M by default indexRef
const iborIndex = {
  id: 'EURIBOR_6M',
  type: 'IBOR',
  family: 'Euribor',
  tenor_number: 6,
  tenor_time_unit: 'Months',
  fixing_days: 2,
  calendar: 'TARGET',
  day_counter: 'Actual360',
  business_day_convention: 'ModifiedFollowing',
}

// Helpers

async function seedAll(page: Page) {
  await page.addInitScript(
    ({
      user,
      apiKey,
      curve,
      index,
    }: {
      user: unknown
      apiKey: string
      curve: unknown
      index: unknown
    }) => {
      localStorage.setItem('quantra_curves', JSON.stringify([curve]))
      localStorage.setItem('quantra_indices', JSON.stringify([index]))

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
    { user: fakeUser, apiKey: FIREBASE_API_KEY, curve: discountCurve, index: iborIndex },
  )
}

// Fixed Rate Bond tests

test.describe('Fixed Rate Bond — D104 orchestrator integration (D103 hermetic)', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/firestore.googleapis.com/**', r => r.abort())
    await page.route('**/identitytoolkit.googleapis.com/**', r => r.abort())
    await page.route('**/securetoken.googleapis.com/**', r => r.abort())
    await seedAll(page)
  })

  // Test A: happy path — orchestrator 200 → Pricing Results panel shown.
  // Also asserts the wire body matches the inline contract: flat ``bond`` trade body
  // (canonical ``coupon_rate``), top-level role-tagged ``curves``, top-level
  // ``as_of``, no ``pricing.*`` nesting, no ``bonds[*].fixed_rate_bond``
  // wrapper.
  test('A: orchestrator 200 → Pricing Results panel visible (Thin-A body asserted)', async ({ page }) => {
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/bonds/fixed`, route => {
      const body = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>
      expect(body.as_of).toBeTruthy()
      expect(Array.isArray(body.curves)).toBe(true)
      expect(body.pricing).toBeUndefined()
      expect((body as { bonds?: unknown }).bonds).toBeUndefined()
      const bondTrade = body.bond as Record<string, unknown>
      expect(bondTrade).toBeDefined()
      expect(bondTrade.coupon_rate).toEqual(expect.any(Number))  // canonical key
      expect(bondTrade.face_amount).toEqual(expect.any(Number))
      expect(bondTrade.effective_date).toEqual(expect.any(String))
      expect(bondTrade.termination_date).toEqual(expect.any(String))
      expect((bondTrade as { fixed_rate_bond?: unknown }).fixed_rate_bond).toBeUndefined()
      expect((bondTrade as { pricing?: unknown }).pricing).toBeUndefined()
      const firstCurve = (body.curves as Array<Record<string, unknown>>)[0]
      expect((firstCurve.body as Record<string, unknown>).role).toBe('discount')
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          pricing_history_id: null,
          assembled_request: {},
          result: { npv: 100.0, clean_price: 99.5, dirty_price: 99.7, accrued: 0.2, extras: {} },
        }),
      })
    })

    await page.goto('/products/fixed-rate-bond/new')

    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-discount"]') })
      .selectOption('e2e-discount')

    const priceBtn = page.getByRole('button', { name: 'Price Bond' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    await expect(page.getByText('Pricing Results')).toBeVisible({ timeout: 15_000 })
  })

  // Test B: coded error — bond_fixed_not_found 404 → error card; no Pricing Results panel
  test('B: bond_fixed_not_found 404 → Not Found error card; Pricing Results absent', async ({ page }) => {
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/bonds/fixed`, route =>
      route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'Fixed rate bond not found.',
          code: 'bond_fixed_not_found',
          request_id: 'req-e2e-bond-fixed-b',
        }),
      }),
    )

    await page.goto('/products/fixed-rate-bond/new')

    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-discount"]') })
      .selectOption('e2e-discount')

    const priceBtn = page.getByRole('button', { name: 'Price Bond' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    // Branch on code, not prose (invariant 9) — PricingErrorCard renders title from category
    await expect(page.getByText('Not Found', { exact: true })).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText('Pricing Results')).not.toBeVisible()
  })

  // Test C: save → reference → price vertical for FIXED bonds.
  //  1. build, 2. Save → assert leaf→root CRUD order
  //     ['curves', 'curve-sets', 'bonds/fixed'] + the saved bond pins
  //     curve_set_id + discount_curve_id (the verified backend read-path) into
  //     request.pricing, 3. Price → by-reference ({bond_id}, no inline),
  //     4. Pricing Results renders.
  test('C: save persists curves→curve-set→bonds/fixed leaf→root, then prices by-reference', async ({ page }) => {
    const BOND_UUID = 'f1111111-2222-3333-4444-555555555555'
    const crudOrder: string[] = []

    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/curves`, route => {
      crudOrder.push('curves')
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ id: 'curve-uuid-e2e', name: 'discount', points: [], body: {}, created_at: '2026-01-15T00:00:00Z', updated_at: '2026-01-15T00:00:00Z' }),
      })
    })
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/curve-sets`, route => {
      crudOrder.push('curve-sets')
      const body = JSON.parse(route.request().postData() ?? '{}') as { body?: { curve_refs?: Array<{ curve_id?: string }> } }
      expect(body.body?.curve_refs?.[0]?.curve_id).toBe('curve-uuid-e2e')
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ id: 'curveset-uuid-e2e', name: 'cs', body: {}, created_at: '2026-01-15T00:00:00Z', updated_at: '2026-01-15T00:00:00Z' }),
      })
    })
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/bonds/fixed`, route => {
      crudOrder.push('bonds/fixed')
      const body = JSON.parse(route.request().postData() ?? '{}') as { request?: { pricing?: { curve_set_id?: string; discount_curve_id?: string } } }
      expect(body.request?.pricing?.curve_set_id).toBe('curveset-uuid-e2e')
      expect(body.request?.pricing?.discount_curve_id).toBe('curve-uuid-e2e')
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ id: BOND_UUID, name: 'bond', request: body.request ?? {}, created_at: '2026-01-15T00:00:00Z', updated_at: '2026-01-15T00:00:00Z' }),
      })
    })

    let pricedBody: Record<string, unknown> | null = null
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/bonds/fixed`, route => {
      pricedBody = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ pricing_history_id: null, assembled_request: {}, result: { npv: 107, clean_price: 107, dirty_price: 107, accrued: 0, extras: {} } }),
      })
    })

    await page.goto('/products/fixed-rate-bond/new')
    await page.locator('select').filter({ has: page.locator('option[value="e2e-discount"]') }).selectOption('e2e-discount')

    const saveBtn = page.getByRole('button', { name: 'Save', exact: true })
    await expect(saveBtn).toBeEnabled({ timeout: 10_000 })
    await saveBtn.click()
    await expect(page.getByText(/Referenced server-side/)).toBeVisible({ timeout: 15_000 })
    expect(crudOrder).toEqual(['curves', 'curve-sets', 'bonds/fixed'])

    const priceBtn = page.getByRole('button', { name: 'Price Bond' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()
    await expect(page.getByText('Pricing Results')).toBeVisible({ timeout: 15_000 })

    expect(pricedBody).not.toBeNull()
    const body = pricedBody as unknown as Record<string, unknown>
    expect(body.bond_id).toBe(BOND_UUID)
    expect(body.as_of).toBeTruthy()
    expect(body.bond).toBeUndefined()
    expect(body.curves).toBeUndefined()
  })
})

// Floating Rate Bond tests

test.describe('Floating Rate Bond — D104 orchestrator integration (D103 hermetic)', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/firestore.googleapis.com/**', r => r.abort())
    await page.route('**/identitytoolkit.googleapis.com/**', r => r.abort())
    await page.route('**/securetoken.googleapis.com/**', r => r.abort())
    await seedAll(page)
  })

  // Test A: happy path — orchestrator 200 → Pricing Results panel shown.
  // Also asserts the inline contract: flat ``bond`` trade body (spread/fixing_days/
  // in_arrears at top), top-level role-tagged ``curves``, top-level
  // ``index`` (IndexRef, ``kind: 'IborIndex'`` + ``body``), ``as_of``, no
  // ``pricing.*`` nesting, no ``bonds[*].floating_rate_bond`` wrapper, no
  // per-trade ``index.id`` on the trade body (the registry-union
  // fix put the resolved id off the trade).
  test('A: orchestrator 200 → Pricing Results panel visible (Thin-A body asserted)', async ({ page }) => {
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/bonds/floating`, route => {
      const body = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>
      expect(body.as_of).toBeTruthy()
      expect(Array.isArray(body.curves)).toBe(true)
      expect(body.pricing).toBeUndefined()
      expect((body as { bonds?: unknown }).bonds).toBeUndefined()
      const bondTrade = body.bond as Record<string, unknown>
      expect(bondTrade).toBeDefined()
      expect(bondTrade.spread).toEqual(expect.any(Number))
      expect(bondTrade.fixing_days).toEqual(expect.any(Number))
      expect(bondTrade.in_arrears).toEqual(expect.any(Boolean))
      expect(bondTrade.face_amount).toEqual(expect.any(Number))
      expect((bondTrade as { floating_rate_bond?: unknown }).floating_rate_bond).toBeUndefined()
      expect((bondTrade as { index?: unknown }).index).toBeUndefined()
      expect((bondTrade as { pricing?: unknown }).pricing).toBeUndefined()
      const firstCurve = (body.curves as Array<Record<string, unknown>>)[0]
      expect((firstCurve.body as Record<string, unknown>).role).toBe('discount')
      const idxRef = body.index as Record<string, unknown>
      expect(idxRef).toBeDefined()
      expect(idxRef.kind).toEqual(expect.any(String))
      expect(idxRef.body).toBeDefined()
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          pricing_history_id: null,
          assembled_request: {},
          result: { npv: 99.8, clean_price: 99.5, dirty_price: 99.7, accrued: 0.2, extras: {} },
        }),
      })
    })

    await page.goto('/products/floating-rate-bond/new')

    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-discount"]') })
      .selectOption('e2e-discount')

    const priceBtn = page.getByRole('button', { name: 'Price Bond' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    await expect(page.getByText('Pricing Results')).toBeVisible({ timeout: 15_000 })
  })

  // Test B: coded error — bond_floating_not_found 404 → error card; no Pricing Results panel
  test('B: bond_floating_not_found 404 → Not Found error card; Pricing Results absent', async ({ page }) => {
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/bonds/floating`, route =>
      route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'Floating rate bond not found.',
          code: 'bond_floating_not_found',
          request_id: 'req-e2e-bond-floating-b',
        }),
      }),
    )

    await page.goto('/products/floating-rate-bond/new')

    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-discount"]') })
      .selectOption('e2e-discount')

    const priceBtn = page.getByRole('button', { name: 'Price Bond' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    // Branch on code, not prose (invariant 9) — PricingErrorCard renders title from category
    await expect(page.getByText('Not Found', { exact: true })).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText('Pricing Results')).not.toBeVisible()
  })

  // Test C: save → reference → price vertical for FLOATING bonds.
  //  1. build, 2. Save → assert leaf→root CRUD order
  //     ['curves', 'curves', 'curve-sets', 'indices', 'bonds/floating']
  //     (two role-tagged curves: discount + projection; index → app.indices,
  //)  + the saved bond pins curve_set_id + discount_curve_id +
  //     forecast_curve_id + index_id (the verified backend read-path),
  //  3. Price → by-reference ({bond_id}, no inline), 4. renders.
  test('C: save persists curves→curve-set→index→bonds/floating leaf→root, then prices by-reference', async ({ page }) => {
    const BOND_UUID = 'f9999999-8888-7777-6666-555555555555'
    const crudOrder: string[] = []
    let curveSeq = 0

    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/curves`, route => {
      crudOrder.push('curves')
      curveSeq += 1
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ id: `curve-uuid-${curveSeq}`, name: 'c', points: [], body: {}, created_at: '2026-01-15T00:00:00Z', updated_at: '2026-01-15T00:00:00Z' }),
      })
    })
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/curve-sets`, route => {
      crudOrder.push('curve-sets')
      const body = JSON.parse(route.request().postData() ?? '{}') as { body?: { curve_refs?: Array<{ curve_id?: string; role?: string }> } }
      // Two role-tagged refs: discount + projection.
      expect(body.body?.curve_refs?.map(r => r.role)).toEqual(['discount', 'projection'])
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ id: 'curveset-uuid-e2e', name: 'cs', body: {}, created_at: '2026-01-15T00:00:00Z', updated_at: '2026-01-15T00:00:00Z' }),
      })
    })
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/indices`, route => {
      crudOrder.push('indices')
      const body = JSON.parse(route.request().postData() ?? '{}') as { kind?: string }
      // Persisted with the app.indices column kind ({IBOR, Overnight, Inflation}),
      // not the engine discriminator ({IborIndex, …}) — the live CHECK constraint
      // ``indices_kind_valid`` rejects the latter (the translator normalises both).
      expect(body.kind).toBe('IBOR')
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ id: 'index-uuid-e2e', name: 'EURIBOR_6M', kind: body.kind ?? 'IborIndex', body: {}, created_at: '2026-01-15T00:00:00Z', updated_at: '2026-01-15T00:00:00Z' }),
      })
    })
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/bonds/floating`, route => {
      crudOrder.push('bonds/floating')
      const body = JSON.parse(route.request().postData() ?? '{}') as {
        request?: { pricing?: { curve_set_id?: string; discount_curve_id?: string; forecast_curve_id?: string; index_id?: string } }
      }
      expect(body.request?.pricing?.curve_set_id).toBe('curveset-uuid-e2e')
      expect(body.request?.pricing?.discount_curve_id).toBe('curve-uuid-1')
      expect(body.request?.pricing?.forecast_curve_id).toBe('curve-uuid-2')
      expect(body.request?.pricing?.index_id).toBe('index-uuid-e2e')
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ id: BOND_UUID, name: 'bond', request: body.request ?? {}, created_at: '2026-01-15T00:00:00Z', updated_at: '2026-01-15T00:00:00Z' }),
      })
    })

    let pricedBody: Record<string, unknown> | null = null
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/bonds/floating`, route => {
      pricedBody = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ pricing_history_id: null, assembled_request: {}, result: { npv: 101.17, clean_price: 101.17, dirty_price: 101.17, accrued: 0, extras: {} } }),
      })
    })

    await page.goto('/products/floating-rate-bond/new')
    // Single-curve mode is the default (useSameCurve=true) → discount content
    // persists under both roles; selecting the discount curve is enough.
    await page.locator('select').filter({ has: page.locator('option[value="e2e-discount"]') }).selectOption('e2e-discount')

    const saveBtn = page.getByRole('button', { name: 'Save', exact: true })
    await expect(saveBtn).toBeEnabled({ timeout: 10_000 })
    await saveBtn.click()
    await expect(page.getByText(/Referenced server-side/)).toBeVisible({ timeout: 15_000 })
    expect(crudOrder).toEqual(['curves', 'curves', 'curve-sets', 'indices', 'bonds/floating'])

    const priceBtn = page.getByRole('button', { name: 'Price Bond' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()
    await expect(page.getByText('Pricing Results')).toBeVisible({ timeout: 15_000 })

    expect(pricedBody).not.toBeNull()
    const body = pricedBody as unknown as Record<string, unknown>
    expect(body.bond_id).toBe(BOND_UUID)
    expect(body.as_of).toBeTruthy()
    expect(body.bond).toBeUndefined()
    expect(body.curves).toBeUndefined()
    expect(body.index).toBeUndefined()
  })
})
