"""Main bot orchestration - scanner → analyzer → executor loop."""

import sys
import time
import signal
import logging
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.logging import RichHandler

from src.config import config, Config
from src.utils.api import GammaClient, ClobClient, DataClient
from src.wallet.wallet import load_wallet, validate_private_key
from src.scanner.scanner import MarketScanner, Mispricing, display_opportunities
from src.trader.trader import Trader
from src.positions.positions import PositionTracker

console = Console()

# Set up logging
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RichHandler(console=console, rich_tracebacks=True),
        logging.FileHandler(config.LOG_FILE),
    ],
)
logger = logging.getLogger("polymarket-bot")


class PolymarketBot:
    """Main bot controller - orchestrates scanning, analysis, and trading."""

    def __init__(self, dry_run: bool | None = None):
        self.dry_run = dry_run if dry_run is not None else config.DRY_RUN
        self.running = False

        # API clients
        self.gamma = GammaClient()
        self.clob = ClobClient()
        self.data = DataClient()

        # Core modules
        self.scanner = MarketScanner(self.gamma, self.clob)
        self.trader = Trader(self.clob)
        self.positions: PositionTracker | None = None

        # Stats
        self._scan_count = 0
        self._opportunities_found = 0
        self._trades_executed = 0

    def start(self) -> None:
        """Start the bot."""
        console.print(Panel.fit(
            "[bold cyan]🔫 ClawBots Polymarket Bot[/]\n"
            f"Mode: {'🔍 DRY RUN (no real trades)' if self.dry_run else '⚡ LIVE TRADING'}\n"
            f"Max Position: ${config.MAX_POSITION_USD:.0f}\n"
            f"Daily Loss Limit: ${config.MAX_DAILY_LOSS_USD:.0f}\n"
            f"Min Edge: {config.MIN_EDGE_PCT:.1f}%\n"
            f"Categories: {', '.join(config.SCAN_CATEGORIES)}",
            title="Starting Bot",
        ))

        # Validate config
        errors = Config.validate()
        if errors and not self.dry_run:
            for err in errors:
                console.print(f"[red]Config error: {err}[/]")
            console.print("[yellow]Run with --dry-run to test without a wallet.[/]")
            sys.exit(1)

        # Authenticate if not in dry-run mode
        if not self.dry_run and config.PRIVATE_KEY:
            self._authenticate()

        # Set up position tracker
        if config.PRIVATE_KEY:
            from src.wallet.wallet import get_address_from_key
            address = get_address_from_key(config.PRIVATE_KEY)
            self.positions = PositionTracker(address, self.data)

        # Register signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        self.running = True
        logger.info("Bot started in %s mode", "DRY RUN" if self.dry_run else "LIVE")

        try:
            self._run_loop()
        except KeyboardInterrupt:
            self._shutdown()

    def stop(self) -> None:
        """Stop the bot gracefully."""
        console.print("\n[yellow]Stopping bot...[/]")
        self.running = False

    def scan_once(self, min_edge: float | None = None) -> list[Mispricing]:
        """Run a single scan and return opportunities."""
        edge = min_edge or config.MIN_EDGE_PCT
        opportunities = self.scanner.scan_all(
            min_edge_pct=edge,
            min_volume=1000.0,
            categories=config.SCAN_CATEGORIES,
        )
        return opportunities

    def _authenticate(self) -> None:
        """Authenticate with Polymarket CLOB API."""
        try:
            console.print("[cyan]Authenticating with Polymarket...[/]")
            creds = self.clob.authenticate(config.PRIVATE_KEY)
            console.print(f"[green]✅ Authenticated as {self.clob.address[:10]}...{self.clob.address[-6:]}[/]")
        except Exception as e:
            console.print(f"[red]Authentication failed: {e}[/]")
            sys.exit(1)

    def _run_loop(self) -> None:
        """Main bot loop."""
        while self.running:
            self._scan_count += 1
            console.print(f"\n[bold cyan]── Scan #{self._scan_count} ──[/]")

            try:
                # 1. Scan for opportunities
                opportunities = self.scan_once()

                if opportunities:
                    self._opportunities_found += len(opportunities)
                    display_opportunities(opportunities)

                    # 2. Try to trade the best opportunity
                    best = opportunities[0]
                    profit_calc = self.trader.calculate_profit_after_fees(best, 10.0)  # $10 default
                    console.print(
                        f"\n[bold]Best opportunity:[/] {best.market_question}\n"
                        f"  Edge: {best.edge_pct:.2f}% | Est. profit: ${profit_calc['net_profit']:.4f}"
                    )

                    if self.dry_run:
                        console.print("[yellow]DRY RUN: Would trade this opportunity.[/]")
                        self.trader.execute_mispricing_trade(best, 10.0)
                    elif profit_calc["profitable"]:
                        trade = self.trader.execute_mispricing_trade(
                            best, min(config.MAX_POSITION_USD, 10.0)
                        )
                        if trade:
                            self._trades_executed += 1

                    # 3. Also check for correlated arb
                    if self._scan_count % 5 == 0:  # Every 5th scan
                        corrs = self.scanner.scan_correlated()
                        for corr in corrs[:3]:  # Top 3
                            console.print(
                                f"  [magenta]Correlation:[/] {corr.description} "
                                f"({corr.edge_pct:.1f}% edge)"
                            )
                else:
                    console.print("[dim]No opportunities found. Waiting...[/]")

                # 4. Check positions
                if self.positions and self._scan_count % 3 == 0:
                    positions = self.positions.refresh_positions()
                    if positions:
                        self.positions.display_positions(positions)

                # 5. Show status
                self._show_status()

            except Exception as e:
                logger.error(f"Scan error: {e}", exc_info=True)
                console.print(f"[red]Error: {e}[/]")

            # Wait before next scan
            console.print(f"[dim]Next scan in {config.SCAN_INTERVAL_SEC}s...[/]")
            time.sleep(config.SCAN_INTERVAL_SEC)

    def _show_status(self) -> None:
        """Show bot status summary."""
        console.print(
            f"\n[bold]── Status ──[/]\n"
            f"  Scans: {self._scan_count}\n"
            f"  Opportunities: {self._opportunities_found}\n"
            f"  Trades: {self._trades_executed}\n"
            f"  Mode: {'DRY RUN' if self.dry_run else 'LIVE'}"
        )

    def _signal_handler(self, signum, frame) -> None:
        """Handle shutdown signals."""
        console.print(f"\n[yellow]Received signal {signum}, shutting down...[/]")
        self.running = False

    def _shutdown(self) -> None:
        """Clean shutdown."""
        # Cancel all open orders if live trading
        if not self.dry_run and self.clob._authenticated:
            try:
                self.trader.cancel_all()
            except Exception:
                pass

        # Save position snapshot
        if self.positions:
            self.positions.save_snapshot()

        console.print("[bold green]Bot shut down cleanly.[/]")
        logger.info("Bot shut down. Scans: %d, Opportunities: %d, Trades: %d",
                    self._scan_count, self._opportunities_found, self._trades_executed)


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="ClawBots Polymarket Bot 🔫🧬")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Simulate trades without executing (default: True)")
    parser.add_argument("--live", action="store_true",
                        help="Enable live trading with real money")
    parser.add_argument("--scan", action="store_true",
                        help="Run a single scan and exit")
    parser.add_argument("--positions", action="store_true",
                        help="Show current positions and exit")
    parser.add_argument("--min-edge", type=float, default=None,
                        help="Override minimum edge %% for scanning")

    args = parser.parse_args()

    # Safety: default to dry-run, require explicit --live for real trading
    dry_run = not args.live

    if args.live:
        console.print("[bold red]⚠️  LIVE TRADING MODE - REAL MONEY AT RISK ⚠️[/]")
        if not config.is_configured():
            console.print("[red]Wallet not configured. Set POLY_PRIVATE_KEY in config/.env[/]")
            sys.exit(1)

    bot = PolymarketBot(dry_run=dry_run)

    if args.scan:
        opportunities = bot.scan_once(min_edge=args.min_edge)
        display_opportunities(opportunities)
        return

    if args.positions:
        if not config.PRIVATE_KEY:
            console.print("[red]Set POLY_PRIVATE_KEY in config/.env to view positions[/]")
            sys.exit(1)
        from src.wallet.wallet import get_address_from_key
        address = get_address_from_key(config.PRIVATE_KEY)
        tracker = PositionTracker(address)
        tracker.display_positions()
        return

    bot.start()


if __name__ == "__main__":
    main()