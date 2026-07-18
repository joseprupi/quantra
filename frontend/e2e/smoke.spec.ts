import { test, expect } from '@playwright/test'

test('app loads: page title set and login header visible', async ({ page }) => {
  await page.goto('/login')

  // Title is in index.html — available immediately, independent of React/Firebase.
  await expect(page).toHaveTitle('Quantra Portal')

  // Login page header text — appears once Firebase onAuthStateChanged(null) fires.
  await expect(
    page.getByText('Open-source derivatives pricing platform')
  ).toBeVisible({ timeout: 15_000 })
})
