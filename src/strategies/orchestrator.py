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
        self.sniper      = LiquiditySniper(
            self.gamma, self.clob, trader=trader, can_place=self._can_trade
        )

        self._health = {
            "mispricing":    HealthMonitor("Mispricing"),
            "near_resolved": HealthMonitor("NearResolved"),
            "correlated":    HealthMonitor("Correlated"),
            "sniper":        HealthMonitor("Sniper"),
        }

        # Global duplicate-position guard: condition_id → expiry timestamp (seconds).
        # Entries older than NEAR_RESOLVED_COOLDOWN_MINUTES are ignored by _can_trade
        # so the same market can be traded again after the cooldown window.
        self._active_condition_ids: dict[str, float] = {}
        self._scan_count: int = 0

        self._mode: str = "live"

        # Cumulative per-strategy P&L (session-level)
        self._pnl: dict[str, float] = {
            "mispricing": 0.0, "near_resolved": 0.0, "correlated": 0.0, "sniper": 0.0
        }

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self, categories: list[str] | None = None) -> StrategyResult:
        """Run all enabled strategies in priority order. Thread-safe."""
        self._scan_count += 1
        result = StrategyResult()

        # Evict expired cooldown entries to prevent unbounded growth and
        # allow the same market to be retried after the cooldown window.
        _now = time.time()
        self._active_condition_ids = {
            k: v for k, v in self._active_condition_ids.items() if v > _now
        }
        cats   = categories or config.SCAN_CATEGORIES

        results: dict[str, object] = {}
        errors:  dict[str, str]    = {}

        # Phase 1: run all strategy SCANS in parallel (pure API calls, no order placement).
        # Sniper uses scan_markets() here — no orders are posted yet.
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

        # Correlated: run on first scan and then every CORRELATED_SCAN_EVERY scans
        if (config.CORRELATED_ARB_ENABLED
                and self._health["correlated"].is_ok
                and (self._scan_count == 1 or self._scan_count % config.CORRELATED_SCAN_EVERY == 0)):
            t = threading.Thread(
                target=self._run_correlated, args=(cats, results, errors), daemon=True
            )
            threads.append(t)

        if config.LIQUIDITY_SNIPE_ENABLED and self._health["sniper"].is_ok:
            # Scan only — placement happens AFTER other _apply_* update cooldowns (Phase 2)
            t = threading.Thread(
                target=self._scan_sniper, args=(cats, results, errors), daemon=True
            )
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)  # Max 60s per strategy batch

        # Phase 2: apply results in priority order — this updates _active_condition_ids cooldowns.
        # Mispricing, NearResolved, Correlated run first so their cooldowns are written
        # before the sniper place phase checks _can_trade().
        self._apply_mispricing(results, errors, result)
        self._apply_near_resolved(results, errors, result)
        self._apply_correlated(results, errors, result)

        # FIX #7 FINAL: place sniper orders AFTER other strategies have updated cooldowns.
        # _place_sniper() calls sniper.place_orders() which triggers OrderbookSniper.place()
        # which checks self._can_place (= self._can_trade) with fully updated cooldown state.
        self._place_sniper(results, errors)
        self._apply_sniper(results, errors, result)

        result.total_trades = len(result.trades_this_run)
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
                mode=         self._mode,
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
        """Legacy single-phase run (kept for reference; no longer called by run())."""
        try:
            sniper_res = self.sniper.run(categories=cats)
            results["sniper"] = sniper_res
            self._health["sniper"].record_success()
        except Exception as e:
            errors["sniper"] = str(e)
            self._health["sniper"].record_failure(str(e))
            results["sniper"] = {}

    def _scan_sniper(
        self, cats: list[str], results: dict, errors: dict
    ) -> None:
        """Phase 1: scan-only (no order placement). Safe to run in parallel."""
        try:
            sniper_scan = self.sniper.scan_markets(categories=cats)
            results["sniper"] = sniper_scan
            self._health["sniper"].record_success()
        except Exception as e:
            errors["sniper"] = str(e)
            self._health["sniper"].record_failure(str(e))
            results["sniper"] = {}

    def _place_sniper(self, results: dict, errors: dict) -> None:
        """Phase 2: place orders AFTER other strategies have updated cooldowns.

        Called sequentially after _apply_mispricing/_apply_near_resolved/
        _apply_correlated so _active_condition_ids is fully up-to-date and
        OrderbookSniper._can_place gives accurate same-cycle cross-strategy dedup.

        HIGH FIX: checks global risk limits before placing any orders so
        orderbook sniper cannot bypass MAX_CONCURRENT_POSITIONS / MAX_TOTAL_EXPOSURE.
        """
        sniper_scan = results.get("sniper")
        if not sniper_scan:
            return
        # Enforce global risk gate before allowing any sniper order placement
        if not self._within_global_limits():
            logger.debug("Orchestrator: sniper placement skipped — global risk limits reached")
            return
        try:
            self.sniper.place_orders(sniper_scan)
            # sniper_scan dict is mutated in-place by place_orders(); results["sniper"] updated.
        except Exception as e:
            logger.warning("Orchestrator: sniper place phase error: %s", e)

    # ── Apply results ─────────────────────────────────────────────────────────

    def _apply_mispricing(
        self, results: dict, errors: dict, out: StrategyResult
    ) -> None:
        opps = results.get("mispricing", [])
        if not opps:
            return

        out.mispricing_opps = len(opps)

        for opp in opps[:5]:  # Try top 5
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
                _cooldown = config.POSITION_COOLDOWN_MINUTES * 60
                self._active_condition_ids[opp.condition_id] = time.time() + _cooldown
                pnl_delta = inv * (opp.net_edge / 100)
                self._pnl["mispricing"] += pnl_delta
                out.mispricing_pnl += pnl_delta
                try:
                    self.db.insert_trade(trade, category, "mispricing", mode=self._mode)
                    self.db.insert_opportunity("mispricing", opp.market_question, opp.edge_pct, True, mode=self._mode)
                except Exception:
                    pass
            else:
                try:
                    self.db.insert_opportunity("mispricing", opp.market_question, opp.edge_pct, False, mode=self._mode)
                except Exception:
                    pass

    def _apply_near_resolved(
        self, results: dict, errors: dict, out: StrategyResult
    ) -> None:
        opps = results.get("near_resolved", [])
        if not opps:
            return

        out.nr_opps = len(opps)

        for opp in opps[:5]:  # Try top 5 per scan
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
                _cooldown = config.POSITION_COOLDOWN_MINUTES * 60
                self._active_condition_ids[opp.condition_id] = time.time() + _cooldown
                self.nr_scanner.mark_traded(opp.condition_id)
                pnl_delta = inv * (opp.return_pct / 100)
                self._pnl["near_resolved"] += pnl_delta
                out.nr_pnl += pnl_delta
                try:
                    self.db.insert_trade(trade, "", "near_resolved", mode=self._mode)
                    self.db.insert_opportunity("near_resolved", opp.market_question, opp.return_pct, True, mode=self._mode)
                except Exception:
                    pass
            else:
                try:
                    self.db.insert_opportunity("near_resolved", opp.market_question, opp.return_pct, False, mode=self._mode)
                except Exception:
                    pass

    def _apply_correlated(
        self, results: dict, errors: dict, out: StrategyResult
    ) -> None:
        pairs = results.get("correlated", [])
        if not pairs:
            return

        out.corr_opps = len(pairs)

        # Respect CORRELATED_MAX_POSITIONS (only count non-expired entries).
        # FIX #4: dedupe key is "corr_<buy_market_id>" — _can_trade must use
        # the same key so the guard actually fires.
        _now = time.time()
        corr_active = sum(
            1 for cid, exp in self._active_condition_ids.items()
            if cid.startswith("corr_") and exp > _now
        )

        for pair in pairs[:config.CORRELATED_MAX_POSITIONS]:
            if corr_active >= config.CORRELATED_MAX_POSITIONS:
                break
            if not self._within_global_limits():
                break

            # FIX #4: use the same prefixed key that we store below
            corr_key = f"corr_{pair.buy_market_id}"
            if not self._can_trade(corr_key):
                continue

            inv = config.DEFAULT_TRADE_SIZE_USD
            _cooldown = config.POSITION_COOLDOWN_MINUTES * 60

            if True:
                # live correlated execution path
                # Strategy: BUY the underpriced leg (market_a), SELL the overpriced leg (market_b).
                # Both orders are GTC maker orders (0% fee).
                try:
                    # HIGH FIX: prospective risk check before committing capital so
                    # correlated path cannot overshoot MAX_OPEN_POSITIONS or MAX_TOTAL_EXPOSURE.
                    if (self.trader._open_position_count >= config.MAX_OPEN_POSITIONS
                            or self.trader._total_exposure_usd + inv > config.MAX_TOTAL_EXPOSURE_USD
                            or self.trader._daily_pnl <= -config.MAX_DAILY_LOSS_USD):
                        logger.debug(
                            "CorrelatedArb: prospective risk check failed — skipping pair %s",
                            pair.buy_market_id[:12],
                        )
                        continue

                    buy_trade = self.trader._place_order(
                        token_id=pair.buy_token_id,
                        side="BUY",
                        size=inv,
                        price=pair.buy_price,
                        condition_id=pair.buy_market_id,
                        order_type="GTC",
                    )
                    if not buy_trade:
                        logger.warning("CorrelatedArb: BUY leg failed for %s", pair.buy_market_id[:12])
                        continue

                    sell_trade = self.trader._place_order(
                        token_id=pair.sell_token_id,
                        side="SELL",
                        size=inv,
                        price=pair.sell_price,
                        condition_id=pair.sell_market_id,
                        order_type="GTC",
                    )
                    if not sell_trade:
                        # Cancel the BUY leg to avoid unhedged directional exposure
                        if buy_trade.order_id:
                            try:
                                self.clob.cancel_order(buy_trade.order_id)
                                logger.warning(
                                    "CorrelatedArb: SELL leg failed — cancelled BUY %s",
                                    buy_trade.order_id,
                                )
                            except Exception as ce:
                                logger.error(
                                    "CorrelatedArb: SELL leg failed AND cancel failed: %s", ce
                                )
                        continue

                    logger.info(
                        "CorrelatedArb LIVE: buy %s @ $%.3f | sell %s @ $%.3f | div=%.1f%%",
                        pair.market_a_question[:30], pair.buy_price,
                        pair.market_b_question[:30], pair.sell_price,
                        pair.divergence_pct,
                    )
                    out.corr_trades += 1
                    corr_active += 1
                    _expiry = time.time() + _cooldown
                    # HIGH FIX: store prefixed corr key (internal dedup) AND raw
                    # condition IDs so mispricing/near_resolved/sniper see cooldown.
                    self._active_condition_ids[corr_key] = _expiry
                    self._active_condition_ids[pair.buy_market_id]  = _expiry
                    self._active_condition_ids[pair.sell_market_id] = _expiry
                    # Debit capital at entry; credit back on redemption.
                    # Mutate ALL counters first, then persist atomically.
                    self.trader._daily_pnl -= inv
                    self.trader._open_position_count += 1
                    self.trader._total_exposure_usd += inv
                    self.trader._persist_daily_pnl()
                    pnl_delta = inv * (pair.divergence_pct / 100) * 0.5
                    self._pnl["correlated"] += pnl_delta
                    out.corr_pnl += pnl_delta
                    try:
                        self.db.insert_opportunity(
                            "correlated", pair.description[:100], pair.divergence_pct, True, mode=self._mode
                        )
                    except Exception:
                        pass
                    try:
                        buy_trade.strategy = "correlated"
                        buy_trade.market_question = pair.market_a_question
                        self.db.insert_trade(buy_trade, "", "correlated", mode=self._mode)
                        self.trader.register_entry(pair.buy_market_id, pair.buy_price, inv)
                    except Exception:
                        pass
                except Exception as exc:
                    logger.warning("CorrelatedArb: execution error: %s", exc)

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

        # FIX #7: apply cross-strategy dedup and cooldown to orderbook orders so that
        # a market already traded by mispricing/near_resolved/correlated is not also
        # entered by the sniper in the same cooldown window.
        for order in orders:
            cid = getattr(order, "condition_id", "") or ""
            if cid and order.status not in ("cancelled", "cancelled_expired"):
                if not self._can_trade(cid):
                    logger.debug("Sniper orderbook: dedup skipped %s (cooldown active)", cid[:12])
                else:
                    _cooldown = config.POSITION_COOLDOWN_MINUTES * 60
                    self._active_condition_ids[cid] = time.time() + _cooldown

        for sig in signals[:2]:
            if not self._within_global_limits():
                break
            if not self._can_trade(sig.condition_id):
                continue

            size = getattr(sig, "size_usd", config.DEFAULT_TRADE_SIZE_USD)

            try:
                trade = self.trader._place_order(
                    token_id=sig.token_id,
                    side="BUY",
                    size=size,
                    price=sig.entry_price,
                    condition_id=sig.condition_id,
                    order_type="GTC",
                )
                if trade:
                    trade.strategy = "sniper"
                    out.sniper_pnl += size * 0.02
                    self._pnl["sniper"] += size * 0.02
                    out.trades_this_run.append(trade)
                    self.trader._daily_pnl -= size
                    self.trader._open_position_count += 1
                    self.trader._total_exposure_usd += size
                    self.trader._persist_daily_pnl()
                    self.trader.register_entry(sig.condition_id, sig.entry_price, size)
                    _cooldown = config.POSITION_COOLDOWN_MINUTES * 60
                    self._active_condition_ids[sig.condition_id] = time.time() + _cooldown
                    try:
                        self.db.insert_trade(trade, "", "sniper", mode=self._mode)
                        self.db.insert_opportunity(
                            "sniper", sig.market_question, 0.0, True, mode=self._mode
                        )
                    except Exception:
                        pass
            except Exception as exc:
                logger.warning("Sniper live execution error: %s", exc)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _can_trade(self, condition_id: str) -> bool:
        """Prevent duplicate positions on same market within the cooldown window."""
        expiry = self._active_condition_ids.get(condition_id)
        return expiry is None or time.time() > expiry

    def _within_global_limits(self, prospective_usd: float = 0.0) -> bool:
        """Check open positions, total exposure, and daily loss against limits.

        Uses the same constants as Trader._check_safety_limits so both
        enforcement paths are consistent. Accepts an optional prospective
        investment size for forward-looking exposure checks.
        """
        daily = self.trader.get_daily_summary()
        if daily["open_positions"] >= config.MAX_OPEN_POSITIONS:
            logger.debug("Orchestrator: max open positions reached")
            return False
        if daily["total_exposure_usd"] + prospective_usd > config.MAX_TOTAL_EXPOSURE_USD:
            logger.debug("Orchestrator: max total exposure reached")
            return False
        if daily["daily_pnl"] <= -config.MAX_DAILY_LOSS_USD:
            logger.debug("Orchestrator: daily loss limit reached")
            return False
        return True

    def _sized_investment(self, multiplier: float) -> float:
        base = self.trader._current_trade_size
        sized = round(base * multiplier, 2)
        return max(config.MIN_TRADE_SIZE_USD, min(sized, config.MAX_POSITION_USD))
