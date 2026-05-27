"""Correlated Arbitrage Scanner.

Detects market pairs that have logical price relationships but are
mis-priced relative to each other:

  1. Same-event different expiration (BTC May vs BTC June)
  2. Mutually exclusive outcomes (A wins vs B wins in same race)
  3. Parent-child relationships (event A requires event B)

Formula: if price_A > price_B * 1.05 for a logical relationship → arbitrage.
Action: buy undervalued side, flag overvalued side for hedging.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.config import config

if TYPE_CHECKING:
    from src.utils.api import GammaClient, ClobClient

logger = logging.getLogger("polymarket-bot")


@dataclass
class CorrelatedPair:
    """A detected correlated arbitrage opportunity."""

    pair_type:         str    # "EXPIRATION", "MUTUALLY_EXCLUSIVE", "PARENT_CHILD", "GENERAL"
    market_a_id:       str
    market_a_question: str
    market_a_price:    float
    market_b_id:       str
    market_b_question: str
    market_b_price:    float
    divergence_pct:    float
    description:       str
    buy_market_id:     str    = ""
    buy_price:         float  = 0.0
    buy_token_id:      str    = ""
    sell_market_id:    str    = ""
    sell_price:        float  = 0.0
    sell_token_id:     str    = ""

    @property
    def is_actionable(self) -> bool:
        return self.divergence_pct >= config.CORRELATED_MIN_DIVERGENCE

    def __str__(self) -> str:
        return (
            f"[CORR/{self.pair_type}] {self.market_a_question[:40]} "
            f"vs {self.market_b_question[:40]} "
            f"| Divergence={self.divergence_pct:.1f}%"
        )


class CorrelatedArbitrageScanner:
    """Scans for cross-market arbitrage opportunities."""

    # Price divergence threshold for assuming a logical relationship is violated
    RELATIONSHIP_THRESHOLD = 1.05  # price_A > price_B * 1.05

    def __init__(self, gamma=None, clob=None):
        from src.utils.api import GammaClient, ClobClient
        self.gamma: GammaClient = gamma or GammaClient()
        self.clob:  ClobClient  = clob  or ClobClient()
        self._scan_count: int            = 0
        self._consecutive_failures: int  = 0
        self.enabled: bool               = True

        # Cache: active_pairs so dashboard can show them
        self._active_pairs: list[CorrelatedPair] = []

    # ── Public API ────────────────────────────────────────────────────────────

    def scan(self, categories: list[str] | None = None) -> list[CorrelatedPair]:
        """Return correlated arbitrage pairs sorted by divergence descending."""
        if not self.enabled:
            return []

        self._scan_count += 1
        opportunities: list[CorrelatedPair] = []
        events = self._fetch_events(categories)

        for event in events:
            markets = event.get("markets", [])
            if len(markets) < 2:
                continue
            try:
                pairs = self._check_event(event, markets)
                opportunities.extend(pairs)
            except Exception as exc:
                logger.debug("CorrelatedArb: skipped event %s: %s", event.get("slug", ""), exc)

        self._consecutive_failures = 0
        actionable = [p for p in opportunities if p.is_actionable]
        self._active_pairs = actionable
        logger.info("CorrelatedArb: found %d pairs (%d actionable)", len(opportunities), len(actionable))
        return sorted(actionable, key=lambda p: p.divergence_pct, reverse=True)

    def get_active_pairs(self) -> list[CorrelatedPair]:
        return list(self._active_pairs)

    # ── Private ───────────────────────────────────────────────────────────────

    def _check_event(self, event: dict, markets: list[dict]) -> list[CorrelatedPair]:
        pairs: list[CorrelatedPair] = []
        parsed: list[tuple[dict, float, float]] = []

        for m in markets:
            prices = self._parse_prices(m)
            if prices and len(prices) >= 2:
                parsed.append((m, prices[0], prices[1]))

        # Check all pairs within the event
        for i, (ma, yes_a, no_a) in enumerate(parsed):
            for j, (mb, yes_b, no_b) in enumerate(parsed):
                if i >= j:
                    continue
                pair = self._check_pair(ma, yes_a, no_a, mb, yes_b, no_b)
                if pair:
                    pairs.append(pair)

        return pairs

    def _check_pair(
        self,
        ma: dict, yes_a: float, no_a: float,
        mb: dict, yes_b: float, no_b: float,
    ) -> CorrelatedPair | None:
        q_a = ma.get("question", "")
        q_b = mb.get("question", "")

        pair_type = self._detect_type(q_a, q_b, ma, mb)
        if not pair_type:
            return None

        tokens_a = self._parse_tokens(ma)
        tokens_b = self._parse_tokens(mb)

        # Divergence: how much does the stronger-priced market exceed the weaker?
        # For mutually exclusive: YES_A + YES_B should not exceed 1.0 (they can't both win)
        if pair_type == "MUTUALLY_EXCLUSIVE":
            divergence = max(0, (yes_a + yes_b - 1.0) * 100)
            if divergence < 1.0:
                return None
            # Buy the undervalued (lower YES price), flag the overvalued
            if yes_a < yes_b:
                buy_m, sell_m = ma, mb
                buy_p, sell_p = yes_a, yes_b
                buy_tok = tokens_a.get("yes", "")
                sell_tok= tokens_b.get("yes", "")
            else:
                buy_m, sell_m = mb, ma
                buy_p, sell_p = yes_b, yes_a
                buy_tok = tokens_b.get("yes", "")
                sell_tok= tokens_a.get("yes", "")

            description = (
                f"Mutually exclusive: {q_a[:35]} ({yes_a:.2f}) + "
                f"{q_b[:35]} ({yes_b:.2f}) = {yes_a+yes_b:.2f} > 1.0"
            )

        elif pair_type == "EXPIRATION":
            # Closer expiry should be <= further expiry for the same event
            divergence = abs(yes_a - yes_b) * 100
            if divergence < 1.0:
                return None
            if yes_a > yes_b * self.RELATIONSHIP_THRESHOLD:
                buy_m, sell_m = mb, ma
                buy_p, sell_p = yes_b, yes_a
                buy_tok = tokens_b.get("yes", "")
                sell_tok= tokens_a.get("yes", "")
            elif yes_b > yes_a * self.RELATIONSHIP_THRESHOLD:
                buy_m, sell_m = ma, mb
                buy_p, sell_p = yes_a, yes_b
                buy_tok = tokens_a.get("yes", "")
                sell_tok= tokens_b.get("yes", "")
            else:
                return None
            description = (
                f"Different expiry: {q_a[:35]} ({yes_a:.2f}) "
                f"vs {q_b[:35]} ({yes_b:.2f})"
            )

        else:
            # PARENT_CHILD / GENERAL
            divergence = abs(yes_a - yes_b) * 100
            if divergence < 1.0:
                return None
            if yes_a > yes_b * self.RELATIONSHIP_THRESHOLD:
                buy_m, sell_m = mb, ma
                buy_p, sell_p = yes_b, yes_a
                buy_tok = tokens_b.get("yes", "")
                sell_tok= tokens_a.get("yes", "")
            else:
                buy_m, sell_m = ma, mb
                buy_p, sell_p = yes_a, yes_b
                buy_tok = tokens_a.get("yes", "")
                sell_tok= tokens_b.get("yes", "")
            description = (
                f"Logical correlation: {q_a[:35]} ({yes_a:.2f}) "
                f"vs {q_b[:35]} ({yes_b:.2f})"
            )

        return CorrelatedPair(
            pair_type=         pair_type,
            market_a_id=       ma.get("conditionId", ""),
            market_a_question= q_a,
            market_a_price=    yes_a,
            market_b_id=       mb.get("conditionId", ""),
            market_b_question= q_b,
            market_b_price=    yes_b,
            divergence_pct=    divergence,
            description=       description,
            buy_market_id=     buy_m.get("conditionId", ""),
            buy_price=         buy_p,
            buy_token_id=      buy_tok,
            sell_market_id=    sell_m.get("conditionId", ""),
            sell_price=        sell_p,
            sell_token_id=     sell_tok,
        )

    @staticmethod
    def _detect_type(q_a: str, q_b: str, ma: dict, mb: dict) -> str | None:
        qa = q_a.lower()
        qb = q_b.lower()

        # Mutually exclusive: same candidate, same race → both YES can't be 1
        exclusive_triggers = [
            ("wins", "beats"),
            ("elected", "elected"),
            ("champion", "champion"),
            ("winner", "winner"),
        ]
        for t1, t2 in exclusive_triggers:
            if t1 in qa and (t1 in qb or t2 in qb):
                # Must share a meaningful subject
                words_a = {w for w in qa.split() if len(w) > 4}
                words_b = {w for w in qb.split() if len(w) > 4}
                if words_a & words_b:
                    return "MUTUALLY_EXCLUSIVE"

        # Same-event different expiration
        date_words = ["january","february","march","april","may","june","july",
                      "august","september","october","november","december",
                      "q1","q2","q3","q4","2025","2026","2027"]
        has_date_a = any(d in qa for d in date_words)
        has_date_b = any(d in qb for d in date_words)
        if has_date_a and has_date_b:
            words_a = {w for w in qa.split() if len(w) > 4 and w not in date_words}
            words_b = {w for w in qb.split() if len(w) > 4 and w not in date_words}
            if len(words_a & words_b) >= 2:
                return "EXPIRATION"

        # Parent-child: threshold dependency (e.g. "above 50k" vs "above 100k")
        if "above" in qa and "above" in qb:
            words_a = {w for w in qa.split() if len(w) > 3}
            words_b = {w for w in qb.split() if len(w) > 3}
            if len(words_a & words_b) >= 2:
                return "PARENT_CHILD"

        # General correlation: high keyword overlap
        stop = {"will","the","a","an","in","on","at","of","is","be","it","to","and","or","for"}
        words_a = {w for w in qa.split() if len(w) > 3} - stop
        words_b = {w for w in qb.split() if len(w) > 3} - stop
        if len(words_a & words_b) >= 3:
            return "GENERAL"

        return None

    def _fetch_events(self, categories: list[str] | None) -> list[dict]:
        all_events: list[dict] = []
        cats = categories or config.SCAN_CATEGORIES
        for cat in cats:
            try:
                t0 = time.time()
                events = self.gamma.get_events(tag=cat, limit=50)
                elapsed = time.time() - t0
                if elapsed > 2.0:
                    logger.warning("CorrelatedArb: slow API %.1fs for %s", elapsed, cat)
                all_events.extend(events)
            except Exception as exc:
                logger.warning("CorrelatedArb: fetch failed for %s: %s", cat, exc)
                self._consecutive_failures += 1
        return all_events

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
