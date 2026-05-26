"""Configuration management for the Polymarket bot."""

import os


class Config:
    """Bot configuration from environment variables."""

    # === Wallet ===
    PRIVATE_KEY: str = os.getenv("POLY_PRIVATE_KEY", "")

    # === Trading mode — always live ===
    DRY_RUN: bool = False

    # === Capital & position sizing ===
    MAX_POSITION_USD: float = float(os.getenv("MAX_POSITION_USD", "5"))
    DEFAULT_TRADE_SIZE_USD: float = float(os.getenv("DEFAULT_TRADE_SIZE_USD", "2"))
    MAX_TOTAL_EXPOSURE_USD: float = float(os.getenv("MAX_TOTAL_EXPOSURE_USD", "200"))

    # === Safety limits ===
    MAX_DAILY_LOSS_USD: float = float(os.getenv("MAX_DAILY_LOSS_USD", "20"))
    MIN_EDGE_PCT: float = float(os.getenv("MIN_EDGE_PCT", "1.0"))
    MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "10"))
    MAX_CONCURRENT_POSITIONS: int = int(os.getenv("MAX_CONCURRENT_POSITIONS", "20"))
    POSITION_COOLDOWN_MINUTES: int = int(os.getenv("POSITION_COOLDOWN_MINUTES", "360"))

    # === Trailing Stop-Loss ===
    TRAILING_STOP_PCT: float = float(os.getenv("TRAILING_STOP_PCT", "30.0"))

    # === Auto-Compound ===
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

    # === Position reconciler ===
    # How often (in scan cycles) to cross-check in-memory counters against the API.
    # Live mode only. Set to 0 to disable.
    RECONCILE_INTERVAL: int = int(os.getenv("RECONCILE_INTERVAL", "5"))

    # ─── Strategy Toggles ────────────────────────────────────────────────────
    MISPRICING_ENABLED: bool    = os.getenv("MISPRICING_ENABLED", "true").lower() in ("true","1","yes")
    NEAR_RESOLVED_ENABLED: bool = os.getenv("NEAR_RESOLVED_ENABLED", "true").lower() in ("true","1","yes")
    CORRELATED_ARB_ENABLED: bool= os.getenv("CORRELATED_ARB_ENABLED", "true").lower() in ("true","1","yes")
    LIQUIDITY_SNIPE_ENABLED: bool=os.getenv("LIQUIDITY_SNIPE_ENABLED", "true").lower() in ("true","1","yes")

    # ─── Mispricing ──────────────────────────────────────────────────────────
    AGGRESSIVE_EDGE: float  = float(os.getenv("AGGRESSIVE_EDGE",  "5.0"))
    CONSERVATIVE_EDGE: float= float(os.getenv("CONSERVATIVE_EDGE","1.0"))
    MIN_LIQUIDITY_USD: float= float(os.getenv("MIN_LIQUIDITY_USD", "1000"))
    MAX_SPREAD_PCT: float   = float(os.getenv("MAX_SPREAD_PCT",    "5.0"))

    # ─── Near-Resolved ───────────────────────────────────────────────────────
    NEAR_RESOLVED_MIN_EDGE: float  = float(os.getenv("NEAR_RESOLVED_MIN_EDGE", "3.0"))
    NEAR_RESOLVED_MAX_HOURS: float = float(os.getenv("NEAR_RESOLVED_MAX_HOURS","4.0"))
    NEAR_RESOLVED_AUTO_EXIT: bool  = os.getenv("NEAR_RESOLVED_AUTO_EXIT","true").lower() in ("true","1","yes")

    # ─── Correlated Arbitrage ────────────────────────────────────────────────
    CORRELATED_MIN_DIVERGENCE: float = float(os.getenv("CORRELATED_MIN_DIVERGENCE","5.0"))
    CORRELATED_SCAN_EVERY: int       = int(os.getenv("CORRELATED_SCAN_EVERY",      "5"))
    CORRELATED_MAX_POSITIONS: int    = int(os.getenv("CORRELATED_MAX_POSITIONS",   "3"))

    # ─── Liquidity Snipe ─────────────────────────────────────────────────────
    SNIPER_ENABLED: bool        = os.getenv("SNIPER_ENABLED","true").lower() in ("true","1","yes")
    SNIPER_TIER1_PRICE: float   = float(os.getenv("SNIPER_TIER1_PRICE","0.01"))
    SNIPER_TIER1_PCT: float     = float(os.getenv("SNIPER_TIER1_PCT",  "50"))
    SNIPER_TIER2_PRICE: float   = float(os.getenv("SNIPER_TIER2_PRICE","0.02"))
    SNIPER_TIER2_PCT: float     = float(os.getenv("SNIPER_TIER2_PCT",  "30"))
    SNIPER_TIER3_PRICE: float   = float(os.getenv("SNIPER_TIER3_PRICE","0.03"))
    SNIPER_TIER3_PCT: float     = float(os.getenv("SNIPER_TIER3_PCT",  "20"))
    ENDCYCLE_ENABLED: bool      = os.getenv("ENDCYCLE_ENABLED","true").lower() in ("true","1","yes")
    ENDCYCLE_MIN_MOVEMENT: float= float(os.getenv("ENDCYCLE_MIN_MOVEMENT","0.5"))
    ENDCYCLE_POSITION_USD: float= float(os.getenv("ENDCYCLE_POSITION_USD","25"))
    CRASH_REBOUND_ENABLED: bool       = os.getenv("CRASH_REBOUND_ENABLED","true").lower() in ("true","1","yes")
    CRASH_REBOUND_DROP_THRESHOLD: float= float(os.getenv("CRASH_REBOUND_DROP_THRESHOLD","15"))
    CRASH_REBOUND_HOLD_HOURS: float   = float(os.getenv("CRASH_REBOUND_HOLD_HOURS","6"))

    # === API endpoints ===
    CLOB_API_URL: str  = os.getenv("CLOB_API_URL",  "https://clob.polymarket.com")
    GAMMA_API_URL: str = os.getenv("GAMMA_API_URL", "https://gamma-api.polymarket.com")
    DATA_API_URL: str  = os.getenv("DATA_API_URL",  "https://data-api.polymarket.com")

    # === Logging ===
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE: str  = os.getenv("LOG_FILE",  "logs/bot.log")

    # === Polygon chain ===
    CHAIN_ID: int    = 137
    RPC_URL: str     = os.getenv("POLYGON_RPC_URL", "https://polygon.drpc.org")
    RPC_FALLBACKS: list[str] = [
        os.getenv("POLYGON_RPC_URL", "https://polygon.drpc.org"),
        "https://polygon-bor-rpc.publicnode.com",
    ]

    # === Token addresses on Polygon ===
    USDC_NATIVE:  str = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
    USDC_BRIDGED: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    PUSD: str         = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

    # === Polymarket V2 contract addresses (Polygon mainnet) ===
    COLLATERAL_ONRAMP:       str = "0x93070a847efEf7F70739046A929D47a521F5B8ee"
    COLLATERAL_OFFRAMP:      str = "0x2957922Eb93258b93368531d39fAcCA3B4dC5854"
    CTF_EXCHANGE_V2:         str = "0xE111180000d2663C0091e4f400237545B87B996B"
    NEG_RISK_CTF_EXCHANGE_V2:str = "0xe2222d279d744050d28e00520010520000310F59"
    CTF_CONTRACT:            str = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

    @classmethod
    def validate(cls) -> list[str]:
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
        return len(cls.validate()) == 0

    @classmethod
    def summary(cls) -> dict:
        return {
            "default_trade_size":   cls.DEFAULT_TRADE_SIZE_USD,
            "max_position_usd":     cls.MAX_POSITION_USD,
            "max_exposure":         cls.MAX_TOTAL_EXPOSURE_USD,
            "max_daily_loss":       cls.MAX_DAILY_LOSS_USD,
            "min_edge_pct":         cls.MIN_EDGE_PCT,
            "max_positions":        cls.MAX_OPEN_POSITIONS,
            "strategies": {
                "mispricing":    cls.MISPRICING_ENABLED,
                "near_resolved": cls.NEAR_RESOLVED_ENABLED,
                "correlated":    cls.CORRELATED_ARB_ENABLED,
                "sniper":        cls.LIQUIDITY_SNIPE_ENABLED,
            },
        }


config = Config()
