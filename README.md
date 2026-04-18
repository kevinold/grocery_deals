# grocery_deals

A single-file Python module that fetches real-time weekly-ad sale data from
**Kroger** (official Products API) and **Publix** (via Flipp's public backend,
where Publix officially publishes its weekly ad). Designed to be wired up as a
tool for an LLM agent.

## Install

```bash
python3 -m pip install -r requirements.txt
```

Python 3.10+. The only runtime dependency is `requests`.

## Credentials

Kroger uses OAuth2 client-credentials. Get a free key at
<https://developer.kroger.com> and export:

```bash
export KROGER_CLIENT_ID=...
export KROGER_CLIENT_SECRET=...
```

Publix needs no credentials.

## Python API

```python
from grocery_deals import (
    Deal,
    get_publix_deals,
    get_kroger_deals,
    find_kroger_location,
    search_across,
)

# Resolve a ZIP to a Kroger locationId
locs = find_kroger_location("45202")
loc_id = locs[0]["location_id"]

kroger_deals = get_kroger_deals(loc_id, "milk", only_on_sale=True)
publix_deals = get_publix_deals("33486", query="chicken")

# Cross-retailer search
both = search_across(
    "chicken",
    kroger_location_id=loc_id,
    publix_zip="33486",
)

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

## CLI

```bash
python -m grocery_deals find-kroger --zip 45202
python -m grocery_deals kroger --location-id 01400376 --query milk
python -m grocery_deals publix --zip 33486 --query chicken --bogo-only
python -m grocery_deals search --query bread \
    --kroger-location-id 01400376 --publix-zip 33486 --json
```

Add `--json` to any subcommand for machine-readable output.

## Caching

Responses are cached to `~/.cache/grocery_deals/`:

- Publix: 6h (flyer cycles Wed/Thu)
- Kroger: 30 min

Delete the directory to force-refresh.

## Tests

```bash
python3 -m pip install pytest
pytest -q
```

Tests are network-free.
