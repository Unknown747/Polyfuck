"""Configuration management for the Polymarket bot."""

import os


class Config:
    """Bot configuration from environment variables."""

    # Required
    PRIVATE_KEY: str = os.getenv("POLY_PRIVATE_KEY", "")

    # Trading mode
    DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")

    # Safety limits
    MAX_POSITION_USD: float = float(os.getenv("MAX_POSITION_USD", "50"))
    MAX_DAILY_LOSS_USD: float = float(os.getenv("MAX_DAILY_LOSS_USD", "20"))
    MIN_EDGE_PCT: float = float(os.getenv("MIN_EDGE_PCT", "3.0"))
    MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "10"))

    # Scanning
    SCAN_INTERVAL_SEC: int = int(os.getenv("SCAN_INTERVAL_SEC", "60"))
    SCAN_CATEGORIES: list[str] = os.getenv(
        "SCAN_CATEGORIES", "crypto,politics,sports,finance"
    ).split(",")

    # API endpoints
    CLOB_API_URL: str = os.getenv("CLOB_API_URL", "https://clob.polymarket.com")
    GAMMA_API_URL: str = os.getenv("GAMMA_API_URL", "https://gamma-api.polymarket.com")
    DATA_API_URL: str = os.getenv("DATA_API_URL", "https://data-api.polymarket.com")

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE: str = os.getenv("LOG_FILE", "logs/bot.log")

    # Polygon chain ID
    CHAIN_ID: int = 137  # Polygon mainnet

    # Token addresses on Polygon
    USDC_NATIVE: str = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
    USDC_BRIDGED: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    PUSD: str = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

    # Polymarket V2 contract addresses (Polygon mainnet)
    COLLATERAL_ONRAMP: str = "0x93070a847efEf7F70739046A929D47a521F5B8ee"
    COLLATERAL_OFFRAMP: str = "0x2957922Eb93258b93368531d39fAcCA3B4dC5854"
    CTF_EXCHANGE_V2: str = "0xE111180000d2663C0091e4f400237545B87B996B"
    NEG_RISK_CTF_EXCHANGE_V2: str = "0xe2222d279d744050d28e00520010520000310F59"

    @classmethod
    def validate(cls) -> list[str]:
        """Return list of configuration errors."""
        errors = []
        if not cls.PRIVATE_KEY:
            errors.append("POLY_PRIVATE_KEY is required - run 'python -m src.wallet.create_wallet' to create one")
        elif not cls.PRIVATE_KEY.startswith("0x") or len(cls.PRIVATE_KEY) != 66:
            errors.append("POLY_PRIVATE_KEY must be a 66-character hex string starting with 0x")
        return errors

    @classmethod
    def is_configured(cls) -> bool:
        """Check if bot is ready to run."""
        return len(cls.validate()) == 0

    @classmethod
    def summary(cls) -> dict:
        """Return config summary for logging (no secrets)."""
        return {
            "dry_run": cls.DRY_RUN,
            "max_position_usd": cls.MAX_POSITION_USD,
            "max_daily_loss_usd": cls.MAX_DAILY_LOSS_USD,
            "min_edge_pct": cls.MIN_EDGE_PCT,
            "max_open_positions": cls.MAX_OPEN_POSITIONS,
            "scan_interval_sec": cls.SCAN_INTERVAL_SEC,
            "scan_categories": cls.SCAN_CATEGORIES,
            "private_key_set": bool(cls.PRIVATE_KEY),
        }


config = Config()
