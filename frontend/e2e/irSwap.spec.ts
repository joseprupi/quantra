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

const iborIndex = {
  id: 'e2e-ibor',
  type: 'IBOR',
  family: 'Euribor',
  tenor_number: 3,
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
      // App storage (curves + indices) is localStorage-backed
      localStorage.setItem('quantra_curves', JSON.stringify([curve]))
      localStorage.setItem('quantra_indices', JSON.stringify([index]))

      // Firebase persists auth in IndexedDB — seed it before Firebase SDK reads it
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

// Tests

test.describe('IrSwap — D104 orchestrator integration (no flag)', () => {
  test.beforeEach(async ({ page }) => {
    // Abort Firebase/Google backend to prevent Firestore writes from hanging
    await page.route('**/firestore.googleapis.com/**', r => r.abort())
    await page.route('**/identitytoolkit.googleapis.com/**', r => r.abort())
    await page.route('**/securetoken.googleapis.com/**', r => r.abort())
    // Pre-seed localStorage + Firebase IDB auth before any page scripts run
    await seedAll(page)
  })

  // Test A: happy path — orchestrator 200 → NPV panel shown.
  // Also asserts the wire body matches the inline contract: flat ``swap`` trade body,
  // top-level ``curves``, top-level ``as_of``, no ``pricing.*`` nesting,
  // and curve points (when carrying a ``quote_id``) ship the id unresolved.
  test('A: orchestrator 200 → Swap Results panel visible (Thin-A body asserted)', async ({ page }) => {
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/swap/ir`, route => {
      const body = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>
      expect(body.as_of).toBeTruthy()
      expect(Array.isArray(body.curves)).toBe(true)
      expect(body.pricing).toBeUndefined()
      const swap = body.swap as Record<string, unknown>
      expect(swap).toBeDefined()
      expect(swap.notional).toEqual(expect.any(Number))
      expect(swap.fixed_rate).toEqual(expect.any(Number))
      expect(swap.swap_type).toEqual(expect.any(String))
      expect(swap.effective_date).toEqual(expect.any(String))
      expect(swap.termination_date).toEqual(expect.any(String))
      // No fat-shape nesting smuggled under ``swap``.
      expect((swap as { pricing?: unknown }).pricing).toBeUndefined()
      expect((swap as { swaps?: unknown }).swaps).toBeUndefined()
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          pricing_history_id: null,
          assembled_request: {},
          result: { npv: 1234.56, extras: {} },
        }),
      })
    })

    await page.goto('/products/ir-swap/new')

    // CurveSetSelector's inner select shows the seeded discount curve;
    // selecting it sets discountCurve state and clears the validation error
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-discount"]') })
      .selectOption('e2e-discount')

    // Button is disabled until discountCurve + indexRef are both set
    const priceBtn = page.getByRole('button', { name: 'Price' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    await expect(page.getByText('Swap Results')).toBeVisible({ timeout: 15_000 })
  })

  // Test B: coded error — swap_ir_not_found 404 → error card; no NPV panel
  test('B: swap_ir_not_found 404 → Not Found error card; Swap Results absent', async ({ page }) => {
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/swap/ir`, route =>
      route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'Swap not found.',
          code: 'swap_ir_not_found',
          request_id: 'req-e2e-b',
        }),
      }),
    )

    await page.goto('/products/ir-swap/new')

    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-discount"]') })
      .selectOption('e2e-discount')

    const priceBtn = page.getByRole('button', { name: 'Price' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    // Branch on code, not prose (invariant 9) — PricingErrorCard renders title from category
    // Use exact to avoid matching "Swap not found." in the error message body
    await expect(page.getByText('Not Found', { exact: true })).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText('Swap Results')).not.toBeVisible()
  })

  // Test C: the full save → reference → price vertical.
  //  1. build a vanilla swap, 2. Save → assert the entity graph is POSTed
  //     leaf→root (curves → curve-sets → swaps/ir) and each returns a UUID,
  //  3. Price → assert the body switched to the by-reference arm
  //     ({swap_id} pinned to the saved swaps_ir UUID, NO inline swap/curves),
  //  4. the Swap Results panel renders. Coexistence with Test A (unsaved →
  //     inline) proves the per-call arm selection.
  test('C: save persists curves→curve-set→swaps/ir leaf→root, then prices by-reference', async ({
    page,
  }) => {
    const SWAP_UUID = 'aaaaaaaa-1111-2222-3333-444444444444'
    const crudOrder: string[] = []

    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/curves`, route => {
      crudOrder.push('curves')
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'curve-uuid-e2e',
          name: 'discount',
          points: [],
          body: {},
          created_at: '2026-01-15T00:00:00Z',
          updated_at: '2026-01-15T00:00:00Z',
        }),
      })
    })
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/curve-sets`, route => {
      crudOrder.push('curve-sets')
      // The curve_set must reference the persisted curve UUID (D9).
      const body = JSON.parse(route.request().postData() ?? '{}') as {
        body?: { curve_refs?: Array<{ curve_id?: string }> }
      }
      expect(body.body?.curve_refs?.[0]?.curve_id).toBe('curve-uuid-e2e')
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'curveset-uuid-e2e',
          name: 'cs',
          body: {},
          created_at: '2026-01-15T00:00:00Z',
          updated_at: '2026-01-15T00:00:00Z',
        }),
      })
    })
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/swaps/ir`, route => {
      crudOrder.push('swaps/ir')
      // The saved trade pins the persisted curve_set for by-reference resolution.
      const body = JSON.parse(route.request().postData() ?? '{}') as {
        request?: { pricing?: { curve_set_id?: string } }
      }
      expect(body.request?.pricing?.curve_set_id).toBe('curveset-uuid-e2e')
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          id: SWAP_UUID,
          name: 'swap',
          request: body.request ?? {},
          created_at: '2026-01-15T00:00:00Z',
          updated_at: '2026-01-15T00:00:00Z',
        }),
      })
    })

    let pricedBody: Record<string, unknown> | null = null
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/swap/ir`, route => {
      pricedBody = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          pricing_history_id: null,
          assembled_request: {},
          result: { npv: -22894.84, extras: { fair_rate: 0.03 } },
        }),
      })
    })

    await page.goto('/products/ir-swap/new')

    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-discount"]') })
      .selectOption('e2e-discount')

    // Save the swap — drives the leaf→root persist.
    const saveBtn = page.getByRole('button', { name: 'Save', exact: true })
    await expect(saveBtn).toBeEnabled({ timeout: 10_000 })
    await saveBtn.click()

    // Success banner mentions the server-side reference (appId stamped).
    await expect(page.getByText(/Referenced server-side/)).toBeVisible({ timeout: 15_000 })
    // Leaf→root order: curves first, curve-set next, swap last.
    expect(crudOrder).toEqual(['curves', 'curve-sets', 'swaps/ir'])

    // Now price — the saved swap_id flips the call to the by-reference arm.
    const priceBtn = page.getByRole('button', { name: 'Price' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    await expect(page.getByText('Swap Results')).toBeVisible({ timeout: 15_000 })

    // By-reference body: swap_id pinned to the saved UUID; NO inline trade/curves.
    expect(pricedBody).not.toBeNull()
    const body = pricedBody as unknown as Record<string, unknown>
    expect(body.swap_id).toBe(SWAP_UUID)
    expect(body.as_of).toBeTruthy()
    expect(body.swap).toBeUndefined()
    expect(body.curves).toBeUndefined()
  })
})
