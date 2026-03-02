"""
kalshi_wrapper.py
-----------------
Thin wrapper around the Kalshi REST API v2.
Handles authentication, market lookup, and order placement.

Set environment variables:
    KALSHI_API_KEY_ID      – your Kalshi key ID
    KALSHI_PRIVATE_KEY     – path to your RSA private key .pem file
                             OR the raw PEM string itself

Kalshi uses RSA-PS256 signatures on every request.
"""

import os
import time
import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
KALSHI_BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"
KALSHI_DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2"      # paper trading

API_KEY_ID   = os.getenv("KALSHI_API_KEY_ID", "")
_PRIV_KEY_ENV = os.getenv("KALSHI_PRIVATE_KEY", "")   # path or raw PEM


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _load_private_key():
    """Load RSA private key from env var (path or raw PEM)."""
    raw = _PRIV_KEY_ENV.strip()
    if not raw:
        raise RuntimeError("KALSHI_PRIVATE_KEY env var not set.")

    if raw.startswith("-----"):
        pem_bytes = raw.encode()
    else:
        pem_bytes = Path(raw).read_bytes()

    return serialization.load_pem_private_key(pem_bytes, password=None, backend=default_backend())


def _sign(method: str, path: str, timestamp_ms: int) -> str:
    """
    Kalshi signature = RSA-PSS-SHA256 over  "<ts_ms><METHOD><path>"
    Returns base64url-encoded signature.
    """
    message = f"{timestamp_ms}{method.upper()}{path}".encode()
    private_key = _load_private_key()
    sig = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


def _auth_headers(method: str, path: str) -> dict:
    ts = int(time.time() * 1000)
    return {
        "Content-Type":        "application/json",
        "KALSHI-ACCESS-KEY":   API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "KALSHI-ACCESS-SIGNATURE": _sign(method, path, ts),
    }


# ---------------------------------------------------------------------------
# Low-level request helpers
# ---------------------------------------------------------------------------

class KalshiError(Exception):
    pass


def _get(path: str, params: dict | None = None, demo: bool = False) -> dict:
    base = KALSHI_DEMO_URL if demo else KALSHI_BASE_URL
    url  = base + path
    headers = _auth_headers("GET", path)
    r = requests.get(url, headers=headers, params=params, timeout=12)
    if not r.ok:
        raise KalshiError(f"GET {path} → {r.status_code}: {r.text[:300]}")
    return r.json()


def _post(path: str, body: dict, demo: bool = False) -> dict:
    base = KALSHI_DEMO_URL if demo else KALSHI_BASE_URL
    url  = base + path
    headers = _auth_headers("POST", path)
    r = requests.post(url, headers=headers, json=body, timeout=12)
    if not r.ok:
        raise KalshiError(f"POST {path} → {r.status_code}: {r.text[:300]}")
    return r.json()


# ---------------------------------------------------------------------------
# Market helpers
# ---------------------------------------------------------------------------

def get_markets(series_ticker: str, demo: bool = False) -> list[dict]:
    """Return all open markets for a series (e.g. 'HIGHNY' for NYC high temp)."""
    data = _get("/markets", params={"series_ticker": series_ticker, "status": "open"}, demo=demo)
    return data.get("markets", [])


def get_market(ticker: str, demo: bool = False) -> dict:
    """Return a single market by its ticker."""
    data = _get(f"/markets/{ticker}", demo=demo)
    return data.get("market", {})


def get_orderbook(ticker: str, depth: int = 5, demo: bool = False) -> dict:
    """Return the order book for a market."""
    data = _get(f"/markets/{ticker}/orderbook", params={"depth": depth}, demo=demo)
    return data.get("orderbook", {})


def get_balance(demo: bool = False) -> float:
    """Return available balance in USD cents → converted to dollars."""
    data = _get("/portfolio/balance", demo=demo)
    return data.get("balance", 0) / 100.0


def get_positions(demo: bool = False) -> list[dict]:
    """Return all open positions."""
    data = _get("/portfolio/positions", demo=demo)
    return data.get("market_positions", [])


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def place_order(
    ticker: str,
    side: str,           # "yes" or "no"
    count: int,          # number of contracts
    limit_price: int,    # in cents (1-99)
    *,
    action: str = "buy", # "buy" or "sell"
    demo: bool = False,
) -> dict:
    """
    Place a limit order on Kalshi.

    Returns the order response dict from Kalshi.
    """
    body = {
        "ticker":      ticker,
        "action":      action,
        "side":        side,
        "count":       count,
        "type":        "limit",
        "yes_price":   limit_price if side == "yes" else 100 - limit_price,
        "no_price":    100 - limit_price if side == "yes" else limit_price,
    }
    return _post("/portfolio/orders", body, demo=demo)


def cancel_order(order_id: str, demo: bool = False) -> dict:
    path = f"/portfolio/orders/{order_id}"
    base = KALSHI_DEMO_URL if demo else KALSHI_BASE_URL
    headers = _auth_headers("DELETE", path)
    r = requests.delete(base + path, headers=headers, timeout=12)
    if not r.ok:
        raise KalshiError(f"DELETE {path} → {r.status_code}: {r.text[:300]}")
    return r.json()
