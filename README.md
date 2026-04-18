# grocery_deals

A single-file Python module that fetches real-time weekly-ad sale data from
**Kroger** and **Publix** via [Flipp](https://backflipp.wishabi.com/) — the
backend where both retailers officially publish their flyers. Designed to be
wired up as a tool for an LLM agent.

One network fetch per `(ZIP, merchant)`, cached 6 hours. Repeated calls with
different keywords against the same store share that cached fetch — the
keyword filter is applied in Python.

## Install

```bash
python3 -m pip install -r requirements.txt
```

Python 3.10+. The only runtime dependency is `requests`. No credentials, no
env vars.

## Python API

```python
from grocery_deals import (
    Deal,
    get_publix_deals,
    get_kroger_deals,
    search_across,
)

# All items in this week's Kroger flyer for ZIP 45202
kroger = get_kroger_deals("45202")

# Filter to milk — no extra network call, served from the same cached flyer
milk = get_kroger_deals("45202", "milk")

# Publix BOGOs
publix = get_publix_deals("33486")
bogos = [d for d in publix if d.promo_type == "bogo"]

# Both retailers, one ZIP, one query
both = search_across("chicken", zip_code="33486")

for d in both:
    print(d.to_dict())
```

### `Deal`

```
retailer, store_id, product_name, brand, size,
regular_price, sale_price, savings,
promo_type      # bogo | amount_off | percent_off | multi_buy | sale
promo_text, valid_from, valid_to,
image_url, source_id
```

`Deal.to_dict()` returns a JSON-serializable dict.

### Function signatures

```python
get_publix_deals(zip_code, query=None, *, only_on_sale=False, hydrate=False)
get_kroger_deals(zip_code, query=None, *, only_on_sale=False, hydrate=False)
search_across(query, *, zip_code, only_on_sale=True)
```

`hydrate=True` issues a per-item detail fetch for each result to pick up
precise regular-price and valid-from/valid-to fields.

## CLI

```bash
python -m grocery_deals kroger --zip 45202 --query milk
python -m grocery_deals publix --zip 33486 --bogo-only
python -m grocery_deals search --zip 33486 --query bread --json
```

Add `--json` to any subcommand for machine-readable output. `--on-sale`
drops items without an identifiable promo.

## Caching

Responses are cached to `~/.cache/grocery_deals/` with a 6h TTL (flyers
cycle Wed/Thu). Delete the directory to force-refresh.

## Tests

```bash
python3 -m pip install pytest
pytest -q
```

Tests are network-free.
