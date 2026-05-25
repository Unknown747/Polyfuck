"""Enhanced mispricing scanner.

Root cause of 0-result bug: Gamma API outcomePrices are complement prices
that ALWAYS sum to exactly 1.0 (e.g., YES=0.52, NO=0.48).  They can never
signal genuine buy-both arbitrage.

Fix: fetch the top N markets by volume from Gamma, then pull real CLOB ask
prices in parallel.  The CLOB ask represents what you actually pay to buy a
token.  If yes_ask + no_ask < 1.0, guaranteed profit exists.

Formula: edge = (1.0 - (yes_ask + no_ask)) * 100
Weighted score: edge * (1 + volume_liquidity_factor)
3-tier thresholds: CONSERVATIVE (<2%), NORMAL (2-5%), AGGRESSIVE (5%+)
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.config import config

if TYPE_CHECKING:
    from src.utils.api import GammaClient, ClobClient

logger = logging.getLogger("polymarket-bot")

# Max markets to check via CLOB per scan — include all active markets since
# lower-volume markets have wider bid-ask spreads = more edge for this strategy
_CLOB_CHECK_LIMIT = 2000
# Thread pool size — keep ≤ pool size (10) to avoid connection-pool warnings
_CLOB_WORKERS = 10


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
        """Return sorted list of mispricing opportunities (best edge first).

        Strategy:
          1. Fetch all active markets from Gamma (includes token IDs + volume).
          2. Filter to markets that have CLOB token IDs and pass volume filter.
          3. Deliberately include all active markets (not just top-volume) since
             low-volume markets have wider bid-ask spreads = more maker edge.
          4. Fetch CLOB BID prices in parallel (10 threads) — this is the price
             at which maker buy orders would fill.
          5. YES_bid + NO_bid < 1.0 always; when the gap exceeds the edge
             threshold it's profitable to place maker limit orders on both sides.
        """
        if not self.enabled:
            logger.warning("MispricingScanner disabled after repeated failures")
            return []

        base_edge = min_edge_pct or config.MIN_EDGE_PCT
        min_vol   = min_volume   or config.MIN_MARKET_VOLUME
        min_liq   = config.MIN_LIQUIDITY_USD

        t0 = time.time()
        all_markets = self._fetch_markets(categories)

        # Keep only markets with CLOB token IDs, deduplicated by conditionId
        seen_ids: set[str] = set()
        tokenized: list[dict] = []
        for m in all_markets:
            cid = m.get("conditionId", "") or m.get("slug", "")
            if cid and cid in seen_ids:
                continue
            if self._parse_tokens(m).get("yes"):
                seen_ids.add(cid)
                tokenized.append(m)

        # Sort by volume ascending — lower-volume markets have wider spreads = more edge
        def _vol(m: dict) -> float:
            return self._safe_float(m.get("volume24hr") or m.get("volumeNum", 0))

        tokenized.sort(key=_vol)          # ascending: wide-spread markets first
        candidates = tokenized[:_CLOB_CHECK_LIMIT]

        logger.info(
            "Mispricing: %d active markets → %d with CLOB token IDs → checking %d via BID prices",
            len(all_markets), len(tokenized), len(candidates),
        )

        opportunities: list[MispriceOpportunity] = []

        # Parallel CLOB price fetch
        with ThreadPoolExecutor(max_workers=_CLOB_WORKERS) as pool:
            future_map = {
                pool.submit(
                    self._evaluate_with_clob,
                    m,
                    self._fee_adjusted_edge(m.get("_category", ""), base_edge),
                    min_vol,
                    min_liq,
                ): m
                for m in candidates
            }
            for future in as_completed(future_map):
                try:
                    opp = future.result()
                    if opp:
                        cat = future_map[future].get("_category", "")
                        if cat:
                            opp.categories = [cat]
                        opportunities.append(opp)
                        self._category_stats[cat] = self._category_stats.get(cat, 0) + 1
                except Exception as exc:
                    logger.debug("Mispricing: CLOB check failed: %s", exc)

        elapsed = time.time() - t0
        self._consecutive_failures = 0
        logger.info(
            "Mispricing: checked %d markets via CLOB in %.1fs, found %d opportunities",
            len(candidates), elapsed, len(opportunities),
        )
        return sorted(opportunities, key=lambda o: o.weighted_score, reverse=True)

    def get_category_stats(self) -> dict[str, int]:
        return dict(self._category_stats)

    # ── Private ───────────────────────────────────────────────────────────────

    def _evaluate_with_clob(
        self,
        market: dict,
        min_edge: float,
        min_vol: float,
        min_liq: float,
    ) -> MispriceOpportunity | None:
        """Check a market for maker-order mispricing via real CLOB prices.

        Why bid prices?
        ─────────────
        YES_ask + NO_ask always > 1.0 in an efficient market (arbitrageurs
        drain the discount instantly → taker buy-both never works).

        YES_bid + NO_bid always < 1.0 by definition (bid < mid < ask).
        The key is by how much:
            bid_sum = 1.0 − total_spread
        When total_spread is large (wide market), the discount on maker
        orders is substantial.  Placing limit BUY orders at the current
        bid prices locks in the discount risk-free *when both fill* — and
        maker orders incur 0% fee on Polymarket.

        Signal: YES_bid + NO_bid < 1.0 − TAKER_FEE_FLAT − min_edge
        means net edge (after fee buffer) ≥ min_edge.
        """
        tokens = self._parse_tokens(market)
        yes_tid = tokens.get("yes", "")
        no_tid  = tokens.get("no", "")
        if not yes_tid or not no_tid:
            return None

        volume_24h   = self._safe_float(market.get("volume24hr"))
        total_volume = self._safe_float(market.get("volumeNum"))
        liquidity    = self._safe_float(market.get("liquidityNum"))

        if volume_24h < min_liq and total_volume < min_vol:
            return None

        try:
            # BUY side = best bid price (highest buy order in the book)
            # This is what you'd pay placing a maker buy at the current best bid.
            # YES_bid + NO_bid < 1.0 always; when the discount is large enough
            # (> min_edge + fee buffer) it's profitable to place both limit orders.
            yes_bid = self.clob.get_price(yes_tid, "BUY")
            no_bid  = self.clob.get_price(no_tid,  "BUY")
        except Exception as exc:
            logger.debug("Mispricing: CLOB price fetch failed for %s: %s",
                         market.get("question", "")[:40], exc)
            return None

        # Sanity-check: prices must be real market prices (not 0 for illiquid sides)
        if not (0.005 <= yes_bid <= 0.995) or not (0.005 <= no_bid <= 0.995):
            return None

        # Bids always sum to < 1.0; edge = total spread of the combined position
        price_sum = yes_bid + no_bid
        edge_pct  = (1.0 - price_sum) * 100   # gross discount from $1.00
        # Maker orders have 0% fee — use a small buffer for gas/slippage
        net_edge  = edge_pct - self.TAKER_FEE_FLAT

        if net_edge <= 0:
            return None
        if edge_pct < config.CONSERVATIVE_EDGE:
            return None
        if edge_pct < min_edge:
            return None

        spread_pct      = abs(yes_bid - no_bid) * 100
        vol_factor      = min(1.0, volume_24h / 50_000)
        liq_factor      = min(1.0, liquidity   / 20_000)
        combined_factor = (vol_factor + liq_factor) / 2
        weighted_score  = edge_pct * (1 + combined_factor)
        tier, multiplier = self._tier(edge_pct)

        logger.info(
            "Mispricing FOUND: %s | YES_bid=%.4f NO_bid=%.4f sum=%.4f edge=%.2f%% [%s]",
            market.get("question", "")[:60], yes_bid, no_bid, price_sum, edge_pct, tier,
        )

        return MispriceOpportunity(
            event_slug=      market.get("slug", "").split("-")[0] if market.get("slug") else "",
            event_title=     market.get("groupItemTitle", "") or market.get("question", "")[:40],
            market_slug=     market.get("slug", ""),
            market_question= market.get("question", ""),
            yes_price=       yes_bid,
            no_price=        no_bid,
            price_sum=       price_sum,
            edge_pct=        edge_pct,
            weighted_score=  weighted_score,
            net_edge=        net_edge,
            volume_24h=      volume_24h,
            total_volume=    total_volume,
            liquidity=       liquidity,
            spread_pct=      spread_pct,
            condition_id=    market.get("conditionId", ""),
            yes_token_id=    yes_tid,
            no_token_id=     no_tid,
            tier=            tier,
            position_multiplier= multiplier,
        )

    def _fee_adjusted_edge(self, category: str, base_edge: float) -> float:
        """Minimum edge required after flat maker-fee deduction.

        We use maker orders (0% fee) so the only cost is the 0.5% flat
        TAKER_FEE_FLAT safety buffer. We require edge > TAKER_FEE_FLAT
        so the net edge after fees is positive, but never require more
        than the configured base_edge.
        """
        return max(self.TAKER_FEE_FLAT, base_edge)

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
                        # Skip resolved/inactive individual markets
                        if market.get("closed") or not market.get("active", True):
                            continue
                        # Skip markets with no active CLOB order book
                        if market.get("enableOrderBook") is False:
                            continue
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
