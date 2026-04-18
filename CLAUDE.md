# CLAUDE.md — project context for AI coding agents

This file captures non-obvious constraints. Read it before making changes.

## What this project is

A single-file Python module (`grocery_deals.py`) that fetches weekly-ad sale
data from Kroger and Publix and normalizes it into a uniform `Deal` schema.
It is meant to be called as a tool by an LLM agent.

## Architecture, in one paragraph

- **Kroger**: official Products API at `api.kroger.com/v1`. OAuth2
  client-credentials, scope `product.compact`. Token is cached in-process and
  refreshed 60s before expiry.
- **Publix**: Flipp public backend (`backflipp.wishabi.com`). Publix
  officially publishes its weekly ad through Flipp. No auth, ZIP-localized.
  Search returns items for many retailers, so we filter `merchant == Publix`.

## Hard rules — do not violate

1. **Do not switch Publix to scraping `publix.com`.** It sits behind Akamai's
   WAF and 403s automated traffic. Flipp is the supported backend.
2. **Do not remove the disk cache.** Publix flyers cycle Wed/Thu so 6h is
   safe; Kroger 30 min keeps us under quota. Removing the cache will get
   real users rate-limited.
3. **Do not add Playwright / Selenium / a headless browser.** The module
   must stay `requests`-only and importable in serverless / agent contexts.
4. **Do not delete existing fallback keys in `_parse_flipp_item`.** Flipp
   renames JSON fields periodically. The `_first(item, "a", "b", "c")`
   pattern is intentional: when Flipp introduces a new key, *add* it to the
   front of the list — never drop the older keys, since cached responses and
   region variants still use them.
5. **Do not break the public API surface.** `Deal`, `get_publix_deals`,
   `get_kroger_deals`, `find_kroger_location`, `search_across` are exported
   in `__all__` and consumed by agent tool definitions.

## Where things commonly break

- **Flipp field drift** → `_parse_flipp_item`. Symptom: deals come back with
  empty `product_name` or `None` prices. Fix: inspect the live response shape
  and add new keys to the relevant `_first(...)` call. Keep the old keys.
- **Kroger `price.promo == 0`** means "no active promo". `_normalize_kroger_promo`
  collapses that to `None`. Don't treat 0 as a sale price.
- **Kroger 401** after a long-lived process: token expired mid-request. The
  client retries once after forcing a refresh — don't add another retry layer.

## Promo classification

`classify_promo()` runs Flipp's `pre_price_text`, `sale_story`, and
`post_price_text` through ordered regexes. Order matters: bogo → multi_buy →
amount_off → percent_off → sale. If you add a new promo type, put it before
the more general ones.

## Tests

`pytest`, no network. Patch `_HTTP` (or `_flipp_get` / `_kroger_get`) — never
let tests hit real endpoints.

## Adding new retailers

Follow the same pattern: a `_parse_x_item` mapper that produces a `Deal`, a
public `get_x_deals(...)` function, cache namespace + TTL, and a CLI subcommand.
Don't add per-retailer fields to `Deal`; stuff retailer-specific extras into
`promo_text` or document them in this file.
