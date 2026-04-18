"""Fetch real-time weekly-ad sale data from Kroger and Publix.

Designed to be used as a tool by an LLM agent. See README.md and CLAUDE.md
for usage details and architectural constraints.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

__all__ = [
    "Deal",
    "get_publix_deals",
    "get_kroger_deals",
    "find_kroger_location",
    "search_across",
]

log = logging.getLogger("grocery_deals")

KROGER_API = "https://api.kroger.com/v1"
KROGER_TOKEN_URL = f"{KROGER_API}/connect/oauth2/token"
KROGER_PRODUCTS_URL = f"{KROGER_API}/products"
KROGER_LOCATIONS_URL = f"{KROGER_API}/locations"

FLIPP_SEARCH_URL = "https://backflipp.wishabi.com/flipp/items/search"
FLIPP_ITEM_URL = "https://backflipp.wishabi.com/flipp/items/{item_id}"

PUBLIX_MERCHANT = "publix"

CACHE_DIR = Path(os.path.expanduser("~/.cache/grocery_deals"))
PUBLIX_TTL = 6 * 60 * 60          # 6 hours
KROGER_TTL = 30 * 60              # 30 minutes
TOKEN_REFRESH_SKEW = 60           # refresh 60s before expiry

USER_AGENT = "grocery_deals/0.1 (+https://github.com/kevinold/grocery_deals)"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Deal:
    retailer: str
    store_id: str | None
    product_name: str
    brand: str | None
    size: str | None
    regular_price: float | None
    sale_price: float | None
    savings: float | None
    promo_type: str          # bogo|amount_off|percent_off|multi_buy|sale
    promo_text: str | None
    valid_from: str | None
    valid_to: str | None
    image_url: str | None
    source_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# HTTP session with retries
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


_HTTP = _session()


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------

def _cache_key(namespace: str, params: dict[str, Any]) -> Path:
    blob = json.dumps({"ns": namespace, "p": params}, sort_keys=True, default=str)
    digest = hashlib.sha1(blob.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{namespace}_{digest}.json"


def _cache_get(path: Path, ttl: int) -> Any | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    if time.time() - st.st_mtime > ttl:
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _cache_put(path: Path, data: Any) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except OSError as exc:
        log.warning("cache write failed: %s", exc)


# ---------------------------------------------------------------------------
# Promo classification
# ---------------------------------------------------------------------------

_BOGO_RX = re.compile(
    r"\b(bogo|b1g1|buy\s*one\s*get\s*one|buy\s*1\s*get\s*1)\b", re.I
)
_MULTI_BUY_RX = re.compile(
    r"(\d+\s*(?:for|/)\s*\$?\d+|\bbuy\s*\d+\s*get\s*\d+)", re.I
)
_AMOUNT_OFF_RX = re.compile(r"save\s*\$?\s*\d+(?:\.\d+)?", re.I)
_PERCENT_OFF_RX = re.compile(r"\d+\s*%\s*off", re.I)


def classify_promo(*texts: str | None) -> str:
    """Return one of bogo|multi_buy|amount_off|percent_off|sale."""
    blob = " ".join(t for t in texts if t).strip()
    if not blob:
        return "sale"
    if _BOGO_RX.search(blob):
        return "bogo"
    if _MULTI_BUY_RX.search(blob):
        return "multi_buy"
    if _AMOUNT_OFF_RX.search(blob):
        return "amount_off"
    if _PERCENT_OFF_RX.search(blob):
        return "percent_off"
    return "sale"


# ---------------------------------------------------------------------------
# Publix (via Flipp public backend)
# ---------------------------------------------------------------------------

def _flipp_get(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    r = _HTTP.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first present non-empty value for any of the given keys.

    Flipp's JSON field names drift over time; this lets us add fallback keys
    without removing the old ones (see CLAUDE.md).
    """
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return default


def _to_float(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_flipp_item(item: dict[str, Any]) -> Deal:
    """Map a Flipp item dict to a Deal.

    NOTE: Flipp renames fields periodically. Keep all existing fallback keys
    when adding new ones.
    """
    pre = _first(item, "pre_price_text", "prePriceText", default="") or ""
    story = _first(item, "sale_story", "saleStory", default="") or ""
    post = _first(item, "post_price_text", "postPriceText", default="") or ""

    promo_text = " ".join(s for s in (pre, story, post) if s).strip() or None
    promo_type = classify_promo(pre, story, post)

    sale_price = _to_float(
        _first(item, "current_price", "price", "sale_price")
    )
    regular_price = _to_float(
        _first(item, "original_price", "regular_price", "was_price", "list_price")
    )
    savings = None
    if regular_price is not None and sale_price is not None:
        diff = round(regular_price - sale_price, 2)
        if diff > 0:
            savings = diff

    return Deal(
        retailer="Publix",
        store_id=str(_first(item, "merchant_id", "store_id", default="") or "") or None,
        product_name=str(_first(item, "name", "title", "display_name", default="")),
        brand=_first(item, "brand", "manufacturer"),
        size=_first(item, "sku_description", "description", "size"),
        regular_price=regular_price,
        sale_price=sale_price,
        savings=savings,
        promo_type=promo_type,
        promo_text=promo_text,
        valid_from=_first(item, "valid_from", "validFrom", "start_date"),
        valid_to=_first(item, "valid_to", "validTo", "end_date"),
        image_url=_first(item, "clipping_image_url", "image_url", "thumbnail_url"),
        source_id=str(_first(item, "flyer_item_id", "id", default="") or "") or None,
    )


def _is_publix(item: dict[str, Any]) -> bool:
    merchant = (_first(item, "merchant", "merchant_name", default="") or "").lower()
    return PUBLIX_MERCHANT in merchant


def get_publix_deals(
    zip_code: str,
    query: str | None = None,
    *,
    hydrate: bool = False,
) -> list[Deal]:
    """Fetch Publix weekly-ad deals via the Flipp public backend.

    Args:
        zip_code: 5-digit ZIP code used to localize the flyer.
        query: Optional keyword filter; when omitted the full Publix flyer is
            returned (q="publix").
        hydrate: When True, fetch per-item detail for each result to populate
            precise regular_price and valid_from/valid_to fields.
    """
    if not zip_code or not zip_code.strip():
        raise ValueError("zip_code is required")

    q = query.strip() if query else "publix"
    params = {"locale": "en-us", "postal_code": zip_code, "q": q}
    cache_path = _cache_key("publix_search", params)

    payload = _cache_get(cache_path, PUBLIX_TTL)
    if payload is None:
        payload = _flipp_get(FLIPP_SEARCH_URL, params=params)
        _cache_put(cache_path, payload)

    items = payload.get("items") or payload.get("results") or []
    publix_items = [it for it in items if _is_publix(it)]

    deals = [_parse_flipp_item(it) for it in publix_items]

    if hydrate:
        deals = [_hydrate_publix_deal(d) for d in deals]
    return deals


def _hydrate_publix_deal(deal: Deal) -> Deal:
    if not deal.source_id:
        return deal
    cache_path = _cache_key("publix_item", {"id": deal.source_id})
    payload = _cache_get(cache_path, PUBLIX_TTL)
    if payload is None:
        try:
            payload = _flipp_get(FLIPP_ITEM_URL.format(item_id=deal.source_id))
            _cache_put(cache_path, payload)
        except requests.RequestException as exc:
            log.warning("hydrate failed for %s: %s", deal.source_id, exc)
            return deal
    item = payload.get("item") or payload
    return _parse_flipp_item(item)


# ---------------------------------------------------------------------------
# Kroger (official Products API)
# ---------------------------------------------------------------------------

class _KrogerToken:
    def __init__(self) -> None:
        self.access_token: str | None = None
        self.expires_at: float = 0.0

    def valid(self) -> bool:
        return bool(self.access_token) and time.time() < (self.expires_at - TOKEN_REFRESH_SKEW)


_KROGER_TOKEN = _KrogerToken()


def _kroger_credentials() -> tuple[str, str]:
    cid = os.environ.get("KROGER_CLIENT_ID")
    sec = os.environ.get("KROGER_CLIENT_SECRET")
    if not cid or not sec:
        raise RuntimeError(
            "KROGER_CLIENT_ID and KROGER_CLIENT_SECRET env vars are required "
            "(free signup at https://developer.kroger.com)"
        )
    return cid, sec


def _kroger_token() -> str:
    if _KROGER_TOKEN.valid():
        return _KROGER_TOKEN.access_token  # type: ignore[return-value]
    cid, sec = _kroger_credentials()
    r = _HTTP.post(
        KROGER_TOKEN_URL,
        auth=(cid, sec),
        data={"grant_type": "client_credentials", "scope": "product.compact"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    r.raise_for_status()
    body = r.json()
    _KROGER_TOKEN.access_token = body["access_token"]
    _KROGER_TOKEN.expires_at = time.time() + float(body.get("expires_in", 1800))
    return _KROGER_TOKEN.access_token  # type: ignore[return-value]


def _kroger_get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    token = _kroger_token()
    r = _HTTP.get(
        url,
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    if r.status_code == 401:
        # Force refresh and retry once
        _KROGER_TOKEN.access_token = None
        token = _kroger_token()
        r = _HTTP.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
    r.raise_for_status()
    return r.json()


def find_kroger_location(zip_code: str, limit: int = 5) -> list[dict[str, Any]]:
    """Translate a ZIP code into one or more Kroger locationId values."""
    if not zip_code or not zip_code.strip():
        raise ValueError("zip_code is required")
    params = {"filter.zipCode.near": zip_code, "filter.limit": limit}
    cache_path = _cache_key("kroger_loc", params)
    payload = _cache_get(cache_path, KROGER_TTL)
    if payload is None:
        payload = _kroger_get(KROGER_LOCATIONS_URL, params=params)
        _cache_put(cache_path, payload)

    out: list[dict[str, Any]] = []
    for loc in payload.get("data", []):
        addr = loc.get("address", {}) or {}
        out.append({
            "location_id": loc.get("locationId"),
            "name": loc.get("name"),
            "chain": loc.get("chain"),
            "address": addr.get("addressLine1"),
            "city": addr.get("city"),
            "state": addr.get("state"),
            "zip": addr.get("zipCode"),
        })
    return out


def _normalize_kroger_promo(price: dict[str, Any]) -> float | None:
    promo = price.get("promo")
    if promo in (None, 0, 0.0, "0", "0.0"):
        return None
    try:
        return float(promo)
    except (TypeError, ValueError):
        return None


def _parse_kroger_product(prod: dict[str, Any], location_id: str) -> Deal | None:
    items = prod.get("items") or []
    if not items:
        return None
    item = items[0]
    price = item.get("price") or {}
    regular = _to_float(price.get("regular"))
    sale = _normalize_kroger_promo(price)

    savings = None
    if regular is not None and sale is not None:
        diff = round(regular - sale, 2)
        if diff > 0:
            savings = diff

    if sale is not None:
        promo_type = "sale"
        promo_text = f"On sale at ${sale:.2f}"
    else:
        promo_type = "sale"
        promo_text = None

    images = prod.get("images") or []
    image_url = None
    for img in images:
        for size in img.get("sizes") or []:
            if size.get("url"):
                image_url = size["url"]
                break
        if image_url:
            break

    return Deal(
        retailer="Kroger",
        store_id=location_id,
        product_name=prod.get("description") or "",
        brand=prod.get("brand"),
        size=item.get("size"),
        regular_price=regular,
        sale_price=sale,
        savings=savings,
        promo_type=promo_type,
        promo_text=promo_text,
        valid_from=None,
        valid_to=None,
        image_url=image_url,
        source_id=prod.get("productId"),
    )


def get_kroger_deals(
    location_id: str,
    query: str,
    *,
    only_on_sale: bool = True,
    limit: int = 50,
) -> list[Deal]:
    """Fetch Kroger products for a query at a given store location."""
    if not location_id or not location_id.strip():
        raise ValueError("location_id is required")
    if not query or not query.strip():
        raise ValueError("query is required")
    limit = max(1, min(int(limit), 50))

    params = {
        "filter.term": query,
        "filter.locationId": location_id,
        "filter.limit": limit,
    }
    cache_path = _cache_key("kroger_prod", params)
    payload = _cache_get(cache_path, KROGER_TTL)
    if payload is None:
        payload = _kroger_get(KROGER_PRODUCTS_URL, params=params)
        _cache_put(cache_path, payload)

    deals: list[Deal] = []
    for prod in payload.get("data", []):
        deal = _parse_kroger_product(prod, location_id)
        if deal is None:
            continue
        if only_on_sale and deal.sale_price is None:
            continue
        deals.append(deal)
    return deals


# ---------------------------------------------------------------------------
# Cross-retailer search
# ---------------------------------------------------------------------------

def search_across(
    query: str,
    *,
    kroger_location_id: str | None,
    publix_zip: str | None,
    only_on_sale: bool = True,
) -> list[Deal]:
    """Run the same query against Kroger and Publix and return combined deals."""
    if not query or not query.strip():
        raise ValueError("query is required")
    deals: list[Deal] = []
    if kroger_location_id:
        try:
            deals.extend(get_kroger_deals(
                kroger_location_id, query, only_on_sale=only_on_sale
            ))
        except Exception as exc:  # noqa: BLE001 - surface partial results
            log.warning("kroger fetch failed: %s", exc)
    if publix_zip:
        try:
            publix_deals = get_publix_deals(publix_zip, query=query)
            if only_on_sale:
                publix_deals = [
                    d for d in publix_deals
                    if d.sale_price is not None or d.promo_type != "sale"
                ]
            deals.extend(publix_deals)
        except Exception as exc:  # noqa: BLE001
            log.warning("publix fetch failed: %s", exc)
    return deals


# ---------------------------------------------------------------------------
# Internal helper for tests
# ---------------------------------------------------------------------------

def _validate_retailer(name: str) -> None:
    if name.lower() not in {"kroger", "publix"}:
        raise ValueError(f"unknown retailer: {name}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _emit(deals: Iterable[Deal], as_json: bool) -> None:
    deals = list(deals)
    if as_json:
        print(json.dumps([d.to_dict() for d in deals], indent=2, default=str))
        return
    if not deals:
        print("(no deals)")
        return
    for d in deals:
        price = f"${d.sale_price:.2f}" if d.sale_price is not None else "n/a"
        reg = f" (was ${d.regular_price:.2f})" if d.regular_price is not None else ""
        promo = f" [{d.promo_type}]" if d.promo_type else ""
        print(f"{d.retailer:<7} {price:<8}{reg:<18} {d.product_name}{promo}")
        if d.promo_text:
            print(f"        {d.promo_text}")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="grocery_deals", description=__doc__)
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("publix", help="fetch Publix deals via Flipp")
    sp.add_argument("--zip", required=True, dest="zip_code")
    sp.add_argument("--query", default=None)
    sp.add_argument("--hydrate", action="store_true")
    sp.add_argument("--bogo-only", action="store_true")

    sk = sub.add_parser("kroger", help="fetch Kroger products")
    sk.add_argument("--location-id", required=True)
    sk.add_argument("--query", required=True)
    sk.add_argument("--limit", type=int, default=50)
    sk.add_argument("--all", action="store_true", help="include items not on sale")

    sf = sub.add_parser("find-kroger", help="resolve ZIP to Kroger locationId")
    sf.add_argument("--zip", required=True, dest="zip_code")
    sf.add_argument("--limit", type=int, default=5)

    ss = sub.add_parser("search", help="cross-retailer search")
    ss.add_argument("--query", required=True)
    ss.add_argument("--kroger-location-id", default=None)
    ss.add_argument("--publix-zip", default=None)
    ss.add_argument("--all", action="store_true", help="include items not on sale")

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "publix":
        deals = get_publix_deals(args.zip_code, args.query, hydrate=args.hydrate)
        if args.bogo_only:
            deals = [d for d in deals if d.promo_type == "bogo"]
        _emit(deals, args.json)
    elif args.cmd == "kroger":
        deals = get_kroger_deals(
            args.location_id, args.query,
            only_on_sale=not args.all, limit=args.limit,
        )
        _emit(deals, args.json)
    elif args.cmd == "find-kroger":
        locs = find_kroger_location(args.zip_code, args.limit)
        if args.json:
            print(json.dumps(locs, indent=2))
        else:
            for loc in locs:
                print(f"{loc['location_id']}  {loc['name']}  "
                      f"{loc['address']}, {loc['city']}, {loc['state']} {loc['zip']}")
    elif args.cmd == "search":
        deals = search_across(
            args.query,
            kroger_location_id=args.kroger_location_id,
            publix_zip=args.publix_zip,
            only_on_sale=not args.all,
        )
        _emit(deals, args.json)
    else:  # pragma: no cover - argparse enforces required subcommand
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
