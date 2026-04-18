"""Fetch real-time weekly-ad sale data from Kroger and Publix via Flipp.

Both retailers officially publish their weekly ads through Flipp
(``backflipp.wishabi.com``). We fetch the full flyer once per (ZIP, merchant)
and apply keyword filtering client-side, so repeated queries for the same
store share a single network call (cached 6h).

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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

__all__ = [
    "Deal",
    "get_publix_deals",
    "get_kroger_deals",
    "search_across",
]

log = logging.getLogger("grocery_deals")

FLIPP_SEARCH_URL = "https://backflipp.wishabi.com/flipp/items/search"
FLIPP_ITEM_URL = "https://backflipp.wishabi.com/flipp/items/{item_id}"

# Merchant tokens are lowercased substrings matched against the Flipp
# ``merchant`` / ``merchant_name`` field. Keep flexible — Flipp's merchant
# strings can read as "Publix", "PUBLIX SUPER MARKETS", "Kroger", etc.
MERCHANT_PUBLIX = "publix"
MERCHANT_KROGER = "kroger"

CACHE_DIR = Path(os.path.expanduser("~/.cache/grocery_deals"))
FLYER_TTL = 6 * 60 * 60      # 6 hours — flyers cycle Wed/Thu

USER_AGENT = "grocery_deals/0.2 (+https://github.com/kevinold/grocery_deals)"


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
        allowed_methods=("GET",),
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
# Flipp shared helpers
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


def _merchant_of(item: dict[str, Any]) -> str:
    return (_first(item, "merchant", "merchant_name", default="") or "").lower()


def _merchant_matches(item: dict[str, Any], token: str) -> bool:
    return token in _merchant_of(item)


def _display_retailer(token: str) -> str:
    return {MERCHANT_PUBLIX: "Publix", MERCHANT_KROGER: "Kroger"}.get(
        token, token.title()
    )


def _parse_flipp_item(item: dict[str, Any], retailer: str) -> Deal:
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
        retailer=retailer,
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


def _keyword_matches(deal: Deal, query: str) -> bool:
    q = query.lower()
    for field in (deal.product_name, deal.brand, deal.size, deal.promo_text):
        if field and q in field.lower():
            return True
    return False


def _fetch_flyer(zip_code: str, merchant_token: str) -> list[dict[str, Any]]:
    """Fetch the full flyer for a merchant at a ZIP, cached 6h.

    We always request ``q=<merchant>`` so repeated calls for different keywords
    against the same store share a single cache entry and a single network
    fetch.
    """
    params = {"locale": "en-us", "postal_code": zip_code, "q": merchant_token}
    cache_path = _cache_key(f"flyer_{merchant_token}", params)
    payload = _cache_get(cache_path, FLYER_TTL)
    if payload is None:
        payload = _flipp_get(FLIPP_SEARCH_URL, params=params)
        _cache_put(cache_path, payload)
    items = payload.get("items") or payload.get("results") or []
    return [it for it in items if _merchant_matches(it, merchant_token)]


def _hydrate_deal(deal: Deal, retailer: str) -> Deal:
    if not deal.source_id:
        return deal
    cache_path = _cache_key("flipp_item", {"id": deal.source_id})
    payload = _cache_get(cache_path, FLYER_TTL)
    if payload is None:
        try:
            payload = _flipp_get(FLIPP_ITEM_URL.format(item_id=deal.source_id))
            _cache_put(cache_path, payload)
        except requests.RequestException as exc:
            log.warning("hydrate failed for %s: %s", deal.source_id, exc)
            return deal
    item = payload.get("item") or payload
    return _parse_flipp_item(item, retailer)


def _deals_for(
    zip_code: str,
    merchant_token: str,
    query: str | None,
    *,
    only_on_sale: bool,
    hydrate: bool,
) -> list[Deal]:
    if not zip_code or not zip_code.strip():
        raise ValueError("zip_code is required")

    retailer = _display_retailer(merchant_token)
    items = _fetch_flyer(zip_code, merchant_token)
    deals = [_parse_flipp_item(it, retailer) for it in items]

    if query and query.strip():
        q = query.strip()
        deals = [d for d in deals if _keyword_matches(d, q)]

    if only_on_sale:
        deals = [d for d in deals if d.sale_price is not None or d.promo_type != "sale"]

    if hydrate:
        deals = [_hydrate_deal(d, retailer) for d in deals]
    return deals


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_publix_deals(
    zip_code: str,
    query: str | None = None,
    *,
    only_on_sale: bool = False,
    hydrate: bool = False,
) -> list[Deal]:
    """Fetch Publix weekly-ad deals via Flipp.

    A single network fetch per (zip_code) is shared across all keyword calls
    in the 6-hour cache window — filtering is applied client-side.
    """
    return _deals_for(
        zip_code, MERCHANT_PUBLIX, query,
        only_on_sale=only_on_sale, hydrate=hydrate,
    )


def get_kroger_deals(
    zip_code: str,
    query: str | None = None,
    *,
    only_on_sale: bool = False,
    hydrate: bool = False,
) -> list[Deal]:
    """Fetch Kroger weekly-ad deals via Flipp.

    A single network fetch per (zip_code) is shared across all keyword calls
    in the 6-hour cache window — filtering is applied client-side.
    """
    return _deals_for(
        zip_code, MERCHANT_KROGER, query,
        only_on_sale=only_on_sale, hydrate=hydrate,
    )


def search_across(
    query: str,
    *,
    zip_code: str,
    only_on_sale: bool = True,
) -> list[Deal]:
    """Run the same query against Kroger and Publix for a single ZIP."""
    if not query or not query.strip():
        raise ValueError("query is required")
    if not zip_code or not zip_code.strip():
        raise ValueError("zip_code is required")

    deals: list[Deal] = []
    for token in (MERCHANT_KROGER, MERCHANT_PUBLIX):
        try:
            deals.extend(_deals_for(
                zip_code, token, query,
                only_on_sale=only_on_sale, hydrate=False,
            ))
        except Exception as exc:  # noqa: BLE001 - surface partial results
            log.warning("%s fetch failed: %s", token, exc)
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
    sp.add_argument("--on-sale", action="store_true",
                    help="drop items with no identifiable promo/sale price")

    sk = sub.add_parser("kroger", help="fetch Kroger deals via Flipp")
    sk.add_argument("--zip", required=True, dest="zip_code")
    sk.add_argument("--query", default=None)
    sk.add_argument("--hydrate", action="store_true")
    sk.add_argument("--bogo-only", action="store_true")
    sk.add_argument("--on-sale", action="store_true")

    ss = sub.add_parser("search", help="cross-retailer search at a single ZIP")
    ss.add_argument("--zip", required=True, dest="zip_code")
    ss.add_argument("--query", required=True)
    ss.add_argument("--all", action="store_true",
                    help="include items without an identifiable promo")

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd in ("publix", "kroger"):
        fn = get_publix_deals if args.cmd == "publix" else get_kroger_deals
        deals = fn(
            args.zip_code, args.query,
            only_on_sale=args.on_sale, hydrate=args.hydrate,
        )
        if args.bogo_only:
            deals = [d for d in deals if d.promo_type == "bogo"]
        _emit(deals, args.json)
    elif args.cmd == "search":
        deals = search_across(
            args.query, zip_code=args.zip_code, only_on_sale=not args.all,
        )
        _emit(deals, args.json)
    else:  # pragma: no cover - argparse enforces required subcommand
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
