"""Polymarket API clients - Gamma, CLOB (via py-clob-client-v2), and Data APIs."""

import requests
from typing import Any

from src.config import config

# Try to import the official client; fall back gracefully
try:
    from py_clob_client_v2 import ClobClient as OfficialClobClient, ApiCreds
    HAS_OFFICIAL_CLIENT = True
except ImportError:
    HAS_OFFICIAL_CLIENT = False


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
        self._client: Any = None  # Official py-clob-client-v2 instance

        if private_key and HAS_OFFICIAL_CLIENT:
            self.authenticate(private_key)

    def authenticate(self, private_key: str) -> dict:
        """Authenticate with the CLOB API using EIP-712 signing.

        Creates or derives API credentials from wallet private key.
        """
        if not HAS_OFFICIAL_CLIENT:
            raise RuntimeError(
                "py-clob-client-v2 is required for trading. "
                "Install with: pip install py-clob-client-v2"
            )

        from eth_account import Account
        account = Account.from_key(private_key)
        self.address = account.address

        # Create the official client with the private key
        self._client = OfficialClobClient(
            host=self.base_url,
            chain_id=137,  # Polygon mainnet
            key=private_key,
        )

        # Create or derive API credentials
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
        """Get the authenticated wallet address."""
        if self._client:
            return self._client.get_address()
        return self.address

    # === Public endpoints (no auth required) ===

    def get_orderbook(self, token_id: str) -> dict:
        """Get order book for a token."""
        if self._client:
            return self._client.get_order_book(token_id)
        raise RuntimeError("Not authenticated")

    def get_price(self, token_id: str, side: str = "BUY") -> float:
        """Get best price for a token."""
        if self._client:
            result = self._client.get_price(token_id, side)
            if isinstance(result, dict) and "price" in result:
                return float(result["price"])
            return float(result) if result else 0.0
        raise RuntimeError("Not authenticated")

    def get_midpoint(self, token_id: str) -> float:
        """Get midpoint price for a token."""
        if self._client:
            return self._client.get_midpoint(token_id)
        raise RuntimeError("Not authenticated")

    def get_spread(self, token_id: str) -> float:
        """Get spread for a token."""
        if self._client:
            return float(self._client.get_spread(token_id))
        raise RuntimeError("Not authenticated")

    def get_last_trade_price(self, token_id: str) -> float:
        """Get last trade price."""
        if self._client:
            return float(self._client.get_last_trade_price(token_id))
        raise RuntimeError("Not authenticated")

    def get_price_history(self, condition_id: str, interval: str = "1d") -> list[dict]:
        """Get historical prices for a market."""
        if self._client:
            return self._client.get_prices_history(
                {"market": condition_id, "interval": interval}
            )
        raise RuntimeError("Not authenticated")

    # === Authenticated endpoints ===

    def get_balance_allowance(self, asset_type: str = "COLLATERAL") -> dict:
        """Check USDC balance and allowances."""
        if not self._client:
            raise RuntimeError("Not authenticated")
        from py_clob_client_v2 import BalanceAllowanceParams, AssetType
        atype = AssetType.COLLATERAL if asset_type == "COLLATERAL" else AssetType.CONDITIONAL
        return self._client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=atype)
        )

    def post_order(self, order_data: dict) -> dict:
        """Place an order (authenticated). Uses official client."""
        if not self._client:
            raise RuntimeError("Not authenticated")
        # This will be called via create_and_post_order in the trader
        # For direct use, pass the signed order from create_order
        return self._client.post_order(order_data)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an order by ID."""
        if not self._client:
            raise RuntimeError("Not authenticated")
        from py_clob_client_v2 import OrderPayload
        return self._client.cancel_order(OrderPayload(order_id=order_id))

    def cancel_all_orders(self) -> dict:
        """Cancel all open orders."""
        if not self._client:
            raise RuntimeError("Not authenticated")
        return self._client.cancel_all()

    def get_open_orders(self) -> list[dict]:
        """Get all open orders."""
        if not self._client:
            raise RuntimeError("Not authenticated")
        return self._client.get_open_orders()

    def send_heartbeat(self) -> dict:
        """Send heartbeat to keep orders alive (every 10s recommended)."""
        if not self._client:
            raise RuntimeError("Not authenticated")
        return self._client.post_heartbeat()

    def create_and_post_order(self, order_args, options=None, order_type="GTC"):
        """Create and post an order in one step.
        
        Args:
            order_args: OrderArgsV2 or MarketOrderArgsV2
            options: CreateOrderOptions (tick_size, etc.)
            order_type: "GTC", "FOK", "GTD", etc.
        """
        if not self._client:
            raise RuntimeError("Not authenticated")
        from py_clob_client_v2 import OrderType
        ot = OrderType(order_type)
        return self._client.create_and_post_order(order_args, options, ot)


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