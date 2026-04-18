"""Network-free smoke tests for grocery_deals."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import grocery_deals as gd


# ---------------------------------------------------------------------------
# Dataclass roundtrip
# ---------------------------------------------------------------------------

def test_deal_roundtrip_to_dict_is_json_serializable():
    deal = gd.Deal(
        retailer="Publix",
        store_id="123",
        product_name="Boneless Chicken Breast",
        brand="Publix",
        size="per lb",
        regular_price=5.99,
        sale_price=2.99,
        savings=3.00,
        promo_type="bogo",
        promo_text="Buy 1 Get 1 Free",
        valid_from="2026-04-15",
        valid_to="2026-04-21",
        image_url="https://example.com/x.jpg",
        source_id="abc",
    )
    d = deal.to_dict()
    assert d["retailer"] == "Publix"
    assert d["promo_type"] == "bogo"
    blob = json.dumps(d)
    assert "Boneless Chicken Breast" in blob


# ---------------------------------------------------------------------------
# Promo classifier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("texts,expected", [
    (("Buy 1 Get 1 FREE",), "bogo"),
    (("BOGO",), "bogo"),
    (("b1g1",), "bogo"),
    (("", "10 for $10", ""), "multi_buy"),
    (("2 for $5",), "multi_buy"),
    (("Buy 2 Get 1",), "multi_buy"),
    (("Save $3",), "amount_off"),
    (("save $1.50 each",), "amount_off"),
    (("25% off",), "percent_off"),
    (("On sale",), "sale"),
    ((None, None, None), "sale"),
    ((), "sale"),
])
def test_classify_promo(texts, expected):
    assert gd.classify_promo(*texts) == expected


def test_classify_promo_bogo_beats_multi_buy_when_both_present():
    assert gd.classify_promo("Buy 1 Get 1 Free", "2 for $5") == "bogo"


# ---------------------------------------------------------------------------
# Flipp parser
# ---------------------------------------------------------------------------

def test_parse_flipp_item_with_missing_fields():
    deal = gd._parse_flipp_item({"name": "Mystery Item"}, "Publix")
    assert deal.retailer == "Publix"
    assert deal.product_name == "Mystery Item"
    assert deal.sale_price is None
    assert deal.regular_price is None
    assert deal.savings is None
    assert deal.promo_type == "sale"
    assert deal.promo_text is None
    assert deal.valid_from is None
    assert deal.image_url is None
    assert deal.source_id is None


def test_parse_flipp_item_full_bogo():
    item = {
        "flyer_item_id": 999,
        "name": "Chicken Breast",
        "brand": "Publix",
        "sku_description": "per lb",
        "current_price": 2.99,
        "original_price": 5.99,
        "pre_price_text": "",
        "sale_story": "Buy 1 Get 1 Free",
        "post_price_text": "",
        "valid_from": "2026-04-15",
        "valid_to": "2026-04-21",
        "clipping_image_url": "https://img.example/x.jpg",
        "merchant_id": "42",
    }
    deal = gd._parse_flipp_item(item, "Publix")
    assert deal.product_name == "Chicken Breast"
    assert deal.brand == "Publix"
    assert deal.size == "per lb"
    assert deal.sale_price == 2.99
    assert deal.regular_price == 5.99
    assert deal.savings == 3.00
    assert deal.promo_type == "bogo"
    assert deal.promo_text == "Buy 1 Get 1 Free"
    assert deal.valid_from == "2026-04-15"
    assert deal.image_url == "https://img.example/x.jpg"
    assert deal.source_id == "999"
    assert deal.store_id == "42"


def test_parse_flipp_item_uses_fallback_keys():
    item = {
        "title": "Eggs",
        "price": 1.99,
        "saleStory": "Save $1",
        "validFrom": "2026-04-15",
        "validTo": "2026-04-21",
        "thumbnail_url": "https://img.example/eggs.jpg",
        "id": 17,
    }
    deal = gd._parse_flipp_item(item, "Kroger")
    assert deal.retailer == "Kroger"
    assert deal.product_name == "Eggs"
    assert deal.sale_price == 1.99
    assert deal.promo_type == "amount_off"
    assert deal.valid_from == "2026-04-15"
    assert deal.image_url == "https://img.example/eggs.jpg"
    assert deal.source_id == "17"


# ---------------------------------------------------------------------------
# Merchant filter
# ---------------------------------------------------------------------------

def test_merchant_matches_publix():
    assert gd._merchant_matches({"merchant": "Publix"}, "publix")
    assert gd._merchant_matches({"merchant_name": "PUBLIX SUPER MARKETS"}, "publix")
    assert not gd._merchant_matches({"merchant": "Kroger"}, "publix")


def test_merchant_matches_kroger():
    assert gd._merchant_matches({"merchant": "Kroger"}, "kroger")
    assert gd._merchant_matches({"merchant_name": "KROGER FAMILY"}, "kroger")
    assert not gd._merchant_matches({"merchant": "Publix"}, "kroger")
    assert not gd._merchant_matches({}, "kroger")


# ---------------------------------------------------------------------------
# Keyword filter
# ---------------------------------------------------------------------------

def _deal(**over):
    base = dict(
        retailer="Kroger", store_id=None, product_name="Whole Milk",
        brand="Kroger", size="1 gal",
        regular_price=None, sale_price=None, savings=None,
        promo_type="sale", promo_text=None,
        valid_from=None, valid_to=None,
        image_url=None, source_id=None,
    )
    base.update(over)
    return gd.Deal(**base)


def test_keyword_matches_product_name():
    assert gd._keyword_matches(_deal(product_name="Whole Milk"), "milk")
    assert not gd._keyword_matches(_deal(product_name="Bread"), "milk")


def test_keyword_matches_is_case_insensitive():
    assert gd._keyword_matches(_deal(product_name="Organic MILK"), "milk")


def test_keyword_matches_brand_and_size():
    assert gd._keyword_matches(_deal(product_name="X", brand="Horizon"), "horizon")
    assert gd._keyword_matches(_deal(product_name="X", size="1 gal"), "gal")


# ---------------------------------------------------------------------------
# Public API: validation + one-fetch-per-store semantics
# ---------------------------------------------------------------------------

def test_get_publix_deals_requires_zip():
    with pytest.raises(ValueError):
        gd.get_publix_deals("")


def test_get_kroger_deals_requires_zip():
    with pytest.raises(ValueError):
        gd.get_kroger_deals("")


def test_search_across_requires_query_and_zip():
    with pytest.raises(ValueError):
        gd.search_across("", zip_code="45202")
    with pytest.raises(ValueError):
        gd.search_across("milk", zip_code="")


def test_repeated_calls_share_one_fetch_per_store(monkeypatch, tmp_path):
    """Calling get_kroger_deals twice for the same ZIP with different
    keywords must hit the network only once — filtering happens in Python."""
    monkeypatch.setattr(gd, "CACHE_DIR", tmp_path)

    calls: list[dict] = []
    payload = {"items": [
        {"merchant": "Kroger", "name": "Whole Milk",
         "current_price": 2.99, "sale_story": "Save $1"},
        {"merchant": "Kroger", "name": "White Bread",
         "current_price": 1.99, "sale_story": "2 for $5"},
        {"merchant": "Publix", "name": "Eggs",
         "current_price": 3.99},  # different merchant: must be filtered out
    ]}

    def fake_get(url, params=None):
        calls.append({"url": url, "params": params})
        return payload

    monkeypatch.setattr(gd, "_flipp_get", fake_get)

    milk = gd.get_kroger_deals("45202", "milk")
    bread = gd.get_kroger_deals("45202", "bread")
    everything = gd.get_kroger_deals("45202")

    assert len(calls) == 1, f"expected 1 network call, got {len(calls)}"
    assert calls[0]["params"]["q"] == "kroger"
    assert calls[0]["params"]["postal_code"] == "45202"

    assert [d.product_name for d in milk] == ["Whole Milk"]
    assert [d.product_name for d in bread] == ["White Bread"]
    assert {d.product_name for d in everything} == {"Whole Milk", "White Bread"}
    assert all(d.retailer == "Kroger" for d in everything)


def test_different_zip_triggers_separate_fetch(monkeypatch, tmp_path):
    monkeypatch.setattr(gd, "CACHE_DIR", tmp_path)
    calls: list[dict] = []

    def fake_get(url, params=None):
        calls.append(params)
        return {"items": []}

    monkeypatch.setattr(gd, "_flipp_get", fake_get)

    gd.get_kroger_deals("45202")
    gd.get_kroger_deals("33486")

    assert len(calls) == 2
    assert {c["postal_code"] for c in calls} == {"45202", "33486"}


def test_search_across_one_fetch_per_merchant(monkeypatch, tmp_path):
    monkeypatch.setattr(gd, "CACHE_DIR", tmp_path)
    calls: list[dict] = []

    def fake_get(url, params=None):
        calls.append(params)
        return {"items": []}

    monkeypatch.setattr(gd, "_flipp_get", fake_get)

    gd.search_across("milk", zip_code="45202")
    assert len(calls) == 2
    assert {c["q"] for c in calls} == {"kroger", "publix"}


# ---------------------------------------------------------------------------
# Unknown retailer
# ---------------------------------------------------------------------------

def test_unknown_retailer_raises():
    with pytest.raises(ValueError):
        gd._validate_retailer("Walmart")
    gd._validate_retailer("Kroger")
    gd._validate_retailer("publix")
