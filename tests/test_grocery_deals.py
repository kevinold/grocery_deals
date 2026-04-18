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
    # Order matters: bogo wins over multi_buy
    assert gd.classify_promo("Buy 1 Get 1 Free", "2 for $5") == "bogo"


# ---------------------------------------------------------------------------
# Flipp parser with missing fields
# ---------------------------------------------------------------------------

def test_parse_flipp_item_with_missing_fields():
    item = {
        "name": "Mystery Item",
        # no prices, no promo text, no dates, no image
    }
    deal = gd._parse_flipp_item(item)
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
    deal = gd._parse_flipp_item(item)
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
    # Older Flipp shape with camelCase keys
    item = {
        "title": "Eggs",
        "price": 1.99,
        "saleStory": "Save $1",
        "validFrom": "2026-04-15",
        "validTo": "2026-04-21",
        "thumbnail_url": "https://img.example/eggs.jpg",
        "id": 17,
    }
    deal = gd._parse_flipp_item(item)
    assert deal.product_name == "Eggs"
    assert deal.sale_price == 1.99
    assert deal.promo_type == "amount_off"
    assert deal.valid_from == "2026-04-15"
    assert deal.image_url == "https://img.example/eggs.jpg"
    assert deal.source_id == "17"


def test_publix_merchant_filter():
    assert gd._is_publix({"merchant": "Publix"})
    assert gd._is_publix({"merchant_name": "PUBLIX SUPER MARKETS"})
    assert not gd._is_publix({"merchant": "Walmart"})
    assert not gd._is_publix({})


# ---------------------------------------------------------------------------
# Kroger argument validation and promo normalization
# ---------------------------------------------------------------------------

def test_get_kroger_deals_requires_non_empty_query():
    with pytest.raises(ValueError):
        gd.get_kroger_deals("01400376", "")
    with pytest.raises(ValueError):
        gd.get_kroger_deals("01400376", "   ")


def test_get_kroger_deals_requires_location_id():
    with pytest.raises(ValueError):
        gd.get_kroger_deals("", "milk")


def test_find_kroger_location_requires_zip():
    with pytest.raises(ValueError):
        gd.find_kroger_location("")


def test_get_publix_deals_requires_zip():
    with pytest.raises(ValueError):
        gd.get_publix_deals("")


def test_search_across_requires_query():
    with pytest.raises(ValueError):
        gd.search_across("", kroger_location_id=None, publix_zip=None)


def test_normalize_kroger_promo_zero_becomes_none():
    assert gd._normalize_kroger_promo({"promo": 0}) is None
    assert gd._normalize_kroger_promo({"promo": "0"}) is None
    assert gd._normalize_kroger_promo({"promo": None}) is None
    assert gd._normalize_kroger_promo({"promo": 2.99}) == 2.99


def test_parse_kroger_product_no_promo():
    prod = {
        "productId": "0001111041700",
        "description": "Kroger 2% Milk",
        "brand": "Kroger",
        "items": [{"size": "1 gal", "price": {"regular": 3.49, "promo": 0}}],
        "images": [],
    }
    deal = gd._parse_kroger_product(prod, "01400376")
    assert deal is not None
    assert deal.regular_price == 3.49
    assert deal.sale_price is None
    assert deal.savings is None
    assert deal.store_id == "01400376"


def test_parse_kroger_product_with_promo_computes_savings():
    prod = {
        "productId": "P1",
        "description": "Bread",
        "brand": "Kroger",
        "items": [{"size": "1 loaf", "price": {"regular": 3.99, "promo": 1.99}}],
        "images": [
            {"sizes": [{"url": "https://img.kroger/bread.jpg"}]},
        ],
    }
    deal = gd._parse_kroger_product(prod, "L1")
    assert deal.sale_price == 1.99
    assert deal.regular_price == 3.99
    assert deal.savings == 2.00
    assert deal.image_url == "https://img.kroger/bread.jpg"


# ---------------------------------------------------------------------------
# Unknown retailer
# ---------------------------------------------------------------------------

def test_unknown_retailer_raises():
    with pytest.raises(ValueError):
        gd._validate_retailer("Walmart")
    gd._validate_retailer("Kroger")
    gd._validate_retailer("publix")
