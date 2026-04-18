# grocery_deals

A single-file Python module that fetches real-time weekly-ad sale data from
**Kroger** and **Publix** via [Flipp](https://backflipp.wishabi.com/) — the
backend where both retailers officially publish their flyers. Designed to be
wired up as a tool for an LLM agent.

One network fetch per `(ZIP, merchant)`, cached 6 hours. Repeated calls with
different keywords against the same store share that cached fetch — the
keyword filter is applied in Python.

## Install

The script declares its dependencies inline (PEP 723), so with
[`uv`](https://docs.astral.sh/uv/) no separate install step is needed —
`uv` provisions an ephemeral environment on first run and caches it:

```bash
./grocery_deals.py kroger --zip 45202 --query milk
# or, equivalently:
uv run grocery_deals.py kroger --zip 45202 --query milk
```

To import it as a library, install the runtime dep into your own env:

```bash
uv pip install -r requirements.txt   # or: python3 -m pip install -r requirements.txt
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
./grocery_deals.py kroger --zip 45202 --query milk
./grocery_deals.py publix --zip 33486 --bogo-only
./grocery_deals.py search --zip 33486 --query bread --json
```

Or via `uv run` / `python -m` if preferred:

```bash
uv run grocery_deals.py kroger --zip 45202 --query milk
python -m grocery_deals search --zip 33486 --query bread --json
```

Add `--json` to any subcommand for machine-readable output. `--on-sale`
drops items without an identifiable promo.

## MCP server

`grocery_deals_mcp.py` exposes the public API as [Model Context
Protocol](https://modelcontextprotocol.io/) tools over stdio, so an
agent (zeroclaw, Claude Desktop, etc.) can call them. It's a separate
uv-inline script — the core module stays `requests`-only.

Tools exposed:

- `publix_deals(zip_code, query=None, only_on_sale=False, hydrate=False)`
- `kroger_deals(zip_code, query=None, only_on_sale=False, hydrate=False)`
- `search_deals(query, zip_code, only_on_sale=True)`

Each returns a list of `Deal` dicts (same shape as `Deal.to_dict()`).

Inspect it locally with the MCP dev UI:

```bash
uv run --with "mcp[cli]" mcp dev ./grocery_deals_mcp.py
```

### Register with zeroclaw

Add to `~/.zeroclaw/config.toml`:

```toml
[mcp]
enabled = true

[[mcp.servers]]
name = "grocery_deals"
transport = "stdio"
command = "/Users/kevinold/projects/grocery_deals/grocery_deals_mcp.py"
args = []
tool_timeout_secs = 30
```

The shebang (`#!/usr/bin/env -S uv run --script`) lets zeroclaw launch
the script directly; uv provisions the ephemeral env on first run and
caches it. If your launchd environment doesn't see `uv` on `PATH`, use
an absolute `uv` path instead:

```toml
command = "/opt/homebrew/bin/uv"
args = ["run", "--script", "/Users/kevinold/projects/grocery_deals/grocery_deals_mcp.py"]
```

Restart the daemon, then ask the agent something like *"use grocery_deals
to find chicken deals at 45202"* — the LLM will see
`grocery_deals__search_deals` and friends as callable tools.

## Caching

Responses are cached to `~/.cache/grocery_deals/` with a 6h TTL (flyers
cycle Wed/Thu). Delete the directory to force-refresh.

## Tests

```bash
uv run --with pytest --with requests pytest -q
# or, classically:
python3 -m pip install pytest requests && pytest -q
```

Tests are network-free.
