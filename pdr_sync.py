#!/usr/bin/env python3
"""
Project Dorm Room sales sync.

Adds up Bedly net sales since the partnership started and writes the total to
the Shopify shop metafield `bedly.pdr_net_sales`. The room tracker on
shopbedly.com/pages/project-dorm-room reads that metafield and shows 5% of it.

Sources
  - Amazon (FBA): SP-API Finances, shipment and refund events.
    Net = item price + promotions (negative) - refunded item price + refunded promotions.
    Tax, shipping charges and Amazon fees are not included.
  - shopbedly.com: Shopify orders, current subtotal (after discounts and
    refunds, before shipping and tax). Shopify orders fulfilled by Amazon MCF do
    not show up as Amazon shipment events, so nothing is counted twice.

Walmart and campus-store sales are not synced. Add them by hand in the theme
editor (Room tracker section -> "Other channel net sales not synced").

The script recomputes the full total from START_DATE on every run, so it is
safe to run as often as you like and it fixes itself if a run fails.

Environment (GitHub Actions secrets):
  LWA_CLIENT_ID, LWA_CLIENT_SECRET, LWA_REFRESH_TOKEN   same as notifier.py
  SHOPIFY_SHOP              e.g. bedly-comfort-products.myshopify.com
  SHOPIFY_ADMIN_TOKEN       Admin API access token (shpat_...), OR
  SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET   Dev Dashboard app (client credentials)
  PDR_START_DATE            optional, ISO time, default 2026-11-04T00:00:00-05:00
  DRY_RUN=1                 optional, print the totals without writing to Shopify
"""

import os
import sys
import time
import datetime
from decimal import Decimal

import requests

LWA_CLIENT_ID     = os.environ["LWA_CLIENT_ID"]
LWA_CLIENT_SECRET = os.environ["LWA_CLIENT_SECRET"]
LWA_REFRESH_TOKEN = os.environ["LWA_REFRESH_TOKEN"]
SPAPI_ENDPOINT    = os.environ.get("SPAPI_ENDPOINT", "https://sellingpartnerapi-na.amazon.com")
LWA_TOKEN_URL     = "https://api.amazon.com/auth/o2/token"

SHOPIFY_SHOP        = os.environ["SHOPIFY_SHOP"]
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2025-10")

START_DATE = datetime.datetime.fromisoformat(
    os.environ.get("PDR_START_DATE", "2026-11-04T00:00:00-05:00")
).astimezone(datetime.timezone.utc)
DRY_RUN = os.environ.get("DRY_RUN") == "1"


# --- HTTP with retries (same approach as notifier.py) -----------------------
def request_with_retries(method, url, *, attempts=5, **kwargs):
    last_err = None
    for i in range(attempts):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = f"HTTP {resp.status_code}"
                raise requests.exceptions.RequestException(last_err)
            return resp
        except requests.exceptions.RequestException as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(2 ** (i + 1))   # 2s, 4s, 8s, 16s
    raise requests.exceptions.RequestException(
        f"{method} {url} failed after {attempts} attempts: {last_err}")


def money(obj):
    """SP-API money object -> Decimal."""
    if not obj:
        return Decimal("0")
    return Decimal(str(obj.get("CurrencyAmount", obj.get("Amount", 0)) or 0))


# --- Amazon -----------------------------------------------------------------
def amazon_token():
    resp = request_with_retries("POST", LWA_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": LWA_REFRESH_TOKEN,
        "client_id": LWA_CLIENT_ID,
        "client_secret": LWA_CLIENT_SECRET,
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


def item_net(items, charges_key, promos_key):
    """Principal minus promotions for a list of shipment or refund items."""
    total = Decimal("0")
    for item in items or []:
        for c in item.get(charges_key) or []:
            if c.get("ChargeType") == "Principal":
                total += money(c.get("ChargeAmount"))
        for p in item.get(promos_key) or []:
            total += money(p.get("PromotionAmount"))   # promotions are negative
    return total


def amazon_net_sales(token):
    url = f"{SPAPI_ENDPOINT}/finances/v0/financialEvents"
    headers = {"x-amz-access-token": token}
    params = {
        "PostedAfter": START_DATE.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "MaxResultsPerPage": 100,
    }
    sales = Decimal("0")
    refunds = Decimal("0")
    while True:
        resp = request_with_retries("GET", url, headers=headers, params=params, timeout=60)
        if resp.status_code == 403:
            raise RuntimeError(
                "Amazon returned 403 for the Finances API. In Seller Central -> "
                "Develop Apps, give the Soldly app the 'Finance and Accounting' role, "
                "then re-authorize to get a new refresh token.")
        resp.raise_for_status()
        payload = resp.json().get("payload", {})
        events = payload.get("FinancialEvents", {})
        for ev in events.get("ShipmentEventList") or []:
            sales += item_net(ev.get("ShipmentItemList"), "ItemChargeList", "PromotionList")
        for ev in events.get("RefundEventList") or []:
            # Refund amounts come back negative, so adding them reduces the total.
            refunds += item_net(ev.get("ShipmentItemAdjustmentList"),
                                "ItemChargeAdjustmentList", "PromotionAdjustmentList")
        next_token = payload.get("NextToken")
        if not next_token:
            break
        params = {"NextToken": next_token}
        time.sleep(2)   # Finances API allows about one request every 2 seconds
    return sales, refunds


# --- Shopify ----------------------------------------------------------------
def shopify_token():
    token = os.environ.get("SHOPIFY_ADMIN_TOKEN")
    if token:
        return token
    resp = request_with_retries("POST", f"https://{SHOPIFY_SHOP}/admin/oauth/access_token", data={
        "grant_type": "client_credentials",
        "client_id": os.environ["SHOPIFY_CLIENT_ID"],
        "client_secret": os.environ["SHOPIFY_CLIENT_SECRET"],
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


def shopify_gql(token, query, variables=None):
    resp = request_with_retries(
        "POST", f"https://{SHOPIFY_SHOP}/admin/api/{SHOPIFY_API_VERSION}/graphql.json",
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}}, timeout=60)
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        raise RuntimeError(f"Shopify error: {body['errors']}")
    return body["data"]


ORDERS_QUERY = """
query($q: String!, $after: String) {
  orders(first: 100, after: $after, query: $q) {
    pageInfo { hasNextPage endCursor }
    nodes { test cancelledAt currentSubtotalPriceSet { shopMoney { amount } } }
  }
}"""


def shopify_net_sales(token):
    q = f"created_at:>='{START_DATE.strftime('%Y-%m-%dT%H:%M:%SZ')}'"
    total = Decimal("0")
    after = None
    while True:
        data = shopify_gql(token, ORDERS_QUERY, {"q": q, "after": after})["orders"]
        for o in data["nodes"]:
            if o["test"] or o["cancelledAt"]:
                continue
            total += Decimal(o["currentSubtotalPriceSet"]["shopMoney"]["amount"])
        if not data["pageInfo"]["hasNextPage"]:
            break
        after = data["pageInfo"]["endCursor"]
    return total


SET_METAFIELDS = """
mutation($m: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $m) { metafields { key value } userErrors { field message } }
}"""


def write_total(token, total):
    shop_id = shopify_gql(token, "{ shop { id } }")["shop"]["id"]
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = shopify_gql(token, SET_METAFIELDS, {"m": [
        {"ownerId": shop_id, "namespace": "bedly", "key": "pdr_net_sales",
         "type": "number_decimal", "value": f"{total:.2f}"},
        {"ownerId": shop_id, "namespace": "bedly", "key": "pdr_synced_at",
         "type": "date_time", "value": now},
    ]})["metafieldsSet"]
    if data["userErrors"]:
        raise RuntimeError(f"Shopify metafield error: {data['userErrors']}")


# --- Main -------------------------------------------------------------------
def main():
    amz_sales, amz_refunds = amazon_net_sales(amazon_token())
    amz_net = amz_sales + amz_refunds
    shop_token = shopify_token()
    site_net = shopify_net_sales(shop_token)
    total = max(amz_net + site_net, Decimal("0"))

    print(f"Since {START_DATE.isoformat()}:")
    print(f"  Amazon sales      {amz_sales:>12.2f}")
    print(f"  Amazon refunds    {amz_refunds:>12.2f}")
    print(f"  shopbedly.com     {site_net:>12.2f}")
    print(f"  Net sales total   {total:>12.2f}")
    print(f"  5% set aside      {total * Decimal('0.05'):>12.2f}")

    if DRY_RUN:
        print("DRY_RUN=1, not writing to Shopify.")
        return
    write_total(shop_token, total)
    print("Wrote bedly.pdr_net_sales to Shopify.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
