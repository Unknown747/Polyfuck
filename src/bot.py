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
            "time":  self.formatTime(record, "%H:%M:%S"),
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

        # Initialise SQLite trade history and wallet balance cache
        trade_db.init_db()
        self._wallet_balance: float = 0.0

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
                # 1. Scan for mispricing opportunities
                opportunities = self.scan_once()

                if opportunities:
                    self._opportunities_found += len(opportunities)
                    display_opportunities(opportunities)

                    best         = opportunities[0]
                    profit_calc  = self.trader.calculate_profit_after_fees(
                        best, self.trader._current_trade_size
                    )
                    # BUG FIX: Mispricing has no _category attr; use categories list instead
                    category     = best.categories[0] if best.categories else ""
                    executed     = False

                    console.print(
                        f"\n[bold]Best opportunity:[/] {best.market_question}\n"
                        f"  Edge: {best.edge_pct:.2f}% | "
                        f"Est. profit: ${profit_calc['net_profit']:.4f} "
                        f"({profit_calc['roi_pct']:.1f}% ROI)"
                    )

                    if profit_calc["profitable"]:
                        trade = self.trader.execute_mispricing_trade(
                            best, self.trader._current_trade_size
                        )
                        if trade:
                            self._trades_executed += 1
                            executed = True
                    else:
                        console.print(
                            f"[yellow]Skipping — edge {best.edge_pct:.2f}% not "
                            f"profitable after fees.[/]"
                        )

                    # Persist to SQLite
                    if trade:
                        trade_db.insert_trade(trade, category, "mispricing")

                    # Feature 3: Log opportunity to CSV/JSONL
                    row = opp_logger.log_mispricing(best, executed, profit_calc, category)
                    _append_opp_log(row)

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

                # 1b. Near-resolved scan — runs every scan (not every 3)
                # so opportunities are not missed when they close quickly.
                near_resolved = self.scanner.scan_near_resolved(
                    categories=config.SCAN_CATEGORIES,
                )
                if near_resolved:
                    self._near_resolved_found += len(near_resolved)
                    display_near_resolved(near_resolved)
                    # Attempt up to 2 near-resolved trades per scan (safety limits apply)
                    for best_nr in near_resolved[:2]:
                        nr_executed = False
                        nr_trade    = self.trader.execute_near_resolved_trade(
                            best_nr, investment_usd=self.trader._current_trade_size
                        )
                        if nr_trade:
                            self._trades_executed += 1
                            nr_executed = True
                            trade_db.insert_trade(nr_trade, "", "near_resolved")

                        # Feature 3: Log near-resolved opportunity
                        nr_row = opp_logger.log_near_resolved(best_nr, nr_executed)
                        _append_opp_log(nr_row)

                # 2. Feature 2: Check trailing stops every scan
                if self.positions and not self.dry_run:
                    current_positions = self.positions.refresh_positions()
                    if current_positions:
                        stopped = self.trader.check_trailing_stops(current_positions)
                        if stopped:
                            console.print(
                                f"[red]🛑 {len(stopped)} position(s) closed by trailing stop[/]"
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
                "time":   time.strftime("%H:%M:%S", time.gmtime(t.timestamp)),
                "market": t.market_question[:50],
                "side":   t.side,
                "price":  round(t.price, 4),
                "size":   round(t.size, 2),
                "status": t.status,
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
            "category_stats":  self.scanner.get_category_stats(),
            "wallet_balance":  self._wallet_balance,
            "db_stats":        trade_db.get_db_stats(),
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
    "db_stats":        {"total": 0, "executed": 0, "dry_run": 0},
}


def _append_opp_log(row: dict) -> None:
    """Append a logged opportunity row (from opp_logger) to the dashboard feed."""
    # BUG FIX: timestamp format is "2026-05-25 12:31:05 UTC"; [-8:] gives "5 UTC" (wrong).
    # Split on space and take the time token (index 1) instead.
    ts_parts = row.get("timestamp", "").split(" ")
    time_str = ts_parts[1] if len(ts_parts) >= 2 else "–"
    entry = {
        "time":      time_str,
        "market":    row.get("market", ""),
        "edge_pct":  row.get("edge_pct", 0),
        "executed":  row.get("executed", False),
    }
    log = _stats.get("opp_log", [])
    log.append(entry)
    _stats["opp_log"] = log[-20:]


# ── Dashboard HTML ──────────────────────────────────────────────────────────

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ClawBots – Polymarket Bot</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;padding:20px}
  h1{font-size:1.4rem;color:#58a6ff;margin-bottom:4px}
  .sub{color:#8b949e;font-size:.82rem;margin-bottom:18px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
  .badge{padding:2px 10px;border-radius:20px;font-size:.78rem;font-weight:600}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#2ecc71;margin-right:4px;animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(165px,1fr));gap:12px;margin-bottom:18px}
  .card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:15px}
  .card .label{color:#8b949e;font-size:.70rem;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px}
  .card .value{font-size:1.5rem;font-weight:700}
  .bar-wrap{background:#21262d;border-radius:4px;height:6px;overflow:hidden;margin-top:7px}
  .bar{height:100%;border-radius:4px;background:#58a6ff;transition:width .4s}
  .section{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:15px;margin-bottom:15px}
  .section h3{font-size:.88rem;color:#8b949e;margin-bottom:11px;font-weight:600}
  .row2{display:grid;grid-template-columns:1fr 1fr;gap:15px;margin-bottom:15px}
  table{width:100%;border-collapse:collapse;font-size:.79rem}
  th{color:#8b949e;text-align:left;padding:4px 8px;font-weight:500;border-bottom:1px solid #30363d}
  td{padding:5px 8px;border-bottom:1px solid #21262d}
  tr:last-child td{border-bottom:none}
  .g{color:#2ecc71}.r{color:#e74c3c}
  .footer{color:#8b949e;font-size:.74rem;text-align:center;margin-top:10px}
  canvas{max-height:190px}
  @media(max-width:620px){.row2{grid-template-columns:1fr}}
</style>
</head>
<body>
<h1>&#x1F52B; ClawBots &#x2013; Polymarket Bot</h1>
<div class="sub">
  <span>Mulai: <span id="started">&#x2013;</span></span>
  <span>Scan terakhir: <span id="lastScan">&#x2013;</span></span>
  <span id="modeBadge" class="badge" style="background:#f39c1222;color:#f39c12">DRY RUN</span>
  <span><span class="dot"></span><span id="tradeSize">&#x2013;</span> per trade</span>
</div>

<div class="grid">
  <div class="card"><div class="label">Total Scan</div><div class="value" id="v-scans">&#x2013;</div></div>
  <div class="card"><div class="label">Mispricing</div><div class="value" id="v-opp">&#x2013;</div></div>
  <div class="card"><div class="label">Near-Resolved</div><div class="value" id="v-nr" style="color:#58a6ff">&#x2013;</div></div>
  <div class="card"><div class="label">Trades</div><div class="value" id="v-trades">&#x2013;</div></div>
  <div class="card"><div class="label">Daily P&amp;L</div><div class="value" id="v-pnl">&#x2013;</div></div>
  <div class="card"><div class="label">Redeemed</div><div class="value" id="v-red">&#x2013;</div></div>
  <div class="card">
    <div class="label">Posisi</div><div class="value" id="v-pos">&#x2013;</div>
    <div class="bar-wrap"><div class="bar" id="b-pos" style="width:0%"></div></div>
  </div>
  <div class="card">
    <div class="label">Eksposur</div><div class="value" id="v-exp">&#x2013;</div>
    <div class="bar-wrap"><div class="bar" id="b-exp" style="width:0%"></div></div>
  </div>
  <div class="card"><div class="label">Stop-Loss Hit</div><div class="value r" id="v-stops">0</div></div>
  <div class="card"><div class="label">Opp Dicatat</div><div class="value" id="v-opp-log">0</div></div>
  <div class="card">
    <div class="label">Wallet Balance</div>
    <div class="value g" id="v-wallet">–</div>
  </div>
  <div class="card">
    <div class="label">Trade DB Total</div>
    <div class="value" id="v-db-total">–</div>
  </div>
</div>

<div class="section">
  <h3>&#x1F4C8; Daily P&amp;L History</h3>
  <canvas id="pnlChart"></canvas>
</div>

<div class="row2">
  <div class="section">
    <h3>&#x1F504; Trade Terbaru</h3>
    <table>
      <thead><tr><th>Waktu</th><th>Market</th><th>Side</th><th>Harga</th><th>Status</th></tr></thead>
      <tbody id="tradeBody"><tr><td colspan="5" style="color:#8b949e;text-align:center">Belum ada trade</td></tr></tbody>
    </table>
  </div>
  <div class="section">
    <h3>&#x1F3AF; Peluang Terbaru</h3>
    <table>
      <thead><tr><th>Waktu</th><th>Market</th><th>Edge</th><th>Eksekusi</th></tr></thead>
      <tbody id="oppBody"><tr><td colspan="4" style="color:#8b949e;text-align:center">Belum ada peluang</td></tr></tbody>
    </table>
  </div>
</div>

<div class="section">
  <h3>&#x1F4C2; Stats per Kategori</h3>
  <table>
    <thead><tr><th>Kategori</th><th>Peluang</th><th>Fee Taker</th><th>Min Edge Efektif</th></tr></thead>
    <tbody id="catBody"><tr><td colspan="4" style="color:#8b949e;text-align:center">Belum ada data</td></tr></tbody>
  </table>
</div>

<div class="section" id="errSection" style="display:none">
  <h3>&#x26A0;&#xFE0F; Error Terbaru</h3>
  <table>
    <thead><tr><th>Waktu</th><th>Level</th><th>Pesan</th></tr></thead>
    <tbody id="errBody"></tbody>
  </table>
</div>

<div class="footer" id="footer">Auto-refresh tiap 5 detik</div>

<script>
const FEES={crypto:.07,sports:.03,finance:.04,politics:.04,economics:.05,culture:.05,geopolitics:0};
const FEE_MULT=1.5;
const ctx=document.getElementById('pnlChart').getContext('2d');
const pnlChart=new Chart(ctx,{
  type:'line',
  data:{labels:[],datasets:[{label:'Daily P&L ($)',data:[],borderColor:'#58a6ff',
    backgroundColor:'rgba(88,166,255,0.08)',fill:true,tension:0.35,
    pointRadius:3,pointBackgroundColor:'#58a6ff'}]},
  options:{responsive:true,animation:false,
    plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>'$'+c.raw.toFixed(4)}}},
    scales:{y:{ticks:{color:'#8b949e',callback:v=>'$'+v.toFixed(2)},grid:{color:'#21262d'}},
            x:{ticks:{color:'#8b949e',maxTicksLimit:10},grid:{display:false}}}}
});

async function fetchStats(){
  try{
    const s=await fetch('/api/stats').then(r=>r.json());
    document.getElementById('started').textContent=s.started||'–';
    document.getElementById('lastScan').textContent=s.last_scan||'–';
    document.getElementById('tradeSize').textContent='$'+parseFloat(s.trade_size_usd||0).toFixed(2);
    const mb=document.getElementById('modeBadge');
    mb.textContent=s.mode||'DRY RUN';
    mb.style.background=s.mode==='LIVE'?'#e74c3c22':'#f39c1222';
    mb.style.color=s.mode==='LIVE'?'#e74c3c':'#f39c12';

    document.getElementById('v-scans').textContent=s.scans??'–';
    document.getElementById('v-opp').textContent=s.opportunities??'–';
    document.getElementById('v-nr').textContent=s.near_resolved??'–';
    document.getElementById('v-trades').textContent=s.trades??'–';
    document.getElementById('v-stops').textContent=s.stops_triggered??'0';
    document.getElementById('v-opp-log').textContent=(s.opp_stats&&s.opp_stats.total_logged)||'0';

    const bal=parseFloat(s.wallet_balance||0);
    const walletEl=document.getElementById('v-wallet');
    walletEl.textContent=s.mode==='DRY RUN'?'N/A':'$'+bal.toFixed(2);

    const dbs=s.db_stats||{};
    document.getElementById('v-db-total').textContent=(dbs.total||0)+' trades';

    const pnl=parseFloat(s.daily_pnl||0);
    const pe=document.getElementById('v-pnl');
    pe.textContent=(pnl>=0?'+':'')+'$'+pnl.toFixed(2);
    pe.style.color=pnl>=0?'#2ecc71':'#e74c3c';

    document.getElementById('v-red').textContent='$'+parseFloat(s.redeemed||0).toFixed(2);
    const posMax=s.max_positions||1,expMax=s.max_exposure||1;
    document.getElementById('v-pos').textContent=(s.open_positions||0)+' / '+posMax;
    document.getElementById('b-pos').style.width=Math.min(100,(s.open_positions/posMax)*100)+'%';
    document.getElementById('v-exp').textContent='$'+parseFloat(s.exposure||0).toFixed(2);
    document.getElementById('b-exp').style.width=Math.min(100,(s.exposure/expMax)*100)+'%';

    const hist=s.pnl_history||[];
    pnlChart.data.labels=hist.map(h=>h.t);
    pnlChart.data.datasets[0].data=hist.map(h=>h.pnl);
    const ds=pnlChart.data.datasets[0];
    const lp=hist.length?hist[hist.length-1].pnl:0;
    ds.borderColor=lp>=0?'#2ecc71':'#e74c3c';
    ds.backgroundColor=lp>=0?'rgba(46,204,113,0.08)':'rgba(231,76,60,0.08)';
    pnlChart.update('none');

    const trades=s.trade_log||[];
    const tbody=document.getElementById('tradeBody');
    tbody.innerHTML=trades.length===0
      ?'<tr><td colspan="5" style="color:#8b949e;text-align:center">Belum ada trade</td></tr>'
      :trades.slice(0,10).map(t=>`<tr>
        <td style="color:#8b949e">${t.time||'–'}</td>
        <td title="${t.market||''}">${(t.market||'').slice(0,32)}&hellip;</td>
        <td class="${t.side==='BUY'?'g':'r'}">${t.side||'–'}</td>
        <td>$${parseFloat(t.price||0).toFixed(3)}</td>
        <td style="color:#8b949e">${t.status||'–'}</td>
      </tr>`).join('');

    const opps=s.opp_log||[];
    const obody=document.getElementById('oppBody');
    obody.innerHTML=opps.length===0
      ?'<tr><td colspan="4" style="color:#8b949e;text-align:center">Belum ada peluang</td></tr>'
      :opps.slice(0,10).map(o=>`<tr>
        <td style="color:#8b949e">${o.time||'–'}</td>
        <td title="${o.market||''}">${(o.market||'').slice(0,32)}&hellip;</td>
        <td class="g">${parseFloat(o.edge_pct||0).toFixed(2)}%</td>
        <td>${o.executed?'&#x2705;':'&#x2013;'}</td>
      </tr>`).join('');

    const cats=s.category_stats||{};
    const cbody=document.getElementById('catBody');
    const entries=Object.entries(cats).sort((a,b)=>b[1]-a[1]);
    cbody.innerHTML=entries.length===0
      ?'<tr><td colspan="4" style="color:#8b949e;text-align:center">Belum ada data</td></tr>'
      :entries.map(([cat,cnt])=>{
        const fee=(FEES[cat]||0);
        const minEdge=Math.max(1.5,fee*50*FEE_MULT).toFixed(1);
        return `<tr>
          <td>${cat}</td><td>${cnt}</td>
          <td style="color:#8b949e">${(fee*100).toFixed(0)}%</td>
          <td style="color:#f39c12">${minEdge}%</td>
        </tr>`;}).join('');

    document.getElementById('footer').textContent=
      'Auto-refresh tiap 5 detik \xb7 Update: '+new Date().toLocaleTimeString();
  }catch(e){
    document.getElementById('footer').textContent='Gagal memuat data \u2014 mencoba lagi...';
  }
}
async function fetchErrors(){
  try{
    const errs=await fetch('/api/errors').then(r=>r.json());
    const sec=document.getElementById('errSection');
    if(!errs||errs.length===0){sec.style.display='none';return;}
    sec.style.display='';
    document.getElementById('errBody').innerHTML=errs.slice(0,10).map(e=>`<tr>
      <td style="color:#8b949e">${e.time||'–'}</td>
      <td class="r">${e.level||'ERROR'}</td>
      <td style="word-break:break-word">${e.msg||''}</td>
    </tr>`).join('');
  }catch(e){}
}
setInterval(fetchStats,5000);
setInterval(fetchErrors,10000);
fetchStats();
fetchErrors();
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
                self._send_json(trade_db.get_trades(50))

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
