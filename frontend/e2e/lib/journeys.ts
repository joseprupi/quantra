/**
 * Journey builders: create the reference entities every product journey
 * needs, by driving the REAL portal UI (no API shortcuts — creating through
 * the forms IS part of the coverage).
 */
import { Page, expect } from '@playwright/test'
import { fieldFor, gotoReady, selectByTextContains, tenorFieldsFor } from './ui'

// ---------------------------------------------------------------------------
// Indices
// ---------------------------------------------------------------------------

export interface IborIndexOpts {
  id: string
  family?: 'Euribor' | 'USDLibor' | 'GBPLibor' | 'JPYLibor' | 'Tibor'
  tenorNumber?: number
  tenorUnit?: 'Days' | 'Weeks' | 'Months' | 'Years'
  calendar?: string
  dayCounter?: string
}

/** Create a custom IBOR index on /indices. */
export async function createIborIndex(page: Page, opts: IborIndexOpts) {
  await gotoReady(page, '/indices')
  await page.getByRole('button', { name: 'New Index' }).click()
  await fieldFor(page, 'Index ID *').fill(opts.id)
  await fieldFor(page, 'Type').selectOption('IBOR')
  if (opts.family) await fieldFor(page, 'Family').selectOption(opts.family)
  if (opts.tenorNumber !== undefined)
    await fieldFor(page, 'Tenor Number').fill(String(opts.tenorNumber))
  if (opts.tenorUnit) await fieldFor(page, 'Tenor Unit').selectOption(opts.tenorUnit)
  if (opts.calendar) await fieldFor(page, 'Calendar').selectOption(opts.calendar)
  if (opts.dayCounter) await fieldFor(page, 'Day Counter').selectOption(opts.dayCounter)
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  // The new index appears in the list.
  await expect(page.getByText(opts.id, { exact: true }).first()).toBeVisible({ timeout: 15_000 })
}

// ---------------------------------------------------------------------------
// Yield curves
// ---------------------------------------------------------------------------

export interface DepositPointOpts {
  ratePct?: number
  quoteId?: string
  tenorNumber: number
  tenorUnit: 'Days' | 'Weeks' | 'Months' | 'Years'
}

export interface SwapPointOpts {
  ratePct?: number
  quoteId?: string
  tenorNumber: number
  tenorUnit?: 'Years'
  /** Saved index id for the floating leg (picked in the IndexPicker Saved tab). */
  floatIndexId?: string
  fixedLegFrequency?: string
  fixedLegDayCounter?: string
  calendar?: string
}

export interface OisPointOpts {
  ratePct?: number
  quoteId?: string
  tenorNumber: number
  tenorUnit?: 'Years'
  /** Saved OVERNIGHT index id (SONIA / SOFR / custom). */
  overnightIndexId: string
  calendar?: string
}

export interface CurveOpts {
  name: string
  currency?: string
  referenceDate?: string
  deposits?: DepositPointOpts[]
  swaps?: SwapPointOpts[]
  oisHelpers?: OisPointOpts[]
}

/** Fill the shared inline-rate-or-quote chooser inside the point editor. */
async function fillRateOrQuote(
  page: Page,
  opts: { ratePct?: number; quoteId?: string },
) {
  if (opts.quoteId) {
    await page.getByText('Quote reference', { exact: true }).click()
    await selectByTextContains(fieldFor(page, 'Quote ID'), opts.quoteId)
  } else if (opts.ratePct !== undefined) {
    await fieldFor(page, 'Rate (%)').fill(String(opts.ratePct))
  }
}

/**
 * Fill the yield-curve editor on /yield-curves/new with deposit + swap
 * helpers. Does NOT save — callers preview and/or save explicitly
 * (saving navigates back to the curve list).
 */
export async function fillYieldCurveForm(page: Page, opts: CurveOpts) {
  await gotoReady(page, '/yield-curves/new')
  await fieldFor(page, 'Curve Name *').fill(opts.name)
  if (opts.currency) await fieldFor(page, 'Currency').selectOption(opts.currency)
  if (opts.referenceDate) await fieldFor(page, 'Reference Date').fill(opts.referenceDate)

  for (const dep of opts.deposits ?? []) {
    await page.getByRole('button', { name: 'Add Instrument' }).first().click()
    await page.getByRole('button', { name: 'Deposit', exact: true }).click()
    await fillRateOrQuote(page, dep)
    const depTenor = tenorFieldsFor(page)
    await depTenor.number.fill(String(dep.tenorNumber))
    await depTenor.unit.selectOption(dep.tenorUnit)
    await page.getByRole('button', { name: 'Add Instrument' }).last().click()
  }

  for (const sw of opts.swaps ?? []) {
    await page.getByRole('button', { name: 'Add Instrument' }).first().click()
    await page.getByRole('button', { name: 'Swap', exact: true }).click()
    await fillRateOrQuote(page, sw)
    const swTenor = tenorFieldsFor(page)
    await swTenor.number.fill(String(sw.tenorNumber))
    await swTenor.unit.selectOption(sw.tenorUnit ?? 'Years')
    if (sw.floatIndexId) {
      // IndexPicker: Saved tab select
      await selectByTextContains(fieldFor(page, 'Floating Index'), sw.floatIndexId)
    }
    if (sw.fixedLegFrequency)
      await fieldFor(page, 'Fixed Leg Frequency').selectOption(sw.fixedLegFrequency)
    if (sw.fixedLegDayCounter)
      await fieldFor(page, 'Fixed Leg Day Counter').selectOption(sw.fixedLegDayCounter)
    if (sw.calendar) await fieldFor(page, 'Calendar').selectOption(sw.calendar)
    await page.getByRole('button', { name: 'Add Instrument' }).last().click()
  }

  for (const ois of opts.oisHelpers ?? []) {
    await page.getByRole('button', { name: 'Add Instrument' }).first().click()
    await page.getByRole('button', { name: 'OIS', exact: true }).click()
    await fillRateOrQuote(page, ois)
    const oisTenor = tenorFieldsFor(page)
    await oisTenor.number.fill(String(ois.tenorNumber))
    await oisTenor.unit.selectOption(ois.tenorUnit ?? 'Years')
    await selectByTextContains(fieldFor(page, 'Overnight Index'), ois.overnightIndexId)
    if (ois.calendar) await fieldFor(page, 'Calendar').selectOption(ois.calendar)
    await page.getByRole('button', { name: 'Add Instrument' }).last().click()
  }
}

// ---------------------------------------------------------------------------
// Value-based curves ("Interpolate given values" construction)
// ---------------------------------------------------------------------------

export interface ValueCurveOpts {
  name: string
  currency?: string
  referenceDate?: string
  /** Visible Quantity button label ('Zero rates' default). */
  quantity?: 'Zero rates' | 'Discount factors' | 'Forward rates'
  /** Two-column paste block applied through "Paste table…". */
  pasteTable?: string
  /**
   * Value for the PINNED reference-date row (zero / fwd short end), in the
   * entry unit (percent). Ignored for DF (fixed at 1.0). Alternative: put a
   * reference-date line in pasteTable — it feeds the pinned row.
   */
  anchorValue?: string
}

/**
 * Fill the curve editor in "Interpolate given values" mode on
 * /yield-curves/new. Does NOT save — callers preview and/or save explicitly.
 */
export async function fillValueCurveForm(page: Page, opts: ValueCurveOpts) {
  await gotoReady(page, '/yield-curves/new')
  await fieldFor(page, 'Curve Name *').fill(opts.name)
  if (opts.currency) await fieldFor(page, 'Currency').selectOption(opts.currency)
  if (opts.referenceDate) await fieldFor(page, 'Reference Date').fill(opts.referenceDate)
  await page.getByRole('button', { name: 'Interpolate given values' }).click()
  if (opts.quantity && opts.quantity !== 'Zero rates') {
    await page.getByRole('button', { name: opts.quantity, exact: true }).click()
  }
  if (opts.pasteTable) {
    await page.getByRole('button', { name: 'Paste table…' }).click()
    await page.getByTestId('paste-table-input').fill(opts.pasteTable)
    await page.getByRole('button', { name: 'Apply', exact: true }).click()
  }
  if (opts.anchorValue !== undefined) {
    await page.getByLabel('Start value at the reference date').fill(opts.anchorValue)
  }
}

const TENOR_UNIT_LABELS: Record<string, string> = {
  d: 'Days',
  w: 'Weeks',
  m: 'Months',
  y: 'Years',
}

/**
 * Set the Maturity of an editable value-curve row (1-based) through the
 * per-row [Tenor | Date] controls and commit it (Enter -> auto-sort by
 * resolved date). Accepts a tenor token like '5Y' / '3M' or an ISO date.
 */
export async function setValueRowMaturity(page: Page, row: number, maturity: string) {
  const rowLoc = page.getByTestId('value-point-row').nth(row - 1)
  if (/^\d{4}-\d{2}-\d{2}$/.test(maturity)) {
    await rowLoc.getByRole('button', { name: 'Date', exact: true }).click()
    const input = page.getByLabel(`Maturity date ${row}`)
    await input.fill(maturity)
    await input.press('Enter')
  } else {
    const m = /^(\d+)\s*([dwmy])$/i.exec(maturity)
    if (!m) throw new Error(`setValueRowMaturity: unrecognized maturity "${maturity}"`)
    await rowLoc.getByRole('button', { name: 'Tenor', exact: true }).click()
    // Number first: typing updates the row WITHOUT sorting, so the row cannot
    // move mid-edit. The commit (+ auto-sort) then happens exactly once: via
    // the unit change when the unit differs, else via Enter on the number
    // (React swallows a same-value select change, so it would not commit).
    const num = page.getByLabel(`Tenor number ${row}`)
    await num.fill(m[1])
    const unitSelect = page.getByLabel(`Tenor unit ${row}`)
    const unitLabel = TENOR_UNIT_LABELS[m[2].toLowerCase()]
    if ((await unitSelect.inputValue()) === unitLabel) {
      await num.press('Enter')
    } else {
      await unitSelect.selectOption(unitLabel)
    }
  }
}

/**
 * Click Preview on the values-mode curve editor and wait for the
 * curve-preview exchange; returns status + body + the REQUEST body.
 */
export async function valueCurvePreview(
  page: Page,
): Promise<{ status: number; body: Record<string, unknown>; requestBody: Record<string, unknown> }> {
  const respPromise = page.waitForResponse(
    (r) => r.url().includes('/v1/curve-preview') && r.request().method() === 'POST',
    { timeout: 60_000 },
  )
  await page.getByRole('button', { name: 'Preview', exact: true }).click()
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

/** Save the curve currently in the editor; lands back on the curve list. */
export async function saveCurve(page: Page, expectedName: string, listPath = '/yield-curves') {
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(listPath.replace('/', '\\/') + '$'), {
    timeout: 20_000,
  })
  await expect(page.getByText(expectedName, { exact: true }).first()).toBeVisible({
    timeout: 15_000,
  })
}

/**
 * Click Bootstrap on the curve editor and wait for the orchestrator
 * curve-preview response; returns the parsed response body.
 */
export async function bootstrapPreview(page: Page): Promise<Record<string, unknown>> {
  const respPromise = page.waitForResponse(
    (r) => r.url().includes('/v1/curve-preview') && r.request().method() === 'POST',
    { timeout: 60_000 },
  )
  // exact: the Construction toggle also carries a "Bootstrap …" label.
  await page.getByRole('button', { name: 'Bootstrap', exact: true }).click()
  const resp = await respPromise
  let body: Record<string, unknown> = {}
  try {
    body = (await resp.json()) as Record<string, unknown>
  } catch {
    /* keep {} */
  }
  return { __status: resp.status(), ...body }
}

// ---------------------------------------------------------------------------
// EUR pricing environment for the vol / swaption family
// ---------------------------------------------------------------------------

/**
 * Create a full EUR pricing environment: discount + forward curves whose
 * SwapHelpers reference the saved EURIBOR_6M index (this is what makes the
 * engine register EURIBOR_6M for vol sampling / swaption pricing), plus a
 * curve set holding both. Returns the entity names.
 */
export async function createEurEnvironment(page: Page, tag: string) {
  // Saved EURIBOR_6M index (a KNOWN backend catalog id) — create if missing.
  await gotoReady(page, '/indices')
  if ((await page.getByText('EURIBOR_6M', { exact: true }).count()) === 0) {
    await createIborIndex(page, {
      id: 'EURIBOR_6M',
      family: 'Euribor',
      tenorNumber: 6,
      tenorUnit: 'Months',
      calendar: 'TARGET',
      dayCounter: 'Actual360',
    })
  }

  const discName = `${tag} disc`
  const fwdName = `${tag} fwd`
  const setName = `${tag} set`

  const curveSpec = {
    currency: 'EUR',
    deposits: [
      { ratePct: 3.0, tenorNumber: 6, tenorUnit: 'Months' as const },
      { ratePct: 3.2, tenorNumber: 2, tenorUnit: 'Years' as const },
    ],
    swaps: [
      { ratePct: 3.5, tenorNumber: 10, floatIndexId: 'EURIBOR_6M' },
      { ratePct: 3.7, tenorNumber: 30, floatIndexId: 'EURIBOR_6M' },
    ],
  }
  await fillYieldCurveForm(page, { name: discName, ...curveSpec })
  await saveCurve(page, discName)

  await fillYieldCurveForm(page, { name: fwdName, ...curveSpec })
  await fieldFor(page, 'Curve Family').selectOption({ label: 'Forward (IBOR)' })
  await saveCurve(page, fwdName)

  // Curve set with both roles (the editor autosaves).
  await gotoReady(page, '/curve-sets')
  // The click POSTs a draft set and client-navigates to the minted id; wait
  // on the navigation itself (an id segment must appear) rather than racing
  // the POST round-trip with a loose URL assertion.
  await page.getByRole('button', { name: /New Curve Set/ }).click()
  await page.waitForURL(/\/curve-sets\/[^/]+$/, { timeout: 30_000 })
  await fieldFor(page, 'Name').fill(setName)
  await fieldFor(page, 'Currency').selectOption('EUR')
  await page.getByRole('button', { name: '+ Discount' }).click()
  await selectByTextContains(fieldFor(page, 'Standalone Curve'), discName)
  await page.getByRole('button', { name: '+ Forward' }).click()
  await selectByTextContains(fieldFor(page, 'Standalone Curve'), fwdName)
  // Give the autosave a beat.
  await page.waitForTimeout(1000)

  return { discName, fwdName, setName }
}

/**
 * Create a swaption vol surface on the workbench wired to the given curve
 * set (EUR_SWAP_6M / EURIBOR_6M). kind = visible "Swaption Surface Kind"
 * option label ('Constant' default, 'ATM Matrix', 'SABR Calibrate', ...).
 * For ATM Matrix the whole grid is filled with a non-flat vol ladder.
 * Returns the surface id.
 */
export async function createSwaptionSurface(
  page: Page,
  setName: string,
  kind?: 'ATM Matrix' | 'SABR Calibrate',
): Promise<string> {
  await gotoReady(page, '/vol-workbench')
  await page.getByRole('button', { name: 'New Surface' }).click()
  await expect(page.getByText(/Working on:/)).toBeVisible({ timeout: 15_000 })
  const working = (await page.getByText(/Working on:/).textContent()) ?? ''
  const volId = working.replace('Working on:', '').trim()
  if (kind) {
    await fieldFor(page, 'Swaption Surface Kind').selectOption({ label: kind })
  }
  await selectByTextContains(fieldFor(page, 'Curve Set'), setName)
  await selectByTextContains(fieldFor(page, 'Swap Index ID'), 'EUR_SWAP_6M')
  await selectByTextContains(fieldFor(page, 'Float Index ID'), 'EURIBOR_6M')
  if (kind === 'ATM Matrix') {
    // A fresh ATM matrix has axes but an EMPTY grid — fill every cell with a
    // non-flat vol ladder so it samples/calibrates.
    const cells = page.locator('table input')
    const count = await cells.count()
    expect(count, 'ATM matrix editor renders editable cells').toBeGreaterThan(20)
    for (let i = 0; i < count; i++) {
      await cells.nth(i).fill((0.01 + (i % 5) * 0.002).toFixed(3))
    }
  }
  await page.waitForTimeout(800)
  return volId
}
