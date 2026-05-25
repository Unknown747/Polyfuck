"""Configuration management for the Polymarket bot."""

import os


class Config:
    """Bot configuration from environment variables.

    All tuneable values live here. For a $10 USDT capital account, the
    defaults below are already calibrated:
      - MAX_POSITION_USD = 3  (max 30% of capital per trade)
      - DEFAULT_TRADE_SIZE_USD = 2  ($2 per arb leg by default)
      - MAX_DAILY_LOSS_USD = 2  (hard stop at 20% daily loss)
      - MAX_OPEN_POSITIONS = 4  (no more than 4 concurrent arb pairs)
      - MIN_EDGE_PCT = 2.0  (catch tighter edges for small capital)
    """

    # === Wallet ===
    PRIVATE_KEY: str = os.getenv("POLY_PRIVATE_KEY", "")

    # === Trading mode ===
    DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")

    # === Capital & position sizing (calibrated for $10 USDT account) ===
    # Maximum USD to risk on a single position (30% of $10)
    MAX_POSITION_USD: float = float(os.getenv("MAX_POSITION_USD", "3"))
    # Default trade size per mispricing opportunity
    DEFAULT_TRADE_SIZE_USD: float = float(os.getenv("DEFAULT_TRADE_SIZE_USD", "2"))
    # Stop trading if total open exposure exceeds this
    MAX_TOTAL_EXPOSURE_USD: float = float(os.getenv("MAX_TOTAL_EXPOSURE_USD", "8"))

    # === Safety limits ===
    # Hard-stop daily loss limit (20% of $10)
    MAX_DAILY_LOSS_USD: float = float(os.getenv("MAX_DAILY_LOSS_USD", "2"))
    # Minimum mispricing edge (%) to enter — lower = more trades with small capital
    MIN_EDGE_PCT: float = float(os.getenv("MIN_EDGE_PCT", "2.0"))
    # Maximum concurrent arb positions (YES+NO pairs count as 1)
    MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "4"))

    # === Scanning ===
    # Seconds between market scans (2 min to be gentle on public APIs)
    SCAN_INTERVAL_SEC: int = int(os.getenv("SCAN_INTERVAL_SEC", "120"))
    # Categories to scan (comma-separated)
    SCAN_CATEGORIES: list[str] = os.getenv(
        "SCAN_CATEGORIES", "crypto,politics,sports,finance"
    ).split(",")
    # Minimum 24h volume (USD) for a market to be considered
    MIN_MARKET_VOLUME: float = float(os.getenv("MIN_MARKET_VOLUME", "500"))

    # === Auto-redemption ===
    # Whether to automatically redeem resolved winning positions on-chain
    AUTO_REDEEM: bool = os.getenv("AUTO_REDEEM", "true").lower() in ("true", "1", "yes")
    # Check for redeemable positions every N scans
    REDEEM_CHECK_INTERVAL: int = int(os.getenv("REDEEM_CHECK_INTERVAL", "5"))

    # === API endpoints ===
    CLOB_API_URL: str = os.getenv("CLOB_API_URL", "https://clob.polymarket.com")
    GAMMA_API_URL: str = os.getenv("GAMMA_API_URL", "https://gamma-api.polymarket.com")
    DATA_API_URL: str = os.getenv("DATA_API_URL", "https://data-api.polymarket.com")

    # === Logging ===
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE: str = os.getenv("LOG_FILE", "logs/bot.log")

    # === Polygon chain ===
    CHAIN_ID: int = 137
    # BUG FIX: polygon-rpc.com is unreachable from Replit servers.
    # polygon.drpc.org is the most reliable public RPC from cloud environments.
    RPC_URL: str = os.getenv("POLYGON_RPC_URL", "https://polygon.drpc.org")

    # Ordered fallback list — tried in sequence if primary RPC fails.
    # Verified reachable from Replit cloud (polygon-mainnet.public.blastapi.io
    # and rpc-mainnet.maticvigil.com are NOT reachable from Replit servers).
    RPC_FALLBACKS: list[str] = [
        os.getenv("POLYGON_RPC_URL", "https://polygon.drpc.org"),
        "https://polygon-bor-rpc.publicnode.com",
    ]

    # === Token addresses on Polygon ===
    USDC_NATIVE: str = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
    USDC_BRIDGED: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    PUSD: str = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

    # === Polymarket V2 contract addresses (Polygon mainnet) ===
    COLLATERAL_ONRAMP: str = "0x93070a847efEf7F70739046A929D47a521F5B8ee"
    COLLATERAL_OFFRAMP: str = "0x2957922Eb93258b93368531d39fAcCA3B4dC5854"
    CTF_EXCHANGE_V2: str = "0xE111180000d2663C0091e4f400237545B87B996B"
    NEG_RISK_CTF_EXCHANGE_V2: str = "0xe2222d279d744050d28e00520010520000310F59"
    # Gnosis Conditional Token Framework contract (handles redemption)
    CTF_CONTRACT: str = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

    @classmethod
    def validate(cls) -> list[str]:
        """Return list of configuration errors."""
        errors = []
        if not cls.PRIVATE_KEY:
            errors.append(
                "POLY_PRIVATE_KEY is not set — add it to Replit Secrets. "
                "Run 'python -m src.wallet.wallet' to generate a new wallet."
            )
        elif not cls.PRIVATE_KEY.startswith("0x") or len(cls.PRIVATE_KEY) != 66:
            errors.append("POLY_PRIVATE_KEY must be a 66-character hex string starting with 0x")
        if cls.DEFAULT_TRADE_SIZE_USD > cls.MAX_POSITION_USD:
            errors.append(
                f"DEFAULT_TRADE_SIZE_USD ({cls.DEFAULT_TRADE_SIZE_USD}) "
                f"exceeds MAX_POSITION_USD ({cls.MAX_POSITION_USD})"
            )
        return errors

    @classmethod
    def is_configured(cls) -> bool:
        """Check if bot is ready for live trading."""
        return len(cls.validate()) == 0

    @classmethod
    def summary(cls) -> dict:
        """Return config summary for logging (no secrets)."""
        return {
            "dry_run": cls.DRY_RUN,
            "default_trade_size_usd": cls.DEFAULT_TRADE_SIZE_USD,
            "max_position_usd": cls.MAX_POSITION_USD,
            "max_total_exposure_usd": cls.MAX_TOTAL_EXPOSURE_USD,
            "max_daily_loss_usd": cls.MAX_DAILY_LOSS_USD,
            "min_edge_pct": cls.MIN_EDGE_PCT,
            "max_open_positions": cls.MAX_OPEN_POSITIONS,
            "scan_interval_sec": cls.SCAN_INTERVAL_SEC,
            "scan_categories": cls.SCAN_CATEGORIES,
            "auto_redeem": cls.AUTO_REDEEM,
            "private_key_set": bool(cls.PRIVATE_KEY),
        }


config = Config()
