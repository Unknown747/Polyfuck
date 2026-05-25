"""Backtesting module — simulate strategies against recently resolved Polymarket markets."""

import json
import time
import datetime
from dataclasses import dataclass, field
from pathlib import Path
from rich.console import Console
from rich.table import Table

from src.utils.api import GammaClient
from src.config import config

console = Console()

_REPORT_DIR = Path("logs/backtest")


@dataclass
class BacktestTrade:
    market_question: str
    strategy: str        # "near_resolved" or "mispricing"
    entry_price: float
    payout: float        # 1.00 on win, 0.0 on loss
    investment: float
    gross_return: float
    fee_est: float
    net_profit: float
    roi_pct: float
    won: bool


@dataclass
class BacktestResult:
    strategy: str
    days: int
    markets_scanned: int
    trades_simulated: int
    wins: int
    losses: int
    total_invested: float
    total_profit: float
    avg_roi_pct: float
    win_rate_pct: float
    best_trade: BacktestTrade | None = None
    worst_trade: BacktestTrade | None = None
    trades: list[BacktestTrade] = field(default_factory=list)


class Backtester:
    """Simulate near-resolved and mispricing strategies on recently closed markets.

    Data source: Polymarket Gamma API (public, no auth needed).
    Methodology:
      1. Fetch markets closed in the last N days.
      2. For each market, use the final outcome prices before resolution:
         - Near-resolved: if winning side was ≥ MIN_NEAR_PRICE before close,
           simulate a buy at that price and a $1.00 payout on resolution.
         - Mispricing: if price_sum < 1.0 - MIN_EDGE, simulate buying both
           sides for a guaranteed profit on resolution.
      3. Subtract estimated taker fees and report aggregated P&L.
    """

    MIN_NEAR_PRICE: float = 0.90   # Winning side must be ≥ $0.90 to qualify
    MIN_EDGE_PCT:   float = 2.0    # Minimum mispricing edge % to qualify
    TAKER_FEE:      float = 0.04   # Conservative flat taker fee estimate

    def __init__(self, days: int = 7, gamma: GammaClient | None = None):
        self.days = days
        self.gamma = gamma or GammaClient()
        _REPORT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Public API ──────────────────────────────────────────────────────────

    def run(self, verbose: bool = True) -> dict:
        """Run the full backtest and return a summary dict."""
        console.print(
            f"\n[bold cyan]🔬 Backtesting — last {self.days} day(s) of resolved markets[/]"
        )

        markets = self._fetch_closed_markets()
        if not markets:
            console.print("[red]No closed markets found. Try increasing --days.[/]")
            return {"error": "no_data", "markets_scanned": 0}

        console.print(f"  [dim]Fetched {len(markets)} resolved markets[/]")

        nr_trades:  list[BacktestTrade] = []
        mp_trades:  list[BacktestTrade] = []

        for m in markets:
            nr = self._sim_near_resolved(m)
            if nr:
                nr_trades.append(nr)
            mp = self._sim_mispricing(m)
            if mp:
                mp_trades.append(mp)

        nr_result = self._summarize("near_resolved", nr_trades, len(markets))
        mp_result = self._summarize("mispricing",    mp_trades, len(markets))

        if verbose:
            self._display(nr_result, mp_result)

        report = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
            "days":          self.days,
            "markets_scanned": len(markets),
            "near_resolved": self._result_to_dict(nr_result),
            "mispricing":    self._result_to_dict(mp_result),
        }
        self._save_report(report)
        return report

    # ── Simulation ──────────────────────────────────────────────────────────

    def _sim_near_resolved(self, market: dict) -> BacktestTrade | None:
        """Simulate the near-resolved maker strategy on a closed market."""
        prices = self._parse_prices(market)
        if not prices or len(prices) < 2:
            return None

        yes_price, no_price = prices[0], prices[1]

        # Determine which side won (if resolution info is available)
        resolution = self._parse_resolution(market)  # "yes", "no", or None

        # Pick the qualifying side
        entry_price: float | None = None
        won: bool = False

        if yes_price >= self.MIN_NEAR_PRICE:
            entry_price = yes_price
            won = (resolution == "yes") if resolution else (yes_price > no_price)
        elif no_price >= self.MIN_NEAR_PRICE:
            entry_price = no_price
            won = (resolution == "no") if resolution else (no_price > yes_price)

        if entry_price is None:
            return None

        investment = config.DEFAULT_TRADE_SIZE_USD
        shares     = investment / entry_price
        payout     = shares * 1.00 if won else 0.0
        fee_est    = investment * self.TAKER_FEE * entry_price * (1.0 - entry_price)
        gross      = payout - investment
        net_profit = gross - fee_est
        roi_pct    = (net_profit / investment * 100) if investment > 0 else 0.0

        return BacktestTrade(
            market_question=market.get("question", "Unknown")[:80],
            strategy="near_resolved",
            entry_price=round(entry_price, 4),
            payout=round(payout, 4),
            investment=investment,
            gross_return=round(gross, 4),
            fee_est=round(fee_est, 6),
            net_profit=round(net_profit, 4),
            roi_pct=round(roi_pct, 2),
            won=won,
        )

    def _sim_mispricing(self, market: dict) -> BacktestTrade | None:
        """Simulate the mispricing (buy-both) strategy on a closed market."""
        prices = self._parse_prices(market)
        if not prices or len(prices) < 2:
            return None

        yes_price, no_price = prices[0], prices[1]
        price_sum = yes_price + no_price
        edge_pct  = (1.0 - price_sum) * 100

        if edge_pct < self.MIN_EDGE_PCT:
            return None

        investment    = config.DEFAULT_TRADE_SIZE_USD
        cost_per_pair = price_sum
        if cost_per_pair <= 0:
            return None

        num_pairs   = investment / cost_per_pair
        payout      = num_pairs * 1.00          # Always pays $1 per pair on resolution
        yes_fee     = investment / 2 * self.TAKER_FEE * yes_price * (1 - yes_price)
        no_fee      = investment / 2 * self.TAKER_FEE * no_price  * (1 - no_price)
        fee_est     = yes_fee + no_fee
        gross       = payout - investment
        net_profit  = gross - fee_est
        roi_pct     = (net_profit / investment * 100) if investment > 0 else 0.0

        return BacktestTrade(
            market_question=market.get("question", "Unknown")[:80],
            strategy="mispricing",
            entry_price=round(price_sum, 4),
            payout=round(payout, 4),
            investment=investment,
            gross_return=round(gross, 4),
            fee_est=round(fee_est, 6),
            net_profit=round(net_profit, 4),
            roi_pct=round(roi_pct, 2),
            won=net_profit > 0,
        )

    # ── Data helpers ────────────────────────────────────────────────────────

    def _fetch_closed_markets(self) -> list[dict]:
        """Fetch markets closed within the last self.days days."""
        try:
            # BUG FIX: GammaClient uses self.session (not self._session).
            # Use the public _get() helper which handles retries and headers.
            raw = self.gamma._get("/markets", {
                "active": "false",
                "closed": "true",
                "limit":  "500",
            })

            # Filter to markets closed within the target window
            cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
                days=self.days
            )
            markets = []
            for m in (raw if isinstance(raw, list) else raw.get("markets", [])):
                end_str = m.get("endDate") or m.get("end_date_iso") or ""
                if not end_str:
                    markets.append(m)
                    continue
                try:
                    end_dt = datetime.datetime.fromisoformat(
                        end_str.replace("Z", "+00:00")
                    )
                    if end_dt >= cutoff:
                        markets.append(m)
                except Exception:
                    markets.append(m)

            return markets[:300]
        except Exception as e:
            console.print(f"[red]Backtest: could not fetch closed markets: {e}[/]")
            return []

    def _parse_prices(self, market: dict) -> list[float] | None:
        """Parse outcomePrices from a Gamma market dict."""
        raw = market.get("outcomePrices")
        if not raw:
            return None
        try:
            if isinstance(raw, str):
                raw = json.loads(raw)
            return [float(p) for p in raw if p is not None]
        except Exception:
            return None

    def _parse_resolution(self, market: dict) -> str | None:
        """Return 'yes', 'no', or None if resolution is unknown."""
        # Try resolutionSource / resolution fields
        for key in ("resolution", "winner", "resolved_outcome"):
            val = market.get(key, "")
            if val:
                v = str(val).strip().lower()
                if v in ("yes", "1", "true"):
                    return "yes"
                if v in ("no", "0", "false"):
                    return "no"
        return None

    # ── Stats & display ─────────────────────────────────────────────────────

    def _summarize(
        self,
        strategy: str,
        trades: list[BacktestTrade],
        markets_scanned: int,
    ) -> BacktestResult:
        if not trades:
            return BacktestResult(
                strategy=strategy,
                days=self.days,
                markets_scanned=markets_scanned,
                trades_simulated=0,
                wins=0,
                losses=0,
                total_invested=0.0,
                total_profit=0.0,
                avg_roi_pct=0.0,
                win_rate_pct=0.0,
            )

        wins   = sum(1 for t in trades if t.won)
        losses = len(trades) - wins
        total_invested = sum(t.investment for t in trades)
        total_profit   = sum(t.net_profit for t in trades)
        avg_roi = total_profit / total_invested * 100 if total_invested > 0 else 0.0
        win_rate = wins / len(trades) * 100 if trades else 0.0

        best  = max(trades, key=lambda t: t.net_profit)
        worst = min(trades, key=lambda t: t.net_profit)

        return BacktestResult(
            strategy=strategy,
            days=self.days,
            markets_scanned=markets_scanned,
            trades_simulated=len(trades),
            wins=wins,
            losses=losses,
            total_invested=round(total_invested, 2),
            total_profit=round(total_profit, 4),
            avg_roi_pct=round(avg_roi, 2),
            win_rate_pct=round(win_rate, 1),
            best_trade=best,
            worst_trade=worst,
            trades=trades,
        )

    def _display(self, nr: BacktestResult, mp: BacktestResult) -> None:
        for result in (nr, mp):
            label = (
                "Near-Resolved (buy winning side ≥ $0.90)"
                if result.strategy == "near_resolved"
                else "Mispricing (buy both sides, price_sum < 0.98)"
            )
            pnl_color = "green" if result.total_profit >= 0 else "red"

            console.print(f"\n[bold underline]{label}[/]")
            table = Table(show_header=True, header_style="bold cyan", box=None)
            table.add_column("Metric", style="dim", width=28)
            table.add_column("Value")
            table.add_row("Markets scanned",    str(result.markets_scanned))
            table.add_row("Trades simulated",   str(result.trades_simulated))
            table.add_row("Wins / Losses",      f"{result.wins} / {result.losses}")
            table.add_row("Win rate",           f"{result.win_rate_pct:.1f}%")
            table.add_row(
                "Total P&L (simulated)",
                f"[{pnl_color}]${result.total_profit:+.4f}[/]",
            )
            table.add_row("Avg ROI per trade",  f"{result.avg_roi_pct:+.2f}%")
            if result.best_trade:
                table.add_row(
                    "Best trade",
                    f"${result.best_trade.net_profit:+.4f} — "
                    f"{result.best_trade.market_question[:45]}",
                )
            if result.worst_trade:
                table.add_row(
                    "Worst trade",
                    f"${result.worst_trade.net_profit:+.4f} — "
                    f"{result.worst_trade.market_question[:45]}",
                )
            console.print(table)

    def _result_to_dict(self, r: BacktestResult) -> dict:
        return {
            "trades_simulated": r.trades_simulated,
            "wins":             r.wins,
            "losses":           r.losses,
            "win_rate_pct":     r.win_rate_pct,
            "total_profit":     r.total_profit,
            "avg_roi_pct":      r.avg_roi_pct,
            "total_invested":   r.total_invested,
        }

    def _save_report(self, report: dict) -> None:
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = _REPORT_DIR / f"report_{ts}.json"
        try:
            path.write_text(json.dumps(report, indent=2))
            console.print(f"[dim]Backtest report saved → {path}[/]")
        except Exception as e:
            console.print(f"[yellow]Could not save backtest report: {e}[/]")
