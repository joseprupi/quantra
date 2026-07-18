/**
 * Tiny direct-API client for the parity oracle and stack readiness.
 *
 * Talks to the orchestrator THROUGH the portal's same-origin proxy (the
 * browser's exact path). Dev-auth bypass is on in the stack, so no headers.
 */
import { APIRequestContext, Page, Request as PwRequest, expect } from '@playwright/test'
import { PORTAL_URL } from './stack'
import { expectRelClose, parseUiNumber } from './ui'

export async function apiGet(request: APIRequestContext, path: string): Promise<unknown> {
  const res = await request.get(`${PORTAL_URL}${path}`)
  expect(res.ok(), `GET ${path} -> ${res.status()}`).toBe(true)
  return res.json()
}

export async function apiPost(
  request: APIRequestContext,
  path: string,
  body: unknown,
): Promise<{ status: number; json: Record<string, unknown> }> {
  const res = await request.post(`${PORTAL_URL}${path}`, { data: body })
  let json: Record<string, unknown> = {}
  try {
    json = (await res.json()) as Record<string, unknown>
  } catch {
    /* non-JSON body */
  }
  return { status: res.status(), json }
}

export interface PricedExchange {
  requestBody: Record<string, unknown>
  responseBody: Record<string, unknown>
  status: number
  path: string
}

/**
 * Arm a one-shot capture of the next `POST /v1/price/<product>` (or any
 * /v1/... path) issued by the page, returning both wire bodies.
 */
export function capturePricing(page: Page, pathPart: string): Promise<PricedExchange> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`No ${pathPart} request within 60s`)),
      60_000,
    )
    const onRequest = (req: PwRequest) => {
      if (!req.url().includes(pathPart) || req.method() !== 'POST') return
      page.off('request', onRequest)
      void (async () => {
        try {
          const requestBody = JSON.parse(req.postData() ?? '{}') as Record<string, unknown>
          const res = await req.response()
          if (!res) throw new Error(`${pathPart}: request has no response`)
          const status = res.status()
          let responseBody: Record<string, unknown> = {}
          try {
            responseBody = (await res.json()) as Record<string, unknown>
          } catch {
            /* non-JSON */
          }
          clearTimeout(timer)
          resolve({ requestBody, responseBody, status, path: new URL(req.url()).pathname })
        } catch (e) {
          clearTimeout(timer)
          reject(e as Error)
        }
      })()
    }
    page.on('request', onRequest)
  })
}

function extractNpv(responseBody: Record<string, unknown>): number {
  const result = responseBody.result as Record<string, unknown> | undefined
  const npv = result?.npv
  expect(typeof npv, `result.npv present (got ${JSON.stringify(responseBody).slice(0, 300)})`).toBe(
    'number',
  )
  return npv as number
}

/**
 * The parity oracle: replay the exact request body the UI sent straight
 * against the orchestrator and compare NPVs (default tol 1e-6 relative).
 * Also checks the UI-displayed NPV text against the wire NPV.
 */
export async function assertPricingParity(
  request: APIRequestContext,
  exchange: PricedExchange,
  uiNpvText: string,
  opts: { tol?: number; uiDecimals?: number } = {},
): Promise<{ uiNpv: number; apiNpv: number }> {
  const tol = opts.tol ?? 1e-6
  expect(exchange.status, `UI pricing call ${exchange.path} status`).toBe(200)
  const uiWireNpv = extractNpv(exchange.responseBody)

  // 1. displayed text == wire NPV (to the displayed precision)
  const shown = parseUiNumber(uiNpvText)
  const decimals = opts.uiDecimals ?? 2
  expect(
    Math.abs(shown - uiWireNpv),
    `UI shows ${shown}, wire says ${uiWireNpv}`,
  ).toBeLessThanOrEqual(0.51 * Math.pow(10, -decimals) + 1e-9)

  // 2. independent API replay of the same body == wire NPV (rel tol)
  const replay = await apiPost(request, exchange.path, exchange.requestBody)
  expect(replay.status, `replay POST ${exchange.path} -> ${JSON.stringify(replay.json).slice(0, 300)}`).toBe(200)
  const apiNpv = extractNpv(replay.json)
  expectRelClose(uiWireNpv, apiNpv, tol, `NPV parity UI-wire vs direct API (${exchange.path})`)
  return { uiNpv: uiWireNpv, apiNpv }
}

/** Poll until the stack has real market data (latest-date non-null). */
export async function waitForRealData(request: APIRequestContext, timeoutMs = 180_000) {
  await expect
    .poll(
      async () => {
        try {
          const res = await request.get(`${PORTAL_URL}/v1/market-data/latest-date`)
          if (!res.ok()) return null
          const body = (await res.json()) as { latest_date?: string | null }
          return body.latest_date ?? null
        } catch {
          return null
        }
      },
      { timeout: timeoutMs, intervals: [2_000], message: 'real market data to land (latest-date)' },
    )
    .not.toBeNull()
}
