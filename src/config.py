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
    MAX_POSITION_USD: float = float(os.getenv("MAX_POSITION_USD", "3"))
    DEFAULT_TRADE_SIZE_USD: float = float(os.getenv("DEFAULT_TRADE_SIZE_USD", "2"))
    MAX_TOTAL_EXPOSURE_USD: float = float(os.getenv("MAX_TOTAL_EXPOSURE_USD", "8"))

    # === Safety limits ===
    MAX_DAILY_LOSS_USD: float = float(os.getenv("MAX_DAILY_LOSS_USD", "2"))
    MIN_EDGE_PCT: float = float(os.getenv("MIN_EDGE_PCT", "2.0"))
    MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "4"))

    # === Trailing Stop-Loss ===
    # Close a position if its current price has dropped >= this % from entry.
    # Set to 0 to disable.
    TRAILING_STOP_PCT: float = float(os.getenv("TRAILING_STOP_PCT", "30.0"))

    # === Auto-Compound ===
    # After a successful redemption, recalculate trade size as
    # COMPOUND_PCT × current USDC balance (clamped between MIN/MAX).
    AUTO_COMPOUND: bool = os.getenv("AUTO_COMPOUND", "false").lower() in ("true", "1", "yes")
    COMPOUND_PCT: float = float(os.getenv("COMPOUND_PCT", "0.20"))
    MIN_TRADE_SIZE_USD: float = float(os.getenv("MIN_TRADE_SIZE_USD", "1.0"))

    # === Scanning ===
    SCAN_INTERVAL_SEC: int = int(os.getenv("SCAN_INTERVAL_SEC", "120"))
    SCAN_CATEGORIES: list[str] = os.getenv(
        "SCAN_CATEGORIES", "crypto,politics,sports,finance"
    ).split(",")
    MIN_MARKET_VOLUME: float = float(os.getenv("MIN_MARKET_VOLUME", "500"))

    # === Smart Category Filtering ===
    # Per-category taker fee rates used to compute a fee-adjusted minimum edge.
    # effective_min_edge = max(MIN_EDGE_PCT, base_edge + fee_rate * 100 * FEE_EDGE_MULT)
    # FEE_EDGE_MULT of 1.5 means we require edge ≥ 1.5× the round-trip taker fee.
    CATEGORY_TAKER_FEES: dict = {
        "crypto":      0.07,
        "sports":      0.03,
        "finance":     0.04,
        "politics":    0.04,
        "economics":   0.05,
        "culture":     0.05,
        "geopolitics": 0.00,
    }
    FEE_EDGE_MULT: float = float(os.getenv("FEE_EDGE_MULT", "1.5"))

    # === Auto-redemption ===
    AUTO_REDEEM: bool = os.getenv("AUTO_REDEEM", "true").lower() in ("true", "1", "yes")
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
    RPC_URL: str = os.getenv("POLYGON_RPC_URL", "https://polygon.drpc.org")
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
        if cls.COMPOUND_PCT <= 0 or cls.COMPOUND_PCT > 1.0:
            errors.append("COMPOUND_PCT must be between 0 and 1.0 (e.g. 0.20 = 20%)")
        if cls.TRAILING_STOP_PCT < 0:
            errors.append("TRAILING_STOP_PCT must be >= 0 (set to 0 to disable)")
        return errors

    @classmethod
    def is_configured(cls) -> bool:
        return len(cls.validate()) == 0

    @classmethod
    def summary(cls) -> dict:
        return {
            "dry_run": cls.DRY_RUN,
            "default_trade_size_usd": cls.DEFAULT_TRADE_SIZE_USD,
            "max_position_usd": cls.MAX_POSITION_USD,
            "max_total_exposure_usd": cls.MAX_TOTAL_EXPOSURE_USD,
            "max_daily_loss_usd": cls.MAX_DAILY_LOSS_USD,
            "min_edge_pct": cls.MIN_EDGE_PCT,
            "max_open_positions": cls.MAX_OPEN_POSITIONS,
            "trailing_stop_pct": cls.TRAILING_STOP_PCT,
            "auto_compound": cls.AUTO_COMPOUND,
            "compound_pct": cls.COMPOUND_PCT,
            "scan_interval_sec": cls.SCAN_INTERVAL_SEC,
            "scan_categories": cls.SCAN_CATEGORIES,
            "auto_redeem": cls.AUTO_REDEEM,
            "private_key_set": bool(cls.PRIVATE_KEY),
        }


config = Config()
