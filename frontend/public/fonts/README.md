# Vendored fonts

Self-hosted so the app makes no third-party font requests.

| Family | Files | Source | License |
|--------|-------|--------|---------|
| Outfit (variable, wght 300–700) | `outfit-latin-var.woff2`, `outfit-latin-ext-var.woff2` | [Google Fonts](https://fonts.google.com/specimen/Outfit) | [SIL OFL 1.1](https://openfontlicense.org) |
| JetBrains Mono (variable, wght 100–800) | `jetbrains-mono-latin-var.woff2`, `jetbrains-mono-latin-ext-var.woff2` | [Google Fonts](https://fonts.google.com/specimen/JetBrains+Mono) | [SIL OFL 1.1](https://openfontlicense.org) |

Only the latin and latin-ext subsets are shipped; other scripts fall back to
system fonts. `@font-face` rules live in `src/index.css`.
