import { test, expect, Page } from '@playwright/test'
import { FIREBASE_API_KEY, ORCHESTRATOR_ORIGIN, buildFakeUser } from './helpers'

// Constants


// Seed fixtures

const fakeUser = buildFakeUser()

const discountCurve = {
  id: 'e2e-discount',
  name: 'E2E Discount',
  currency: 'USD',
  role: 'discount',
  day_counter: 'Actual365Fixed',
  interpolator: 'LogLinear',
  bootstrap_trait: 'Discount',
  reference_date: '2026-05-23',
  points: [],
  createdAt: '',
  updatedAt: '',
}

const dividendCurve = {
  id: 'e2e-dividend',
  name: 'E2E Dividend',
  currency: 'USD',
  role: 'forward',
  day_counter: 'Actual365Fixed',
  interpolator: 'LogLinear',
  bootstrap_trait: 'Discount',
  reference_date: '2026-05-23',
  points: [],
  createdAt: '',
  updatedAt: '',
}

const volSurface = {
  id: 'e2e-vol-surface',
  payload_type: 'BlackVolSpec',
  base: {
    shape: 'Constant',
    constant_vol: 0.2,
  },
}

// Helpers

async function seedAll(page: Page) {
  await page.addInitScript(
    ({
      user,
      apiKey,
      curves,
      volSurfaces,
    }: {
      user: unknown
      apiKey: string
      curves: unknown[]
      volSurfaces: unknown[]
    }) => {
      localStorage.setItem('quantra_curves', JSON.stringify(curves))
      localStorage.setItem('quantra_vol_surfaces', JSON.stringify(volSurfaces))

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
      curves: [discountCurve, dividendCurve],
      volSurfaces: [volSurface],
    },
  )
}

// Tests

test.describe('Equity Option — D104 orchestrator integration (no flag)', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/firestore.googleapis.com/**', r => r.abort())
    await page.route('**/identitytoolkit.googleapis.com/**', r => r.abort())
    await page.route('**/securetoken.googleapis.com/**', r => r.abort())
    await page.route('**/market.quantra.io/**', route =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [] }),
      }),
    )
    await seedAll(page)
  })

  // Test A: happy path — orchestrator 200 → Results panel visible.
  // Also asserts the wire body matches the inline contract: flat ``equity_option``
  // trade body (option_type / strike / expiry_date / settlement at the
  // top), top-level role-tagged ``curves`` (discount + dividend),
  // top-level ``vol_surface`` (kind ``BlackVolSpec``), top-level ``spot``
  // with ``canonical_id`` (underlier id derived from spot),
  // ``as_of`` at the envelope root; no ``pricing.*`` nesting, no
  // ``options[*]`` wrapper.
  test('A: orchestrator 200 → Results panel visible (Thin-A body asserted)', async ({ page }) => {
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/equity-option`, route => {
      const body = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>
      expect(body.as_of).toBeTruthy()
      expect(Array.isArray(body.curves)).toBe(true)
      expect(body.pricing).toBeUndefined()
      expect((body as { options?: unknown }).options).toBeUndefined()
      const trade = body.equity_option as Record<string, unknown>
      expect(trade).toBeDefined()
      expect(trade.option_type).toEqual(expect.any(String))
      expect(trade.strike).toEqual(expect.any(Number))
      expect(trade.expiry_date).toEqual(expect.any(String))
      expect(trade.settlement).toEqual(expect.any(String))
      expect((trade as { options?: unknown }).options).toBeUndefined()
      expect((trade as { pricing?: unknown }).pricing).toBeUndefined()
      expect((trade as { underlying?: unknown }).underlying).toBeUndefined()
      const curves = body.curves as Array<Record<string, unknown>>
      expect(curves.length).toBeGreaterThanOrEqual(1)
      expect((curves[0].body as Record<string, unknown>).role).toBe('discount')
      const vol = body.vol_surface as Record<string, unknown>
      expect(vol).toBeDefined()
      expect(vol.kind).toBe('BlackVolSpec')
      expect((vol.payload as Record<string, unknown>).base).toBeDefined()
      expect((vol.payload as { payload?: unknown }).payload).toBeUndefined()
      const spot = body.spot as Record<string, unknown>
      expect(spot).toBeDefined()
      // Portal forwards spot value + canonical_id (so the orchestrator
      // derives a stable per-trade underlier id from canonical_id).
      expect(spot.value === undefined && spot.canonical_id === undefined).toBe(false)
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          pricing_history_id: null,
          assembled_request: {},
          result: {
            npv: 12.50,
            delta: 0.55,
            gamma: 0.02,
            vega: 8.5,
            theta: -0.15,
            rho: 3.2,
            implied_volatility: 0.22,
            used_spot: 100.0,
            used_strike: 100.0,
            used_settlement: 'Cash',
            extras: { engine_used: 'AnalyticBSM' },
          },
        }),
      })
    })

    await page.goto('/products/equity-options/new')

    // Select discount curve (role=discount; only appears in discount CurveSetSelector)
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-discount"]') })
      .selectOption('e2e-discount')

    // Select dividend curve (role=forward; only appears in dividend CurveSetSelector)
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-dividend"]') })
      .selectOption('e2e-dividend')

    const priceBtn = page.getByRole('button', { name: 'Price' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    await expect(page.getByText('Results')).toBeVisible({ timeout: 15_000 })
  })

  // Test B: equity_option_spot_resolution_failed 422 → error div visible; "Results" absent
  test('B: equity_option_spot_resolution_failed 422 → error div visible; Results absent', async ({ page }) => {
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/equity-option`, route =>
      route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'Spot quote id SPOT_XYZ not resolvable at 2026-05-23.',
          code: 'equity_option_spot_resolution_failed',
          request_id: 'req-e2e-eq-b',
        }),
      }),
    )

    await page.goto('/products/equity-options/new')

    // Select discount curve
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-discount"]') })
      .selectOption('e2e-discount')

    // Select dividend curve
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-dividend"]') })
      .selectOption('e2e-dividend')

    const priceBtn = page.getByRole('button', { name: 'Price' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    await expect(page.getByText(/Spot quote id SPOT_XYZ not resolvable/, { exact: false })).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText('Results')).not.toBeVisible()
  })

  // Test C: the full save → reference → price vertical for equity options.
  //  1. build an equity option (inline default BlackVol surface + inline spot),
  //  2. Save → assert the entity graph is POSTed leaf→root including the
  //     equity-specific vol_surface leaf, with NO curve_set and NO model entity:
  //       ['curves', 'curves', 'vol-surfaces', 'equity-options']
  //     and that the saved option pins pricing.curves (a role-tagged list) +
  //     vol_surface_id + inline spot into request.pricing (the exact refs the
  //     backend actually reads for equity options — verified read-path),
  //  3. Price → assert the body switched to the by-reference arm
  //     ({equity_option_id} pinned to the saved app.equity_options UUID, NO inline
  //     equity_option/curves/vol_surface/spot),
  //  4. the Results panel renders. Coexistence with Test A (unsaved → inline
  //     inline) proves the per-call arm selection.
  test('C: save persists curves→vol-surface→equity-option leaf→root, then prices by-reference', async ({
    page,
  }) => {
    const EQUITY_OPTION_UUID = 'eeeeeeee-1111-2222-3333-555555555555'
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
          created_at: '2026-05-23T00:00:00Z',
          updated_at: '2026-05-23T00:00:00Z',
        }),
      })
    })
    // Equity options have NO curve_set — fail loudly if the save flow ever POSTs one.
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/curve-sets`, route => {
      crudOrder.push('curve-sets')
      return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'unexpected curve_set', code: 'unexpected' }) })
    })
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/vol-surfaces`, route => {
      crudOrder.push('vol-surfaces')
      // The vol surface persists its literal {kind, payload} (base-value-only);
      // the by-reference price loads this row via pricing.vol_surface_id.
      const body = JSON.parse(route.request().postData() ?? '{}') as {
        kind?: string
        payload?: Record<string, unknown>
      }
      expect(body.kind).toBe('BlackVolSpec')
      expect(body.payload).toBeDefined()
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'volsurface-uuid-e2e',
          name: 'AAPL-vol',
          kind: 'BlackVolSpec',
          payload: body.payload ?? {},
          created_at: '2026-05-23T00:00:00Z',
          updated_at: '2026-05-23T00:00:00Z',
        }),
      })
    })
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/equity-options`, route => {
      crudOrder.push('equity-options')
      // The saved trade pins the refs the equity_options assembler reads: a
      // role-tagged pricing.curves list, pricing.vol_surface_id, and inline
      // pricing.spot. No curve_set_id / discount_curve_id.
      const body = JSON.parse(route.request().postData() ?? '{}') as {
        request?: {
          pricing?: {
            curves?: Array<{ curve_id?: string; role?: string }>
            vol_surface_id?: string
            spot?: { canonical_id?: string; value?: number }
            curve_set_id?: string
            discount_curve_id?: string
          }
        }
      }
      const pricing = body.request?.pricing
      expect(pricing?.curves?.[0]?.curve_id).toBe('curve-uuid-1')
      expect(pricing?.curves?.[0]?.role).toBe('discount')
      expect(pricing?.curves?.[1]?.curve_id).toBe('curve-uuid-2')
      expect(pricing?.curves?.[1]?.role).toBe('dividend')
      expect(pricing?.vol_surface_id).toBe('volsurface-uuid-e2e')
      expect(pricing?.spot?.value === undefined && pricing?.spot?.canonical_id === undefined).toBe(false)
      expect(pricing?.curve_set_id).toBeUndefined()
      expect(pricing?.discount_curve_id).toBeUndefined()
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          id: EQUITY_OPTION_UUID,
          name: 'equity-option',
          request: body.request ?? {},
          created_at: '2026-05-23T00:00:00Z',
          updated_at: '2026-05-23T00:00:00Z',
        }),
      })
    })

    let pricedBody: Record<string, unknown> | null = null
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/equity-option`, route => {
      pricedBody = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          pricing_history_id: null,
          assembled_request: {},
          result: { npv: 8.81, delta: 0.573, vega: 38.73, extras: {} },
        }),
      })
    })

    await page.goto('/products/equity-options/new')

    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-discount"]') })
      .selectOption('e2e-discount')
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-dividend"]') })
      .selectOption('e2e-dividend')

    // Save the equity option — drives the leaf→root persist.
    const saveBtn = page.getByRole('button', { name: 'Save', exact: true })
    await expect(saveBtn).toBeEnabled({ timeout: 10_000 })
    await saveBtn.click()

    // Success banner mentions the server-side reference (appGraph stamped).
    await expect(page.getByText(/Referenced server-side/)).toBeVisible({ timeout: 15_000 })
    // Leaf→root order: curves (discount) → curves (dividend) → vol-surface → equity-option.
    expect(crudOrder).toEqual(['curves', 'curves', 'vol-surfaces', 'equity-options'])

    // Now price — the saved equity_option_id flips the call to the by-reference arm.
    const priceBtn = page.getByRole('button', { name: 'Price' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    await expect(page.getByText('Results')).toBeVisible({ timeout: 15_000 })

    // By-reference body: equity_option_id pinned to the saved UUID; NO inline payloads.
    expect(pricedBody).not.toBeNull()
    const body = pricedBody as unknown as Record<string, unknown>
    expect(body.equity_option_id).toBe(EQUITY_OPTION_UUID)
    expect(body.as_of).toBeTruthy()
    expect(body.equity_option).toBeUndefined()
    expect(body.curves).toBeUndefined()
    expect(body.vol_surface).toBeUndefined()
    expect(body.spot).toBeUndefined()
  })
})
