/**
 * Small UI utilities shared by the full-suite page journeys.
 *
 * The portal's forms are label-above-input without htmlFor associations, so
 * `getByLabel` does not work; `fieldFor` resolves the first input/select that
 * FOLLOWS a given label text in document order, optionally scoped to a
 * container locator.
 */
import { Locator, Page, expect } from '@playwright/test'

type Scope = Page | Locator

function xpathLiteral(s: string): string {
  if (!s.includes('"')) return `"${s}"`
  if (!s.includes("'")) return `'${s}'`
  return `concat("${s.split('"').join(`", '"', "`)}")`
}

/**
 * The first <input>/<select>/<textarea> following a <label> whose normalized
 * text equals `label`. Works for the portal's `<label>X</label><input/>`
 * and `<label>X</label><div><input/><select/></div>` layouts.
 */
export function fieldFor(scope: Scope, label: string, nth = 0): Locator {
  const lit = xpathLiteral(label)
  return scope
    .locator(
      `xpath=.//label[normalize-space(.)=${lit}]/following::*[self::input or self::select or self::textarea][1]`,
    )
    .nth(nth)
}

/** Both tenor controls (number + unit select) following a "Tenor" label. */
export function tenorFieldsFor(scope: Scope, label = 'Tenor', nth = 0) {
  const lit = xpathLiteral(label)
  const base = scope.locator(
    `xpath=.//label[normalize-space(.)=${lit}]/following::*[self::input or self::select][position() <= 2]`,
  )
  return { number: base.nth(nth * 2), unit: base.nth(nth * 2 + 1) }
}

/** Auto-accept every native confirm()/alert() for the page. */
export function autoAcceptDialogs(page: Page) {
  page.on('dialog', (d) => {
    void d.accept()
  })
}

/**
 * Navigate and wait for the app shell to be ready (the ProtectedRoute
 * "Loading..." spinner gone and the header rendered).
 */
export async function gotoReady(page: Page, path: string) {
  await page.goto(path)
  await expect(page.locator('header')).toBeVisible({ timeout: 20_000 })
  await expect(page.getByText('Loading...', { exact: true })).toHaveCount(0, { timeout: 20_000 })
}

/** Parse a UI-formatted number like "1,234,567.89" or "-988.03". */
export function parseUiNumber(text: string): number {
  return parseFloat(text.replace(/,/g, '').trim())
}

/** Relative closeness assert (|a-b| <= tol * max(1, |a|, |b|)). */
export function expectRelClose(a: number, b: number, tol = 1e-6, what = 'value') {
  const denom = Math.max(1, Math.abs(a), Math.abs(b))
  const rel = Math.abs(a - b) / denom
  expect(rel, `${what}: |${a} - ${b}| rel=${rel} > tol=${tol}`).toBeLessThanOrEqual(tol)
}

/**
 * The CurveSetSelector widget under a given label: returns its two selects.
 * DOM: <div><label>{label}</label><div class="space-y-2"><select set/><select curve/></div></div>
 */
export function curveSetSelector(scope: Scope, label: string) {
  const lit = xpathLiteral(label)
  const selects = scope.locator(
    `xpath=.//label[normalize-space(.)=${lit}]/following-sibling::div//select`,
  )
  return { setSelect: selects.nth(0), curveSelect: selects.nth(1) }
}

/**
 * Pick a STANDALONE saved curve (by visible option substring) in a
 * CurveSetSelector. The widget defaults to the standalone list when no curve
 * set is selected; we force `__standalone__` first to be deterministic.
 */
export async function pickStandaloneCurve(scope: Scope, label: string, curveNamePart: string) {
  const { setSelect, curveSelect } = curveSetSelector(scope, label)
  await setSelect.selectOption('__standalone__')
  await selectByTextContains(curveSelect, curveNamePart)
}

/** Pick a curve set by visible option substring, then (optionally) a curve. */
export async function pickCurveFromSet(
  scope: Scope,
  label: string,
  setNamePart: string,
  curveNamePart?: string,
) {
  const { setSelect, curveSelect } = curveSetSelector(scope, label)
  await selectByTextContains(setSelect, setNamePart)
  if (curveNamePart) await selectByTextContains(curveSelect, curveNamePart)
}

/** selectOption by option-text substring (the UI decorates option labels). */
export async function selectByTextContains(select: Locator, part: string) {
  await expect
    .poll(
      async () =>
        (await select.locator('option').allTextContents()).some((t) => t.includes(part)),
      { timeout: 15_000, message: `option containing "${part}" to appear` },
    )
    .toBe(true)
  const options = await select.locator('option').all()
  for (const opt of options) {
    const text = (await opt.textContent()) ?? ''
    if (text.includes(part)) {
      const value = await opt.getAttribute('value')
      await select.selectOption(value ?? { label: text })
      return
    }
  }
  throw new Error(`No option containing "${part}"`)
}
