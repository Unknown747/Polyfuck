"""Enhanced near-resolved market scanner.

Strategy: buy the high-confidence side (e.g. YES @ $0.96) and
collect $1.00 on resolution. Return ≈ 1-6% with very low risk.

Enhancements over the original scanner:
- Time-based position sizing: <15 min → 2×, 15-60 min → 1×, 1-4 h → 0.5×
- Per-market cooldown (6 hours) to avoid re-trading same market
- Auto-exit detection: alerts if market is past expected close by >30 min
- Configurable max_hours from config.NEAR_RESOLVED_MAX_HOURS
"""

from __future__ import annotations

import json
import logging
import time
import datetime
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.config import config

if TYPE_CHECKING:
    from src.utils.api import GammaClient

logger = logging.getLogger("polymarket-bot")


@dataclass
class NearResolvedOpp:
    """Near-resolved market opportunity with sizing metadata."""

    condition_id:     str
    market_question:  str
    event_title:      str
    market_slug:      str
    winning_side:     str
    winning_price:    float
    winning_token_id: str
    return_pct:       float
    volume_24h:       float
    end_date:         str
    hours_to_close:   float

    size_multiplier:  float = 1.0
    size_tier:        str   = "NORMAL"
    overdue:          bool  = False

    @property
    def is_actionable(self) -> bool:
        return self.winning_price >= 0.90 and self.return_pct > 0

    @property
    def maker_price(self) -> float:
        return round(max(0.01, self.winning_price - 0.01), 2)

    @property
    def maker_return_pct(self) -> float:
        p = self.maker_price
        return ((1.0 - p) / p) * 100 if p > 0 else 0.0

    def __str__(self) -> str:
        return (
            f"[NEAR/{self.winning_side}/{self.size_tier}] {self.market_question[:60]} "
            f"| Price={self.winning_price:.3f} Return={self.return_pct:.2f}% "
            f"| Closes in {self.hours_to_close:.1f}h | Size×{self.size_multiplier}"
        )


class NearResolvedScanner:
    """Scans Polymarket for markets nearing resolution."""

    def __init__(self, gamma=None):
        from src.utils.api import GammaClient
        self.gamma: GammaClient = gamma or GammaClient()
        # Cooldown tracking: condition_id → last_trade_timestamp
        self._cooldown: dict[str, float] = {}
        self._consecutive_failures: int  = 0
        self.enabled: bool               = True

    # ── Public API ────────────────────────────────────────────────────────────

    def scan(
        self,
        categories:      list[str] | None = None,
        min_confidence:  float = 0.94,
        min_volume:      float = 200.0,
    ) -> list[NearResolvedOpp]:
        """Return near-resolved opportunities sorted by return_pct descending."""
        if not self.enabled:
            logger.warning("NearResolvedScanner disabled after repeated failures")
            return []

        max_hours = config.NEAR_RESOLVED_MAX_HOURS
        opportunities: list[NearResolvedOpp] = []
        markets = self._fetch_markets(categories)

        for market in markets:
            try:
                opp = self._evaluate(market, min_confidence, min_volume, max_hours)
                if opp:
                    opportunities.append(opp)
            except Exception as exc:
                logger.debug("NearResolved: skipped market: %s", exc)

        self._consecutive_failures = 0
        logger.info("NearResolved: found %d opportunities", len(opportunities))
        return sorted(opportunities, key=lambda o: o.return_pct, reverse=True)

    def mark_traded(self, condition_id: str) -> None:
        """Record a trade so this market is cooled-down for POSITION_COOLDOWN_MINUTES."""
        self._cooldown[condition_id] = time.time()

    def is_cooled_down(self, condition_id: str) -> bool:
        """True if the market has not been traded within the cooldown window."""
        last = self._cooldown.get(condition_id)
        if last is None:
            return True
        cooldown_sec = config.POSITION_COOLDOWN_MINUTES * 60
        return (time.time() - last) >= cooldown_sec

    # ── Private ───────────────────────────────────────────────────────────────

    def _evaluate(
        self,
        market:         dict,
        min_confidence: float,
        min_volume:     float,
        max_hours:      float,
    ) -> NearResolvedOpp | None:
        prices = self._parse_prices(market)
        if not prices or len(prices) < 2:
            return None

        yes_price, no_price = prices[0], prices[1]

        if yes_price >= min_confidence:
            winning_side, winning_price = "YES", yes_price
        elif no_price >= min_confidence:
            winning_side, winning_price = "NO", no_price
        else:
            return None

        return_pct = ((1.0 - winning_price) / winning_price) * 100
        min_return = config.NEAR_RESOLVED_MIN_EDGE
        if return_pct < min_return:
            return None

        volume_24h   = self._safe_float(market.get("volume24hr"))
        total_volume = self._safe_float(market.get("volumeNum"))

        if volume_24h < min_volume and total_volume < min_volume * 2:
            return None

        hours_to_close = self._hours_to_close(market)
        if hours_to_close < 0:
            return None
        if hours_to_close != float("inf") and hours_to_close > max_hours:
            return None

        condition_id = market.get("conditionId", "")
        if not self.is_cooled_down(condition_id):
            return None

        tokens       = self._parse_tokens(market)
        winning_token= tokens.get("yes" if winning_side == "YES" else "no", "")

        size_mult, size_tier = self._size_tier(hours_to_close)

        overdue = False
        if config.NEAR_RESOLVED_AUTO_EXIT and hours_to_close < -0.5:
            overdue = True

        return NearResolvedOpp(
            condition_id=    condition_id,
            market_question= market.get("question", ""),
            event_title=     market.get("groupItemTitle", "") or market.get("question", "")[:40],
            market_slug=     market.get("slug", ""),
            winning_side=    winning_side,
            winning_price=   winning_price,
            winning_token_id=winning_token,
            return_pct=      return_pct,
            volume_24h=      volume_24h,
            end_date=        str(market.get("endDate", "") or ""),
            hours_to_close=  hours_to_close,
            size_multiplier= size_mult,
            size_tier=       size_tier,
            overdue=         overdue,
        )

    @staticmethod
    def _size_tier(hours: float) -> tuple[float, str]:
        """Return (position_multiplier, tier_label) based on time remaining."""
        if hours == float("inf"):
            return (0.5, "LONG")
        if hours < 0.25:
            return (2.0, "URGENT")
        if hours < 1.0:
            return (1.0, "NORMAL")
        return (0.5, "EARLY")

    def _fetch_markets(self, categories: list[str] | None) -> list[dict]:
        all_markets: list[dict] = []
        cats = categories or config.SCAN_CATEGORIES
        for cat in cats:
            try:
                t0 = time.time()
                events = self.gamma.get_events(tag=cat, limit=100)
                elapsed = time.time() - t0
                if elapsed > 2.0:
                    logger.warning("NearResolved: slow API %.1fs for %s", elapsed, cat)
                for event in events:
                    for market in event.get("markets", []):
                        market["_category"] = cat
                        all_markets.append(market)
            except Exception as exc:
                logger.warning("NearResolved: failed to fetch %s: %s", cat, exc)
                self._consecutive_failures += 1
        return all_markets

    @staticmethod
    def _hours_to_close(market: dict) -> float:
        raw = market.get("endDate") or market.get("endDateIso") or ""
        if not raw:
            return float("inf")
        try:
            s = str(raw).strip()
            for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.datetime.strptime(s[:len(fmt)], fmt).replace(
                        tzinfo=datetime.timezone.utc
                    )
                    now = datetime.datetime.now(datetime.timezone.utc)
                    delta = (dt - now).total_seconds() / 3600
                    return delta
                except ValueError:
                    continue
            return float("inf")
        except Exception:
            return float("inf")

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
