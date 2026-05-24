"""Trading execution module - places and manages orders via Polymarket CLOB API."""

import time
import json
from dataclasses import dataclass
from typing import Any
from rich.console import Console

from src.config import config
from src.utils.api import ClobClient
from src.scanner.scanner import Mispricing

console = Console()


@dataclass
class Trade:
    """Record of a trade execution."""
    market_question: str
    condition_id: str
    token_id: str
    side: str  # "BUY" or "SELL"
    price: float
    size: float  # Number of shares for SELL, dollar amount for BUY
    order_type: str  # "GTC", "FOK", "GTD"
    status: str  # "pending", "filled", "partial", "cancelled", "dry_run"
    order_id: str = ""
    timestamp: float = time.time()
    filled_price: float = 0.0
    filled_size: float = 0.0
    fee_estimate: float = 0.0


class Trader:
    """Executes trades on Polymarket CLOB."""

    # Category fee rates (maker = 0, taker only)
    TAKER_FEE_RATES = {
        "crypto": 0.07,
        "sports": 0.03,
        "finance": 0.04,
        "politics": 0.04,
        "economics": 0.05,
        "culture": 0.05,
        "geopolitics": 0.0,
    }

    def __init__(self, clob: ClobClient | None = None):
        self.clob = clob or ClobClient()
        self._trade_log: list[Trade] = []
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0

    def estimate_fee(self, price: float, size: float, category: str = "crypto") -> float:
        """Estimate taker fee for a trade.

        Formula: fee = C × feeRate × p × (1 - p)
        where C = shares traded, p = price
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
        Number of pairs = investment / cost_per_pair.
        """
        cost_per_pair = opp.price_sum  # Total cost to buy 1 YES share + 1 NO share
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
        guaranteed_return = num_pairs * 1.00  # Each pair resolves to $1.00

        # Estimate fees for each side
        yes_cost = num_pairs * opp.yes_price
        no_cost = num_pairs * opp.no_price
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
        self, opp: Mispricing, investment_usd: float, category: str = "crypto"
    ) -> Trade | None:
        """Execute an arbitrage trade on a mispriced market.

        If YES + NO < $1.00: Buy both sides for guaranteed profit.
        If YES + NO > $1.00: Sell both sides (if we hold positions).

        Args:
            opp: The mispricing opportunity to trade
            investment_usd: Dollar amount to invest
            category: Market category for fee estimation
        """
        # Safety checks
        if not self._check_safety_limits(investment_usd):
            return None

        # Calculate expected profit
        profit_calc = self.calculate_profit_after_fees(opp, investment_usd, category)

        if not profit_calc["profitable"]:
            console.print(f"[red]Trade not profitable after fees: ${profit_calc['net_profit']:.4f}[/]")
            return None

        console.print(
            f"[green]Opportunity found:[/] {opp.market_question}\n"
            f"  Investment: ${investment_usd:.2f} | "
            f"Est. Profit: ${profit_calc['net_profit']:.4f} ({profit_calc['roi_pct']:.1f}% ROI)\n"
            f"  Fees: ${profit_calc['total_fees']:.4f}"
        )

        # Dry run mode
        if config.DRY_RUN:
            console.print(f"[yellow]DRY RUN: Would place trade on {opp.market_question}[/]")
            return Trade(
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

        # Live trading
        if not self.clob._authenticated:
            console.print("[red]Not authenticated. Call authenticate() first.[/]")
            return None

        try:
            # Place YES side order
            yes_trade = self._place_order(
                token_id=opp.yes_token_id,
                side="BUY",
                size=investment_usd * 0.5,  # Split investment
                price=opp.yes_price,
                condition_id=opp.condition_id,
            )

            # Place NO side order
            no_trade = self._place_order(
                token_id=opp.no_token_id,
                side="BUY",
                size=investment_usd * 0.5,
                price=opp.no_price,
                condition_id=opp.condition_id,
            )

            if yes_trade and no_trade:
                self._daily_pnl -= investment_usd
                self._daily_trades += 1
                console.print(f"[bold green]✅ Trade executed![/] YES order: {yes_trade.order_id}, NO order: {no_trade.order_id}")
                return yes_trade

        except Exception as e:
            console.print(f"[red]Trade failed: {e}[/]")
            return None

        return None

    def close_position(self, token_id: str, size: float, condition_id: str) -> Trade | None:
        """Close a position by selling shares."""
        if config.DRY_RUN:
            console.print(f"[yellow]DRY RUN: Would close position on {token_id[:10]}...[/]")
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
            return self._place_order(
                token_id=token_id,
                side="SELL",
                size=size,
                price=0.0,  # Market order
                condition_id=condition_id,
                order_type="FOK",
            )
        except Exception as e:
            console.print(f"[red]Close position failed: {e}[/]")
            return None

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
        """Return logged trade history."""
        return self._trade_log.copy()

    def get_daily_summary(self) -> dict:
        """Return daily trading summary."""
        return {
            "daily_pnl": self._daily_pnl,
            "daily_trades": self._daily_trades,
            "trade_log": self._trade_log,
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
        """Place an order on the CLOB."""
        # Determine size field: BUY = dollar amount, SELL = share count
        # Convert to the fixed-math format (6 decimal places)
        maker_amount = str(int(size * 1_000_000))
        taker_amount = str(int(size * price * 1_000_000)) if side == "BUY" else str(int(size * 1_000_000))

        # Construct the order payload for the CLOB API
        order_data = {
            "order": {
                "maker": self.clob.address,
                "signer": self.clob.address,
                "tokenId": token_id,
                "makerAmount": maker_amount if side == "BUY" else taker_amount,
                "takerAmount": taker_amount if side == "BUY" else maker_amount,
                "side": side,
                "expiration": str(int(time.time()) + 300 + 60),  # 5 min + 60s security threshold
                "timestamp": str(int(time.time() * 1000)),
                "metadata": "",
                "builder": "0x0000000000000000000000000000000000000000",
                "salt": str(int(time.time() * 1000)),
            },
            "orderType": order_type,
        }

        # Note: In production, we need to sign the order with EIP-712
        # For now, use py-clob-client-v2 if available, otherwise raw API
        try:
            response = self.clob.post_order(order_data)
            console.print(f"[green]Order placed: {response}[/]")
            return Trade(
                market_question="",
                condition_id=condition_id,
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                order_type=order_type,
                status="pending",
                order_id=response.get("orderID", ""),
            )
        except Exception as e:
            console.print(f"[red]Order failed: {e}[/]")
            return None

    def _check_safety_limits(self, investment_usd: float) -> bool:
        """Check if trade is within safety limits."""
        # Check max position size
        if investment_usd > config.MAX_POSITION_USD:
            console.print(
                f"[red]Position size ${investment_usd:.2f} exceeds max ${config.MAX_POSITION_USD:.2f}[/]"
            )
            return False

        # Check daily loss limit
        if abs(self._daily_pnl) > config.MAX_DAILY_LOSS_USD:
            console.print(
                f"[red]Daily loss ${abs(self._daily_pnl):.2f} exceeds limit ${config.MAX_DAILY_LOSS_USD:.2f}[/]"
            )
            return False

        # Check max open positions
        if self._daily_trades >= config.MAX_OPEN_POSITIONS:
            console.print(
                f"[red]Max open positions ({config.MAX_OPEN_POSITIONS}) reached[/]"
            )
            return False

        # Check minimum edge
        if config.DRY_RUN:
            console.print(f"[yellow]✓ Safety checks passed (DRY RUN mode)[/]")

        return True