/**
 * OIS helper overnight params (engine >= 0.6): payment_lag, averaging_method
 * (Compound | Simple), lookback_days, lockout_days, apply_observation_shift
 * on OISHelper + DatedOISHelper curve points, plus the DatedOIS
 * fixed_leg_frequency (the engine hardcoded Annual before 0.6).
 *
 * GATED: beforeAll probes POST /v1/curve-preview directly with a minimal
 * inline OIS curve twice (payment_lag 0 vs a large lag) and requires (a) 200
 * on both and (b) the bootstrapped DFs to actually DIFFER. On an engine < 0.6
 * the shifted OISHelper wire slots reject (non-200); on an orchestrator that
 * merely IGNORES the unknown keys the DFs come back identical — both cases
 * SKIP the whole suite cleanly. No journeys.ts changes (standalone spec).
 */
import { test, expect, Page } from '@playwright/test'
import { fieldFor, gotoReady, selectByTextContains, tenorFieldsFor } from '../lib/ui'
import { PORTAL_URL, uniq } from '../lib/stack'

/** Inline rates only — as_of == reference_date makes any date coherent. */
const AS_OF = '2025-01-15'

/**
 * The backend live-proof SOFR strip (engine fixture
 * curves_usd_ois_sofr_paylag*_df_zero_long_end + a 50Y pillar): rates in
 * PERCENT for the UI's "Rate (%)" field.
 */
const SOFR_STRIP: Array<[number, 'Months' | 'Years', string]> = [
  [1, 'Months', '5.33'],
  [3, 'Months', '5.3'],
  [6, 'Months', '5.2'],
  [1, 'Years', '4.95'],
  [2, 'Years', '4.4'],
  [3, 'Years', '4.05'],
  [5, 'Years', '3.75'],
  [7, 'Years', '3.65'],
  [10, 'Years', '3.6'],
  [15, 'Years', '3.58'],
  [20, 'Years', '3.52'],
  [25, 'Years', '3.42'],
  [30, 'Years', '3.32'],
  [40, 'Years', '3.1'],
  [50, 'Years', '2.95'],
]

/** Expected 50Y DFs from the backend worker's B5.1 table (engine 0.6.0-dev). */
const EXPECTED_DF50 = { paylag0: 0.2627593509860267, paylag2: 0.2627555798307977 }

interface OvernightParams {
  paymentLag?: number
  averaging?: 'Compound' | 'Simple'
  lookbackDays?: number
  lockoutDays?: number
  obsShift?: boolean
}

function probeOisPoint(paymentLag: number, n: number, unit: string, rate: number) {
  return {
    point_type: 'OISHelper',
    point: {
      tenor: { n, unit },
      rate,
      settlement_days: 2,
      overnight_index: { id: 'SOFR' },
      calendar: 'UnitedStatesGovernmentBond',
      fixed_leg_convention: 'ModifiedFollowing',
      fixed_leg_frequency: 'Annual',
      payment_lag: paymentLag,
      averaging_method: 'Compound',
      lookback_days: 0,
      lockout_days: 0,
      apply_observation_shift: false,
    },
  }
}

function probePayload(paymentLag: number) {
  return {
    pricing: {
      as_of_date: AS_OF,
      curves: [
        {
          id: 'e2e-ois060-probe',
          day_counter: 'Actual365Fixed',
          interpolator: 'LogLinear',
          bootstrap_trait: 'Discount',
          reference_date: AS_OF,
          points: [
            probeOisPoint(paymentLag, 1, 'Years', 0.05),
            probeOisPoint(paymentLag, 2, 'Years', 0.048),
          ],
        },
      ],
    },
    queries: [
      {
        curve_id: 'e2e-ois060-probe',
        measures: ['DF'],
        grid: { grid_type: 'TenorGrid', grid: { tenors: [{ n: 2, unit: 'Years' }] } },
      },
    ],
  }
}

function dfSeries(body: Record<string, unknown>): number[] {
  const results = (body.results ?? []) as Array<{
    error?: unknown
    series?: Array<{ measure?: string; values?: number[] }>
  }>
  const first = results[0]
  if (!first || first.error) return []
  const df = first.series?.find((s) => s.measure === 'DF' || !s.measure)
  return (df?.values ?? []).filter((v) => Number.isFinite(v))
}

function gridDates(body: Record<string, unknown>): string[] {
  const results = (body.results ?? []) as Array<{ grid_dates?: string[] }>
  return results[0]?.grid_dates ?? []
}

let skipReason: string | null = 'probe did not run'

test.beforeAll(async ({ request }) => {
  // Probe the whole orchestrator -> engine path with payment_lag 0 vs a LARGE
  // lag: only a stack that both accepts AND honors the new fields passes.
  const dfs: number[][] = []
  for (const lag of [0, 40]) {
    const res = await request.post(`${PORTAL_URL}/v1/curve-preview`, { data: probePayload(lag) })
    if (res.status() !== 200) {
      skipReason = `curve-preview with payment_lag=${lag} -> ${res.status()} (engine/orchestrator without OIS overnight params)`
      return
    }
    const values = dfSeries((await res.json()) as Record<string, unknown>)
    if (values.length === 0) {
      skipReason = `curve-preview with payment_lag=${lag} returned no DF series`
      return
    }
    dfs.push(values)
  }
  if (Math.abs(dfs[0][0] - dfs[1][0]) < 1e-12) {
    skipReason = 'payment_lag accepted but has NO pricing effect (params silently ignored)'
    return
  }
  skipReason = null

  // The OIS editor needs a saved USD overnight index: seed SOFR exactly like
  // backend/scripts/seed_demo_entities.py (409 = already seeded, fine).
  const seeded = await request.post(`${PORTAL_URL}/v1/indices`, {
    data: {
      name: 'SOFR',
      kind: 'Overnight',
      currency: 'USD',
      calendar: 'UnitedStates',
      day_counter: 'Actual360',
      body: {
        id: 'SOFR',
        local_id: 'SOFR',
        type: 'Overnight',
        overnight_name: 'SOFR',
        index_type: 'Overnight',
        currency: 'USD',
        tenor_number: 0,
        tenor_time_unit: 'Days',
        fixing_days: 0,
        calendar: 'UnitedStates',
        day_counter: 'Actual360',
        description: 'Secured Overnight Financing Rate',
      },
    },
  })
  expect([200, 201, 409]).toContain(seeded.status())
})

test.beforeEach(() => {
  test.skip(skipReason !== null, skipReason ?? undefined)
})

/** Fill the shared overnight-params controls inside the open point editor. */
async function fillOvernightParams(page: Page, params: OvernightParams) {
  if (params.paymentLag !== undefined)
    await fieldFor(page, 'Payment lag (bd)').fill(String(params.paymentLag))
  if (params.averaging) await fieldFor(page, 'Averaging').selectOption(params.averaging)
  if (params.lookbackDays !== undefined)
    await fieldFor(page, 'Lookback (d)').fill(String(params.lookbackDays))
  if (params.lockoutDays !== undefined)
    await fieldFor(page, 'Lockout (d)').fill(String(params.lockoutDays))
  if (params.obsShift) await page.getByLabel('Obs. shift').check()
}

/** Add one OIS helper point (SOFR / UnitedStates calendar) to the open curve editor. */
async function addSofrOisPoint(
  page: Page,
  n: number,
  unit: 'Months' | 'Years',
  ratePct: string,
  params: OvernightParams,
) {
  await page.getByRole('button', { name: 'Add Instrument' }).first().click()
  await page.getByRole('button', { name: 'OIS', exact: true }).click()
  await fieldFor(page, 'Rate (%)').fill(ratePct)
  const tenor = tenorFieldsFor(page)
  await tenor.number.fill(String(n))
  await tenor.unit.selectOption(unit)
  await selectByTextContains(fieldFor(page, 'Overnight Index'), 'SOFR')
  // The editor's calendar list has no UnitedStatesGovernmentBond — use the
  // plain UnitedStates calendar (b51 parity band widened accordingly).
  await fieldFor(page, 'Calendar').selectOption('UnitedStates')
  await fillOvernightParams(page, params)
  await page.getByRole('button', { name: 'Add Instrument' }).last().click()
}

/** Start a new USD curve on /yield-curves/new anchored at AS_OF. */
async function startUsdCurve(page: Page, name: string) {
  await gotoReady(page, '/yield-curves/new')
  await fieldFor(page, 'Curve Name *').fill(name)
  await fieldFor(page, 'Currency').selectOption('USD')
  await fieldFor(page, 'Reference Date').fill(AS_OF)
}

/** Click Bootstrap and capture the curve-preview exchange (request + response). */
async function previewExchange(page: Page) {
  const respPromise = page.waitForResponse(
    (r) => r.url().includes('/v1/curve-preview') && r.request().method() === 'POST',
    { timeout: 120_000 },
  )
  await page.getByRole('button', { name: 'Bootstrap', exact: true }).click()
  const resp = await respPromise
  let body: Record<string, unknown> = {}
  try {
    body = (await resp.json()) as Record<string, unknown>
  } catch {
    /* keep {} */
  }
  let requestBody: Record<string, unknown> = {}
  try {
    requestBody = resp.request().postDataJSON() as Record<string, unknown>
  } catch {
    /* keep {} */
  }
  return { status: resp.status(), body, requestBody }
}

function wirePoints(requestBody: Record<string, unknown>) {
  const curves = ((requestBody.pricing as Record<string, unknown>)?.curves ?? []) as Array<
    Record<string, unknown>
  >
  return (curves[0]?.points ?? []) as Array<{ point_type: string; point: Record<string, unknown> }>
}

/** Save the curve in the editor and wait for the list to show it. */
async function saveAndLand(page: Page, name: string) {
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page).toHaveURL(/\/yield-curves$/, { timeout: 30_000 })
  await expect(page.getByText(name, { exact: true }).first()).toBeVisible({ timeout: 15_000 })
}

test.describe('OIS overnight params (engine >= 0.6, gated)', () => {
  test('paylag-2 OIS curve: editor fields -> wire -> preview grid -> save -> reload persists', async ({
    page,
  }) => {
    test.setTimeout(180_000)
    const name = uniq('E2E OIS paylag2')

    await startUsdCurve(page, name)
    await addSofrOisPoint(page, 1, 'Years', '4.95', { paymentLag: 2 })
    // Second point exercises the lookback/lockout variant on the same curve.
    await addSofrOisPoint(page, 5, 'Years', '3.75', {
      paymentLag: 2,
      lookbackDays: 2,
      lockoutDays: 2,
    })

    // The preview wire must carry the params VERBATIM as numbers/bools.
    const preview = await previewExchange(page)
    const points = wirePoints(preview.requestBody)
    expect(points).toHaveLength(2)
    expect(points[0].point).toMatchObject({
      tenor: { n: 1, unit: 'Years' },
      payment_lag: 2,
      averaging_method: 'Compound',
      lookback_days: 0,
      lockout_days: 0,
      apply_observation_shift: false,
    })
    expect(points[1].point).toMatchObject({
      tenor: { n: 5, unit: 'Years' },
      payment_lag: 2,
      lookback_days: 2,
      lockout_days: 2,
    })
    expect(typeof points[0].point.payment_lag).toBe('number')
    expect(typeof points[1].point.lookback_days).toBe('number')

    // And the engine bootstraps a real DF grid off it.
    expect(preview.status, `curve-preview: ${JSON.stringify(preview.body).slice(0, 400)}`).toBe(200)
    const dfs = dfSeries(preview.body)
    expect(dfs.length, 'DF grid has points').toBeGreaterThan(3)
    for (const df of dfs) {
      expect(df).toBeGreaterThan(0)
      expect(df).toBeLessThanOrEqual(1)
    }

    // Save -> reopen from the list -> the params persisted onto both points.
    await saveAndLand(page, name)
    await page.getByText(name, { exact: true }).first().click()
    await expect(fieldFor(page, 'Curve Name *')).toHaveValue(name, { timeout: 20_000 })

    await page.getByTitle('Edit').first().click()
    await expect(fieldFor(page, 'Payment lag (bd)')).toHaveValue('2')
    await expect(fieldFor(page, 'Averaging')).toHaveValue('Compound')
    await expect(fieldFor(page, 'Lookback (d)')).toHaveValue('0')
    await expect(fieldFor(page, 'Lockout (d)')).toHaveValue('0')
    await expect(page.getByLabel('Obs. shift')).not.toBeChecked()
    await page.getByRole('button', { name: 'Cancel', exact: true }).click()

    await page.getByTitle('Edit').nth(1).click()
    await expect(fieldFor(page, 'Payment lag (bd)')).toHaveValue('2')
    await expect(fieldFor(page, 'Lookback (d)')).toHaveValue('2')
    await expect(fieldFor(page, 'Lockout (d)')).toHaveValue('2')
  })

  test('Dated OIS point: fixed_leg_frequency + overnight params reach the wire and persist', async ({
    page,
  }) => {
    test.setTimeout(180_000)
    const name = uniq('E2E dated OIS')

    await startUsdCurve(page, name)
    // A plain OIS pillar keeps the curve bootstrappable on its own...
    await addSofrOisPoint(page, 1, 'Years', '4.95', { paymentLag: 2 })
    // ...and the Dated OIS point carries the new fields.
    await page.getByRole('button', { name: 'Add Instrument' }).first().click()
    await page.getByRole('button', { name: 'Dated OIS', exact: true }).click()
    await fieldFor(page, 'Rate (%)').fill('4.5')
    await fieldFor(page, 'Start Date').fill('2025-01-17')
    await fieldFor(page, 'End Date').fill('2027-01-17')
    await selectByTextContains(fieldFor(page, 'Overnight Index'), 'SOFR')
    await fieldFor(page, 'Calendar').selectOption('UnitedStates')
    await fieldFor(page, 'Fixed leg freq').selectOption('Quarterly')
    await fillOvernightParams(page, { paymentLag: 2, averaging: 'Simple' })
    await page.getByRole('button', { name: 'Add Instrument' }).last().click()

    const preview = await previewExchange(page)
    const points = wirePoints(preview.requestBody)
    const dated = points.find((p) => p.point_type === 'DatedOISHelper')
    expect(dated?.point).toMatchObject({
      start_date: '2025-01-17',
      end_date: '2027-01-17',
      fixed_leg_frequency: 'Quarterly',
      payment_lag: 2,
      averaging_method: 'Simple',
      lookback_days: 0,
      lockout_days: 0,
      apply_observation_shift: false,
    })
    expect(typeof dated?.point.payment_lag).toBe('number')
    expect(preview.status, `curve-preview: ${JSON.stringify(preview.body).slice(0, 400)}`).toBe(200)
    expect(dfSeries(preview.body).length).toBeGreaterThan(3)

    // Save -> reload -> the dated point kept its frequency + params.
    await saveAndLand(page, name)
    await page.getByText(name, { exact: true }).first().click()
    await expect(fieldFor(page, 'Curve Name *')).toHaveValue(name, { timeout: 20_000 })
    await page.getByTitle('Edit').nth(1).click()
    await expect(fieldFor(page, 'Fixed leg freq')).toHaveValue('Quarterly')
    await expect(fieldFor(page, 'Payment lag (bd)')).toHaveValue('2')
    await expect(fieldFor(page, 'Averaging')).toHaveValue('Simple')
  })

  test('MUFG-style SOFR twins: payment lag 2 vs 0 diverges the long-end DFs (B5.1)', async ({
    page,
  }) => {
    // Two 15-pillar curves built through the UI — deliberately slow.
    test.setTimeout(600_000)

    const df50ByLag: Record<number, number> = {}
    for (const lag of [0, 2]) {
      const name = uniq(`E2E SOFR paylag${lag}`)
      await startUsdCurve(page, name)
      for (const [n, unit, ratePct] of SOFR_STRIP) {
        await addSofrOisPoint(page, n, unit, ratePct, { paymentLag: lag })
      }

      // Extend the preview grid to the long end (25/40/50Y rows; new rows
      // default to 1 Years — only the number needs setting).
      await page.getByText('Grid Options', { exact: true }).click()
      const tenorInputs = page.locator(
        'xpath=//label[normalize-space(.)="Tenors"]/following-sibling::div//input',
      )
      const baseCount = await tenorInputs.count()
      for (const [i, longTenor] of [25, 40, 50].entries()) {
        await page.getByRole('button', { name: '+ Add tenor' }).click()
        await tenorInputs.nth(baseCount + i).fill(String(longTenor))
      }

      const preview = await previewExchange(page)
      expect(preview.status, `paylag${lag}: ${JSON.stringify(preview.body).slice(0, 400)}`).toBe(200)
      const points = wirePoints(preview.requestBody)
      expect(points).toHaveLength(SOFR_STRIP.length)
      expect(points.every((p) => p.point.payment_lag === lag)).toBe(true)

      const dfs = dfSeries(preview.body)
      const dates = gridDates(preview.body)
      expect(dfs.length).toBe(dates.length)
      const df50 = dfs[dfs.length - 1] // the 50Y row appended last
      expect(df50).toBeGreaterThan(0.2)
      expect(df50).toBeLessThan(0.35)
      df50ByLag[lag] = df50
    }

    // Long-end divergence in the B5.1 direction: paying 2bd later cheapens
    // the fixed leg -> the 50Y discount factor drops.
    const diff = df50ByLag[0] - df50ByLag[2]
    expect(diff, '50Y DF paylag0 - paylag2').toBeGreaterThan(1e-6)
    expect(diff, '50Y DF divergence stays small').toBeLessThan(1e-4)
    // And both twins land on the backend live-proof B5.1 values (loose band —
    // the UI grid adjusts dates with TARGET/ModifiedFollowing).
    expect(Math.abs(df50ByLag[0] - EXPECTED_DF50.paylag0)).toBeLessThan(2e-4)
    expect(Math.abs(df50ByLag[2] - EXPECTED_DF50.paylag2)).toBeLessThan(2e-4)
  })
})
