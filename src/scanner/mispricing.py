"""Enhanced mispricing scanner.

Formula: edge = (1.0 - (yes_price + no_price)) * 100
Weighted score: edge * (1 + volume_liquidity_factor)
3-tier thresholds: CONSERVATIVE (<2%), NORMAL (2-5%), AGGRESSIVE (5%+)
Filters: volume_24h > MIN_LIQUIDITY_USD, spread <= MAX_SPREAD_PCT
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.config import config

if TYPE_CHECKING:
    from src.utils.api import GammaClient, ClobClient

logger = logging.getLogger("polymarket-bot")


@dataclass
class MispriceOpportunity:
    """Enhanced mispricing opportunity with weighted scoring and tiering."""

    event_slug:       str
    event_title:      str
    market_slug:      str
    market_question:  str
    yes_price:        float
    no_price:         float
    price_sum:        float
    edge_pct:         float
    weighted_score:   float
    net_edge:         float
    volume_24h:       float  = 0.0
    total_volume:     float  = 0.0
    liquidity:        float  = 0.0
    spread_pct:       float  = 0.0
    condition_id:     str    = ""
    yes_token_id:     str    = ""
    no_token_id:      str    = ""
    categories:       list[str] = field(default_factory=list)
    tier:             str    = "NORMAL"
    position_multiplier: float = 1.0

    @property
    def direction(self) -> str:
        return "BUY_BOTH" if self.price_sum < 1.0 else "SELL_BOTH"

    @property
    def guaranteed_profit_per_share(self) -> float:
        return abs(1.0 - self.price_sum)

    def __str__(self) -> str:
        return (
            f"[{self.tier}] {self.market_question[:60]} "
            f"| Edge={self.edge_pct:.2f}% NetEdge={self.net_edge:.2f}% "
            f"| Score={self.weighted_score:.2f} Vol24h=${self.volume_24h:.0f}"
        )


class MispricingScanner:
    """Scans Polymarket for mispriced binary markets."""

    # Maker orders are 0% fee; use 0.5% as a conservative flat estimate
    TAKER_FEE_FLAT = 0.5

    def __init__(self, gamma=None, clob=None):
        from src.utils.api import GammaClient, ClobClient
        self.gamma: GammaClient = gamma or GammaClient()
        self.clob:  ClobClient  = clob  or ClobClient()
        self._category_stats: dict[str, int] = {}
        self._consecutive_failures: int = 0
        self.enabled: bool = True

    # ── Public API ────────────────────────────────────────────────────────────

    def scan(
        self,
        categories: list[str] | None = None,
        min_edge_pct: float | None = None,
        min_volume: float | None = None,
    ) -> list[MispriceOpportunity]:
        """Return sorted list of mispricing opportunities (best edge first)."""
        if not self.enabled:
            logger.warning("MispricingScanner disabled after repeated failures")
            return []

        base_edge   = min_edge_pct  or config.MIN_EDGE_PCT
        min_vol     = min_volume    or config.MIN_MARKET_VOLUME
        min_liq     = config.MIN_LIQUIDITY_USD
        max_spread  = config.MAX_SPREAD_PCT

        opportunities: list[MispriceOpportunity] = []
        markets = self._fetch_markets(categories)
        checked = 0

        for market in markets:
            checked += 1
            try:
                category    = market.get("_category", "")
                cat_edge    = self._fee_adjusted_edge(category, base_edge)
                opp = self._evaluate(market, cat_edge, min_vol, min_liq, max_spread)
                if opp:
                    if category:
                        opp.categories = [category]
                    opportunities.append(opp)
                    self._category_stats[category] = self._category_stats.get(category, 0) + 1
            except Exception as exc:
                logger.debug("Mispricing: skipped market: %s", exc)

        self._consecutive_failures = 0
        logger.info("Mispricing: checked %d markets, found %d opportunities", checked, len(opportunities))
        return sorted(opportunities, key=lambda o: o.weighted_score, reverse=True)

    def get_category_stats(self) -> dict[str, int]:
        return dict(self._category_stats)

    # ── Private ───────────────────────────────────────────────────────────────

    def _fee_adjusted_edge(self, category: str, base_edge: float) -> float:
        """Minimum edge required after flat maker-fee deduction.

        We use maker orders (0% fee) so the only cost is the 0.5% flat
        TAKER_FEE_FLAT safety buffer.  The old complex formula
        (fee_rate * 50 * FEE_EDGE_MULT) produced 5.25% for crypto which
        is almost never achievable — it was blocking all opportunities.
        New formula: max(base_edge, TAKER_FEE_FLAT + 0.5) ensures we
        only enter when the net edge after fees is positive.
        """
        return max(base_edge, self.TAKER_FEE_FLAT + 0.5)

    def _evaluate(
        self,
        market: dict,
        min_edge: float,
        min_vol: float,
        min_liq: float,
        max_spread: float,
    ) -> MispriceOpportunity | None:
        prices = self._parse_prices(market)
        if not prices or len(prices) < 2:
            return None

        yes_price, no_price = prices[0], prices[1]

        if not (0.01 <= yes_price <= 0.99) or not (0.01 <= no_price <= 0.99):
            return None

        # Only trade when YES+NO < 1 (guaranteed arbitrage buy-both direction)
        price_sum = yes_price + no_price
        if price_sum >= 1.0:
            return None

        edge_pct = (1.0 - price_sum) * 100
        net_edge = edge_pct - self.TAKER_FEE_FLAT

        if net_edge <= 0:
            return None
        if edge_pct < config.CONSERVATIVE_EDGE:
            return None
        if edge_pct < min_edge:
            return None

        spread_pct = abs(yes_price - no_price) * 100
        if spread_pct > max_spread:
            return None  # BUG FIX: was `pass` — spread filter was dead code

        volume_24h   = self._safe_float(market.get("volume24hr"))
        total_volume = self._safe_float(market.get("volumeNum"))
        liquidity    = self._safe_float(market.get("liquidityNum"))

        if volume_24h < min_liq and total_volume < min_vol:
            return None

        vol_factor      = min(1.0, volume_24h / 50_000)
        liq_factor      = min(1.0, liquidity   / 20_000)
        combined_factor = (vol_factor + liq_factor) / 2
        weighted_score  = edge_pct * (1 + combined_factor)

        tier, multiplier = self._tier(edge_pct)

        tokens = self._parse_tokens(market)
        return MispriceOpportunity(
            event_slug=   market.get("slug", "").split("-")[0] if market.get("slug") else "",
            event_title=  market.get("groupItemTitle", "") or market.get("question", "")[:40],
            market_slug=  market.get("slug", ""),
            market_question= market.get("question", ""),
            yes_price=    yes_price,
            no_price=     no_price,
            price_sum=    price_sum,
            edge_pct=     edge_pct,
            weighted_score= weighted_score,
            net_edge=     net_edge,
            volume_24h=   volume_24h,
            total_volume= total_volume,
            liquidity=    liquidity,
            spread_pct=   spread_pct,
            condition_id= market.get("conditionId", ""),
            yes_token_id= tokens.get("yes", ""),
            no_token_id=  tokens.get("no", ""),
            tier=         tier,
            position_multiplier= multiplier,
        )

    @staticmethod
    def _tier(edge_pct: float) -> tuple[str, float]:
        if edge_pct >= config.AGGRESSIVE_EDGE:
            return ("AGGRESSIVE", 2.0)
        if edge_pct >= config.MIN_EDGE_PCT:
            return ("NORMAL", 1.0)
        return ("CONSERVATIVE", 0.5)

    def _fetch_markets(self, categories: list[str] | None) -> list[dict]:
        all_markets: list[dict] = []
        cats = categories or config.SCAN_CATEGORIES
        for cat in cats:
            try:
                t0 = time.time()
                events = self.gamma.get_events(tag=cat, limit=100)
                elapsed = time.time() - t0
                if elapsed > 2.0:
                    logger.warning("Mispricing: slow API response %.1fs for category %s", elapsed, cat)
                for event in events:
                    for market in event.get("markets", []):
                        market["_category"] = cat
                        all_markets.append(market)
            except Exception as exc:
                logger.warning("Mispricing: failed to fetch category %s: %s", cat, exc)
                self._consecutive_failures += 1
        return all_markets

    @staticmethod
    def _parse_prices(market: dict) -> list[float] | None:
        raw = market.get("outcomePrices")
        if not raw:
            return None
        if isinstance(raw, str):
            try:
                prices = json.loads(raw)
            except json.JSONDecodeError:
                return None
        elif isinstance(raw, list):
            prices = raw
        else:
            return None
        try:
            return [float(p) for p in prices]
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_tokens(market: dict) -> dict[str, str]:
        raw = market.get("clobTokenIds")
        if not raw:
            return {}
        if isinstance(raw, str):
            try:
                tokens = json.loads(raw)
            except json.JSONDecodeError:
                return {}
        elif isinstance(raw, list):
            tokens = raw
        else:
            return {}
        if len(tokens) >= 2:
            return {"yes": tokens[0], "no": tokens[1]}
        return {}

    @staticmethod
    def _safe_float(val) -> float:
        try:
            return float(val or 0)
        except (ValueError, TypeError):
            return 0.0
