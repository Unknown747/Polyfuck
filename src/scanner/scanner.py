"""Market scanner - detects mispriced markets and cross-market arbitrage opportunities."""

import json
import time
import datetime
from dataclasses import dataclass, field
from typing import Any
from rich.console import Console
from rich.table import Table

from src.utils.api import GammaClient, ClobClient

console = Console()


@dataclass
class Mispricing:
    """A detected mispricing opportunity."""
    event_slug: str
    event_title: str
    market_slug: str
    market_question: str
    yes_price: float
    no_price: float
    price_sum: float
    edge_pct: float
    volume_24h: float = 0.0
    total_volume: float = 0.0
    liquidity: float = 0.0
    condition_id: str = ""
    yes_token_id: str = ""
    no_token_id: str = ""
    categories: list[str] = field(default_factory=list)

    @property
    def is_mispriced(self) -> bool:
        """True if sum deviates from $1.00 beyond minimum edge."""
        return abs(self.edge_pct) > 0

    @property
    def direction(self) -> str:
        """Which side to buy for arbitrage."""
        if self.price_sum < 1.0:
            return "BUY_BOTH"  # Sum < 1, buy YES+NO for guaranteed profit
        else:
            return "SELL_BOTH"  # Sum > 1, sell both

    @property
    def guaranteed_profit_per_share(self) -> float:
        """Guaranteed profit per $1 of shares if mispricing is exploited."""
        return abs(1.0 - self.price_sum)

    def __str__(self) -> str:
        return (
            f"[{self.direction}] {self.market_question} "
            f"| YES={self.yes_price:.3f} NO={self.no_price:.3f} "
            f"Sum={self.price_sum:.4f} Edge={self.edge_pct:.2f}% "
            f"Profit=${self.guaranteed_profit_per_share:.4f}/share"
        )


@dataclass
class CorrelatedArbitrage:
    """Cross-market arbitrage based on logical dependencies."""
    primary_market: str
    primary_question: str
    secondary_market: str
    secondary_question: str
    dependency: str  # e.g., "If A then B"
    primary_yes_price: float
    secondary_yes_price: float
    edge_pct: float
    description: str

    @property
    def is_actionable(self) -> bool:
        return abs(self.edge_pct) > 3.0


@dataclass
class NearResolvedOpportunity:
    """A market near resolution — one side is priced very close to $1.00.

    Strategy: buy the high-confidence side (e.g. YES @ $0.96) and collect
    $1.00 when the market resolves. Return ≈ 1–6% with very low risk.
    Post as a maker limit order (0% fee) to maximise profit.
    """
    condition_id:      str
    market_question:   str
    event_title:       str
    market_slug:       str
    winning_side:      str    # "YES" or "NO"
    winning_price:     float  # current market price, e.g. 0.96
    winning_token_id:  str
    return_pct:        float  # (1.0 - price) / price * 100
    volume_24h:        float
    end_date:          str    # ISO string from API, may be empty
    hours_to_close:    float  # inf if unknown

    @property
    def is_actionable(self) -> bool:
        return self.winning_price >= 0.90 and self.return_pct > 0

    @property
    def maker_price(self) -> float:
        """Price 1 tick lower than market — qualifies for 0% maker fee."""
        return round(max(0.01, self.winning_price - 0.01), 2)

    @property
    def maker_return_pct(self) -> float:
        """Return % if filled at maker price."""
        p = self.maker_price
        return ((1.0 - p) / p) * 100 if p > 0 else 0.0

    def __str__(self) -> str:
        return (
            f"[NEAR_RESOLVED/{self.winning_side}] {self.market_question} "
            f"| Price={self.winning_price:.3f} Return={self.return_pct:.2f}% "
            f"| Closes in {self.hours_to_close:.0f}h"
        )


class MarketScanner:
    """Scans Polymarket for mispricing and arbitrage opportunities."""

    def __init__(self, gamma: GammaClient | None = None, clob: ClobClient | None = None):
        self.gamma = gamma or GammaClient()
        self.clob = clob or ClobClient()
        self._market_cache: dict[str, dict] = {}
        # Per-category opportunity counts for the dashboard
        self._category_stats: dict[str, int] = {}

    def _get_category_min_edge(self, category: str, base_edge: float) -> float:
        """Compute a fee-adjusted minimum edge for a category.

        Ensures the strategy edge is large enough to cover round-trip taker fees
        (buy + sell) × FEE_EDGE_MULT safety margin.

        Formula: max(base_edge, fee_rate × 100 × FEE_EDGE_MULT)

        Why ×100 (not ×200): for mispricing (buy both YES+NO sides and hold to
        resolution), total taker fee ≈ fee_rate × investment — NOT 2× — because
        the YES and NO fee terms cancel to exactly fee_rate × payout in a balanced
        market.  The FEE_EDGE_MULT safety margin already provides headroom.
        For crypto (7% fee, mult=1.5): effective min_edge = 7 × 1.5 = 10.5%.
        """
        from src.config import config
        fee_rate = config.CATEGORY_TAKER_FEES.get(category, 0.04)
        fee_adjusted = fee_rate * 100 * config.FEE_EDGE_MULT
        return max(base_edge, fee_adjusted)

    def get_category_stats(self) -> dict[str, int]:
        """Return per-category opportunity counts since scanner started."""
        return dict(self._category_stats)

    def scan_all(
        self,
        min_edge_pct: float = 2.0,
        min_volume: float = 1000.0,
        categories: list[str] | None = None,
    ) -> list[Mispricing]:
        """Scan all active markets for simple mispricing (YES + NO != $1.00).

        This is the most basic arbitrage: buy both sides for less than $1,
        guaranteeing profit regardless of outcome.
        """
        console.print("[bold cyan]Scanning markets for mispricing...[/]")

        opportunities: list[Mispricing] = []
        markets = self._fetch_markets(categories)
        checked = 0

        for market in markets:
            checked += 1
            if checked % 50 == 0:
                console.print(f"  Checked {checked} markets...")

            try:
                # Smart Category Filtering: raise effective min_edge for high-fee categories
                category = market.get("_category", "")
                cat_min_edge = self._get_category_min_edge(category, min_edge_pct)
                opp = self._check_single_market(market, cat_min_edge, min_volume)
                if opp:
                    opportunities.append(opp)
                    self._category_stats[category] = (
                        self._category_stats.get(category, 0) + 1
                    )
            except Exception as e:
                # BUG FIX: log errors instead of silently swallowing them
                console.print(f"[dim yellow]Warning: skipped market due to error: {e}[/]")
                continue

        console.print(f"[green]Checked {checked} markets, found {len(opportunities)} opportunities[/]")
        return sorted(opportunities, key=lambda x: abs(x.edge_pct), reverse=True)

    def scan_correlated(
        self,
        keywords: list[str] | None = None,
        min_edge_pct: float = 3.0,
    ) -> list[CorrelatedArbitrage]:
        """Scan for cross-market arbitrage based on logical dependencies.

        Detects when correlated markets have inconsistent pricing.
        Example: "Trump wins PA" at 48% but "Republicans win PA by 5+" at 32%
        - if Reps win by 5+, Trump must win PA, creating arbitrage.
        """
        console.print("[bold cyan]Scanning for cross-market correlations...[/]")

        opportunities: list[CorrelatedArbitrage] = []
        events = self._fetch_events(keywords)

        # Group markets by event (shared events have logical dependencies)
        event_markets: dict[str, list[dict]] = {}
        for event in events:
            slug = event.get("slug", "")
            markets_data = event.get("markets", [])
            if markets_data and isinstance(markets_data, list):
                event_markets[slug] = markets_data

        # Check for within-event price inconsistencies
        for slug, markets in event_markets.items():
            if len(markets) < 2:
                continue

            try:
                corrs = self._check_event_correlations(slug, markets, min_edge_pct)
                opportunities.extend(corrs)
            except Exception as e:
                console.print(f"[dim yellow]Warning: skipped event {slug} in correlation scan: {e}[/]")
                continue

        console.print(f"[green]Found {len(opportunities)} cross-market opportunities[/]")
        return opportunities

    def get_market_detail(self, condition_id: str) -> dict | None:
        """Get detailed market data including orderbook."""
        try:
            market = self.gamma.get_market(condition_id)
            if not market:
                return None

            # Enrich with orderbook data if token IDs available
            tokens = self._parse_token_ids(market)
            if tokens:
                for side, token_id in [("yes", tokens.get("yes", "")), ("no", tokens.get("no", ""))]:
                    if token_id:
                        try:
                            book = self.clob.get_orderbook(token_id)
                            market[f"{side}_orderbook"] = book
                        except Exception:
                            pass

            return market
        except Exception:
            return None

    # === Private Methods ===

    def _fetch_markets(self, categories: list[str] | None = None) -> list[dict]:
        """Fetch active markets, optionally filtered by category."""
        all_markets: list[dict] = []

        if categories:
            for cat in categories:
                try:
                    events = self.gamma.get_events(tag=cat, limit=100)
                    for event in events:
                        for market in event.get("markets", []):
                            market["_category"] = cat
                            all_markets.append(market)
                except Exception as e:
                    console.print(f"[yellow]Warning: Failed to fetch {cat}: {e}[/]")
        else:
            try:
                all_markets = self.gamma.get_markets(active_only=True, limit=500)
            except Exception as e:
                console.print(f"[red]Error fetching markets: {e}[/]")

        return all_markets

    def _fetch_events(self, keywords: list[str] | None = None) -> list[dict]:
        """Fetch events, optionally filtered by keywords."""
        if keywords:
            events = []
            for kw in keywords:
                results = self.gamma.search_markets(kw, limit=50)
                events.extend(results)
            return events
        else:
            return self.gamma.get_events(limit=200)

    def _check_single_market(
        self, market: dict, min_edge_pct: float, min_volume: float
    ) -> Mispricing | None:
        """Check a single market for YES+NO mispricing."""
        # Parse outcome prices
        prices = self._parse_outcome_prices(market)
        if not prices or len(prices) < 2:
            return None

        yes_price = prices[0]
        no_price = prices[1]
        price_sum = yes_price + no_price

        # Edge = deviation from $1.00
        edge_pct = abs(1.0 - price_sum) * 100

        if edge_pct < min_edge_pct:
            return None

        # Check volume threshold
        # BUG FIX: guard against non-numeric API values that would raise ValueError
        try:
            volume_24h = float(market.get("volume24hr", 0) or 0)
        except (ValueError, TypeError):
            volume_24h = 0.0
        try:
            total_volume = float(market.get("volumeNum", 0) or 0)
        except (ValueError, TypeError):
            total_volume = 0.0
        try:
            liquidity = float(market.get("liquidityNum", 0) or 0)
        except (ValueError, TypeError):
            liquidity = 0.0

        if total_volume < min_volume and volume_24h < min_volume * 0.1:
            return None  # Not enough liquidity

        tokens = self._parse_token_ids(market)

        return Mispricing(
            event_slug=market.get("slug", "").split("-")[0] if market.get("slug") else "",
            event_title=self._get_event_title(market),
            market_slug=market.get("slug", ""),
            market_question=market.get("question", ""),
            yes_price=yes_price,
            no_price=no_price,
            price_sum=price_sum,
            edge_pct=edge_pct,
            volume_24h=volume_24h,
            total_volume=total_volume,
            liquidity=liquidity,
            condition_id=market.get("conditionId", ""),
            yes_token_id=tokens.get("yes", ""),
            no_token_id=tokens.get("no", ""),
        )

    def _check_event_correlations(
        self, event_slug: str, markets: list[dict], min_edge_pct: float
    ) -> list[CorrelatedArbitrage]:
        """Check markets within the same event for logical inconsistencies."""
        opportunities: list[CorrelatedArbitrage] = []

        if len(markets) < 2:
            return opportunities

        # Simple heuristic: within an event, all complementary outcomes
        # should sum to ~1.0, and subset probabilities should be <= superset
        for i, m1 in enumerate(markets):
            for j, m2 in enumerate(markets):
                if i >= j:
                    continue

                prices1 = self._parse_outcome_prices(m1)
                prices2 = self._parse_outcome_prices(m2)

                if not prices1 or not prices2 or len(prices1) < 2 or len(prices2) < 2:
                    continue

                # Check if one market implies constraints on another
                # (This is a simplified version - full implementation would use
                # constraint satisfaction / linear programming)
                q1 = m1.get("question", "")
                q2 = m2.get("question", "")

                # Detect overlap in question keywords
                common = self._detect_logical_dependency(q1, q2)
                if not common:
                    continue

                yes1 = prices1[0]
                yes2 = prices2[0]

                # Simple correlation check: if A implies B, then P(A) <= P(B)
                edge = abs(yes1 - yes2) * 100

                if edge > min_edge_pct:
                    opportunities.append(CorrelatedArbitrage(
                        primary_market=m1.get("slug", f"market_{i}"),
                        primary_question=q1,
                        secondary_market=m2.get("slug", f"market_{j}"),
                        secondary_question=q2,
                        dependency=common,
                        primary_yes_price=yes1,
                        secondary_yes_price=yes2,
                        edge_pct=edge,
                        description=f"If '{q1}' then '{q2}': P({yes1:.2f}) vs P({yes2:.2f})",
                    ))

        return opportunities

    def _detect_logical_dependency(self, q1: str, q2: str) -> str | None:
        """Detect if two questions have a logical dependency."""
        # Simplified keyword matching - a full implementation would use NLP
        dependency_patterns = [
            ("win", "by"),      # "X wins" -> "X wins by Y+"
            ("Republican", ""),  # Same-party correlations
            ("Democrat", ""),
            ("before", ""),     # Temporal dependencies
            ("at least", ""),   # Threshold dependencies
        ]

        q1_lower = q1.lower()
        q2_lower = q2.lower()

        # BUG FIX: require >=3 meaningful shared keywords to reduce false positives.
        # Also filter out short words and generic political terms that appear everywhere.
        stop_words = {
            "will", "the", "a", "an", "in", "on", "at", "of", "is", "be", "it",
            "to", "and", "or", "for", "win", "by", "get", "do", "did", "has",
            "have", "more", "than", "over", "under", "least", "most", "what",
            "who", "when", "how", "which", "that", "this", "his", "her", "their",
        }
        words1 = {w for w in q1_lower.split() if len(w) > 3} - stop_words
        words2 = {w for w in q2_lower.split() if len(w) > 3} - stop_words
        overlap = words1 & words2

        if len(overlap) >= 3:
            return f"Shared concepts: {', '.join(sorted(overlap)[:3])}"

        return None

    def _parse_outcome_prices(self, market: dict) -> list[float] | None:
        """Parse outcomePrices field from market data."""
        raw = market.get("outcomePrices")
        if not raw:
            return None

        # Handle double-encoded JSON
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

    def _parse_token_ids(self, market: dict) -> dict[str, str]:
        """Parse clobTokenIds into {'yes': ..., 'no': ...}."""
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

    def _get_event_title(self, market: dict) -> str:
        """Extract event title from market data."""
        return market.get("groupItemTitle", "") or market.get("question", "").split("?")[0] + "?"

    # ── Near-Resolved Strategy ────────────────────────────────────────────────

    def scan_near_resolved(
        self,
        min_confidence: float = 0.94,
        max_hours: float = 72.0,
        min_volume: float = 200.0,
        categories: list[str] | None = None,
    ) -> list[NearResolvedOpportunity]:
        """Scan for markets where one outcome is priced > min_confidence.

        Strategy: buy the high-confidence side (e.g. YES @ $0.96) and
        collect $1.00 on resolution. Return ≈ 1-6% with very low directional
        risk. Post as maker limit order (0% fee) to maximise net return.

        Args:
            min_confidence: Minimum price to qualify (default 0.94 = 94% sure)
            max_hours:      Only include markets closing within this window
            min_volume:     24h volume threshold to ensure liquidity
            categories:     Category filter (same as scan_all)
        """
        console.print("[bold cyan]Scanning for near-resolved markets...[/]")
        opportunities: list[NearResolvedOpportunity] = []
        markets = self._fetch_markets(categories)

        for market in markets:
            try:
                opp = self._check_near_resolved(market, min_confidence, min_volume, max_hours)
                if opp:
                    opportunities.append(opp)
            except Exception as e:
                console.print(f"[dim yellow]Warning: near-resolved check skipped: {e}[/]")
                continue

        console.print(f"[green]Found {len(opportunities)} near-resolved opportunities[/]")
        return sorted(opportunities, key=lambda x: x.return_pct, reverse=True)

    def _check_near_resolved(
        self,
        market: dict,
        min_confidence: float,
        min_volume: float,
        max_hours: float,
    ) -> "NearResolvedOpportunity | None":
        """Check if a single market qualifies as near-resolved."""
        prices = self._parse_outcome_prices(market)
        if not prices or len(prices) < 2:
            return None

        yes_price = prices[0]
        no_price  = prices[1]

        if yes_price >= min_confidence:
            winning_side  = "YES"
            winning_price = yes_price
        elif no_price >= min_confidence:
            winning_side  = "NO"
            winning_price = no_price
        else:
            return None

        # Volume filter
        try:
            volume_24h   = float(market.get("volume24hr", 0) or 0)
            total_volume = float(market.get("volumeNum",  0) or 0)
        except (ValueError, TypeError):
            volume_24h = total_volume = 0.0

        if volume_24h < min_volume and total_volume < min_volume * 2:
            return None

        # Time-to-close filter
        hours_to_close = self._hours_to_close(market)
        if hours_to_close < 0:
            return None  # already expired
        if hours_to_close != float("inf") and hours_to_close > max_hours:
            return None

        tokens        = self._parse_token_ids(market)
        winning_token = tokens.get("yes", "") if winning_side == "YES" else tokens.get("no", "")
        return_pct    = ((1.0 - winning_price) / winning_price) * 100

        return NearResolvedOpportunity(
            condition_id=market.get("conditionId", ""),
            market_question=market.get("question", ""),
            event_title=self._get_event_title(market),
            market_slug=market.get("slug", ""),
            winning_side=winning_side,
            winning_price=winning_price,
            winning_token_id=winning_token,
            return_pct=return_pct,
            volume_24h=volume_24h,
            end_date=str(market.get("endDate", "") or ""),
            hours_to_close=hours_to_close,
        )

    def _hours_to_close(self, market: dict) -> float:
        """Hours until market closes. Returns inf if unknown, <0 if expired.

        BUG FIX: datetime.utcnow() is timezone-naive and deprecated since
        Python 3.12. Using timezone-aware datetime.now(UTC) ensures correct
        comparison when the API returns UTC timestamps.
        """
        raw = market.get("endDate") or market.get("endDateIso") or ""
        if not raw:
            return float("inf")
        try:
            s = str(raw).strip()
            # Normalise to an aware UTC datetime regardless of input format
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.datetime.fromisoformat(s if "T" in s else s + "T00:00:00+00:00")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            delta = dt - now
            return delta.total_seconds() / 3600
        except Exception:
            return float("inf")


def display_near_resolved(opportunities: list[NearResolvedOpportunity], limit: int = 10) -> None:
    """Display near-resolved opportunities in a rich table."""
    if not opportunities:
        console.print("[yellow]No near-resolved opportunities found.[/]")
        return

    table = Table(title="📅 Near-Resolved Markets (Low-Risk)")
    table.add_column("Market", style="cyan", max_width=45, no_wrap=True)
    table.add_column("Side",   justify="center", style="bold")
    table.add_column("Price",  justify="right",  style="green")
    table.add_column("Maker $", justify="right", style="bold green")
    table.add_column("Return", justify="right",  style="bold yellow")
    table.add_column("Maker Ret", justify="right", style="bold cyan")
    table.add_column("Closes",  justify="right")
    table.add_column("Volume", justify="right")

    for opp in opportunities[:limit]:
        closes = (
            f"{opp.hours_to_close:.0f}h"
            if opp.hours_to_close != float("inf")
            else "?"
        )
        table.add_row(
            opp.market_question[:45],
            opp.winning_side,
            f"${opp.winning_price:.3f}",
            f"${opp.maker_price:.3f}",
            f"{opp.return_pct:.2f}%",
            f"{opp.maker_return_pct:.2f}%",
            closes,
            f"${opp.volume_24h:,.0f}",
        )

    console.print(table)


def display_opportunities(opportunities: list[Mispricing], limit: int = 20) -> None:
    """Display mispricing opportunities in a rich table."""
    if not opportunities:
        console.print("[yellow]No mispricing opportunities found.[/]")
        return

    table = Table(title="🔍 Polymarket Mispricing Opportunities")
    table.add_column("Market", style="cyan", max_width=50, no_wrap=True)
    table.add_column("YES", justify="right", style="green")
    table.add_column("NO", justify="right", style="red")
    table.add_column("Sum", justify="right")
    table.add_column("Edge", justify="right", style="bold yellow")
    table.add_column("Profit/Share", justify="right", style="bold green")
    table.add_column("Volume", justify="right")

    for opp in opportunities[:limit]:
        table.add_row(
            opp.market_question[:50],
            f"{opp.yes_price:.3f}",
            f"{opp.no_price:.3f}",
            f"${opp.price_sum:.4f}",
            f"{opp.edge_pct:.2f}%",
            f"${opp.guaranteed_profit_per_share:.4f}",
            f"${opp.total_volume:,.0f}",
        )

    console.print(table)