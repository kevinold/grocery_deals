# CLAUDE.md — project context for AI coding agents

This file captures non-obvious constraints. Read it before making changes.

## What this project is

A single-file Python module (`grocery_deals.py`) that fetches weekly-ad sale
data from **Kroger and Publix via Flipp** and normalizes it into a uniform
`Deal` schema. It is meant to be called as a tool by an LLM agent.

A thin sibling script `grocery_deals_mcp.py` wraps the public API as an
MCP server (FastMCP over stdio) for clients like zeroclaw. The wrapper
has its own inline deps (`mcp`); the core module must not import `mcp`.

## Architecture, in one paragraph

Both retailers publish their weekly ads through Flipp
(`backflipp.wishabi.com`). We hit the public Flipp search endpoint once per
(ZIP, merchant), cache it for 6 hours, and filter the result set in Python —
by merchant first, then by the caller's optional keyword. No auth, no OAuth,
no credentials. `q=publix` and `q=kroger` both return the full flyer for that
retailer at a given ZIP.

## Hard rules — do not violate

1. **Do not scrape `publix.com` or `kroger.com` directly.** Publix sits
   behind Akamai's WAF and 403s automated traffic; kroger.com has similar
   protections. Flipp is the supported backend for both.
2. **Do not remove the disk cache, and do not add `query` to the flyer cache
   key.** The cache key is `(ZIP, merchant)` on purpose: repeated calls with
   different keywords against the same store must share one network fetch.
   Filtering happens client-side in `_deals_for`.
3. **Do not add Playwright / Selenium / a headless browser.** The module
   must stay `requests`-only and importable in serverless / agent contexts.
4. **Do not delete existing fallback keys in `_parse_flipp_item`.** Flipp
   renames JSON fields periodically. The `_first(item, "a", "b", "c")`
   pattern is intentional: when Flipp introduces a new key, *add* it to the
   front of the list — never drop the older keys, since cached responses and
   region variants still use them.
5. **Do not break the public API surface.** `Deal`, `get_publix_deals`,
   `get_kroger_deals`, `search_across` are exported in `__all__` and consumed
   by agent tool definitions *and* by `grocery_deals_mcp.py`. Changing a
   signature here means updating the MCP tool wrapper too.
6. **Do not add a Kroger OAuth / Products-API path back.** The project was
   originally dual-sourced (Kroger official API + Publix via Flipp) and was
   intentionally consolidated onto Flipp for uniform caching, fewer
   credentials, and one-fetch-per-store semantics.

## Where things commonly break

- **Flipp field drift** → `_parse_flipp_item`. Symptom: deals come back with
  empty `product_name` or `None` prices. Fix: inspect the live response shape
  and add new keys to the relevant `_first(...)` call. Keep the old keys.
- **Merchant string drift** → `_merchant_matches`. Flipp can return
  `"Publix"`, `"PUBLIX SUPER MARKETS"`, `"Kroger"`, etc. We match on a
  lowercased substring token (`publix`, `kroger`). Don't tighten to equality.
- **Mixed-merchant results** → Flipp search returns items for every retailer
  in the ZIP. The merchant filter in `_fetch_flyer` is load-bearing; don't
  remove it.

## Promo classification

`classify_promo()` runs Flipp's `pre_price_text`, `sale_story`, and
`post_price_text` through ordered regexes. Order matters: bogo → multi_buy →
amount_off → percent_off → sale. If you add a new promo type, put it before
the more general ones.

## Tests

`pytest`, no network. Patch `_HTTP` (or `_flipp_get` / `_fetch_flyer`) —
never let tests hit real endpoints.

## Adding new retailers

Follow the same pattern: add a merchant token constant, a display-name entry
in `_display_retailer`, and a public `get_x_deals(zip_code, query=None)`
wrapper around `_deals_for`. Don't add per-retailer fields to `Deal`; stuff
retailer-specific extras into `promo_text` or document them in this file.
