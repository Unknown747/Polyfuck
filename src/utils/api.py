"""Polymarket API client - Gamma, CLOB, and Data APIs."""

import time
import json
import hmac
import hashlib
import requests
from typing import Any
from eth_account.messages import encode_defunct
from web3 import Web3

from src.config import config


class GammaClient:
    """Public Gamma API client - no auth required. Market discovery and metadata."""

    def __init__(self):
        self.base_url = config.GAMMA_API_URL
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def _get(self, path: str, params: dict | None = None) -> Any:
        resp = self.session.get(f"{self.base_url}{path}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def search_markets(self, query: str, limit: int = 20) -> list[dict]:
        """Search markets by keyword."""
        return self._get("/public-search", {"q": query, "limit": limit})

    def get_markets(self, active_only: bool = True, limit: int = 100, offset: int = 0) -> list[dict]:
        """List markets with optional filters."""
        params = {"limit": limit, "offset": offset}
        if active_only:
            params["active"] = "true"
            params["closed"] = "false"
        return self._get("/markets", params)

    def get_market(self, condition_id: str) -> dict:
        """Get a single market by condition ID."""
        result = self._get("/markets", {"condition_id": condition_id})
        if isinstance(result, list) and result:
            return result[0]
        return result

    def get_events(self, tag: str | None = None, limit: int = 20, offset: int = 0) -> list[dict]:
        """List events, optionally filtered by category tag."""
        params = {"limit": limit, "offset": offset}
        if tag:
            params["tag"] = tag
        return self._get("/events", params)

    def get_event_by_slug(self, slug: str) -> dict:
        """Get event by its URL slug."""
        return self._get(f"/events/slug/{slug}")


class ClobClient:
    """CLOB API client for order books, prices, and trading. Requires auth for trading."""

    def __init__(self):
        self.base_url = config.CLOB_API_URL
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        # Auth state - set up via authenticate()
        self.api_key: str = ""
        self.api_secret: str = ""
        self.api_passphrase: str = ""
        self.address: str = ""
        self._authenticated = False

    def _get(self, path: str, params: dict | None = None) -> Any:
        """Unauthenticated GET request."""
        resp = self.session.get(f"{self.base_url}{path}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _auth_headers(self, method: str, path: str) -> dict:
        """Generate L2 authentication headers for authenticated requests."""
        if not self._authenticated:
            raise RuntimeError("Not authenticated. Call authenticate() first.")

        timestamp = str(int(time.time()))
        message = f"{timestamp}{method}{path}"
        signature = hmac.new(
            self.api_secret.encode(), message.encode(), hashlib.sha256
        ).hexdigest()

        return {
            "POLY_ADDRESS": self.address,
            "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": timestamp,
            "POLY_API_KEY": self.api_key,
            "POLY_PASSPHRASE": self.api_passphrase,
        }

    def _auth_request(self, method: str, path: str, json_data: dict | None = None) -> Any:
        """Make an authenticated request."""
        headers = self._auth_headers(method, path)
        headers["Content-Type"] = "application/json"
        if json_data:
            resp = self.session.request(method, f"{self.base_url}{path}", headers=headers, json=json_data, timeout=15)
        else:
            resp = self.session.request(method, f"{self.base_url}{path}", headers=headers, timeout=15)
        resp.raise_for_status()
        try:
            return resp.json()
        except json.JSONDecodeError:
            return {"status": resp.status_code, "text": resp.text}

    def authenticate(self, private_key: str) -> dict:
        """Create or derive API credentials from a private key (L1 -> L2 auth)."""
        from eth_account import Account
        account = Account.from_key(private_key)
        self.address = account.address

        # Create L1 signature for API key derivation
        timestamp = str(int(time.time()))
        message = f"Polymarket Authentication\n{timestamp}"
        signed = account.sign_message(encode_defunct(text=message))

        l1_headers = {
            "POLY_ADDRESS": self.address,
            "POLY_SIGNATURE": signed.signature.hex(),
            "POLY_TIMESTAMP": timestamp,
            "POLY_NONCE": str(int(time.time() * 1000)),
        }

        # First try to derive existing key
        resp = self.session.get(
            f"{self.base_url}/auth/derive-api-key",
            headers=l1_headers,
            timeout=15,
        )

        if resp.status_code == 200:
            creds = resp.json()
        else:
            # Create new API key
            resp = self.session.post(
                f"{self.base_url}/auth/api-key",
                headers=l1_headers,
                timeout=15,
            )
            resp.raise_for_status()
            creds = resp.json()

        self.api_key = creds["apiKey"]
        self.api_secret = creds["secret"]
        self.api_passphrase = creds["passphrase"]
        self._authenticated = True

        return creds

    # === Public endpoints (no auth required) ===

    def get_orderbook(self, token_id: str) -> dict:
        """Get order book for a token."""
        return self._get("/book", {"token_id": token_id})

    def get_price(self, token_id: str, side: str = "BUY") -> float:
        """Get best price for a token."""
        result = self._get("/price", {"token_id": token_id, "side": side})
        if isinstance(result, dict) and "price" in result:
            return float(result["price"])
        return 0.0

    def get_midpoint(self, token_id: str) -> float:
        """Get midpoint price for a token."""
        result = self._get("/midpoint", {"token_id": token_id})
        if isinstance(result, dict) and "midpoint" in result:
            return float(result["midpoint"])
        return 0.0

    def get_spread(self, token_id: str) -> float:
        """Get spread for a token."""
        result = self._get("/spread", {"token_id": token_id})
        if isinstance(result, dict) and "spread" in result:
            return float(result["spread"])
        return 0.0

    def get_last_trade_price(self, token_id: str) -> float:
        """Get last trade price."""
        result = self._get("/last-trade-price", {"token_id": token_id})
        if isinstance(result, dict) and "price" in result:
            return float(result["price"])
        return 0.0

    def get_price_history(self, condition_id: str, interval: str = "1d") -> list[dict]:
        """Get historical prices for a market."""
        return self._get("/prices-history", {"market": condition_id, "interval": interval})

    # === Authenticated endpoints ===

    def get_balance_allowance(self) -> dict:
        """Check USDC balance and allowances."""
        return self._auth_request("GET", "/balance-allowance")

    def post_order(self, order_data: dict) -> dict:
        """Place an order (authenticated)."""
        return self._auth_request("POST", "/order", order_data)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an order by ID."""
        return self._auth_request("DELETE", f"/order/{order_id}")

    def cancel_all_orders(self) -> dict:
        """Cancel all open orders."""
        return self._auth_request("DELETE", "/cancel-all")

    def get_open_orders(self) -> list[dict]:
        """Get all open orders."""
        return self._auth_request("GET", "/orders")

    def send_heartbeat(self) -> dict:
        """Send heartbeat to keep orders alive (every 10s recommended)."""
        return self._auth_request("POST", "/heartbeat")


class DataClient:
    """Data API client for positions, trades, and analytics."""

    def __init__(self):
        self.base_url = config.DATA_API_URL
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def _get(self, path: str, params: dict | None = None) -> Any:
        resp = self.session.get(f"{self.base_url}{path}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_positions(self, address: str, limit: int = 100) -> list[dict]:
        """Get open positions for an address."""
        return self._get("/positions", {"user": address, "sizeThreshold": "1", "limit": limit})

    def get_closed_positions(self, address: str, limit: int = 100) -> list[dict]:
        """Get closed positions for an address."""
        return self._get("/closed-positions", {"user": address, "limit": limit})

    def get_value(self, address: str) -> dict:
        """Get total value of all positions."""
        return self._get("/value", {"user": address})

    def get_trades(self, address: str, limit: int = 100) -> list[dict]:
        """Get trade history for an address."""
        return self._get("/trades", {"user": address, "limit": limit})