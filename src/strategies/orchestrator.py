"""Strategy Orchestrator.

Runs all 4 strategies in priority order, enforces global limits,
prevents duplicate positions, and tracks per-strategy health.

Priority (highest → lowest):
  1. Mispricing
  2. Near-Resolved
  3. Liquidity Snipe
  4. Correlated Arbitrage

Health check: auto-disable a strategy after 3 consecutive failures.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.config import config

if TYPE_CHECKING:
    from src.utils.api import GammaClient, ClobClient
    from src.trader.trader import Trader

logger = logging.getLogger("polymarket-bot")


# ── Result containers ─────────────────────────────────────────────────────────

@dataclass
class StrategyResult:
    """Holds all outputs from one orchestrator run."""

    # Mispricing
    mispricing_opps:   int   = 0
    mispricing_trades: int   = 0
    mispricing_pnl:    float = 0.0

    # Near-resolved
    nr_opps:           int   = 0
    nr_trades:         int   = 0
    nr_pnl:            float = 0.0

    # Correlated arbitrage
    corr_opps:         int   = 0
    corr_trades:       int   = 0
    corr_pnl:          float = 0.0

    # Liquidity sniper
    sniper_orders:     int   = 0
    sniper_signals:    int   = 0
    sniper_pnl:        float = 0.0

    # Global
    total_trades:      int   = 0
    trades_this_run:   list  = field(default_factory=list)
    errors:            list[str] = field(default_factory=list)

    # Dashboard extras
    active_pairs:      list  = field(default_factory=list)
    active_sniper_orders: list = field(default_factory=list)


@dataclass
class HealthMonitor:
    """Tracks consecutive failures per strategy and disables if threshold hit."""

    name:              str
    failures:          int  = 0
    max_failures:      int  = 3
    disabled:          bool = False
    last_success_ts:   float = field(default_factory=time.time)

    def record_success(self) -> None:
        self.failures        = 0
        self.disabled        = False
        self.last_success_ts = time.time()

    def record_failure(self, err: str) -> None:
        self.failures += 1
        logger.warning("%s: failure #%d — %s", self.name, self.failures, err)
        if self.failures >= self.max_failures:
            self.disabled = True
            logger.error("%s: disabled after %d consecutive failures", self.name, self.failures)

    @property
    def is_ok(self) -> bool:
        return not self.disabled


# ── Orchestrator ─────────────────────────────────────────────────────────────

class Orchestrator:
    """Coordinates all 4 strategies with priority enforcement and safety guards."""

    def __init__(self, trader: "Trader", gamma=None, clob=None):
        from src.utils.api import GammaClient, ClobClient
        from src.scanner.mispricing import MispricingScanner
        from src.scanner.near_resolved import NearResolvedScanner
        from src.scanner.correlated_arbitrage import CorrelatedArbitrageScanner
        from src.scanner.liquidity_sniper import LiquiditySniper
        import src.utils.db as db

        self.trader = trader
        self.gamma:  GammaClient = gamma or GammaClient()
        self.clob:   ClobClient  = clob  or ClobClient()
        self.db = db

        self.mispricing = MispricingScanner(self.gamma, self.clob)
        self.nr_scanner  = NearResolvedScanner(self.gamma)
        self.corr_scanner= CorrelatedArbitrageScanner(self.gamma, self.clob)
        self.sniper      = LiquiditySniper(self.gamma, self.clob)

        self._health = {
            "mispricing":    HealthMonitor("Mispricing"),
            "near_resolved": HealthMonitor("NearResolved"),
            "correlated":    HealthMonitor("Correlated"),
            "sniper":        HealthMonitor("Sniper"),
        }

        # Global duplicate-position guard: set of condition_ids currently held
        self._active_condition_ids: set[str] = set()
        self._scan_count: int = 0

        # Cumulative per-strategy P&L (session-level)
        self._pnl: dict[str, float] = {
            "mispricing": 0.0, "near_resolved": 0.0, "correlated": 0.0, "sniper": 0.0
        }

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self, categories: list[str] | None = None) -> StrategyResult:
        """Run all enabled strategies in priority order. Thread-safe."""
        self._scan_count += 1
        result = StrategyResult()
        cats   = categories or config.SCAN_CATEGORIES

        results: dict[str, object] = {}
        errors:  dict[str, str]    = {}

        # Run strategies in parallel threads (I/O-bound API calls)
        threads = []

        if config.MISPRICING_ENABLED and self._health["mispricing"].is_ok:
            t = threading.Thread(
                target=self._run_mispricing, args=(cats, results, errors), daemon=True
            )
            threads.append(t)

        if config.NEAR_RESOLVED_ENABLED and self._health["near_resolved"].is_ok:
            t = threading.Thread(
                target=self._run_near_resolved, args=(cats, results, errors), daemon=True
            )
            threads.append(t)

        # Correlated: run every CORRELATED_SCAN_EVERY scans
        if (config.CORRELATED_ARB_ENABLED
                and self._health["correlated"].is_ok
                and self._scan_count % config.CORRELATED_SCAN_EVERY == 0):
            t = threading.Thread(
                target=self._run_correlated, args=(cats, results, errors), daemon=True
            )
            threads.append(t)

        if config.LIQUIDITY_SNIPE_ENABLED and self._health["sniper"].is_ok:
            t = threading.Thread(
                target=self._run_sniper, args=(cats, results, errors), daemon=True
            )
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)  # Max 60s per strategy batch

        # Apply results in priority order
        self._apply_mispricing(results, errors, result)
        self._apply_near_resolved(results, errors, result)
        self._apply_correlated(results, errors, result)
        self._apply_sniper(results, errors, result)

        result.total_trades = result.mispricing_trades + result.nr_trades + result.corr_trades
        result.errors       = list(errors.values())
        result.active_pairs = self.corr_scanner.get_active_pairs()
        result.active_sniper_orders = self.sniper.get_active_orders()

        # Persist daily P&L snapshot
        try:
            self.db.upsert_daily_pnl(
                mispricing=   self._pnl["mispricing"],
                near_resolved=self._pnl["near_resolved"],
                correlation=  self._pnl["correlated"],
                sniper=       self._pnl["sniper"],
            )
        except Exception as e:
            logger.warning("Orchestrator: failed to persist daily_pnl: %s", e)

        return result

    def get_category_stats(self) -> dict[str, int]:
        return self.mispricing.get_category_stats()

    def get_pnl(self) -> dict[str, float]:
        return dict(self._pnl)

    def get_health(self) -> dict[str, dict]:
        return {
            name: {"disabled": h.disabled, "failures": h.failures}
            for name, h in self._health.items()
        }

    # ── Private runners ───────────────────────────────────────────────────────

    def _run_mispricing(
        self, cats: list[str], results: dict, errors: dict
    ) -> None:
        try:
            opps = self.mispricing.scan(categories=cats)
            results["mispricing"] = opps
            self._health["mispricing"].record_success()
        except Exception as e:
            errors["mispricing"] = str(e)
            self._health["mispricing"].record_failure(str(e))
            results["mispricing"] = []

    def _run_near_resolved(
        self, cats: list[str], results: dict, errors: dict
    ) -> None:
        try:
            opps = self.nr_scanner.scan(categories=cats)
            results["near_resolved"] = opps
            self._health["near_resolved"].record_success()
        except Exception as e:
            errors["near_resolved"] = str(e)
            self._health["near_resolved"].record_failure(str(e))
            results["near_resolved"] = []

    def _run_correlated(
        self, cats: list[str], results: dict, errors: dict
    ) -> None:
        try:
            pairs = self.corr_scanner.scan(categories=cats)
            results["correlated"] = pairs
            self._health["correlated"].record_success()
        except Exception as e:
            errors["correlated"] = str(e)
            self._health["correlated"].record_failure(str(e))
            results["correlated"] = []

    def _run_sniper(
        self, cats: list[str], results: dict, errors: dict
    ) -> None:
        try:
            sniper_res = self.sniper.run(categories=cats)
            results["sniper"] = sniper_res
            self._health["sniper"].record_success()
        except Exception as e:
            errors["sniper"] = str(e)
            self._health["sniper"].record_failure(str(e))
            results["sniper"] = {}

    # ── Apply results ─────────────────────────────────────────────────────────

    def _apply_mispricing(
        self, results: dict, errors: dict, out: StrategyResult
    ) -> None:
        opps = results.get("mispricing", [])
        if not opps:
            return

        out.mispricing_opps = len(opps)

        for opp in opps[:3]:  # Try top 3
            if not self._can_trade(opp.condition_id):
                continue
            if not self._within_global_limits():
                break

            category = opp.categories[0] if opp.categories else "unknown"
            inv      = self._sized_investment(opp.position_multiplier)

            trade = self.trader.execute_mispricing_trade(
                opp, investment_usd=inv, category=category
            )
            if trade:
                trade.strategy = "mispricing"
                out.mispricing_trades += 1
                out.trades_this_run.append(trade)
                self._active_condition_ids.add(opp.condition_id)
                pnl_delta = -inv
                self._pnl["mispricing"] += pnl_delta
                out.mispricing_pnl += pnl_delta
                try:
                    self.db.insert_trade(trade, category, "mispricing")
                    self.db.insert_opportunity("mispricing", opp.market_question, opp.edge_pct, True)
                except Exception:
                    pass
            else:
                try:
                    self.db.insert_opportunity("mispricing", opp.market_question, opp.edge_pct, False)
                except Exception:
                    pass

    def _apply_near_resolved(
        self, results: dict, errors: dict, out: StrategyResult
    ) -> None:
        opps = results.get("near_resolved", [])
        if not opps:
            return

        out.nr_opps = len(opps)

        for opp in opps[:2]:  # Try top 2
            if not self._can_trade(opp.condition_id):
                continue
            if not self._within_global_limits():
                break
            if not self.nr_scanner.is_cooled_down(opp.condition_id):
                continue

            inv = self._sized_investment(opp.size_multiplier)

            trade = self.trader.execute_near_resolved_trade(
                opp, investment_usd=inv
            )
            if trade:
                trade.strategy = "near_resolved"
                out.nr_trades += 1
                out.trades_this_run.append(trade)
                self._active_condition_ids.add(opp.condition_id)
                self.nr_scanner.mark_traded(opp.condition_id)
                pnl_delta = -inv
                self._pnl["near_resolved"] += pnl_delta
                out.nr_pnl += pnl_delta
                try:
                    self.db.insert_trade(trade, "", "near_resolved")
                    self.db.insert_opportunity("near_resolved", opp.market_question, opp.return_pct, True)
                except Exception:
                    pass
            else:
                try:
                    self.db.insert_opportunity("near_resolved", opp.market_question, opp.return_pct, False)
                except Exception:
                    pass

    def _apply_correlated(
        self, results: dict, errors: dict, out: StrategyResult
    ) -> None:
        pairs = results.get("correlated", [])
        if not pairs:
            return

        out.corr_opps = len(pairs)

        # Respect CORRELATED_MAX_POSITIONS
        corr_active = sum(
            1 for cid in self._active_condition_ids
            if cid.startswith("corr_")
        )

        for pair in pairs[:config.CORRELATED_MAX_POSITIONS]:
            if corr_active >= config.CORRELATED_MAX_POSITIONS:
                break
            if not self._within_global_limits():
                break
            if not self._can_trade(pair.buy_market_id):
                continue

            inv = config.DEFAULT_TRADE_SIZE_USD

            # Build a minimal trade-like object for DB logging
            if config.DRY_RUN:
                logger.info(
                    "CorrelatedArb DRY: buy %s @ $%.3f | sell %s @ $%.3f | div=%.1f%%",
                    pair.market_a_question[:30], pair.buy_price,
                    pair.market_b_question[:30], pair.sell_price,
                    pair.divergence_pct,
                )
                out.corr_trades += 1
                corr_active += 1
                self._active_condition_ids.add(f"corr_{pair.buy_market_id}")
                pnl_delta = -inv
                self._pnl["correlated"] += pnl_delta
                out.corr_pnl += pnl_delta
                try:
                    self.db.insert_opportunity(
                        "correlated", pair.description[:100], pair.divergence_pct, True
                    )
                except Exception:
                    pass

    def _apply_sniper(
        self, results: dict, errors: dict, out: StrategyResult
    ) -> None:
        sniper_res = results.get("sniper", {})
        if not sniper_res:
            return

        orders  = sniper_res.get("orderbook_orders", [])
        signals = sniper_res.get("endcycle_signals", []) + sniper_res.get("crash_signals", [])

        out.sniper_orders  = len(orders)
        out.sniper_signals = len(signals)

        for sig in signals[:2]:
            if not self._within_global_limits():
                break
            if not self._can_trade(sig.condition_id):
                continue

            size = getattr(sig, "size_usd", config.DEFAULT_TRADE_SIZE_USD)

            if config.DRY_RUN:
                logger.info(
                    "Sniper DRY: %s %s @ $%.3f | $%.2f",
                    sig.side, sig.market_question[:40], sig.entry_price, size
                )
                out.sniper_pnl -= size
                self._pnl["sniper"] -= size
                self._active_condition_ids.add(sig.condition_id)
                try:
                    self.db.insert_opportunity(
                        "sniper", sig.market_question, 0.0, True
                    )
                except Exception:
                    pass

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _can_trade(self, condition_id: str) -> bool:
        """Prevent duplicate positions on same market."""
        return condition_id not in self._active_condition_ids

    def _within_global_limits(self) -> bool:
        """Check against MAX_CONCURRENT_POSITIONS and MAX_TOTAL_EXPOSURE_USD."""
        daily = self.trader.get_daily_summary()
        if daily["open_positions"] >= config.MAX_CONCURRENT_POSITIONS:
            logger.debug("Orchestrator: max concurrent positions reached")
            return False
        if daily["total_exposure_usd"] >= config.MAX_TOTAL_EXPOSURE_USD:
            logger.debug("Orchestrator: max total exposure reached")
            return False
        return True

    def _sized_investment(self, multiplier: float) -> float:
        base = self.trader._current_trade_size
        sized = round(base * multiplier, 2)
        return min(sized, config.MAX_POSITION_USD)
