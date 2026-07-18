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

// Tests

test.describe('Swaption — D104 orchestrator integration (no flag)', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/firestore.googleapis.com/**', r => r.abort())
    await page.route('**/identitytoolkit.googleapis.com/**', r => r.abort())
    await page.route('**/securetoken.googleapis.com/**', r => r.abort())
    await seedAll(page)
  })

  // Test A: happy path — orchestrator 200 → Pricing Results panel shown.
  // Also asserts the wire body matches the inline contract: flat ``swaption`` trade
  // body (notional / strike / swap_type / exercise/effective/termination dates
  // at the top), top-level role-tagged ``curves``, top-level ``vol_surface``
  // (kind ``SwaptionVolSpec``), top-level ``swaption_model``, ``as_of`` at the
  // envelope root; no ``pricing.*`` nesting, no ``swaptions[*]`` wrapper.
  test('A: orchestrator 200 → Pricing Results panel visible (Thin-A body asserted)', async ({ page }) => {
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/swaption`, route => {
      const body = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>
      expect(body.as_of).toBeTruthy()
      expect(Array.isArray(body.curves)).toBe(true)
      expect(body.pricing).toBeUndefined()
      expect((body as { swaptions?: unknown }).swaptions).toBeUndefined()
      const trade = body.swaption as Record<string, unknown>
      expect(trade).toBeDefined()
      expect(trade.notional).toEqual(expect.any(Number))
      expect(trade.strike).toEqual(expect.any(Number))
      expect(trade.swap_type).toEqual(expect.any(String))
      expect(trade.exercise_date).toEqual(expect.any(String))
      expect(trade.effective_date).toEqual(expect.any(String))
      expect(trade.termination_date).toEqual(expect.any(String))
      expect((trade as { swaptions?: unknown }).swaptions).toBeUndefined()
      expect((trade as { pricing?: unknown }).pricing).toBeUndefined()
      expect((trade as { underlying?: unknown }).underlying).toBeUndefined()
      const firstCurve = (body.curves as Array<Record<string, unknown>>)[0]
      expect((firstCurve.body as Record<string, unknown>).role).toBe('discount')
      const vol = body.vol_surface as Record<string, unknown>
      expect(vol).toBeDefined()
      expect(vol.kind).toBe('SwaptionVolSpec')
      expect(vol.payload).toBeDefined()
      const model = body.swaption_model as Record<string, unknown>
      expect(model).toBeDefined()
      expect(model.kind).toEqual(expect.any(String))
      expect(model.payload).toBeDefined()
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          pricing_history_id: null,
          assembled_request: {},
          result: { npv: 500.0, delta: -0.45, vega: 123.0, extras: {} },
        }),
      })
    })

    await page.goto('/products/swaption/new')

    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-discount"]') })
      .selectOption('e2e-discount')

    const priceBtn = page.getByRole('button', { name: 'Price Swaption' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    await expect(page.getByText('Pricing Results')).toBeVisible({ timeout: 15_000 })
  })

  // Test B: coded error — swaption_not_found 404 → error card; no Pricing Results panel
  test('B: swaption_not_found 404 → Not Found error card; Pricing Results absent', async ({ page }) => {
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/swaption`, route =>
      route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'Swaption not found.',
          code: 'swaption_not_found',
          request_id: 'req-e2e-swaption-b',
        }),
      }),
    )

    await page.goto('/products/swaption/new')

    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-discount"]') })
      .selectOption('e2e-discount')

    const priceBtn = page.getByRole('button', { name: 'Price Swaption' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    // Branch on code, not prose (invariant 9) — PricingErrorCard renders title from category
    await expect(page.getByText('Not Found', { exact: true })).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText('Pricing Results')).not.toBeVisible()
  })

  // Test D: the full save → reference → price vertical for swaption.
  //  1. build a swaption (inline default vol surface + HullWhiteLattice model),
  //  2. Save → assert the entity graph is POSTed leaf→root including the
  //     swaption-specific vol_surface + swaption_model leaves:
  //       ['curves', 'curve-sets', 'vol-surfaces', 'swaption-models', 'swaptions']
  //     and that the saved swaption pins curve_set_id + vol_surface_id +
  //     swaption_model_id into request.pricing (the exact refs the swaption
  //     backend actually reads — verified read-path),
  //  3. Price → assert the body switched to the by-reference arm
  //     ({swaption_id} pinned to the saved app.swaptions UUID, NO inline
  //     swaption/curves/vol_surface/swaption_model),
  //  4. the Pricing Results panel renders. Coexistence with Test A (unsaved →
  //     inline) proves the per-call arm selection.
  test('D: save persists curves→curve-set→vol-surface→swaption-model→swaption leaf→root, then prices by-reference', async ({
    page,
  }) => {
    const SWAPTION_UUID = 'eeeeeeee-1111-2222-3333-444444444444'
    const crudOrder: string[] = []

    // Hermetic MD backend (defensive — empty curve points means no call, but
    // keep parity with the cds spec so the save flow never blocks on network).
    await page.route('**/market.quantra.io/**', route =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) }),
    )

    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/curves`, route => {
      crudOrder.push('curves')
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'curve-uuid-e2e',
          name: 'discount',
          points: [],
          body: { role: 'discount' },
          created_at: '2026-01-15T00:00:00Z',
          updated_at: '2026-01-15T00:00:00Z',
        }),
      })
    })
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/curve-sets`, route => {
      crudOrder.push('curve-sets')
      // The curve_set must reference the persisted curve UUID (D9), discount-first.
      const body = JSON.parse(route.request().postData() ?? '{}') as {
        body?: { curve_refs?: Array<{ curve_id?: string; role?: string }> }
      }
      expect(body.body?.curve_refs?.[0]?.curve_id).toBe('curve-uuid-e2e')
      expect(body.body?.curve_refs?.[0]?.role).toBe('discount')
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
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/vol-surfaces`, route => {
      crudOrder.push('vol-surfaces')
      // The vol surface persists its literal {kind, payload} (base-value-only);
      // the by-reference price loads this row via pricing.vol_surface_id.
      const body = JSON.parse(route.request().postData() ?? '{}') as {
        kind?: string
        payload?: Record<string, unknown>
      }
      expect(body.kind).toBe('SwaptionVolSpec')
      expect(body.payload).toBeDefined()
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'volsurface-uuid-e2e',
          name: 'swaptvol',
          kind: 'SwaptionVolSpec',
          payload: body.payload ?? {},
          created_at: '2026-01-15T00:00:00Z',
          updated_at: '2026-01-15T00:00:00Z',
        }),
      })
    })
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/swaption-models`, route => {
      crudOrder.push('swaption-models')
      // Production model is HullWhiteLattice, prices off hw_sigma.
      const body = JSON.parse(route.request().postData() ?? '{}') as {
        kind?: string
        payload?: Record<string, unknown>
      }
      expect(body.kind).toBe('HullWhiteLattice')
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'model-uuid-e2e',
          name: 'swaptmodel',
          kind: 'HullWhiteLattice',
          payload: body.payload ?? {},
          created_at: '2026-01-15T00:00:00Z',
          updated_at: '2026-01-15T00:00:00Z',
        }),
      })
    })
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/swaptions`, route => {
      crudOrder.push('swaptions')
      // The saved trade pins the three refs the swaption assembler reads:
      // curve_set_id (curves) + vol_surface_id + swaption_model_id.
      const body = JSON.parse(route.request().postData() ?? '{}') as {
        request?: { pricing?: { curve_set_id?: string; vol_surface_id?: string; swaption_model_id?: string } }
      }
      expect(body.request?.pricing?.curve_set_id).toBe('curveset-uuid-e2e')
      expect(body.request?.pricing?.vol_surface_id).toBe('volsurface-uuid-e2e')
      expect(body.request?.pricing?.swaption_model_id).toBe('model-uuid-e2e')
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          id: SWAPTION_UUID,
          name: 'swaption',
          request: body.request ?? {},
          created_at: '2026-01-15T00:00:00Z',
          updated_at: '2026-01-15T00:00:00Z',
        }),
      })
    })

    let pricedBody: Record<string, unknown> | null = null
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/swaption`, route => {
      pricedBody = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          pricing_history_id: null,
          assembled_request: {},
          result: { npv: 7133.03, delta: 0.5, vega: 100.0, extras: {} },
        }),
      })
    })

    await page.goto('/products/swaption/new')

    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-discount"]') })
      .selectOption('e2e-discount')

    // Select the production model (HullWhiteLattice) so the persisted
    // swaption_model matches the live default; the by-ref price equals the
    // inline price for the same model (HW prices off hw_sigma, ignores the
    // BlackVol surface — deferred).
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="HullWhiteLattice"]') })
      .selectOption('HullWhiteLattice')

    // Save the swaption — drives the leaf→root persist.
    const saveBtn = page.getByRole('button', { name: 'Save', exact: true })
    await expect(saveBtn).toBeEnabled({ timeout: 10_000 })
    await saveBtn.click()

    // Success banner mentions the server-side reference (appGraph stamped).
    await expect(page.getByText(/Referenced server-side/)).toBeVisible({ timeout: 15_000 })
    // Leaf→root order: curves → curve-set → vol-surface → swaption-model → swaption.
    expect(crudOrder).toEqual(['curves', 'curve-sets', 'vol-surfaces', 'swaption-models', 'swaptions'])

    // Now price — the saved swaption_id flips the call to the by-reference arm.
    const priceBtn = page.getByRole('button', { name: 'Price Swaption' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    await expect(page.getByText('Pricing Results')).toBeVisible({ timeout: 15_000 })

    // By-reference body: swaption_id pinned to the saved UUID; NO inline payloads.
    expect(pricedBody).not.toBeNull()
    const body = pricedBody as unknown as Record<string, unknown>
    expect(body.swaption_id).toBe(SWAPTION_UUID)
    expect(body.as_of).toBeTruthy()
    expect(body.swaption).toBeUndefined()
    expect(body.curves).toBeUndefined()
    expect(body.vol_surface).toBeUndefined()
    expect(body.swaption_model).toBeUndefined()
  })
})
