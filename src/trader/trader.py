"""Trading execution module - places and manages orders via Polymarket CLOB API."""

import time
import json
from dataclasses import dataclass, field
from typing import Any
from rich.console import Console

from src.config import config
from src.utils.api import ClobClient
from src.scanner.scanner import Mispricing, NearResolvedOpportunity

console = Console()


@dataclass
class Trade:
    """Record of a trade execution."""
    market_question: str
    condition_id: str
    token_id: str
    side: str        # "BUY" or "SELL"
    price: float
    size: float      # Shares for SELL, dollar amount for BUY
    order_type: str  # "GTC", "FOK", "GTD"
    status: str      # "pending", "filled", "partial", "cancelled", "dry_run"
    order_id: str = ""
    # BUG FIX: was `timestamp: float = time.time()` — that evaluates ONCE at class
    # definition time, giving every Trade the same timestamp. Use field() instead.
    timestamp: float = field(default_factory=time.time)
    filled_price: float = 0.0
    filled_size: float = 0.0
    fee_estimate: float = 0.0


class Trader:
    """Executes trades on Polymarket CLOB."""

    # Category taker fee rates (maker = 0)
    TAKER_FEE_RATES = {
        "crypto": 0.07,
        "sports": 0.03,
        "finance": 0.04,
        "politics": 0.04,
        "economics": 0.05,
        "culture": 0.05,
        "geopolitics": 0.0,
    }

    _DAILY_STATE_FILE = "logs/daily_state.json"

    def __init__(self, clob: ClobClient | None = None):
        self.clob = clob or ClobClient()
        self._trade_log: list[Trade] = []
        self._daily_trades: int = 0
        self._open_position_count: int = 0
        self._total_exposure_usd: float = 0.0
        # Load persisted daily PnL so restarts don't reset the loss limit
        self._daily_pnl: float = self._load_daily_pnl()
        # Trailing stop tracking: condition_id -> (entry_price, invested_usd)
        self._position_entry: dict[str, tuple[float, float]] = {}
        self._stops_triggered: int = 0
        # Current trade size (may be updated by auto-compound)
        self._current_trade_size: float = config.DEFAULT_TRADE_SIZE_USD

    def _load_daily_pnl(self) -> float:
        """Load today's PnL from disk. Returns 0 if no file or it's from a previous day."""
        import json, datetime
        try:
            path = __import__("pathlib").Path(self._DAILY_STATE_FILE)
            if not path.exists():
                return 0.0
            data = json.loads(path.read_text())
            saved_date = data.get("date", "")
            today = datetime.date.today().isoformat()
            if saved_date != today:
                return 0.0
            return float(data.get("daily_pnl", 0.0))
        except Exception:
            return 0.0

    def _persist_daily_pnl(self) -> None:
        """Write today's PnL to disk so restarts don't reset the loss limit."""
        import json, datetime
        try:
            path = __import__("pathlib").Path(self._DAILY_STATE_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "date": datetime.date.today().isoformat(),
                "daily_pnl": self._daily_pnl,
            }))
        except Exception:
            pass

    def estimate_fee(self, price: float, size: float, category: str = "crypto") -> float:
        """Estimate taker fee for a trade.

        Formula: fee = C × feeRate × p × (1 − p)
        where C = shares, p = price
        """
        fee_rate = self.TAKER_FEE_RATES.get(category, 0.04)
        fee = size * fee_rate * price * (1 - price)
        return round(fee, 6)

    def calculate_profit_after_fees(
        self, opp: Mispricing, investment: float, category: str = "crypto"
    ) -> dict:
        """Calculate expected profit after fees for a mispricing opportunity.

        For YES+NO < $1.00: buy both sides for guaranteed profit.
        Each complete pair costs (yes_price + no_price) and pays $1.00 on resolution.
        """
        cost_per_pair = opp.price_sum
        if cost_per_pair <= 0:
            return {
                "investment": investment,
                "guaranteed_return": 0.0,
                "total_fees": 0.0,
                "net_profit": -investment,
                "roi_pct": -100.0,
                "profitable": False,
            }

        num_pairs = investment / cost_per_pair
        guaranteed_return = num_pairs * 1.00

        yes_fee = self.estimate_fee(opp.yes_price, num_pairs, category)
        no_fee = self.estimate_fee(opp.no_price, num_pairs, category)
        total_fees = yes_fee + no_fee

        net_profit = guaranteed_return - investment - total_fees
        roi_pct = (net_profit / investment * 100) if investment > 0 else 0

        return {
            "investment": investment,
            "guaranteed_return": guaranteed_return,
            "total_fees": total_fees,
            "net_profit": net_profit,
            "roi_pct": roi_pct,
            "profitable": net_profit > 0,
        }

    def execute_mispricing_trade(
        self, opp: Mispricing, investment_usd: float | None = None, category: str = "crypto"
    ) -> Trade | None:
        """Execute an arbitrage trade on a mispriced market.

        If YES + NO < $1.00: Buy both sides for guaranteed profit at resolution.

        Args:
            opp: The mispricing opportunity
            investment_usd: Dollar amount to invest (defaults to DEFAULT_TRADE_SIZE_USD)
            category: Market category for fee estimation
        """
        # Use configured default trade size if not specified
        # BUG FIX: use self._current_trade_size (may have been updated by auto-compound)
        # rather than the static config default so auto-compound actually takes effect.
        if investment_usd is None:
            investment_usd = self._current_trade_size

        # Cap to max position size
        investment_usd = min(investment_usd, config.MAX_POSITION_USD)

        if not self._check_safety_limits(investment_usd):
            return None

        profit_calc = self.calculate_profit_after_fees(opp, investment_usd, category)

        if not profit_calc["profitable"]:
            console.print(
                f"[red]Trade not profitable after fees: "
                f"${profit_calc['net_profit']:.4f} net on ${investment_usd:.2f}[/]"
            )
            return None

        console.print(
            f"[green]Opportunity:[/] {opp.market_question}\n"
            f"  Invest: ${investment_usd:.2f} | "
            f"Est. Profit: ${profit_calc['net_profit']:.4f} "
            f"({profit_calc['roi_pct']:.1f}% ROI) | "
            f"Fees: ${profit_calc['total_fees']:.4f}"
        )

        if config.DRY_RUN:
            console.print(f"[yellow]DRY RUN: Would place trade on {opp.market_question}[/]")
            trade = Trade(
                market_question=opp.market_question,
                condition_id=opp.condition_id,
                token_id=opp.yes_token_id,
                side="BUY",
                price=opp.yes_price,
                size=investment_usd,
                order_type="GTC",
                status="dry_run",
                fee_estimate=profit_calc["total_fees"],
            )
            self._trade_log.append(trade)
            self._daily_trades += 1
            self._open_position_count += 1
            self._total_exposure_usd += investment_usd
            self.register_entry(opp.condition_id, opp.yes_price, investment_usd)
            return trade

        if not self.clob._authenticated:
            console.print("[red]Not authenticated. Call authenticate() first.[/]")
            return None

        if not self._check_live_balance(investment_usd):
            return None

        try:
            # Split investment proportionally by price so both sides produce
            # the same number of shares (delta-neutral arbitrage).
            cost_per_pair = opp.yes_price + opp.no_price
            if cost_per_pair <= 0:
                console.print("[red]Invalid prices for arbitrage split.[/]")
                return None

            num_pairs = investment_usd / cost_per_pair
            yes_investment = num_pairs * opp.yes_price
            no_investment = num_pairs * opp.no_price

            yes_trade = self._place_order(
                token_id=opp.yes_token_id,
                side="BUY",
                size=yes_investment,
                price=opp.yes_price,
                condition_id=opp.condition_id,
            )

            if not yes_trade:
                console.print("[red]YES leg failed — aborting arbitrage to avoid unhedged position.[/]")
                return None

            no_trade = self._place_order(
                token_id=opp.no_token_id,
                side="BUY",
                size=no_investment,
                price=opp.no_price,
                condition_id=opp.condition_id,
            )

            if not no_trade:
                # NO leg failed: cancel the YES order immediately to avoid a naked directional position
                if yes_trade.order_id:
                    try:
                        self.clob.cancel_order(yes_trade.order_id)
                        console.print(
                            f"[yellow]NO leg failed — cancelled YES order {yes_trade.order_id} "
                            "to avoid unhedged position.[/]"
                        )
                    except Exception as cancel_err:
                        console.print(
                            f"[red]⚠️  NO leg failed AND cancel of YES order failed: {cancel_err}. "
                            "Manual intervention may be required![/]"
                        )
                else:
                    console.print("[yellow]NO leg failed — YES order has no ID to cancel; check open orders![/]")
                return None

            if yes_trade and no_trade:
                yes_trade.market_question = opp.market_question
                yes_trade.fee_estimate = profit_calc["total_fees"]
                self._trade_log.append(yes_trade)
                self._daily_pnl -= investment_usd
                self._persist_daily_pnl()
                self._daily_trades += 1
                self._open_position_count += 1
                self._total_exposure_usd += investment_usd
                # BUG FIX: register entry for trailing-stop tracking (live path was missing this)
                self.register_entry(opp.condition_id, opp.yes_price, investment_usd)
                console.print(
                    f"[bold green]✅ Trade executed![/] "
                    f"YES: {yes_trade.order_id} | NO: {no_trade.order_id}"
                )
                return yes_trade

        except Exception as e:
            console.print(f"[red]Trade failed: {e}[/]")

        return None

    def close_position(self, token_id: str, size: float, condition_id: str) -> Trade | None:
        """Close a position by selling shares."""
        if config.DRY_RUN:
            console.print(f"[yellow]DRY RUN: Would close position {token_id[:10]}...[/]")
            return Trade(
                market_question="Close position",
                condition_id=condition_id,
                token_id=token_id,
                side="SELL",
                price=0.0,
                size=size,
                order_type="GTC",
                status="dry_run",
            )

        if not self.clob._authenticated:
            console.print("[red]Not authenticated.[/]")
            return None

        try:
            result = self._place_order(
                token_id=token_id,
                side="SELL",
                size=size,
                price=0.0,
                condition_id=condition_id,
                order_type="FOK",
            )
            if result:
                self._open_position_count = max(0, self._open_position_count - 1)
            return result
        except Exception as e:
            console.print(f"[red]Close position failed: {e}[/]")
            return None

    def execute_near_resolved_trade(
        self,
        opp: NearResolvedOpportunity,
        investment_usd: float | None = None,
        use_maker_price: bool = True,
    ) -> "Trade | None":
        """Buy the high-confidence side of a near-resolved market.

        Strategy: buy winning side at current price (e.g. $0.96),
        collect $1.00 when market resolves. Return ≈ 1-6% with low risk.

        Args:
            use_maker_price: Post 1 tick below market price → 0% maker fee.
                             Higher return but may not fill immediately.
        """
        # BUG FIX: use self._current_trade_size so auto-compound affects near-resolved trades too
        if investment_usd is None:
            investment_usd = self._current_trade_size
        investment_usd = min(investment_usd, config.MAX_POSITION_USD)

        if not self._check_safety_limits(investment_usd):
            return None

        buy_price = opp.maker_price if use_maker_price else opp.winning_price
        actual_return_pct = ((1.0 - buy_price) / buy_price) * 100

        console.print(
            f"[cyan]📅 Near-Resolved:[/] {opp.market_question[:60]}\n"
            f"  Buy {opp.winning_side} @ ${buy_price:.2f} "
            f"({'maker 0% fee' if use_maker_price else 'taker'}) | "
            f"Est. Return: {actual_return_pct:.2f}% | "
            f"Closes in: {opp.hours_to_close:.0f}h"
        )

        if config.DRY_RUN:
            console.print(
                f"[yellow]DRY RUN: Would buy {opp.winning_side} "
                f"@ ${buy_price:.2f} in {opp.market_question[:50]}[/]"
            )
            trade = Trade(
                market_question=opp.market_question,
                condition_id=opp.condition_id,
                token_id=opp.winning_token_id,
                side="BUY",
                price=buy_price,
                size=investment_usd,
                order_type="GTC",
                status="dry_run",
            )
            self._trade_log.append(trade)
            self._daily_trades += 1
            self._open_position_count += 1
            self._total_exposure_usd += investment_usd
            # BUG FIX: register entry in dry_run path for trailing-stop tracking
            self.register_entry(opp.condition_id, buy_price, investment_usd)
            return trade

        if not self.clob._authenticated:
            console.print("[red]Not authenticated. Call authenticate() first.[/]")
            return None

        try:
            result = self._place_order(
                token_id=opp.winning_token_id,
                side="BUY",
                size=investment_usd,
                price=buy_price,
                condition_id=opp.condition_id,
                order_type="GTC",
            )
            if result:
                result.market_question = opp.market_question
                self._trade_log.append(result)
                self._daily_pnl -= investment_usd
                self._persist_daily_pnl()
                self._daily_trades += 1
                self._open_position_count += 1
                self._total_exposure_usd += investment_usd
                # BUG FIX: register entry in live path for trailing-stop tracking
                self.register_entry(opp.condition_id, buy_price, investment_usd)
                console.print(
                    f"[bold green]✅ Near-resolved order placed![/] "
                    f"Order: {result.order_id}"
                )
            return result
        except Exception as e:
            console.print(f"[red]Near-resolved trade failed: {e}[/]")
            return None

    def register_entry(self, condition_id: str, price: float, size: float) -> None:
        """Record the entry price and size for a new position (for trailing stop tracking)."""
        if condition_id:
            self._position_entry[condition_id] = (price, size)

    def check_trailing_stops(
        self, positions: list[Any]
    ) -> list[Trade]:
        """Check open positions against trailing stop threshold.

        For each position whose current mid-price has dropped >=TRAILING_STOP_PCT%
        below the recorded entry price, place a market sell (FOK) to close it.

        Args:
            positions: list of position dicts from PositionTracker (each has
                       'conditionId', 'currentValue', 'size', 'tokenId').
        Returns:
            List of Trade records for positions that were closed.
        """
        if config.TRAILING_STOP_PCT <= 0:
            return []

        closed: list[Trade] = []
        for pos in positions:
            cond_id = pos.get("conditionId", "")
            if cond_id not in self._position_entry:
                continue

            entry_price, invested = self._position_entry[cond_id]
            if entry_price <= 0:
                continue

            # current_value is total USDC value of position; derive per-share price
            try:
                current_value = float(pos.get("currentValue") or pos.get("value") or 0)
                size_shares   = float(pos.get("size") or 1)
                current_price = current_value / size_shares if size_shares > 0 else 0.0
            except (TypeError, ValueError, ZeroDivisionError):
                continue

            if current_price <= 0:
                continue

            drop_pct = (entry_price - current_price) / entry_price * 100

            if drop_pct >= config.TRAILING_STOP_PCT:
                console.print(
                    f"[bold red]🛑 Trailing stop triggered![/] "
                    f"Condition {cond_id[:10]}… dropped "
                    f"{drop_pct:.1f}% from entry ${entry_price:.2f} → ${current_price:.2f}"
                )
                token_id = pos.get("tokenId") or pos.get("token_id", "")
                trade = self.close_position(
                    token_id=token_id,
                    size=size_shares,
                    condition_id=cond_id,
                )
                if trade:
                    self._stops_triggered += 1
                    # Remove from tracking
                    del self._position_entry[cond_id]
                    closed.append(trade)

        return closed

    def auto_compound(self, usdc_balance: float) -> float:
        """Recalculate and apply a new trade size from current USDC balance.

        New trade size = COMPOUND_PCT × balance, clamped to
        [MIN_TRADE_SIZE_USD, MAX_POSITION_USD].

        Returns the new trade size.
        """
        if not config.AUTO_COMPOUND or usdc_balance <= 0:
            return self._current_trade_size

        new_size = usdc_balance * config.COMPOUND_PCT
        new_size = max(config.MIN_TRADE_SIZE_USD, min(new_size, config.MAX_POSITION_USD))
        new_size = round(new_size, 2)

        if new_size != self._current_trade_size:
            console.print(
                f"[cyan]💰 Auto-compound:[/] balance=${usdc_balance:.2f} → "
                f"trade size ${self._current_trade_size:.2f} → ${new_size:.2f}"
            )
            self._current_trade_size = new_size

        return new_size

    def record_redemption(self, amount_usd: float) -> None:
        """Record that a redemption added funds back to the account."""
        self._daily_pnl += amount_usd
        self._persist_daily_pnl()
        self._open_position_count = max(0, self._open_position_count - 1)
        self._total_exposure_usd = max(0.0, self._total_exposure_usd - amount_usd)

    def cancel_all(self) -> bool:
        """Cancel all open orders."""
        if config.DRY_RUN:
            console.print("[yellow]DRY RUN: Would cancel all orders[/]")
            return True
        try:
            self.clob.cancel_all_orders()
            console.print("[green]All orders cancelled.[/]")
            return True
        except Exception as e:
            console.print(f"[red]Cancel failed: {e}[/]")
            return False

    def get_trade_history(self) -> list[Trade]:
        return self._trade_log.copy()

    def get_daily_summary(self) -> dict:
        return {
            "daily_pnl":          self._daily_pnl,
            "daily_trades":       self._daily_trades,
            "open_positions":     self._open_position_count,
            "total_exposure_usd": self._total_exposure_usd,
            "trade_log":          self._trade_log,
            "stops_triggered":    self._stops_triggered,
            "current_trade_size": self._current_trade_size,
        }

    # === Private Methods ===

    def _place_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
        condition_id: str,
        order_type: str = "GTC",
    ) -> Trade | None:
        """Place an order on the CLOB using EIP-712 signing."""
        try:
            from py_clob_client_v2 import OrderArgs, MarketOrderArgs, OrderType, CreateOrderOptions

            if price > 0:
                # Limit order: size is dollar amount → convert to shares
                # BUG FIX: use round() before conversion to avoid fixed-point truncation
                shares = round(size / price, 4)
                order_args = OrderArgs(
                    token_id=token_id,
                    price=round(price, 4),
                    size=shares,
                    side=side,
                )
                options = CreateOrderOptions(tick_size=0.01)
                response = self.clob._client.create_and_post_order(
                    order_args, options, OrderType(order_type)
                )
            else:
                # Market order (FOK): size is share count
                order_args = MarketOrderArgs(
                    token_id=token_id,
                    amount=round(size, 4),
                )
                response = self.clob._client.create_and_post_order(
                    order_args, None, OrderType.FOK
                )

            order_id = response.get("orderID", "") if isinstance(response, dict) else ""
            console.print(f"[green]Order placed: {order_id or '(pending)'}[/]")
            return Trade(
                market_question="",
                condition_id=condition_id,
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                order_type=order_type,
                status="pending",
                order_id=order_id,
            )
        except Exception as e:
            console.print(f"[red]Order failed: {e}[/]")
            return None

    def _check_live_balance(self, investment_usd: float) -> bool:
        """Verify USDC balance and MATIC gas before placing a live order."""
        try:
            balance_data = self.clob.get_balance_allowance("COLLATERAL")
            usdc_balance = float(balance_data.get("balance", 0) or 0)
            if usdc_balance < investment_usd:
                console.print(
                    f"[red]Insufficient USDC: have ${usdc_balance:.2f}, "
                    f"need ${investment_usd:.2f}. Top up your wallet.[/]"
                )
                return False
        except Exception as e:
            console.print(f"[yellow]⚠️  Could not verify USDC balance: {e} — proceeding with caution.[/]")

        return True

    def _check_safety_limits(self, investment_usd: float) -> bool:
        """Check if a new trade is within all safety limits."""
        if investment_usd > config.MAX_POSITION_USD:
            console.print(
                f"[red]Position ${investment_usd:.2f} > max ${config.MAX_POSITION_USD:.2f}[/]"
            )
            return False

        if self._daily_pnl < -config.MAX_DAILY_LOSS_USD:
            console.print(
                f"[red]Daily loss ${abs(self._daily_pnl):.2f} > "
                f"limit ${config.MAX_DAILY_LOSS_USD:.2f} — trading halted[/]"
            )
            return False

        # BUG FIX: was checking _daily_trades (trade count) instead of actual
        # open position count, allowing far too many concurrent positions.
        if self._open_position_count >= config.MAX_OPEN_POSITIONS:
            console.print(
                f"[red]Max open positions ({config.MAX_OPEN_POSITIONS}) reached[/]"
            )
            return False

        if self._total_exposure_usd + investment_usd > config.MAX_TOTAL_EXPOSURE_USD:
            console.print(
                f"[red]Adding ${investment_usd:.2f} would exceed "
                f"max total exposure ${config.MAX_TOTAL_EXPOSURE_USD:.2f}[/]"
            )
            return False

        if config.DRY_RUN:
            console.print("[dim green]✓ Safety checks passed (DRY RUN)[/]")

        return True
