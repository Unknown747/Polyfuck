"""Polymarket API clients — Gamma (discovery), CLOB (trading), Data (analytics)."""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import Any

from src.config import config

try:
    from py_clob_client_v2 import ClobClient as OfficialClobClient, ApiCreds
    HAS_OFFICIAL_CLIENT = True
except ImportError:
    HAS_OFFICIAL_CLIENT = False


def _make_session() -> requests.Session:
    """Create a requests Session with automatic retry on transient errors."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"Accept": "application/json"})
    return session


class GammaClient:
    """Public Gamma API client — no auth required. Market discovery and metadata."""

    def __init__(self):
        self.base_url = config.GAMMA_API_URL
        self.session = _make_session()

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
        params = {"limit": limit, "offset": offset, "active": "true", "closed": "false"}
        if tag:
            params["tag"] = tag
        return self._get("/events", params)

    def get_event_by_slug(self, slug: str) -> dict:
        """Get event by its URL slug."""
        return self._get(f"/events/slug/{slug}")


class ClobClient:
    """CLOB API client wrapping the official py-clob-client-v2.

    Handles authentication, order placement, balance queries, etc.
    """

    def __init__(self, private_key: str | None = None):
        self.base_url = config.CLOB_API_URL
        self.api_key: str = ""
        self.api_secret: str = ""
        self.api_passphrase: str = ""
        self.address: str = ""
        self._authenticated = False
        self._client: Any = None
        self._public_session = _make_session()

        if private_key and HAS_OFFICIAL_CLIENT:
            self.authenticate(private_key)

    def authenticate(self, private_key: str) -> dict:
        """Authenticate with the CLOB API using EIP-712 signing."""
        if not HAS_OFFICIAL_CLIENT:
            raise RuntimeError(
                "py-clob-client-v2 is required for trading. "
                "Install with: pip install py-clob-client-v2"
            )

        from eth_account import Account
        account = Account.from_key(private_key)
        self.address = account.address

        self._client = OfficialClobClient(
            host=self.base_url,
            chain_id=137,
            key=private_key,
        )

        creds = self._client.create_or_derive_api_key()
        self._client.set_api_creds(creds)

        self.api_key = creds.api_key
        self.api_secret = creds.api_secret
        self.api_passphrase = creds.api_passphrase
        self._authenticated = True

        return {
            "apiKey": creds.api_key,
            "secret": creds.api_secret,
            "passphrase": creds.api_passphrase,
        }

    def get_address(self) -> str:
        if self._client:
            return self._client.get_address()
        return self.address

    # === Public endpoints (no auth required) ===
    # These use a direct HTTP fallback so they work even without a private key.

    def _public_get(self, path: str, params: dict | None = None) -> Any:
        """Direct HTTP GET against the CLOB REST API — no auth needed."""
        resp = self._public_session.get(
            f"{self.base_url}{path}", params=params, timeout=15
        )
        resp.raise_for_status()
        return resp.json()

    def get_orderbook(self, token_id: str) -> dict:
        if self._client:
            return self._client.get_order_book(token_id)
        return self._public_get("/book", {"token_id": token_id})

    def get_price(self, token_id: str, side: str = "BUY") -> float:
        if self._client:
            result = self._client.get_price(token_id, side)
            if isinstance(result, dict) and "price" in result:
                return float(result["price"])
            return float(result) if result else 0.0
        data = self._public_get("/price", {"token_id": token_id, "side": side})
        return float(data.get("price", 0) or 0)

    def get_midpoint(self, token_id: str) -> float:
        if self._client:
            return self._client.get_midpoint(token_id)
        data = self._public_get("/midpoint", {"token_id": token_id})
        return float(data.get("mid", 0) or 0)

    def get_spread(self, token_id: str) -> float:
        if self._client:
            return float(self._client.get_spread(token_id))
        data = self._public_get("/spread", {"token_id": token_id})
        return float(data.get("spread", 0) or 0)

    def get_last_trade_price(self, token_id: str) -> float:
        if self._client:
            return float(self._client.get_last_trade_price(token_id))
        data = self._public_get("/last-trade-price", {"token_id": token_id})
        return float(data.get("price", 0) or 0)

    def get_price_history(self, condition_id: str, interval: str = "1d") -> list[dict]:
        if self._client:
            return self._client.get_prices_history(
                {"market": condition_id, "interval": interval}
            )
        return self._public_get("/prices-history", {"market": condition_id, "interval": interval})

    # === Authenticated endpoints ===

    def get_balance_allowance(self, asset_type: str = "COLLATERAL") -> dict:
        if not self._client:
            raise RuntimeError("Not authenticated")
        from py_clob_client_v2 import BalanceAllowanceParams, AssetType
        atype = AssetType.COLLATERAL if asset_type == "COLLATERAL" else AssetType.CONDITIONAL
        return self._client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=atype)
        )

    def post_order(self, order_data: dict) -> dict:
        """Place a pre-signed order directly."""
        if not self._client:
            raise RuntimeError("Not authenticated")
        return self._client.post_order(order_data)

    def cancel_order(self, order_id: str) -> dict:
        if not self._client:
            raise RuntimeError("Not authenticated")
        from py_clob_client_v2 import OrderPayload
        return self._client.cancel_order(OrderPayload(order_id=order_id))

    def cancel_all_orders(self) -> dict:
        if not self._client:
            raise RuntimeError("Not authenticated")
        return self._client.cancel_all()

    def get_open_orders(self) -> list[dict]:
        if not self._client:
            raise RuntimeError("Not authenticated")
        return self._client.get_open_orders()

    def send_heartbeat(self) -> dict:
        """Send heartbeat to keep GTD orders alive (call every ~10s)."""
        if not self._client:
            raise RuntimeError("Not authenticated")
        return self._client.post_heartbeat()


class DataClient:
    """Data API client for positions, trades, and analytics."""

    def __init__(self):
        self.base_url = config.DATA_API_URL
        self.session = _make_session()

    def _get(self, path: str, params: dict | None = None) -> Any:
        resp = self.session.get(f"{self.base_url}{path}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_positions(self, address: str, limit: int = 100) -> list[dict]:
        """Get open positions for an address."""
        return self._get(
            "/positions",
            {"user": address, "sizeThreshold": "0.01", "limit": limit},
        )

    def get_closed_positions(self, address: str, limit: int = 100) -> list[dict]:
        """Get closed positions for an address."""
        return self._get("/closed-positions", {"user": address, "limit": limit})

    def get_value(self, address: str) -> dict:
        """Get total portfolio value."""
        return self._get("/value", {"user": address})

    def get_trades(self, address: str, limit: int = 100) -> list[dict]:
        """Get trade history for an address."""
        return self._get("/trades", {"user": address, "limit": limit})
