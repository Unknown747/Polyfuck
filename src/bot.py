"""Main bot orchestration — scanner → trader → redeemer loop."""

import sys
import time
import signal
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
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
from src.redemption.redemption import AutoRedeemer

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
    """Main bot controller — orchestrates scanning, trading, and redemption."""

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
        self.redeemer: AutoRedeemer | None = None

        # Stats
        self._scan_count = 0
        self._opportunities_found = 0
        self._trades_executed = 0
        self._total_redeemed_usd = 0.0

    def start(self) -> None:
        """Start the bot."""
        console.print(Panel.fit(
            "[bold cyan]🔫 ClawBots Polymarket Bot[/]\n"
            f"Mode: {'🔍 DRY RUN (no real trades)' if self.dry_run else '⚡ LIVE TRADING'}\n"
            f"Capital Config: ${config.DEFAULT_TRADE_SIZE_USD:.0f} per trade "
            f"/ ${config.MAX_POSITION_USD:.0f} max position\n"
            f"Daily Loss Limit: ${config.MAX_DAILY_LOSS_USD:.0f} "
            f"/ Max Exposure: ${config.MAX_TOTAL_EXPOSURE_USD:.0f}\n"
            f"Min Edge: {config.MIN_EDGE_PCT:.1f}% | "
            f"Max Positions: {config.MAX_OPEN_POSITIONS}\n"
            f"Auto-Redeem: {'✅ ON' if config.AUTO_REDEEM else '❌ OFF'}\n"
            f"Categories: {', '.join(config.SCAN_CATEGORIES)}",
            title="Starting Bot",
        ))

        errors = Config.validate()
        if errors and not self.dry_run:
            for err in errors:
                console.print(f"[red]Config error: {err}[/]")
            console.print("[yellow]Run with --dry-run to test without a wallet.[/]")
            sys.exit(1)

        # Authenticate if not in dry-run mode
        if not self.dry_run and config.PRIVATE_KEY:
            self._authenticate()

        # Set up position tracker and redeemer
        if config.PRIVATE_KEY:
            from src.wallet.wallet import get_address_from_key
            address = get_address_from_key(config.PRIVATE_KEY)
            self.positions = PositionTracker(address, self.data)
            self.redeemer = AutoRedeemer(
                address=address,
                private_key=config.PRIVATE_KEY,
                tracker=self.positions,
            )

        # Register signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        self.running = True
        logger.info(
            "Bot started in %s mode | trade_size=$%.2f max_pos=$%.2f",
            "DRY RUN" if self.dry_run else "LIVE",
            config.DEFAULT_TRADE_SIZE_USD,
            config.MAX_POSITION_USD,
        )

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
        return self.scanner.scan_all(
            min_edge_pct=edge,
            min_volume=config.MIN_MARKET_VOLUME,
            categories=config.SCAN_CATEGORIES,
        )

    def _authenticate(self) -> None:
        """Authenticate with Polymarket CLOB API."""
        try:
            console.print("[cyan]Authenticating with Polymarket...[/]")
            self.clob.authenticate(config.PRIVATE_KEY)
            short = self.clob.address
            console.print(f"[green]✅ Authenticated as {short[:10]}...{short[-6:]}[/]")
        except Exception as e:
            console.print(f"[red]Authentication failed: {e}[/]")
            sys.exit(1)

    def _run_loop(self) -> None:
        """Main bot loop: scan → trade → redeem → repeat."""
        while self.running:
            self._scan_count += 1
            console.print(f"\n[bold cyan]── Scan #{self._scan_count} ──[/]")

            try:
                # 1. Scan for mispricing opportunities
                opportunities = self.scan_once()

                if opportunities:
                    self._opportunities_found += len(opportunities)
                    display_opportunities(opportunities)

                    best = opportunities[0]
                    profit_calc = self.trader.calculate_profit_after_fees(
                        best, config.DEFAULT_TRADE_SIZE_USD
                    )
                    console.print(
                        f"\n[bold]Best opportunity:[/] {best.market_question}\n"
                        f"  Edge: {best.edge_pct:.2f}% | "
                        f"Est. profit: ${profit_calc['net_profit']:.4f} "
                        f"({profit_calc['roi_pct']:.1f}% ROI)"
                    )

                    # Execute trade (dry-run or live, both go through the same path)
                    if profit_calc["profitable"]:
                        trade = self.trader.execute_mispricing_trade(best)
                        if trade:
                            self._trades_executed += 1
                    else:
                        console.print(
                            f"[yellow]Skipping — edge {best.edge_pct:.2f}% not "
                            f"profitable after fees.[/]"
                        )

                    # Every 5th scan: also check cross-market correlations
                    if self._scan_count % 5 == 0:
                        corrs = self.scanner.scan_correlated()
                        for corr in corrs[:3]:
                            console.print(
                                f"  [magenta]Correlation:[/] {corr.description} "
                                f"({corr.edge_pct:.1f}% edge)"
                            )
                else:
                    console.print("[dim]No opportunities found. Waiting...[/]")

                # 2. Auto-redeem resolved positions every N scans
                if (
                    config.AUTO_REDEEM
                    and self.redeemer
                    and self._scan_count % config.REDEEM_CHECK_INTERVAL == 0
                ):
                    self._run_redemption()

                # 3. Show open positions every 3 scans
                if self.positions and self._scan_count % 3 == 0:
                    current = self.positions.refresh_positions()
                    if current:
                        self.positions.display_positions(current)

                # 4. Show status summary
                self._show_status()

            except Exception as e:
                logger.error("Scan error: %s", e, exc_info=True)
                console.print(f"[red]Error in scan loop: {e}[/]")

            console.print(f"[dim]Next scan in {config.SCAN_INTERVAL_SEC}s...[/]")
            time.sleep(config.SCAN_INTERVAL_SEC)

    def _run_redemption(self) -> None:
        """Check for and process redeemable positions."""
        try:
            results = self.redeemer.run()
            for r in results:
                if r.succeeded and r.estimated_usdc > 0:
                    self._total_redeemed_usd += r.estimated_usdc
                    # Notify trader so it updates exposure tracking
                    self.trader.record_redemption(r.estimated_usdc)
        except Exception as e:
            logger.error("Redemption check failed: %s", e, exc_info=True)
            console.print(f"[red]Auto-redemption error: {e}[/]")

    def _show_status(self) -> None:
        """Show bot status summary and update shared dashboard stats."""
        daily = self.trader.get_daily_summary()
        console.print(
            f"\n[bold]── Status ──[/]\n"
            f"  Scans: {self._scan_count} | "
            f"Opportunities: {self._opportunities_found} | "
            f"Trades: {self._trades_executed}\n"
            f"  Open positions: {daily['open_positions']} / {config.MAX_OPEN_POSITIONS} | "
            f"Exposure: ${daily['total_exposure_usd']:.2f} / "
            f"${config.MAX_TOTAL_EXPOSURE_USD:.2f}\n"
            f"  Daily P&L: ${daily['daily_pnl']:+.2f} | "
            f"Redeemed: ${self._total_redeemed_usd:.2f} | "
            f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}"
        )
        # Push live stats to HTML dashboard
        _stats.update({
            "scans": self._scan_count,
            "opportunities": self._opportunities_found,
            "trades": self._trades_executed,
            "open_positions": daily["open_positions"],
            "max_positions": config.MAX_OPEN_POSITIONS,
            "exposure": daily["total_exposure_usd"],
            "max_exposure": config.MAX_TOTAL_EXPOSURE_USD,
            "daily_pnl": daily["daily_pnl"],
            "redeemed": self._total_redeemed_usd,
            "mode": "DRY RUN" if self.dry_run else "LIVE",
            "last_scan": time.strftime("%H:%M:%S UTC", time.gmtime()),
        })

    def _signal_handler(self, signum, frame) -> None:
        console.print(f"\n[yellow]Signal {signum} received, shutting down...[/]")
        self.running = False

    def _shutdown(self) -> None:
        """Clean shutdown — cancel orders, save snapshot."""
        if not self.dry_run and self.clob._authenticated:
            try:
                self.trader.cancel_all()
            except Exception:
                pass

        # Run a final redemption pass on shutdown
        if config.AUTO_REDEEM and self.redeemer:
            try:
                self._run_redemption()
            except Exception:
                pass

        if self.positions:
            self.positions.save_snapshot()

        daily = self.trader.get_daily_summary()
        console.print("[bold green]Bot shut down cleanly.[/]")
        logger.info(
            "Shutdown. Scans: %d | Opportunities: %d | Trades: %d | "
            "Daily P&L: $%.2f | Redeemed: $%.2f",
            self._scan_count,
            self._opportunities_found,
            self._trades_executed,
            daily["daily_pnl"],
            self._total_redeemed_usd,
        )


# Shared stats dict — updated by bot, read by HTTP handler
_stats: dict = {
    "scans": 0,
    "opportunities": 0,
    "trades": 0,
    "open_positions": 0,
    "max_positions": 4,
    "exposure": 0.0,
    "max_exposure": 8.0,
    "daily_pnl": 0.0,
    "redeemed": 0.0,
    "mode": "DRY RUN",
    "last_scan": "–",
    "started": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
}


def _render_html() -> str:
    s = _stats
    pnl_color = "#2ecc71" if s["daily_pnl"] >= 0 else "#e74c3c"
    mode_color = "#e74c3c" if s["mode"] == "LIVE" else "#f39c12"
    exposure_pct = int((s["exposure"] / s["max_exposure"]) * 100) if s["max_exposure"] else 0
    pos_pct = int((s["open_positions"] / s["max_positions"]) * 100) if s["max_positions"] else 0
    return f"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>ClawBots – Polymarket Bot</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;padding:24px}}
  h1{{font-size:1.4rem;margin-bottom:4px;color:#58a6ff}}
  .sub{{color:#8b949e;font-size:.85rem;margin-bottom:24px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px;margin-bottom:24px}}
  .card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:18px}}
  .card .label{{color:#8b949e;font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px}}
  .card .value{{font-size:1.6rem;font-weight:700}}
  .bar-wrap{{background:#21262d;border-radius:4px;height:8px;overflow:hidden;margin-top:8px}}
  .bar{{height:100%;border-radius:4px;background:#58a6ff;transition:width .4s}}
  .badge{{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.78rem;font-weight:600}}
  .footer{{color:#8b949e;font-size:.78rem;text-align:center;margin-top:8px}}
</style>
</head>
<body>
<h1>🔫 ClawBots – Polymarket Bot</h1>
<p class="sub">Mulai: {s['started']} &nbsp;|&nbsp; Scan terakhir: {s['last_scan']} &nbsp;|&nbsp;
  <span class="badge" style="background:{mode_color}22;color:{mode_color}">{s['mode']}</span>
</p>
<div class="grid">
  <div class="card">
    <div class="label">Total Scan</div>
    <div class="value">{s['scans']}</div>
  </div>
  <div class="card">
    <div class="label">Peluang</div>
    <div class="value">{s['opportunities']}</div>
  </div>
  <div class="card">
    <div class="label">Trade Dieksekusi</div>
    <div class="value">{s['trades']}</div>
  </div>
  <div class="card">
    <div class="label">Daily P&L</div>
    <div class="value" style="color:{pnl_color}">${s['daily_pnl']:+.2f}</div>
  </div>
  <div class="card">
    <div class="label">Diredeeem</div>
    <div class="value">${s['redeemed']:.2f}</div>
  </div>
  <div class="card">
    <div class="label">Posisi Terbuka</div>
    <div class="value">{s['open_positions']} / {s['max_positions']}</div>
    <div class="bar-wrap"><div class="bar" style="width:{pos_pct}%"></div></div>
  </div>
  <div class="card">
    <div class="label">Eksposur</div>
    <div class="value">${s['exposure']:.2f}</div>
    <div class="bar-wrap"><div class="bar" style="width:{exposure_pct}%"></div></div>
  </div>
</div>
<p class="footer">Auto-refresh setiap 30 detik &nbsp;·&nbsp; polyfuck--ren00991122.replit.app</p>
</body>
</html>"""


def _start_health_server() -> None:
    """Start HTML dashboard + health-check server in a background thread."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = _render_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()


def main():
    """CLI entry point."""
    import argparse

    _start_health_server()

    parser = argparse.ArgumentParser(description="ClawBots Polymarket Bot 🔫🧬")
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Simulate trades without executing (default: True)"
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Enable live trading with real money"
    )
    parser.add_argument(
        "--scan", action="store_true",
        help="Run a single scan and exit"
    )
    parser.add_argument(
        "--positions", action="store_true",
        help="Show current positions and exit"
    )
    parser.add_argument(
        "--redeem", action="store_true",
        help="Check and redeem all resolvable positions, then exit"
    )
    parser.add_argument(
        "--min-edge", type=float, default=None,
        help="Override minimum edge %% for scanning"
    )

    args = parser.parse_args()
    dry_run = not args.live

    if args.live:
        console.print("[bold red]⚠️  LIVE TRADING MODE — REAL MONEY AT RISK ⚠️[/]")
        if not config.is_configured():
            for err in Config.validate():
                console.print(f"[red]{err}[/]")
            sys.exit(1)

    bot = PolymarketBot(dry_run=dry_run)

    if args.scan:
        opportunities = bot.scan_once(min_edge=args.min_edge)
        display_opportunities(opportunities)
        return

    if args.positions:
        if not config.PRIVATE_KEY:
            console.print("[red]Set POLY_PRIVATE_KEY in Replit Secrets to view positions.[/]")
            sys.exit(1)
        from src.wallet.wallet import get_address_from_key
        address = get_address_from_key(config.PRIVATE_KEY)
        tracker = PositionTracker(address, DataClient())
        tracker.display_positions()
        return

    if args.redeem:
        if not config.PRIVATE_KEY:
            console.print("[red]Set POLY_PRIVATE_KEY in Replit Secrets to redeem.[/]")
            sys.exit(1)
        from src.wallet.wallet import get_address_from_key
        address = get_address_from_key(config.PRIVATE_KEY)
        tracker = PositionTracker(address, DataClient())
        redeemer = AutoRedeemer(address=address, private_key=config.PRIVATE_KEY, tracker=tracker)
        results = redeemer.run()
        total = sum(r.estimated_usdc for r in results if r.succeeded)
        console.print(f"\n[bold green]Total redeemable: ${total:.2f} USDC[/]")
        return

    bot.start()


if __name__ == "__main__":
    main()
