import { test, expect, Page, request as pwRequest } from '@playwright/test'
import { FIREBASE_API_KEY, ORCHESTRATOR_ORIGIN, buildFakeUser } from './helpers'

// Server-side quote-resolution live acceptance:
//   The portal must NOT resolve quote_ids client-side. A curve point carrying a
//   quote_id must reach the orchestrator UNRESOLVED (id intact, no inlined rate),
//   and the orchestrator must pull the value from the MD server.
//
// This spec drives the REAL IrSwap page against the running Vite dev server and:
//   (1) intercepts the outgoing POST /v1/price/swap/ir and asserts every curve
//       point that carried a quote_id STILL carries it (and was NOT inlined) —
//       proving the portal-side client-side resolver was removed;
//   (2) replays the portal-built curve against the REAL orchestrator (:8080) with
//       forward-starting dates at the seeded as_of and asserts the response's
//       resolved_quotes echo carries the synthetic MD values — proving the
//       backend PULLED market data server-side.

const AS_OF = '2026-01-15' // seeded synthetic-MD as_of

const fakeUser = buildFakeUser()

// USD discount curve whose points REFERENCE canonical md.* ids (no inlined rate).
const quoteRefCurve = {
  id: 'e2e-usd-qref',
  name: 'E2E USD Quote-Ref',
  currency: 'USD',
  role: 'discount',
  day_counter: 'Actual365Fixed',
  interpolator: 'LogLinear',
  bootstrap_trait: 'Discount',
  reference_date: AS_OF,
  points: [
    {
      point_type: 'DepositHelper',
      point: {
        quote_id: 'USD.IRS.1Y',
        tenor_number: 1,
        tenor_time_unit: 'Years',
        fixing_days: 2,
        calendar: 'TARGET',
        business_day_convention: 'ModifiedFollowing',
        day_counter: 'Actual365Fixed',
      },
    },
    {
      point_type: 'SwapHelper',
      point: {
        quote_id: 'USD.IRS.2Y',
        tenor_number: 2,
        tenor_time_unit: 'Years',
        calendar: 'TARGET',
        sw_fixed_leg_frequency: 'Annual',
        sw_fixed_leg_convention: 'ModifiedFollowing',
        sw_fixed_leg_day_counter: 'Thirty360',
      },
    },
    {
      point_type: 'SwapHelper',
      point: {
        quote_id: 'USD.IRS.5Y',
        tenor_number: 5,
        tenor_time_unit: 'Years',
        calendar: 'TARGET',
        sw_fixed_leg_frequency: 'Annual',
        sw_fixed_leg_convention: 'ModifiedFollowing',
        sw_fixed_leg_day_counter: 'Thirty360',
      },
    },
  ],
  createdAt: '',
  updatedAt: '',
}

const usdIndex = {
  id: 'e2e-usd-ibor',
  type: 'IBOR',
  family: 'USDLibor',
  tenor_number: 3,
  tenor_time_unit: 'Months',
  fixing_days: 2,
  calendar: 'UnitedStates',
  day_counter: 'Actual360',
  business_day_convention: 'ModifiedFollowing',
}

async function seedAll(page: Page) {
  await page.addInitScript(
    ({ user, apiKey, curve, index }: { user: unknown; apiKey: string; curve: unknown; index: unknown }) => {
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
    { user: fakeUser, apiKey: FIREBASE_API_KEY, curve: quoteRefCurve, index: usdIndex },
  )
}

test.describe('MD-1 — portal ships quote_ids UNRESOLVED; backend pulls from MD', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/firestore.googleapis.com/**', r => r.abort())
    await page.route('**/identitytoolkit.googleapis.com/**', r => r.abort())
    await page.route('**/securetoken.googleapis.com/**', r => r.abort())
    await seedAll(page)
  })

  test('quote-ref curve reaches the wire with quote_id intact; MD resolves server-side', async ({ page }) => {
    let outgoing: Record<string, unknown> | null = null

    // Capture the portal's outgoing price request; fulfill a canned 200 so the
    // UI completes regardless of trade-date anchoring. The assertion is on the
    // REQUEST shape (quote_id preserved), not the mocked response.
    await page.route('**/v1/price/swap/ir', route => {
      outgoing = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          pricing_history_id: null,
          assembled_request: {},
          result: { npv: 1.0, extras: { fair_rate: 0.039 } },
        }),
      })
    })

    await page.goto('/products/ir-swap/new')
    await page
      .locator('select')
      .filter({ has: page.locator('option[value="e2e-usd-qref"]') })
      .selectOption('e2e-usd-qref')

    const priceBtn = page.getByRole('button', { name: 'Price' })
    await expect(priceBtn).toBeEnabled({ timeout: 10_000 })
    await priceBtn.click()
    await expect(page.getByText('Swap Results')).toBeVisible({ timeout: 15_000 })

    // (1) Portal-side proof: the outgoing curve points STILL carry quote_id and
    //     were NOT inlined to a literal rate.
    expect(outgoing).not.toBeNull()
    const body = outgoing as unknown as { curves: Array<{ points: Array<{ point: Record<string, unknown> }> }> }
    const points = body.curves[0].points
    const ids = points.map(p => p.point.quote_id)
    expect(ids).toEqual(['USD.IRS.1Y', 'USD.IRS.2Y', 'USD.IRS.5Y'])
    for (const p of points) {
      // no client-side inlining: a quote-ref point must not carry a literal rate
      expect(p.point.rate).toBeUndefined()
    }

    // (2) Backend-side proof: replay the portal-built curve against the REAL
    //     orchestrator with forward-starting dates; assert MD resolved the ids.
    const api = await pwRequest.newContext()
    const replay = await api.post(`${ORCHESTRATOR_ORIGIN}/v1/price/swap/ir`, {
      headers: { 'X-Request-Id': `md1-e2e-${Date.now()}` },
      data: {
        swap: { notional: 1_000_000, effective_date: '2026-01-19', termination_date: '2031-01-19' },
        curves: body.curves,
        as_of: AS_OF,
      },
    })
    expect(replay.status()).toBe(200)
    const priced = (await replay.json()) as {
      assembled_request: { resolved_quotes: Array<{ canonical_id: string; value: number; source: string }> }
    }
    const resolved = Object.fromEntries(
      priced.assembled_request.resolved_quotes.map(q => [q.canonical_id, q]),
    )
    expect(resolved['USD.IRS.1Y'].source).toBe('synthetic')
    expect(resolved['USD.IRS.1Y'].value).toBeCloseTo(0.025502, 6)
    expect(resolved['USD.IRS.5Y'].value).toBeCloseTo(0.039028, 6)
    await api.dispose()
  })
})
