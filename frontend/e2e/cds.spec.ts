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

test.describe('CDS — D104 orchestrator integration (no flag)', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/firestore.googleapis.com/**', r => r.abort())
    await page.route('**/identitytoolkit.googleapis.com/**', r => r.abort())
    await page.route('**/securetoken.googleapis.com/**', r => r.abort())
    // Hermetic: intercept MD backend (market.quantra.io) with correct MdResolvedBatchResponse
    // so buildPricingQuoteSnapshotWithBackend never blocks on a real network call.
    await page.route('**/market.quantra.io/**', route =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [] }),
      }),
    )
    await seedAll(page)
  })

  // Test A: happy path — orchestrator 200 → Pricing Results panel shown.
  // Also asserts the wire body matches the inline contract: flat ``cds`` trade body,
  // top-level ``curves`` (role-tagged), top-level ``credit_curve`` carrying
  // inline ``flat_hazard_rate`` (no quote_id), top-level ``as_of``,
  // no ``pricing.*`` nesting, no ``cds_list`` wrapper, no ``upfront_date``
  // (only emitted when an explicit upfront is provided).
  test('A: orchestrator 200 → Pricing Results panel visible (Thin-A body asserted)', async ({ page }) => {
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/cds`, route => {
      const body = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>
      expect(body.as_of).toBeTruthy()
      expect(Array.isArray(body.curves)).toBe(true)
      expect(body.pricing).toBeUndefined()
      expect((body as { cds_list?: unknown }).cds_list).toBeUndefined()
      const cds = body.cds as Record<string, unknown>
      expect(cds).toBeDefined()
      expect(cds.side).toEqual(expect.any(String))
      expect(cds.notional).toEqual(expect.any(Number))
      expect(cds.running_coupon).toEqual(expect.any(Number))
      expect(cds.upfront_date).toBeUndefined()
      expect(cds.upfront).toBeUndefined()
      const schedule = cds.schedule as Record<string, unknown>
      expect(schedule.effective_date).toEqual(expect.any(String))
      expect(schedule.termination_date).toEqual(expect.any(String))
      // No fat-shape nesting smuggled under ``cds``.
      expect((cds as { pricing?: unknown }).pricing).toBeUndefined()
      expect((cds as { cds_list?: unknown }).cds_list).toBeUndefined()
      const cc = body.credit_curve as Record<string, unknown>
      expect(cc).toBeDefined()
      expect(cc.recovery_rate).toEqual(expect.any(Number))
      const ccBody = cc.body as Record<string, unknown>
      expect(ccBody).toBeDefined()
      // inline hazard input — either flat_hazard_rate OR points (no quote_id)
      const hasFlat = typeof ccBody.flat_hazard_rate === 'number'
      const hasPoints = Array.isArray(ccBody.points) && (ccBody.points as unknown[]).length > 0
      expect(hasFlat || hasPoints).toBe(true)
      if (hasPoints) {
        for (const p of ccBody.points as Array<Record<string, unknown>>) {
          expect(p.quote_id).toBeUndefined()
        }
      }
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          pricing_history_id: null,
          assembled_request: {},
          result: {
            npv: -25000.0,
            fair_spread: 0.0105,
            fair_upfront: 0.001,
            protection_leg_npv: 30000.0,
            premium_leg_npv: -55000.0,
            extras: { default_leg_npv: 30000.0 },
          },
        }),
      })
    })

    await page.goto('/products/cds/new')

    // Inject discount curve via CurveSetSelector
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-discount"]') })
      .selectOption('e2e-discount')

    // Set Curve Source to "Inline override in this trade" to bypass saved-curve guard
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="inline"]') })
      .selectOption('inline')

    // Set Curve Mode to "Flat hazard rate"
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="flat"]') })
      .selectOption('flat')

    const priceBtn = page.getByRole('button', { name: 'Price CDS' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    await expect(page.getByText('Pricing Results')).toBeVisible({ timeout: 15_000 })
  })

  // Test B: coded error — cds_credit_curve_resolution_failed 422 → error card; no Pricing Results
  test('B: cds_credit_curve_resolution_failed 422 → Invalid Request error card; Pricing Results absent', async ({ page }) => {
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/cds`, route =>
      route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'Credit curve resolution failed: source=quote_book not supported.',
          code: 'cds_credit_curve_resolution_failed',
          request_id: 'req-e2e-cds-b',
          details: [{ role: 'credit', source: 'quote_book' }],
        }),
      }),
    )

    await page.goto('/products/cds/new')

    // Inject discount curve
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-discount"]') })
      .selectOption('e2e-discount')

    // Set Curve Source to "Inline override in this trade"
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="inline"]') })
      .selectOption('inline')

    // Set Curve Mode to "Flat hazard rate"
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="flat"]') })
      .selectOption('flat')

    const priceBtn = page.getByRole('button', { name: 'Price CDS' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    // Branch on code (invariant 9): 'validation' category → PricingErrorCard renders "Invalid Request"
    await expect(page.getByText('Invalid Request', { exact: true })).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText('Pricing Results')).not.toBeVisible()
  })

  // Test C: engine_unavailable 502 (the orchestrator now raises a clean
  // 502 on engine error instead of swallowing it) → "Backend Unavailable" card;
  // no Pricing Results.
  test('C: engine_unavailable 502 → Backend Unavailable error card; Pricing Results absent', async ({ page }) => {
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/cds`, route =>
      route.fulfill({
        status: 502,
        contentType: 'application/json',
        body: JSON.stringify({
          error: 'Pricing engine returned an error.',
          code: 'engine_unavailable',
          request_id: 'req-e2e-cds-c',
        }),
      }),
    )

    await page.goto('/products/cds/new')

    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-discount"]') })
      .selectOption('e2e-discount')

    await page
      .locator('select')
      .filter({ has: page.locator('option[value="inline"]') })
      .selectOption('inline')

    await page
      .locator('select')
      .filter({ has: page.locator('option[value="flat"]') })
      .selectOption('flat')

    const priceBtn = page.getByRole('button', { name: 'Price CDS' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    // Branch on code (invariant 9): 'unavailable' category → PricingErrorCard renders "Backend Unavailable"
    await expect(page.getByText('Backend Unavailable', { exact: true })).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText('Pricing Results')).not.toBeVisible()
  })

  // Test D: the full save → reference → price vertical for CDS.
  //  1. build a CDS (inline + flat hazard credit curve),
  //  2. Save → assert the entity graph is POSTed leaf→root including the
  //     cds-specific credit_curve leaf:
  //       ['curves', 'curve-sets', 'credit-curves', 'cds']
  //     and that the saved cds pins curve_set_id + credit_curve_id (+ the
  //     discount_curve_id the assembler reads) into request.pricing,
  //  3. Price → assert the body switched to the by-reference arm
  //     ({cds_id} pinned to the saved app.cds UUID, NO inline cds/curves/
  //     credit_curve),
  //  4. the Pricing Results panel renders. Coexistence with Test A (unsaved →
  //     inline) proves the per-call arm selection.
  test('D: save persists curves→curve-set→credit-curve→cds leaf→root, then prices by-reference', async ({
    page,
  }) => {
    const CDS_UUID = 'cccccccc-1111-2222-3333-444444444444'
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
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/credit-curves`, route => {
      crudOrder.push('credit-curves')
      // The credit curve persists its literal hazard input + recovery;
      // no quote_id anywhere on its body.
      const body = JSON.parse(route.request().postData() ?? '{}') as {
        source?: string
        recovery_rate?: number
        body?: Record<string, unknown>
      }
      expect(body.source).toBe('flat')
      expect(typeof body.recovery_rate).toBe('number')
      expect(typeof body.body?.flat_hazard_rate).toBe('number')
      expect(JSON.stringify(body.body)).not.toContain('quote_id')
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'creditcurve-uuid-e2e',
          name: 'inline_credit_curve',
          source: 'flat',
          recovery_rate: body.recovery_rate ?? 0.4,
          body: body.body ?? {},
          created_at: '2026-01-15T00:00:00Z',
          updated_at: '2026-01-15T00:00:00Z',
        }),
      })
    })
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/cds`, route => {
      crudOrder.push('cds')
      // The saved trade pins the persisted refs for by-reference resolution:
      // curve_set_id (grouping) + discount_curve_id (what the cds assembler
      // reads) + credit_curve_id (pricing.credit_curve_id).
      const body = JSON.parse(route.request().postData() ?? '{}') as {
        request?: { pricing?: { curve_set_id?: string; discount_curve_id?: string; credit_curve_id?: string } }
      }
      expect(body.request?.pricing?.curve_set_id).toBe('curveset-uuid-e2e')
      expect(body.request?.pricing?.discount_curve_id).toBe('curve-uuid-e2e')
      expect(body.request?.pricing?.credit_curve_id).toBe('creditcurve-uuid-e2e')
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          id: CDS_UUID,
          name: 'cds',
          request: body.request ?? {},
          created_at: '2026-01-15T00:00:00Z',
          updated_at: '2026-01-15T00:00:00Z',
        }),
      })
    })

    let pricedBody: Record<string, unknown> | null = null
    await page.route(`${ORCHESTRATOR_ORIGIN}/v1/price/cds`, route => {
      pricedBody = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          pricing_history_id: null,
          assembled_request: {},
          result: {
            npv: -25000.0,
            fair_spread: 0.0105,
            fair_upfront: 0.001,
            protection_leg_npv: 30000.0,
            premium_leg_npv: -55000.0,
            extras: { default_leg_npv: 30000.0 },
          },
        }),
      })
    })

    await page.goto('/products/cds/new')

    // Inject discount curve via CurveSetSelector
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-discount"]') })
      .selectOption('e2e-discount')

    // Inline + flat hazard credit curve so a credit_curve body is built.
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="inline"]') })
      .selectOption('inline')
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="flat"]') })
      .selectOption('flat')

    // Save the CDS — drives the leaf→root persist.
    const saveBtn = page.getByRole('button', { name: 'Save', exact: true })
    await expect(saveBtn).toBeEnabled({ timeout: 10_000 })
    await saveBtn.click()

    // Success banner mentions the server-side reference (appId stamped).
    await expect(page.getByText(/Referenced server-side/)).toBeVisible({ timeout: 15_000 })
    // Leaf→root order: curves → curve-set → credit-curve → cds.
    expect(crudOrder).toEqual(['curves', 'curve-sets', 'credit-curves', 'cds'])

    // Now price — the saved cds_id flips the call to the by-reference arm.
    const priceBtn = page.getByRole('button', { name: 'Price CDS' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()

    await expect(page.getByText('Pricing Results')).toBeVisible({ timeout: 15_000 })

    // By-reference body: cds_id pinned to the saved UUID; NO inline trade/curves/credit_curve.
    expect(pricedBody).not.toBeNull()
    const body = pricedBody as unknown as Record<string, unknown>
    expect(body.cds_id).toBe(CDS_UUID)
    expect(body.as_of).toBeTruthy()
    expect(body.cds).toBeUndefined()
    expect(body.curves).toBeUndefined()
    expect(body.credit_curve).toBeUndefined()
  })
})
