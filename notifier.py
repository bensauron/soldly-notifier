#!/usr/bin/env python3
"""
Bedly Amazon sale notifier.

Polls Amazon SP-API for new orders and pushes a phone notification for each
one it hasn't seen before. Designed to run on a 5-minute cron (e.g. GitHub
Actions). No AWS signing required — SP-API now authenticates with a plain
Login-with-Amazon (LWA) OAuth token.

All secrets come from environment variables (set as GitHub Actions Secrets).
Nothing sensitive is ever stored in this file.
"""

import os
import sys
import json
import time
import datetime

import requests

# --- Config (from environment) ---------------------------------------------
LWA_CLIENT_ID     = os.environ["LWA_CLIENT_ID"]
LWA_CLIENT_SECRET = os.environ["LWA_CLIENT_SECRET"]
LWA_REFRESH_TOKEN = os.environ["LWA_REFRESH_TOKEN"]

# US marketplace + North America endpoint by default.
# Change MARKETPLACE_ID / SPAPI_ENDPOINT if you sell elsewhere.
MARKETPLACE_ID = os.environ.get("MARKETPLACE_ID", "ATVPDKIKX0DER")          # US
SPAPI_ENDPOINT = os.environ.get("SPAPI_ENDPOINT",
                                "https://sellingpartnerapi-na.amazon.com")  # NA

# ntfy push target. Install the free ntfy app, pick a hard-to-guess topic.
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC  = os.environ["NTFY_TOPIC"]

# How far back to look each run. Wider than the cron interval so we never miss
# an order if a scheduled run is delayed. Duplicates are filtered by state.
LOOKBACK_MINUTES = int(os.environ.get("LOOKBACK_MINUTES", "20"))

STATE_FILE = "seen_orders.json"
LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"


# --- LWA auth ---------------------------------------------------------------
def get_access_token():
    """Exchange the long-lived refresh token for a 1-hour access token."""
    resp = requests.post(
        LWA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": LWA_REFRESH_TOKEN,
            "client_id": LWA_CLIENT_ID,
            "client_secret": LWA_CLIENT_SECRET,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# --- Orders -----------------------------------------------------------------
def get_recent_orders(token):
    """Return all orders created within the lookback window."""
    created_after = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(minutes=LOOKBACK_MINUTES)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    url = f"{SPAPI_ENDPOINT}/orders/v0/orders"
    headers = {"x-amz-access-token": token}
    params = {"MarketplaceIds": MARKETPLACE_ID, "CreatedAfter": created_after}

    orders = []
    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 429:        # rate limited — back off and retry
            time.sleep(2)
            continue
        resp.raise_for_status()
        payload = resp.json().get("payload", {})
        orders.extend(payload.get("Orders", []))
        next_token = payload.get("NextToken")
        if not next_token:
            break
        params = {"MarketplaceIds": MARKETPLACE_ID, "NextToken": next_token}
    return orders


# --- State (dedupe across runs) ---------------------------------------------
def load_seen():
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen):
    # Keep the most recent ~1000 ids so the file stays tiny.
    with open(STATE_FILE, "w") as f:
        json.dump(list(seen)[-1000:], f)


# --- Push -------------------------------------------------------------------
def notify(order):
    """Send one phone notification for an order."""
    oid = order.get("AmazonOrderId", "unknown")
    total = order.get("OrderTotal", {}) or {}
    amount = total.get("Amount")
    currency = total.get("CurrencyCode", "")
    items = (order.get("NumberOfItemsShipped", 0) or 0) + \
            (order.get("NumberOfItemsUnshipped", 0) or 0)

    parts = [f"Order {oid}"]
    if amount:
        parts.append(f"{amount} {currency}".strip())
    if items:
        parts.append(f"{items} item(s)")
    body = "  |  ".join(parts)

    requests.post(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "Title": "Bedly: New Amazon sale!",
            "Priority": "high",
            "Tags": "moneybag",
        },
        timeout=30,
    )


# --- Main -------------------------------------------------------------------
def main():
    token = get_access_token()
    orders = get_recent_orders(token)
    seen = load_seen()

    new_orders = [o for o in orders if o.get("AmazonOrderId") not in seen]
    for o in new_orders:
        notify(o)
        seen.add(o.get("AmazonOrderId"))

    if new_orders:
        save_seen(seen)

    print(f"Window had {len(orders)} order(s); {len(new_orders)} new -> notified.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                 # fail loudly in the Actions log
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
