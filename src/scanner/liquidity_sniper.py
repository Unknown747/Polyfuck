"""Liquidity Sniper — three sub-strategies.

4a. OrderbookSniper
    Place 3-tier GTC limit orders at deep discounts (1¢/2¢/3¢).
    Auto-cancel orders after 24 hours.

4b. EndcycleSniper
    Monitor BTC/ETH/SOL/XRP 5-minute Polymarket markets.
    Track real-time price from Binance REST (no auth needed).
    Trigger: >0.5% price movement in 30 seconds.
    Entry: T-10 seconds before market close.

4c. CrashReboundSniper
    Detect 15%+ price drop in <60 minutes with 2× volume spike.
    Entry: 10% after crash stabilization.
    Exit: 15% rebound or 6-hour max hold.
    Stop: additional 10% drop from entry.
"""

from __future__ import annotations

import json
import logging
import time
import datetime
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import requests

from src.config import config

if TYPE_CHECKING:
    from src.utils.api import GammaClient, ClobClient

logger = logging.getLogger("polymarket-bot")

# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class SniperOrder:
    """A GTC limit order placed by the orderbook sniper."""
    market_question: str
    condition_id:    str
    token_id:        str
    side:            str
    price:           float
    size_pct:        float    # % of sniper allocation at this tier
    order_id:        str   = ""
    placed_at:       float = field(default_factory=time.time)
    status:          str   = "pending"

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.placed_at) > 86_400  # 24 hours


@dataclass
class EndcycleSignal:
    """A 5-minute market endcycle trading signal."""
    condition_id:    str
    market_question: str
    token_id:        str
    side:            str    # YES or NO
    entry_price:     float
    crypto_symbol:   str
    movement_pct:    float
    seconds_to_close:float
    size_usd:        float  = 0.0


@dataclass
class CrashReboundSignal:
    """A detected crash-rebound opportunity."""
    condition_id:    str
    market_question: str
    token_id:        str
    side:            str
    entry_price:     float
    crash_pct:       float    # magnitude of detected crash
    volume_factor:   float    # actual / average volume ratio
    stop_price:      float    # entry × 0.90 (10% further drop)
    target_price:    float    # entry × 1.15 (15% rebound)
    max_hold_until:  float    = field(default_factory=lambda: time.time() + 6*3600)


# ── Sub-strategy: OrderbookSniper ─────────────────────────────────────────────

class OrderbookSniper:
    """Places 3-tier GTC limit orders at deep discounts in liquid markets.

    FIX #6a: tier prices now read from config (SNIPER_TIER1_PRICE etc.) instead
    of being hardcoded to 1¢/2¢/3¢.
    FIX #6b: per-market + per-tier deduplication — only one pending order per
    (condition_id, tier) is kept; re-placing the same order every scan is avoided.
    FIX #6c: live path reuses the shared clob/trader instance rather than
    creating a fresh Trader() per order, so accumulated risk counters are preserved.
    """

    @property
    def TIERS(self):
        return [
            (1, config.SNIPER_TIER1_PRICE),
            (2, config.SNIPER_TIER2_PRICE),
            (3, config.SNIPER_TIER3_PRICE),
        ]

    @property
    def TIER_ALLOC(self):
        return {
            1: config.SNIPER_TIER1_PCT / 100,
            2: config.SNIPER_TIER2_PCT / 100,
            3: config.SNIPER_TIER3_PCT / 100,
        }

    def __init__(self, gamma=None, clob=None, trader=None, can_place=None):
        from src.utils.api import GammaClient, ClobClient
        self.gamma:  GammaClient = gamma  or GammaClient()
        self.clob:   ClobClient  = clob   or ClobClient()
        self._trader = trader   # shared Trader instance (optional, used in live mode)
        # FIX #7: optional cross-strategy dedup predicate injected by orchestrator.
        # Signature: (condition_id: str) -> bool — returns True if the market is
        # not already in a cooldown window from another strategy.
        self._can_place = can_place
        self._orders: list[SniperOrder] = []
        self._consecutive_failures: int = 0
        self.enabled: bool = True
        # FIX #6b: track (condition_id, tier_num) pairs that already have a live order
        self._placed: set[tuple[str, int]] = set()

    def scan(self, categories: list[str] | None = None) -> list[dict]:
        """Phase 1 (parallel-safe): fetch candidate markets from the API only.

        No orders are posted here. Call place() afterwards, once the
        orchestrator has updated cross-strategy cooldowns via _apply_*.
        """
        if not self.enabled or not config.SNIPER_ENABLED:
            return []
        self._cancel_expired()
        return self._fetch_liquid_markets(categories)[:5]

    def place(self, markets: list[dict]) -> list[SniperOrder]:
        """Phase 2 (sequential, post-cooldown-update): place tier orders.

        Must be called AFTER the orchestrator has applied mispricing /
        near-resolved / correlated results so _active_condition_ids is
        up-to-date and _can_place() gives accurate same-cycle dedup.
        """
        placed: list[SniperOrder] = []
        total_budget = config.DEFAULT_TRADE_SIZE_USD
        for market in markets:
            orders = self._place_tiers(market, total_budget)
            placed.extend(orders)
        return placed

    def scan_and_place(self, categories: list[str] | None = None) -> list[SniperOrder]:
        """Convenience wrapper (kept for backward-compat). Prefer scan()+place()."""
        return self.place(self.scan(categories))

    def get_active_orders(self) -> list[SniperOrder]:
        return [o for o in self._orders if o.status in ("pending", "dry_run") and not o.is_expired]

    def _cancel_expired(self) -> None:
        # Medium fix: expire both "pending" (live) and "dry_run" orders so _placed
        # keys are cleared in long dry-run sessions and re-placement can occur.
        for order in self._orders:
            if order.is_expired and order.status in ("pending", "dry_run"):
                if order.status == "pending" and not config.DRY_RUN and order.order_id:
                    try:
                        self.clob.cancel_order(order.order_id)
                    except Exception as e:
                        logger.warning("OrderbookSniper: cancel failed %s: %s", order.order_id, e)
                order.status = "cancelled_expired"
        # Clear _placed keys for any order that just expired
        expired_keys: set[tuple[str, int]] = set()
        for o in self._orders:
            if o.status == "cancelled_expired":
                for tier_num, tier_price in self.TIERS:
                    if o.price == tier_price:
                        expired_keys.add((o.condition_id, tier_num))
        self._placed -= expired_keys
        self._orders = [o for o in self._orders if o.status != "cancelled_expired"]

    def _place_tiers(self, market: dict, budget: float) -> list[SniperOrder]:
        orders: list[SniperOrder] = []
        tokens = self._parse_tokens(market)
        condition_id = market.get("conditionId", "")
        question     = market.get("question", "")
        token_id     = tokens.get("yes", "")

        if not condition_id or not token_id:
            return []

        # FIX #7: cross-strategy dedup — if the orchestrator's cooldown registry
        # says this market was already traded in another strategy this cycle,
        # skip all tiers entirely before posting any order.
        if self._can_place is not None and not self._can_place(condition_id):
            logger.debug(
                "OrderbookSniper: cross-strategy dedup skip %s (cooldown active)",
                condition_id[:12],
            )
            return []

        for tier_num, price in self.TIERS:
            # FIX #6b: skip if this (market, tier) already has a live pending order
            dedup_key = (condition_id, tier_num)
            if dedup_key in self._placed:
                continue

            alloc    = self.TIER_ALLOC.get(tier_num, 0.2)
            size_usd = round(budget * alloc, 2)

            order = SniperOrder(
                market_question= question,
                condition_id=    condition_id,
                token_id=        token_id,
                side=            "BUY",
                price=           price,
                size_pct=        alloc * 100,
            )

            if config.DRY_RUN:
                order.status   = "dry_run"
                order.order_id = f"dry_{tier_num}_{condition_id[:8]}"
                logger.info(
                    "OrderbookSniper DRY: Tier %d @ $%.2f × $%.2f in %s",
                    tier_num, price, size_usd, question[:40]
                )
                self._placed.add(dedup_key)
            else:
                try:
                    if self.clob._authenticated:
                        # HIGH FIX: check ALL global risk limits per-tier before each order:
                        # open positions, prospective exposure, and daily loss so multiple
                        # tiers cannot collectively breach any limit while counters unchanged.
                        if self._trader is not None:
                            t = self._trader
                            if (t._open_position_count >= config.MAX_OPEN_POSITIONS
                                    or t._total_exposure_usd + size_usd > config.MAX_TOTAL_EXPOSURE_USD
                                    or t._daily_pnl <= -config.MAX_DAILY_LOSS_USD):
                                logger.debug(
                                    "OrderbookSniper: risk limit reached at tier %d — stopping",
                                    tier_num,
                                )
                                break  # stop placing further tiers for this market

                            trade = t._place_order(
                                token_id=token_id,
                                side="BUY",
                                size=size_usd,
                                price=price,
                                condition_id=condition_id,
                                order_type="GTC",
                            )
                        else:
                            logger.warning(
                                "OrderbookSniper: no shared Trader injected — "
                                "skipping tier %d for %s to avoid bypassing risk limits",
                                tier_num, condition_id[:12],
                            )
                            continue

                        if trade:
                            order.order_id = trade.order_id or ""
                            order.status   = trade.status
                            self._placed.add(dedup_key)
                            # HIGH FIX: update and persist risk counters immediately so
                            # subsequent tier/market checks see accurate exposure totals.
                            if self._trader is not None:
                                self._trader._daily_pnl        -= size_usd
                                self._trader._open_position_count  += 1
                                self._trader._total_exposure_usd   += size_usd
                                self._trader._persist_daily_pnl()
                                # Record to DB and register for trailing stop
                                trade.strategy       = "sniper"
                                trade.market_question = question
                                _mode = "paper" if config.DRY_RUN else "live"
                                try:
                                    import src.utils.db as _sniper_db
                                    _sniper_db.insert_trade(trade, "", "sniper", mode=_mode)
                                except Exception:
                                    pass
                                self._trader.register_entry(condition_id, price, size_usd)
                except Exception as e:
                    logger.warning("OrderbookSniper: place failed tier %d: %s", tier_num, e)
                    self._consecutive_failures += 1

            self._orders.append(order)
            orders.append(order)

        return orders

    def _fetch_liquid_markets(self, categories: list[str] | None) -> list[dict]:
        markets = []
        cats = categories or ["crypto", "politics"]
        for cat in cats[:2]:  # Snipe only top 2 categories
            try:
                events = self.gamma.get_events(tag=cat, limit=20)
                for event in events:
                    for m in event.get("markets", []):
                        try:
                            vol = float(m.get("volume24hr", 0) or 0)
                            if vol >= config.MIN_LIQUIDITY_USD:
                                m["_category"] = cat
                                markets.append(m)
                        except (ValueError, TypeError):
                            pass
            except Exception as e:
                logger.warning("OrderbookSniper: fetch failed for %s: %s", cat, e)
        return sorted(markets, key=lambda m: float(m.get("volume24hr", 0) or 0), reverse=True)

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


# ── Sub-strategy: EndcycleSniper ──────────────────────────────────────────────

class EndcycleSniper:
    """Monitors 5-minute crypto markets and snipes near close on price movement."""

    # Binance REST endpoint (no auth required)
    BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"
    CRYPTO_SYMBOLS = {
        "bitcoin":  "BTCUSDT",
        "btc":      "BTCUSDT",
        "ethereum": "ETHUSDT",
        "eth":      "ETHUSDT",
        "solana":   "SOLUSDT",
        "sol":      "SOLUSDT",
        "xrp":      "XRPUSDT",
        "ripple":   "XRPUSDT",
    }

    def __init__(self, gamma=None):
        from src.utils.api import GammaClient
        self.gamma: GammaClient = gamma or GammaClient()
        # Price history: symbol → deque of (timestamp, price)
        self._price_history: dict[str, deque] = {}
        self._last_binance_fetch: dict[str, float] = {}
        self._consecutive_failures: int = 0
        self.enabled: bool = True

    def scan(self, categories: list[str] | None = None) -> list[EndcycleSignal]:
        """Scan for endcycle opportunities in 5-minute crypto markets."""
        if not self.enabled or not config.ENDCYCLE_ENABLED:
            return []

        signals: list[EndcycleSignal] = []
        near_close = self._find_near_close_markets(categories)

        for market, symbol, secs_to_close in near_close:
            try:
                movement = self._get_movement(symbol)
                if abs(movement) >= config.ENDCYCLE_MIN_MOVEMENT:
                    signal = self._build_signal(market, symbol, movement, secs_to_close)
                    if signal:
                        signals.append(signal)
            except Exception as e:
                logger.debug("EndcycleSniper: signal error %s: %s", symbol, e)

        return signals

    def _find_near_close_markets(
        self, categories: list[str] | None
    ) -> list[tuple[dict, str, float]]:
        """Find crypto markets closing within 10 minutes."""
        result = []
        cats = categories or ["crypto"]
        for cat in cats:
            try:
                events = self.gamma.get_events(tag=cat, limit=50)
                for event in events:
                    for market in event.get("markets", []):
                        hours = self._hours_to_close(market)
                        if hours == float("inf") or hours < 0:
                            continue
                        secs = hours * 3600
                        if secs > 600:  # Only within 10 minutes
                            continue
                        # Detect crypto symbol from question
                        question = market.get("question", "").lower()
                        symbol = None
                        for keyword, sym in self.CRYPTO_SYMBOLS.items():
                            if keyword in question:
                                symbol = sym
                                break
                        if symbol:
                            result.append((market, symbol, secs))
            except Exception as e:
                logger.warning("EndcycleSniper: fetch failed for %s: %s", cat, e)
                self._consecutive_failures += 1
        return result

    def _get_movement(self, symbol: str) -> float:
        """Return price movement % in last 30 seconds from Binance REST."""
        now = time.time()

        # Fetch current price (max once per 5 seconds per symbol)
        if now - self._last_binance_fetch.get(symbol, 0) >= 5:
            try:
                resp = requests.get(
                    self.BINANCE_TICKER,
                    params={"symbol": symbol},
                    timeout=3,
                )
                if resp.status_code == 200:
                    price = float(resp.json().get("price", 0))
                    if symbol not in self._price_history:
                        self._price_history[symbol] = deque(maxlen=60)
                    self._price_history[symbol].append((now, price))
                    self._last_binance_fetch[symbol] = now
            except Exception as e:
                logger.debug("EndcycleSniper: Binance fetch failed %s: %s", symbol, e)
                return 0.0

        history = self._price_history.get(symbol)
        if not history or len(history) < 2:
            return 0.0

        # Compare current price vs price 30 seconds ago
        current_ts, current_price = history[-1]
        for ts, price in history:
            if current_ts - ts >= 30:
                if price > 0:
                    return ((current_price - price) / price) * 100
        return 0.0

    def _build_signal(
        self, market: dict, symbol: str, movement: float, secs_to_close: float
    ) -> EndcycleSignal | None:
        prices = self._parse_prices(market)
        if not prices or len(prices) < 2:
            return None

        yes_price, no_price = prices[0], prices[1]
        tokens = self._parse_tokens(market)

        # If price moving UP → YES more likely; DOWN → NO more likely
        if movement > 0:
            side, price, token_id = "YES", yes_price, tokens.get("yes", "")
        else:
            side, price, token_id = "NO", no_price, tokens.get("no", "")

        if not token_id or price <= 0.01 or price >= 0.99:
            return None

        size_usd = min(config.ENDCYCLE_POSITION_USD, config.MAX_POSITION_USD)

        logger.info(
            "EndcycleSniper: %s %s movement %.2f%% in last 30s, snipe %s @ $%.3f",
            symbol, ("UP" if movement > 0 else "DOWN"), abs(movement), side, price
        )

        return EndcycleSignal(
            condition_id=    market.get("conditionId", ""),
            market_question= market.get("question", ""),
            token_id=        token_id,
            side=            side,
            entry_price=     price,
            crypto_symbol=   symbol,
            movement_pct=    movement,
            seconds_to_close=secs_to_close,
            size_usd=        size_usd,
        )

    @staticmethod
    def _hours_to_close(market: dict) -> float:
        raw = market.get("endDate") or market.get("endDateIso") or ""
        if not raw:
            return float("inf")
        try:
            s = str(raw).strip()
            # BUG FIX: was strptime(s[:len(fmt)], fmt) — len(fmt) is the length of
            # the format string (e.g. 19), not the length of the date value (20+).
            # This always caused a ValueError, so the method always returned inf and
            # EndcycleSniper never fired. Use fromisoformat instead (same approach
            # as MarketScanner._hours_to_close which is known-correct).
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.datetime.fromisoformat(s if "T" in s else s + "T00:00:00+00:00")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            return (dt - now).total_seconds() / 3600
        except Exception:
            return float("inf")

    @staticmethod
    def _parse_prices(market: dict) -> list[float] | None:
        raw = market.get("outcomePrices")
        if not raw:
            return None
        if isinstance(raw, str):
            try:
                return [float(p) for p in json.loads(raw)]
            except Exception:
                return None
        if isinstance(raw, list):
            try:
                return [float(p) for p in raw]
            except Exception:
                return None
        return None

    @staticmethod
    def _parse_tokens(market: dict) -> dict[str, str]:
        raw = market.get("clobTokenIds")
        if not raw:
            return {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                return {}
        if isinstance(raw, list) and len(raw) >= 2:
            return {"yes": raw[0], "no": raw[1]}
        return {}


# ── Sub-strategy: CrashReboundSniper ─────────────────────────────────────────

class CrashReboundSniper:
    """Detects 15%+ market price crashes and enters on stabilisation."""

    def __init__(self, gamma=None):
        from src.utils.api import GammaClient
        self.gamma: GammaClient = gamma or GammaClient()
        # Price history per market: condition_id → deque[(ts, price)]
        self._price_history: dict[str, deque] = {}
        # Volume history: condition_id → deque[(ts, volume)]
        self._vol_history:   dict[str, deque] = {}
        # Active holds: condition_id → CrashReboundSignal
        self._active: dict[str, CrashReboundSignal] = {}
        self._consecutive_failures: int = 0
        self.enabled: bool = True

    def update_and_scan(
        self, categories: list[str] | None = None
    ) -> list[CrashReboundSignal]:
        """Update price history and return new crash-rebound signals."""
        if not self.enabled or not config.CRASH_REBOUND_ENABLED:
            return []

        signals: list[CrashReboundSignal] = []
        markets = self._fetch_markets(categories)

        for market in markets:
            cid = market.get("conditionId", "")
            if not cid:
                continue
            if cid in self._active:
                continue  # Already holding

            try:
                prices = self._parse_prices(market)
                if not prices or len(prices) < 2:
                    continue

                yes_price = prices[0]
                vol       = self._safe_float(market.get("volume24hr"))
                now       = time.time()

                # Update histories
                if cid not in self._price_history:
                    self._price_history[cid] = deque(maxlen=120)
                    self._vol_history[cid]   = deque(maxlen=120)
                self._price_history[cid].append((now, yes_price))
                self._vol_history[cid].append((now, vol))

                signal = self._detect_crash(market, cid, now)
                if signal:
                    signals.append(signal)
                    self._active[cid] = signal

            except Exception as e:
                logger.debug("CrashReboundSniper: market %s error: %s", cid[:8], e)

        return signals

    def check_exits(self) -> list[str]:
        """Return condition_ids of positions that should be closed."""
        exits = []
        for cid, sig in list(self._active.items()):
            history = self._price_history.get(cid)
            if not history:
                continue
            _, current_price = history[-1]

            # Target hit (15% rebound)
            if current_price >= sig.target_price:
                logger.info("CrashRebound: target hit for %s (%.3f)", cid[:8], current_price)
                exits.append(cid)
                del self._active[cid]
                continue

            # Stop-loss hit (10% further drop)
            if current_price <= sig.stop_price:
                logger.warning("CrashRebound: stop-loss hit for %s (%.3f)", cid[:8], current_price)
                exits.append(cid)
                del self._active[cid]
                continue

            # Max hold time expired
            if time.time() > sig.max_hold_until:
                logger.info("CrashRebound: max hold expired for %s", cid[:8])
                exits.append(cid)
                del self._active[cid]

        return exits

    def _detect_crash(self, market: dict, cid: str, now: float) -> CrashReboundSignal | None:
        history = list(self._price_history.get(cid, []))
        vol_hist= list(self._vol_history.get(cid, []))

        if len(history) < 5:
            return None

        _, current_price = history[-1]

        # Check for 15%+ drop in last 60 minutes
        one_hour_ago = now - 3600
        baseline_price = None
        for ts, price in history:
            if ts >= one_hour_ago:
                baseline_price = price
                break

        if baseline_price is None or baseline_price <= 0:
            return None

        drop_pct = ((baseline_price - current_price) / baseline_price) * 100

        if drop_pct < config.CRASH_REBOUND_DROP_THRESHOLD:
            return None

        # Confirm with volume spike (current vol >= 2× average)
        if len(vol_hist) >= 2:
            avg_vol = sum(v for _, v in vol_hist[:-1]) / max(1, len(vol_hist) - 1)
            _, current_vol = vol_hist[-1]
            vol_factor = current_vol / max(1, avg_vol)
            if vol_factor < 2.0:
                return None
        else:
            vol_factor = 1.0

        # Entry: 10% above current (after stabilisation detection)
        # We approximate "stabilisation" as current price not dropping further
        # in the last 2 data points
        if len(history) >= 3 and history[-1][1] < history[-2][1]:
            return None  # Still falling, wait

        tokens = self._parse_tokens(market)
        entry  = round(current_price * 1.10, 4)
        stop   = round(entry * 0.90, 4)
        target = round(entry * 1.15, 4)

        logger.info(
            "CrashRebound: %s crashed %.1f%% | vol×%.1f | entry %.3f stop %.3f target %.3f",
            market.get("question","")[:40], drop_pct, vol_factor, entry, stop, target
        )

        return CrashReboundSignal(
            condition_id=    cid,
            market_question= market.get("question", ""),
            token_id=        tokens.get("yes", ""),
            side=            "YES",
            entry_price=     entry,
            crash_pct=       drop_pct,
            volume_factor=   vol_factor,
            stop_price=      stop,
            target_price=    target,
        )

    def _fetch_markets(self, categories: list[str] | None) -> list[dict]:
        markets = []
        cats = categories or config.SCAN_CATEGORIES
        for cat in cats:
            try:
                events = self.gamma.get_events(tag=cat, limit=30)
                for event in events:
                    for m in event.get("markets", []):
                        m["_category"] = cat
                        markets.append(m)
            except Exception as e:
                logger.warning("CrashReboundSniper: fetch failed %s: %s", cat, e)
                self._consecutive_failures += 1
        return markets

    @staticmethod
    def _parse_prices(market: dict) -> list[float] | None:
        raw = market.get("outcomePrices")
        if not raw:
            return None
        if isinstance(raw, str):
            try:
                return [float(p) for p in json.loads(raw)]
            except Exception:
                return None
        if isinstance(raw, list):
            try:
                return [float(p) for p in raw]
            except Exception:
                return None
        return None

    @staticmethod
    def _parse_tokens(market: dict) -> dict[str, str]:
        raw = market.get("clobTokenIds")
        if not raw:
            return {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                return {}
        if isinstance(raw, list) and len(raw) >= 2:
            return {"yes": raw[0], "no": raw[1]}
        return {}

    @staticmethod
    def _safe_float(val) -> float:
        try:
            return float(val or 0)
        except (ValueError, TypeError):
            return 0.0


# ── Public facade ─────────────────────────────────────────────────────────────

class LiquiditySniper:
    """Facade that coordinates all three sniper sub-strategies."""

    def __init__(self, gamma=None, clob=None, trader=None, can_place=None):
        self.orderbook  = OrderbookSniper(gamma, clob, trader=trader, can_place=can_place)
        self.endcycle   = EndcycleSniper(gamma)
        self.crash      = CrashReboundSniper(gamma)
        self._scan_count = 0

    def scan_markets(self, categories: list[str] | None = None) -> dict:
        """Phase 1: API-only scan (safe to run in parallel with other strategies).

        Returns candidate markets + endcycle/crash signals. No orders posted.
        Call place_orders(scan_result) afterwards once cooldowns are updated.
        """
        self._scan_count += 1
        results = {
            "orderbook_markets": [],  # raw market dicts for Phase 2 placement
            "orderbook_orders":  [],  # filled in by place_orders()
            "endcycle_signals":  [],
            "crash_signals":     [],
            "crash_exits":       [],
        }

        try:
            if config.SNIPER_ENABLED:
                results["orderbook_markets"] = self.orderbook.scan(categories)
        except Exception as e:
            logger.error("LiquiditySniper: orderbook scan error: %s", e)

        try:
            if config.ENDCYCLE_ENABLED:
                results["endcycle_signals"] = self.endcycle.scan(categories)
        except Exception as e:
            logger.error("LiquiditySniper: endcycle error: %s", e)

        try:
            if config.CRASH_REBOUND_ENABLED:
                results["crash_signals"] = self.crash.update_and_scan(categories)
                results["crash_exits"]   = self.crash.check_exits()
        except Exception as e:
            logger.error("LiquiditySniper: crash rebound error: %s", e)

        return results

    def place_orders(self, scan_result: dict) -> dict:
        """Phase 2: place orderbook orders using scan_result from scan_markets().

        Must be called AFTER the orchestrator has applied all other strategies
        so _active_condition_ids cooldowns are fully up-to-date.
        """
        try:
            if config.SNIPER_ENABLED:
                markets = scan_result.get("orderbook_markets", [])
                scan_result["orderbook_orders"] = self.orderbook.place(markets)
        except Exception as e:
            logger.error("LiquiditySniper: orderbook place error: %s", e)
        return scan_result

    def run(self, categories: list[str] | None = None) -> dict:
        """Legacy single-phase run (scan + place together). Kept for backward-compat."""
        result = self.scan_markets(categories)
        return self.place_orders(result)

    def get_active_orders(self) -> list[SniperOrder]:
        return self.orderbook.get_active_orders()
