#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests>=2.31",
#     "mcp>=1.2",
# ]
# ///

"""MCP server exposing grocery_deals.py as agent tools over stdio.

Wraps the existing public API (get_publix_deals, get_kroger_deals,
search_across) so an LLM agent can call them via the Model Context
Protocol. Intended for use with zeroclaw or any MCP-capable client.

Run directly (uv provisions deps via PEP 723):

    ./grocery_deals_mcp.py

Or inspect with the MCP dev UI:

    uv run --with "mcp[cli]" mcp dev ./grocery_deals_mcp.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the sibling grocery_deals.py is importable when this script is
# invoked by absolute path from an MCP client (zeroclaw, Claude Desktop, …).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

import grocery_deals  # noqa: E402

mcp = FastMCP("grocery_deals")


@mcp.tool()
def publix_deals(
    zip_code: str,
    query: str | None = None,
    only_on_sale: bool = False,
    hydrate: bool = False,
) -> list[dict]:
    """Fetch this week's Publix weekly-ad items for a ZIP, optionally filtered.

    Args:
        zip_code: 5-digit US ZIP code (store locator).
        query: optional keyword filter applied to product name/brand
            (e.g. "milk", "chicken"). Client-side — no extra network call.
        only_on_sale: drop items with no identifiable promo or sale price.
        hydrate: issue a per-item detail fetch to pick up precise
            regular-price and valid-from/valid-to fields (slower).
    """
    deals = grocery_deals.get_publix_deals(
        zip_code, query, only_on_sale=only_on_sale, hydrate=hydrate
    )
    return [d.to_dict() for d in deals]


@mcp.tool()
def kroger_deals(
    zip_code: str,
    query: str | None = None,
    only_on_sale: bool = False,
    hydrate: bool = False,
) -> list[dict]:
    """Fetch this week's Kroger weekly-ad items for a ZIP, optionally filtered.

    Args:
        zip_code: 5-digit US ZIP code (store locator).
        query: optional keyword filter applied to product name/brand.
        only_on_sale: drop items with no identifiable promo or sale price.
        hydrate: issue a per-item detail fetch for richer price fields.
    """
    deals = grocery_deals.get_kroger_deals(
        zip_code, query, only_on_sale=only_on_sale, hydrate=hydrate
    )
    return [d.to_dict() for d in deals]


@mcp.tool()
def search_deals(
    query: str,
    zip_code: str,
    only_on_sale: bool = True,
) -> list[dict]:
    """Search both Publix and Kroger at one ZIP for items matching a keyword.

    Args:
        query: keyword to match against product name / brand.
        zip_code: 5-digit US ZIP code.
        only_on_sale: drop items with no identifiable promo (default True).
    """
    deals = grocery_deals.search_across(
        query, zip_code=zip_code, only_on_sale=only_on_sale
    )
    return [d.to_dict() for d in deals]


if __name__ == "__main__":
    mcp.run()
