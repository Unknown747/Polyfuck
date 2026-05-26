"""Main bot orchestration — scanner → trader → redeemer loop."""

import sys
import json
import time
import signal
import logging
import os
import threading
import collections
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import src.utils.db as trade_db

from rich.console import Console
from rich.panel import Panel
from rich.logging import RichHandler

from src.config import config, Config
from src.utils.api import GammaClient, ClobClient, DataClient
from src.wallet.wallet import load_wallet, validate_private_key
from src.scanner.scanner import (
    MarketScanner, Mispricing, NearResolvedOpportunity,
    display_opportunities, display_near_resolved,
)
from src.trader.trader import Trader
from src.positions.positions import PositionTracker
from src.redemption.redemption import AutoRedeemer
from src.strategies.orchestrator import Orchestrator
import src.utils.opportunity_logger as opp_logger

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

# Dedicated error log file + in-memory ring buffer exposed via /api/errors
_error_log: collections.deque = collections.deque(maxlen=20)

_err_file_handler = logging.FileHandler("logs/errors.log")
_err_file_handler.setLevel(logging.ERROR)
_err_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_err_file_handler)


class _ErrorCapture(logging.Handler):
    """Capture ERROR+ log records into the in-memory ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        _error_log.append({
            "time":  logging.Formatter().formatTime(record, "%H:%M:%S"),
            "level": record.levelname,
            "msg":   record.getMessage()[:200],
        })


_err_capture = _ErrorCapture(level=logging.ERROR)
logger.addHandler(_err_capture)


class PolymarketBot:
    """Main bot controller — orchestrates scanning, trading, and redemption."""

    def __init__(self, dry_run: bool | None = None):
        self.dry_run = dry_run if dry_run is not None else config.DRY_RUN
        self.running = False

        # API clients
        self.gamma = GammaClient()
        self.clob  = ClobClient()
        self.data  = DataClient()

        # Core modules
        self.scanner  = MarketScanner(self.gamma, self.clob)
        self.trader   = Trader(self.clob)
        self.positions: PositionTracker | None = None
        self.redeemer:  AutoRedeemer   | None = None

        # Stats
        self._scan_count          = 0
        self._opportunities_found = 0
        self._trades_executed     = 0
        self._total_redeemed_usd  = 0.0
        self._near_resolved_found = 0

        # 'paper' or 'live' — used to filter DB reads so modes never mix
        self._db_mode: str = "paper" if self.dry_run else "live"

        # Initialise SQLite trade history and wallet balance cache
        trade_db.init_db()
        self._wallet_balance: float = 0.0

        # Strategy orchestrator (handles all 4 strategies)
        self.orchestrator = Orchestrator(self.trader, self.gamma, self.clob, dry_run=self.dry_run)

        # Per-strategy cumulative counters (session-level)
        self._strategy_stats: dict = {
            "mispricing":    {"opps": 0, "trades": 0, "pnl": 0.0},
            "near_resolved": {"opps": 0, "trades": 0, "pnl": 0.0},
            "correlated":    {"opps": 0, "trades": 0, "pnl": 0.0},
            "sniper":        {"opps": 0, "trades": 0, "pnl": 0.0},
        }

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
            f"Auto-Redeem: {'✅ ON' if config.AUTO_REDEEM else '❌ OFF'} | "
            f"Trailing Stop: {config.TRAILING_STOP_PCT:.0f}% | "
            f"Auto-Compound: {'✅ ON' if config.AUTO_COMPOUND else '❌ OFF'}\n"
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
            self.redeemer  = AutoRedeemer(
                address=address,
                private_key=config.PRIVATE_KEY,
                tracker=self.positions,
            )

        # Register signal handlers
        signal.signal(signal.SIGINT,  self._signal_handler)
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

    # ── Main Loop ──────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """Main bot loop: scan → log → trade → trailing-stop → redeem → repeat."""
        while self.running:
            self._scan_count += 1
            console.print(f"\n[bold cyan]── Scan #{self._scan_count} ──[/]")

            try:
                # ── Run all 4 strategies via orchestrator ──────────────────
                orch_result = self.orchestrator.run(categories=config.SCAN_CATEGORIES)

                # Accumulate strategy counters
                self._opportunities_found += orch_result.mispricing_opps + orch_result.nr_opps
                self._near_resolved_found += orch_result.nr_opps
                self._trades_executed     += orch_result.total_trades

                ss = self._strategy_stats
                ss["mispricing"]["opps"]   += orch_result.mispricing_opps
                ss["mispricing"]["trades"] += orch_result.mispricing_trades
                ss["mispricing"]["pnl"]    += orch_result.mispricing_pnl
                ss["near_resolved"]["opps"]   += orch_result.nr_opps
                ss["near_resolved"]["trades"] += orch_result.nr_trades
                ss["near_resolved"]["pnl"]    += orch_result.nr_pnl
                ss["correlated"]["opps"]   += orch_result.corr_opps
                ss["correlated"]["trades"] += orch_result.corr_trades
                ss["correlated"]["pnl"]    += orch_result.corr_pnl
                ss["sniper"]["opps"]   += orch_result.sniper_signals
                ss["sniper"]["trades"] += orch_result.sniper_orders
                ss["sniper"]["pnl"]    += orch_result.sniper_pnl

                # Console summary
                console.print(
                    f"[bold]Orchestrator results:[/] "
                    f"Mispricing={orch_result.mispricing_opps}opps/{orch_result.mispricing_trades}trades "
                    f"| NearResolved={orch_result.nr_opps}/{orch_result.nr_trades} "
                    f"| Correlated={orch_result.corr_opps}/{orch_result.corr_trades} "
                    f"| Sniper={orch_result.sniper_signals}sig/{orch_result.sniper_orders}ord"
                )
                if orch_result.errors:
                    for err in orch_result.errors:
                        console.print(f"[red]Strategy error: {err}[/]")

                # Update dashboard active_pairs + sniper_orders
                _stats["active_pairs"]       = [
                    {"type": p.pair_type, "a": p.market_a_question[:40],
                     "b": p.market_b_question[:40], "div": round(p.divergence_pct, 1)}
                    for p in orch_result.active_pairs[:5]
                ]
                _stats["active_sniper_orders"] = len(orch_result.active_sniper_orders)

                # 2. Check trailing stops + reconcile counters (live mode only)
                if self.positions and not self.dry_run:
                    current_positions = self.positions.refresh_positions()
                    if current_positions:
                        stopped = self.trader.check_trailing_stops(current_positions)
                        if stopped:
                            console.print(
                                f"[red]🛑 {len(stopped)} position(s) closed by trailing stop[/]"
                            )

                        # Reconcile in-memory risk counters with live API every N scans.
                        if (config.RECONCILE_INTERVAL > 0
                                and self._scan_count % config.RECONCILE_INTERVAL == 0):
                            recon = self.trader.reconcile_counters(current_positions)
                            if recon["corrections"]:
                                logger.info(
                                    "Reconciler applied corrections: %s", recon["corrections"]
                                )

                # 3. Auto-redeem resolved positions every N scans
                if (
                    config.AUTO_REDEEM
                    and self.redeemer
                    and self._scan_count % config.REDEEM_CHECK_INTERVAL == 0
                ):
                    self._run_redemption()

                # 4. Show open positions every 3 scans
                if self.positions and self._scan_count % 3 == 0:
                    current = self.positions.refresh_positions()
                    if current:
                        self.positions.display_positions(current)

                # 5. Show status summary and push to dashboard
                self._show_status()

            except Exception as e:
                logger.error("Scan error: %s", e, exc_info=True)
                console.print(f"[red]Error in scan loop: {e}[/]")

            console.print(f"[dim]Next scan in {config.SCAN_INTERVAL_SEC}s...[/]")
            time.sleep(config.SCAN_INTERVAL_SEC)

    def _run_redemption(self) -> None:
        """Check for and process redeemable positions.
        Feature 6: After a successful redemption, run auto-compound to resize trade."""
        try:
            results = self.redeemer.run()
            total_redeemed = 0.0
            for r in results:
                if r.succeeded and r.estimated_usdc > 0:
                    self._total_redeemed_usd += r.estimated_usdc
                    total_redeemed           += r.estimated_usdc
                    self.trader.record_redemption(r.estimated_usdc)

            # Feature 6: Auto-compound if we actually redeemed something
            if total_redeemed > 0 and config.AUTO_COMPOUND:
                try:
                    balance_data = self.clob.get_balance_allowance("COLLATERAL")
                    usdc_balance = float(balance_data.get("balance", 0) or 0)
                    new_size     = self.trader.auto_compound(usdc_balance)
                    _stats["trade_size_usd"] = new_size
                except Exception as e:
                    logger.warning("Auto-compound balance fetch failed: %s", e)

        except Exception as e:
            logger.error("Redemption check failed: %s", e, exc_info=True)
            console.print(f"[red]Auto-redemption error: {e}[/]")

    def _show_status(self) -> None:
        """Show bot status summary and push all live stats to the dashboard."""
        daily = self.trader.get_daily_summary()
        console.print(
            f"\n[bold]── Status ──[/]\n"
            f"  Scans: {self._scan_count} | "
            f"Opportunities: {self._opportunities_found} | "
            f"Near-Resolved: {self._near_resolved_found} | "
            f"Trades: {self._trades_executed}\n"
            f"  Open positions: {daily['open_positions']} / {config.MAX_OPEN_POSITIONS} | "
            f"Exposure: ${daily['total_exposure_usd']:.2f} / "
            f"${config.MAX_TOTAL_EXPOSURE_USD:.2f}\n"
            f"  Daily P&L: ${daily['daily_pnl']:+.2f} | "
            f"Redeemed: ${self._total_redeemed_usd:.2f} | "
            f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}"
        )

        # Serialize trade log for dashboard (safe for JSON)
        trade_log_serialized = [
            {
                "time":     time.strftime("%H:%M:%S", time.gmtime(t.timestamp)),
                "market":   t.market_question[:50],
                "side":     t.side,
                "price":    round(t.price, 4),
                "size":     round(t.size, 2),
                "status":   t.status,
                "strategy": getattr(t, "strategy", ""),
            }
            for t in daily["trade_log"][-20:]
        ]

        # Append P&L history point
        pnl_pt = {"t": time.strftime("%H:%M", time.gmtime()), "pnl": round(daily["daily_pnl"], 4)}
        hist   = _stats.get("pnl_history", [])
        hist.append(pnl_pt)
        if len(hist) > 50:
            hist = hist[-50:]

        # Refresh wallet balance every 10 scans (or at startup)
        if self._scan_count % 10 == 1:
            self._wallet_balance = self._fetch_wallet_balance()

        # Orchestrator health
        orch_health = self.orchestrator.get_health()
        orch_pnl    = self.orchestrator.get_pnl()

        _stats.update({
            "scans":           self._scan_count,
            "opportunities":   self._opportunities_found,
            "near_resolved":   self._near_resolved_found,
            "trades":          self._trades_executed,
            "open_positions":  daily["open_positions"],
            "max_positions":   config.MAX_OPEN_POSITIONS,
            "exposure":        daily["total_exposure_usd"],
            "max_exposure":    config.MAX_TOTAL_EXPOSURE_USD,
            "daily_pnl":       daily["daily_pnl"],
            "redeemed":        self._total_redeemed_usd,
            "mode":            "DRY RUN" if self.dry_run else "LIVE",
            "last_scan":       time.strftime("%H:%M:%S UTC", time.gmtime()),
            "trade_size_usd":  daily["current_trade_size"],
            "stops_triggered": daily["stops_triggered"],
            "trade_log":       trade_log_serialized,
            "pnl_history":     hist,
            "opp_stats":       opp_logger.get_stats(),
            "category_stats":  self.orchestrator.get_category_stats(),
            "wallet_balance":  self._wallet_balance,
            "db_stats":        trade_db.get_db_stats(mode=self._db_mode),
            "strategy_stats":  self._strategy_stats,
            "strategy_pnl":    orch_pnl,
            "strategy_health": orch_health,
        })

    def _fetch_wallet_balance(self) -> float:
        """Return USDC wallet balance. Returns 0.0 in dry-run or if unavailable."""
        if self.dry_run or not self.clob._authenticated:
            return 0.0
        try:
            data = self.clob.get_balance_allowance("COLLATERAL")
            return float(data.get("balance", 0) or 0)
        except Exception as e:
            logger.warning("Could not fetch wallet balance: %s", e)
            return 0.0

    def _authenticate(self) -> None:
        try:
            console.print("[cyan]Authenticating with Polymarket...[/]")
            self.clob.authenticate(config.PRIVATE_KEY)
            short = self.clob.address
            console.print(f"[green]✅ Authenticated as {short[:10]}...{short[-6:]}[/]")
        except Exception as e:
            console.print(f"[red]Authentication failed: {e}[/]")
            sys.exit(1)

    def _signal_handler(self, signum, frame) -> None:
        console.print(f"\n[yellow]Signal {signum} received, shutting down...[/]")
        self.running = False

    def _shutdown(self) -> None:
        if not self.dry_run and self.clob._authenticated:
            try:
                self.trader.cancel_all()
            except Exception:
                pass

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


# ── Shared state (bot → dashboard) ─────────────────────────────────────────

_stats: dict = {
    "scans":           0,
    "opportunities":   0,
    "near_resolved":   0,
    "trades":          0,
    "open_positions":  0,
    "max_positions":   config.MAX_OPEN_POSITIONS,
    "exposure":        0.0,
    "max_exposure":    config.MAX_TOTAL_EXPOSURE_USD,
    "daily_pnl":       0.0,
    "redeemed":        0.0,
    "mode":            "DRY RUN",
    "last_scan":       "–",
    "started":         time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
    "trade_size_usd":  config.DEFAULT_TRADE_SIZE_USD,
    "stops_triggered": 0,
    "trade_log":       [],
    "opp_log":         [],
    "pnl_history":     [],
    "opp_stats":       {"total_logged": 0, "executed": 0, "avg_edge_pct": 0.0},
    "category_stats":  {},
    "wallet_balance":  0.0,
    "db_stats":        {"total": 0, "executed": 0, "dry_run": 0, "by_strategy": {}},
    "strategy_stats":  {
        "mispricing":    {"opps": 0, "trades": 0, "pnl": 0.0},
        "near_resolved": {"opps": 0, "trades": 0, "pnl": 0.0},
        "correlated":    {"opps": 0, "trades": 0, "pnl": 0.0},
        "sniper":        {"opps": 0, "trades": 0, "pnl": 0.0},
    },
    "strategy_pnl":    {"mispricing": 0.0, "near_resolved": 0.0, "correlated": 0.0, "sniper": 0.0},
    "strategy_health": {
        "mispricing":    {"disabled": False, "failures": 0},
        "near_resolved": {"disabled": False, "failures": 0},
        "correlated":    {"disabled": False, "failures": 0},
        "sniper":        {"disabled": False, "failures": 0},
    },
    "active_pairs":          [],
    "active_sniper_orders":  0,
}


# ── Dashboard HTML ──────────────────────────────────────────────────────────

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ClawBots – 4-Strategy Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;padding:20px;max-width:1400px;margin:0 auto}
  h1{font-size:1.4rem;color:#58a6ff;margin-bottom:4px}
  .sub{color:#8b949e;font-size:.82rem;margin-bottom:18px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
  .badge{padding:2px 10px;border-radius:20px;font-size:.78rem;font-weight:600}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#2ecc71;margin-right:4px;animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:10px;margin-bottom:16px}
  .card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:13px}
  .card .label{color:#8b949e;font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
  .card .value{font-size:1.45rem;font-weight:700}
  .card .sub-val{font-size:.72rem;color:#8b949e;margin-top:2px}
  .bar-wrap{background:#21262d;border-radius:4px;height:5px;overflow:hidden;margin-top:6px}
  .bar{height:100%;border-radius:4px;background:#58a6ff;transition:width .4s}
  .section{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px;margin-bottom:14px}
  .section h3{font-size:.86rem;color:#8b949e;margin-bottom:10px;font-weight:600;display:flex;align-items:center;gap:6px}
  .health-dot{width:7px;height:7px;border-radius:50%;display:inline-block}
  .ok{background:#2ecc71}.warn{background:#f39c12}.err{background:#e74c3c}
  .row2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
  .row4{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:10px;margin-bottom:14px}
  .strat-card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:13px}
  .strat-card .strat-name{font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;font-weight:700;margin-bottom:8px}
  .strat-card .s-row{display:flex;justify-content:space-between;font-size:.78rem;margin:3px 0}
  .strat-card .s-row .sk{color:#8b949e}
  .strat-card .s-row .sv{font-weight:600}
  table{width:100%;border-collapse:collapse;font-size:.78rem}
  th{color:#8b949e;text-align:left;padding:4px 8px;font-weight:500;border-bottom:1px solid #30363d}
  td{padding:5px 8px;border-bottom:1px solid #21262d}
  tr:last-child td{border-bottom:none}
  .g{color:#2ecc71}.r{color:#e74c3c}.b{color:#58a6ff}.y{color:#f39c12}
  .footer{color:#8b949e;font-size:.74rem;text-align:center;margin-top:10px}
  canvas{max-height:200px}
  .cmp-header{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;margin-bottom:6px}
  .cmp-header span{text-align:center;font-size:.72rem;font-weight:700;letter-spacing:.06em;padding:4px 0;border-radius:6px}
  .cmp-header span:first-child{text-align:left;color:#8b949e}
  .cmp-paper-h{color:#58a6ff;background:rgba(88,166,255,.08)}
  .cmp-live-h{color:#2ecc71;background:rgba(46,204,113,.08)}
  .cmp-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;padding:6px 0;border-bottom:1px solid #21262d;align-items:center}
  .cmp-row:last-child{border-bottom:none;font-weight:700;font-size:.84rem}
  .cmp-row span{text-align:center;font-size:.80rem}
  .cmp-row span:first-child{text-align:left;color:#8b949e;font-size:.76rem}
  .cmp-paper{color:#58a6ff}
  .cmp-live{color:#2ecc71}
  .cmp-chart-wrap{margin-top:14px}
  .cmp-canvas{max-height:160px}
  .bal-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px}
  .bal-card{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:10px;text-align:center}
  .bal-card .bl{font-size:.65rem;color:#8b949e;text-transform:uppercase;letter-spacing:.06em}
  .bal-card .bv{font-size:1.1rem;font-weight:700;margin-top:2px}
  @media(max-width:900px){.row4{grid-template-columns:1fr 1fr}.row2{grid-template-columns:1fr}.bal-grid{grid-template-columns:1fr 1fr}}
  @media(max-width:540px){.row4{grid-template-columns:1fr}.bal-grid{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<h1>&#x1F52B; ClawBots &mdash; 4-Strategy Polymarket Bot</h1>
<div class="sub">
  <span>Started: <span id="started">&ndash;</span></span>
  <span>Last scan: <span id="lastScan">&ndash;</span></span>
  <span id="modeBadge" class="badge" style="background:#f39c1222;color:#f39c12">DRY RUN</span>
  <span><span class="dot"></span><span id="tradeSize">&ndash;</span>/trade</span>
</div>

<!-- Balance display -->
<div class="bal-grid">
  <div class="bal-card"><div class="bl">Wallet USDC</div><div class="bv g" id="b-wallet">N/A</div></div>
  <div class="bal-card"><div class="bl">Exposure</div><div class="bv y" id="b-exp-val">$0</div></div>
  <div class="bal-card"><div class="bl">Available</div><div class="bv" id="b-avail">–</div></div>
  <div class="bal-card"><div class="bl">Redeemed</div><div class="bv g" id="b-red">$0</div></div>
</div>

<!-- Global summary cards -->
<div class="grid">
  <div class="card"><div class="label">Scans</div><div class="value" id="v-scans">&ndash;</div></div>
  <div class="card"><div class="label">Total Opps</div><div class="value" id="v-opp">&ndash;</div></div>
  <div class="card"><div class="label">Total Trades</div><div class="value" id="v-trades">&ndash;</div></div>
  <div class="card"><div class="label">Daily P&amp;L</div><div class="value" id="v-pnl">&ndash;</div></div>
  <div class="card">
    <div class="label">Positions</div><div class="value" id="v-pos">&ndash;</div>
    <div class="bar-wrap"><div class="bar" id="b-pos" style="width:0%"></div></div>
  </div>
  <div class="card">
    <div class="label">Exposure</div><div class="value" id="v-exp">&ndash;</div>
    <div class="bar-wrap"><div class="bar" id="b-exp" style="width:0%"></div></div>
  </div>
  <div class="card"><div class="label">Stop-Loss Hit</div><div class="value r" id="v-stops">0</div></div>
  <div class="card"><div class="label">DB Trades</div><div class="value" id="v-db-total">&ndash;</div></div>
</div>

<!-- 4 Strategy panels -->
<div class="row4">
  <div class="strat-card" id="sc-mispricing">
    <div class="strat-name b">&#x26A1; Mispricing</div>
    <div class="s-row"><span class="sk">Opps</span><span class="sv" id="sm-opps">0</span></div>
    <div class="s-row"><span class="sk">Trades</span><span class="sv" id="sm-trades">0</span></div>
    <div class="s-row"><span class="sk">P&amp;L</span><span class="sv" id="sm-pnl">$0.00</span></div>
    <div class="s-row"><span class="sk">Health</span><span class="sv"><span class="health-dot ok" id="hd-m"></span></span></div>
  </div>
  <div class="strat-card" id="sc-nr">
    <div class="strat-name" style="color:#2ecc71">&#x1F4C5; Near-Resolved</div>
    <div class="s-row"><span class="sk">Opps</span><span class="sv" id="snr-opps">0</span></div>
    <div class="s-row"><span class="sk">Trades</span><span class="sv" id="snr-trades">0</span></div>
    <div class="s-row"><span class="sk">P&amp;L</span><span class="sv" id="snr-pnl">$0.00</span></div>
    <div class="s-row"><span class="sk">Health</span><span class="sv"><span class="health-dot ok" id="hd-nr"></span></span></div>
  </div>
  <div class="strat-card" id="sc-corr">
    <div class="strat-name" style="color:#9b59b6">&#x1F517; Correlated Arb</div>
    <div class="s-row"><span class="sk">Pairs</span><span class="sv" id="sc-opps">0</span></div>
    <div class="s-row"><span class="sk">Trades</span><span class="sv" id="sc-trades">0</span></div>
    <div class="s-row"><span class="sk">P&amp;L</span><span class="sv" id="sc-pnl">$0.00</span></div>
    <div class="s-row"><span class="sk">Health</span><span class="sv"><span class="health-dot ok" id="hd-c"></span></span></div>
  </div>
  <div class="strat-card" id="sc-sniper">
    <div class="strat-name" style="color:#e74c3c">&#x1F3AF; Liquidity Sniper</div>
    <div class="s-row"><span class="sk">Signals</span><span class="sv" id="ss-opps">0</span></div>
    <div class="s-row"><span class="sk">Orders</span><span class="sv" id="ss-trades">0</span></div>
    <div class="s-row"><span class="sk">P&amp;L</span><span class="sv" id="ss-pnl">$0.00</span></div>
    <div class="s-row"><span class="sk">Health</span><span class="sv"><span class="health-dot ok" id="hd-s"></span></span></div>
  </div>
</div>

<!-- Stacked P&L chart by strategy -->
<div class="section">
  <h3>&#x1F4C8; Cumulative P&amp;L by Strategy (session)</h3>
  <canvas id="stratChart"></canvas>
</div>

<!-- Global P&L history chart -->
<div class="section">
  <h3>&#x1F4C9; Daily P&amp;L History</h3>
  <canvas id="pnlChart"></canvas>
</div>

<!-- Trade log + opportunities row -->
<div class="row2">
  <div class="section">
    <h3>&#x1F504; Recent Trades (with strategy)</h3>
    <table>
      <thead><tr><th>Time</th><th>Market</th><th>Strategy</th><th>Side</th><th>Price</th><th>Status</th></tr></thead>
      <tbody id="tradeBody"><tr><td colspan="6" style="color:#8b949e;text-align:center">No trades yet</td></tr></tbody>
    </table>
  </div>
  <div class="section">
    <h3>&#x1F3AF; Active Correlated Pairs</h3>
    <table>
      <thead><tr><th>Type</th><th>Market A</th><th>Market B</th><th>Div%</th></tr></thead>
      <tbody id="pairBody"><tr><td colspan="4" style="color:#8b949e;text-align:center">No pairs detected</td></tr></tbody>
    </table>
  </div>
</div>

<!-- Category stats -->
<div class="section">
  <h3>&#x1F4C2; Category Stats</h3>
  <table>
    <thead><tr><th>Category</th><th>Opps</th><th>Taker Fee</th><th>Min Edge</th></tr></thead>
    <tbody id="catBody"><tr><td colspan="4" style="color:#8b949e;text-align:center">No data yet</td></tr></tbody>
  </table>
</div>

<!-- Paper vs Live Comparison -->
<div class="section">
  <h3>&#x1F4CA; Paper vs Live &mdash; Performance Comparison</h3>
  <div class="cmp-header">
    <span>Strategy</span>
    <span class="cmp-paper-h">&#x1F4DD; PAPER</span>
    <span class="cmp-live-h">&#x26A1; LIVE</span>
  </div>
  <div class="cmp-row"><span>Total Trades (DB)</span><span class="cmp-paper" id="cmp-t-paper">&ndash;</span><span class="cmp-live" id="cmp-t-live">&ndash;</span></div>
  <div class="cmp-row"><span>Mispricing P&amp;L</span><span class="cmp-paper" id="cmp-m-paper">&ndash;</span><span class="cmp-live" id="cmp-m-live">&ndash;</span></div>
  <div class="cmp-row"><span>Near-Resolved P&amp;L</span><span class="cmp-paper" id="cmp-nr-paper">&ndash;</span><span class="cmp-live" id="cmp-nr-live">&ndash;</span></div>
  <div class="cmp-row"><span>Correlated Arb P&amp;L</span><span class="cmp-paper" id="cmp-c-paper">&ndash;</span><span class="cmp-live" id="cmp-c-live">&ndash;</span></div>
  <div class="cmp-row"><span>Sniper P&amp;L</span><span class="cmp-paper" id="cmp-s-paper">&ndash;</span><span class="cmp-live" id="cmp-s-live">&ndash;</span></div>
  <div class="cmp-row"><span>&#x1F4B0; Total P&amp;L</span><span class="cmp-paper" id="cmp-total-paper">&ndash;</span><span class="cmp-live" id="cmp-total-live">&ndash;</span></div>
  <div class="cmp-chart-wrap">
    <canvas id="cmpChart" class="cmp-canvas"></canvas>
  </div>
</div>

<!-- Error panel (hidden when empty) -->
<div class="section" id="errSection" style="display:none">
  <h3>&#x26A0;&#xFE0F; Recent Errors</h3>
  <table>
    <thead><tr><th>Time</th><th>Level</th><th>Message</th></tr></thead>
    <tbody id="errBody"></tbody>
  </table>
</div>

<div class="footer" id="footer">Auto-refresh every 5s</div>

<script>
const FEES={crypto:.07,sports:.03,finance:.04,politics:.04,economics:.05,culture:.05,geopolitics:0};
const FEE_MULT=1.5;
const STRAT_COLORS={
  mispricing:   {border:'#58a6ff', bg:'rgba(88,166,255,0.15)'},
  near_resolved:{border:'#2ecc71', bg:'rgba(46,204,113,0.15)'},
  correlated:   {border:'#9b59b6', bg:'rgba(155,89,182,0.15)'},
  sniper:       {border:'#e74c3c', bg:'rgba(231,76,60,0.15)'},
};

// Stacked strategy P&L bar chart
const sCtx=document.getElementById('stratChart').getContext('2d');
const stratChart=new Chart(sCtx,{
  type:'bar',
  data:{
    labels:['Mispricing','Near-Resolved','Correlated','Sniper'],
    datasets:[{
      label:'Session P&L ($)',
      data:[0,0,0,0],
      backgroundColor:['rgba(88,166,255,0.7)','rgba(46,204,113,0.7)','rgba(155,89,182,0.7)','rgba(231,76,60,0.7)'],
      borderColor:['#58a6ff','#2ecc71','#9b59b6','#e74c3c'],
      borderWidth:1,borderRadius:5,
    }]
  },
  options:{responsive:true,animation:false,
    plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>'$'+c.raw.toFixed(4)}}},
    scales:{y:{ticks:{color:'#8b949e',callback:v=>'$'+v.toFixed(2)},grid:{color:'#21262d'}},
            x:{ticks:{color:'#8b949e'},grid:{display:false}}}}
});

// Daily P&L line chart
const pCtx=document.getElementById('pnlChart').getContext('2d');
const pnlChart=new Chart(pCtx,{
  type:'line',
  data:{labels:[],datasets:[{label:'Daily P&L ($)',data:[],borderColor:'#58a6ff',
    backgroundColor:'rgba(88,166,255,0.08)',fill:true,tension:0.35,
    pointRadius:3,pointBackgroundColor:'#58a6ff'}]},
  options:{responsive:true,animation:false,
    plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>'$'+c.raw.toFixed(4)}}},
    scales:{y:{ticks:{color:'#8b949e',callback:v=>'$'+v.toFixed(2)},grid:{color:'#21262d'}},
            x:{ticks:{color:'#8b949e',maxTicksLimit:10},grid:{display:false}}}}
});

function pnlColor(v){return v>=0?'#2ecc71':'#e74c3c';}
function fmt(v,pre='$'){const n=parseFloat(v||0);return pre+(n>=0?'':'')+n.toFixed(2);}
function healthCls(h){return h&&h.disabled?'err':(h&&h.failures>0?'warn':'ok');}

async function fetchStats(){
  try{
    const s=await fetch('/api/stats').then(r=>r.json());

    // Header
    document.getElementById('started').textContent=s.started||'–';
    document.getElementById('lastScan').textContent=s.last_scan||'–';
    document.getElementById('tradeSize').textContent='$'+parseFloat(s.trade_size_usd||0).toFixed(2);
    const mb=document.getElementById('modeBadge');
    mb.textContent=s.mode||'DRY RUN';
    mb.style.background=s.mode==='LIVE'?'#e74c3c22':'#f39c1222';
    mb.style.color=s.mode==='LIVE'?'#e74c3c':'#f39c12';

    // Balance row
    const bal=parseFloat(s.wallet_balance||0);
    const exp=parseFloat(s.exposure||0);
    const avail=s.mode==='LIVE'?Math.max(0,bal-exp):null;
    document.getElementById('b-wallet').textContent=s.mode==='DRY RUN'?'DRY RUN':'$'+bal.toFixed(2);
    document.getElementById('b-exp-val').textContent='$'+exp.toFixed(2);
    document.getElementById('b-avail').textContent=s.mode==='DRY RUN'?'DRY RUN':(avail!==null?'$'+avail.toFixed(2):'N/A');
    document.getElementById('b-red').textContent='$'+parseFloat(s.redeemed||0).toFixed(2);

    // Global summary
    document.getElementById('v-scans').textContent=s.scans??'–';
    document.getElementById('v-opp').textContent=s.opportunities??'–';
    document.getElementById('v-trades').textContent=s.trades??'–';
    document.getElementById('v-stops').textContent=s.stops_triggered??'0';
    const dbs=s.db_stats||{};
    document.getElementById('v-db-total').textContent=(dbs.total||0);

    const pnl=parseFloat(s.daily_pnl||0);
    const pe=document.getElementById('v-pnl');
    pe.textContent=(pnl>=0?'+':'')+'$'+pnl.toFixed(2);
    pe.style.color=pnlColor(pnl);

    const posMax=s.max_positions||1,expMax=s.max_exposure||1;
    document.getElementById('v-pos').textContent=(s.open_positions||0)+' / '+posMax;
    document.getElementById('b-pos').style.width=Math.min(100,(s.open_positions/posMax)*100)+'%';
    document.getElementById('v-exp').textContent='$'+exp.toFixed(2);
    document.getElementById('b-exp').style.width=Math.min(100,(exp/expMax)*100)+'%';

    // Strategy panels
    const ss=s.strategy_stats||{};
    const sp=s.strategy_pnl||{};
    const sh=s.strategy_health||{};

    const STRATS=[
      ['mispricing','sm','hd-m'],['near_resolved','snr','hd-nr'],
      ['correlated','sc','hd-c'],['sniper','ss','hd-s']
    ];
    STRATS.forEach(([key,pre,hdId])=>{
      const d=ss[key]||{};const p=sp[key]||0;const h=sh[key]||{};
      document.getElementById(pre+'-opps').textContent=d.opps||0;
      document.getElementById(pre+'-trades').textContent=d.trades||0;
      const pEl=document.getElementById(pre+'-pnl');
      pEl.textContent=(p>=0?'+':'')+'$'+parseFloat(p).toFixed(2);
      pEl.style.color=pnlColor(p);
      const hDot=document.getElementById(hdId);
      hDot.className='health-dot '+healthCls(h);
    });

    // Strategy P&L bar chart
    stratChart.data.datasets[0].data=[
      sp.mispricing||0, sp.near_resolved||0, sp.correlated||0, sp.sniper||0
    ];
    stratChart.update('none');

    // Daily P&L line chart
    const hist=s.pnl_history||[];
    pnlChart.data.labels=hist.map(h=>h.t);
    pnlChart.data.datasets[0].data=hist.map(h=>h.pnl);
    const lp=hist.length?hist[hist.length-1].pnl:0;
    pnlChart.data.datasets[0].borderColor=pnlColor(lp);
    pnlChart.data.datasets[0].backgroundColor=lp>=0?'rgba(46,204,113,0.08)':'rgba(231,76,60,0.08)';
    pnlChart.update('none');

    // Trade log with strategy attribution
    const trades=s.trade_log||[];
    const tbody=document.getElementById('tradeBody');
    tbody.innerHTML=trades.length===0
      ?'<tr><td colspan="6" style="color:#8b949e;text-align:center">No trades yet</td></tr>'
      :trades.slice(0,10).map(t=>{
        const stratCol={mispricing:'#58a6ff',near_resolved:'#2ecc71',correlated:'#9b59b6',sniper:'#e74c3c'};
        const sc=stratCol[t.strategy]||'#8b949e';
        return `<tr>
          <td style="color:#8b949e;white-space:nowrap">${t.time||'–'}</td>
          <td title="${t.market||''}">${(t.market||'').slice(0,28)}&hellip;</td>
          <td style="color:${sc};font-size:.72rem;font-weight:600">${(t.strategy||'?').replace('_',' ')}</td>
          <td class="${t.side==='BUY'?'g':'r'}">${t.side||'–'}</td>
          <td>$${parseFloat(t.price||0).toFixed(3)}</td>
          <td style="color:#8b949e">${t.status||'–'}</td>
        </tr>`;}).join('');

    // Active correlated pairs
    const pairs=s.active_pairs||[];
    const pbody=document.getElementById('pairBody');
    pbody.innerHTML=pairs.length===0
      ?'<tr><td colspan="4" style="color:#8b949e;text-align:center">No active pairs</td></tr>'
      :pairs.map(p=>`<tr>
          <td style="color:#9b59b6;font-size:.72rem">${p.type||'–'}</td>
          <td style="font-size:.74rem">${(p.a||'').slice(0,28)}</td>
          <td style="font-size:.74rem">${(p.b||'').slice(0,28)}</td>
          <td class="y">${(p.div||0).toFixed(1)}%</td>
        </tr>`).join('');

    // Category stats
    const cats=s.category_stats||{};
    const cbody=document.getElementById('catBody');
    const entries=Object.entries(cats).sort((a,b)=>b[1]-a[1]);
    cbody.innerHTML=entries.length===0
      ?'<tr><td colspan="4" style="color:#8b949e;text-align:center">No data yet</td></tr>'
      :entries.map(([cat,cnt])=>{
        const fee=FEES[cat]||0;
        const minEdge=Math.max(1.5,fee*50*FEE_MULT).toFixed(1);
        return `<tr><td>${cat}</td><td>${cnt}</td>
          <td style="color:#8b949e">${(fee*100).toFixed(0)}%</td>
          <td style="color:#f39c12">${minEdge}%</td></tr>`;
      }).join('');

    document.getElementById('footer').textContent=
      'Auto-refresh every 5s \xb7 Updated: '+new Date().toLocaleTimeString();
  }catch(e){
    document.getElementById('footer').textContent='Failed to load data — retrying...';
  }
}

async function fetchErrors(){
  try{
    const errs=await fetch('/api/errors').then(r=>r.json());
    const sec=document.getElementById('errSection');
    if(!errs||errs.length===0){sec.style.display='none';return;}
    sec.style.display='';
    document.getElementById('errBody').innerHTML=errs.slice(0,10).map(e=>`<tr>
      <td style="color:#8b949e;white-space:nowrap">${e.time||'–'}</td>
      <td class="r">${e.level||'ERROR'}</td>
      <td style="word-break:break-word;font-size:.75rem">${e.msg||''}</td>
    </tr>`).join('');
  }catch(e){}
}

// Paper vs Live comparison chart
const cmpCtx=document.getElementById('cmpChart').getContext('2d');
const cmpChart=new Chart(cmpCtx,{
  type:'bar',
  data:{
    labels:['Mispricing','Near-Resolved','Correlated','Sniper'],
    datasets:[
      {label:'Paper',data:[0,0,0,0],
       backgroundColor:'rgba(88,166,255,0.55)',borderColor:'#58a6ff',borderWidth:1,borderRadius:4},
      {label:'Live', data:[0,0,0,0],
       backgroundColor:'rgba(46,204,113,0.55)',borderColor:'#2ecc71',borderWidth:1,borderRadius:4},
    ]
  },
  options:{responsive:true,animation:false,
    plugins:{
      legend:{display:true,labels:{color:'#8b949e',font:{size:11}}},
      tooltip:{callbacks:{label:c=>'$'+parseFloat(c.raw).toFixed(4)}}
    },
    scales:{
      y:{ticks:{color:'#8b949e',callback:v=>'$'+v.toFixed(2)},grid:{color:'#21262d'}},
      x:{ticks:{color:'#8b949e'},grid:{display:false}}
    }
  }
});

function fmtPnl(v){
  const n=parseFloat(v||0);
  return (n>=0?'+':'')+'\$'+n.toFixed(4);
}

async function fetchCompare(){
  try{
    const d=await fetch('/api/compare').then(r=>r.json());
    const p=d.paper||{};const l=d.live||{};
    const pp=p.strategy_pnl||{};const lp=l.strategy_pnl||{};
    const pd=p.db_stats||{};const ld=l.db_stats||{};

    // Table rows
    document.getElementById('cmp-t-paper').textContent=pd.total||0;
    document.getElementById('cmp-t-live').textContent=ld.total||0;

    const rows=[
      ['cmp-m',  pp.mispricing,    lp.mispricing],
      ['cmp-nr', pp.near_resolved, lp.near_resolved],
      ['cmp-c',  pp.correlated,    lp.correlated],
      ['cmp-s',  pp.sniper,        lp.sniper],
    ];
    rows.forEach(([pre,pv,lv])=>{
      const pe=document.getElementById(pre+'-paper');
      const le=document.getElementById(pre+'-live');
      pe.textContent=fmtPnl(pv);
      pe.style.color=parseFloat(pv||0)>=0?'#58a6ff':'#e74c3c';
      le.textContent=fmtPnl(lv);
      le.style.color=parseFloat(lv||0)>=0?'#2ecc71':'#e74c3c';
    });

    const pTotal=(pp.mispricing||0)+(pp.near_resolved||0)+(pp.correlated||0)+(pp.sniper||0);
    const lTotal=(lp.mispricing||0)+(lp.near_resolved||0)+(lp.correlated||0)+(lp.sniper||0);
    const ptEl=document.getElementById('cmp-total-paper');
    const ltEl=document.getElementById('cmp-total-live');
    ptEl.textContent=fmtPnl(pTotal);
    ptEl.style.color=pTotal>=0?'#58a6ff':'#e74c3c';
    ltEl.textContent=fmtPnl(lTotal);
    ltEl.style.color=lTotal>=0?'#2ecc71':'#e74c3c';

    // Chart
    cmpChart.data.datasets[0].data=[pp.mispricing||0,pp.near_resolved||0,pp.correlated||0,pp.sniper||0];
    cmpChart.data.datasets[1].data=[lp.mispricing||0,lp.near_resolved||0,lp.correlated||0,lp.sniper||0];
    cmpChart.update('none');
  }catch(e){}
}

setInterval(fetchStats,5000);
setInterval(fetchErrors,10000);
setInterval(fetchCompare,15000);
fetchStats();
fetchErrors();
fetchCompare();
</script>
</body>
</html>"""


def _render_html() -> str:
    return _HTML_TEMPLATE


def _serialize_stats() -> bytes:
    """Return a JSON-safe copy of _stats (trade_log already serialized as dicts)."""
    try:
        return json.dumps(_stats).encode("utf-8")
    except (TypeError, ValueError):
        # Fallback: strip non-serializable items
        safe = {k: v for k, v in _stats.items()
                if k not in ("trade_log", "opp_log", "pnl_history")}
        safe["trade_log"]  = []
        safe["opp_log"]    = []
        safe["pnl_history"] = []
        return json.dumps(safe).encode("utf-8")


def _start_health_server() -> None:
    """Start HTML dashboard + JSON API server in a background thread."""

    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, data) -> None:
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/stats" or self.path.startswith("/api/stats?"):
                self._send_json(json.loads(_serialize_stats()))

            elif self.path.startswith("/api/balance"):
                self._send_json({
                    "balance": _stats.get("wallet_balance", 0.0),
                    "mode":    _stats.get("mode", "DRY RUN"),
                })

            elif self.path.startswith("/api/trades"):
                _mode = "live" if _stats.get("mode") == "LIVE" else "paper"
                self._send_json(trade_db.get_trades(50, mode=_mode))

            elif self.path.startswith("/api/compare"):
                self._send_json({
                    "paper": {
                        "db_stats":     trade_db.get_db_stats(mode="paper"),
                        "strategy_pnl": trade_db.get_strategy_pnl_totals(mode="paper"),
                    },
                    "live": {
                        "db_stats":     trade_db.get_db_stats(mode="live"),
                        "strategy_pnl": trade_db.get_strategy_pnl_totals(mode="live"),
                    },
                })

            elif self.path.startswith("/api/errors"):
                self._send_json(list(_error_log))

            else:
                body = _render_html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    port = int(os.environ.get("PORT", 8080))
    try:
        server = HTTPServer(("0.0.0.0", port), Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        logging.getLogger("polymarket-bot").info("Dashboard running on port %d", port)
    except OSError as e:
        logging.getLogger("polymarket-bot").warning("Could not start dashboard server: %s", e)


def main():
    """CLI entry point."""
    import argparse

    _start_health_server()

    parser = argparse.ArgumentParser(description="ClawBots Polymarket Bot \U0001f52b\U0001f9ec")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Simulate trades without executing (default: True)")
    parser.add_argument("--live", action="store_true",
                        help="Enable live trading with real money")
    parser.add_argument("--scan", action="store_true",
                        help="Run a single scan and exit")
    parser.add_argument("--positions", action="store_true",
                        help="Show current positions and exit")
    parser.add_argument("--redeem", action="store_true",
                        help="Check and redeem all resolvable positions, then exit")
    parser.add_argument("--min-edge", type=float, default=None,
                        help="Override minimum edge %% for scanning")
    # Feature 4: Backtesting
    parser.add_argument("--backtest", action="store_true",
                        help="Run backtest on recently resolved markets and exit")
    parser.add_argument("--days", type=int, default=7,
                        help="Number of days of history for backtest (default: 7)")

    args    = parser.parse_args()
    dry_run = not args.live

    if args.live:
        console.print("[bold red]⚠️  LIVE TRADING MODE — REAL MONEY AT RISK ⚠️[/]")
        if not config.is_configured():
            for err in Config.validate():
                console.print(f"[red]{err}[/]")
            sys.exit(1)

    # Feature 4: Backtest mode
    if args.backtest:
        from src.backtest.backtest import Backtester
        bt = Backtester(days=args.days, gamma=GammaClient())
        bt.run(verbose=True)
        return

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
        tracker  = PositionTracker(address, DataClient())
        redeemer = AutoRedeemer(
            address=address, private_key=config.PRIVATE_KEY, tracker=tracker
        )
        results = redeemer.run()
        total   = sum(r.estimated_usdc for r in results if r.succeeded)
        console.print(f"\n[bold green]Total redeemable: ${total:.2f} USDC[/]")
        return

    bot.start()


if __name__ == "__main__":
    main()
