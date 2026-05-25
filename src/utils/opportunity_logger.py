"""Opportunity Logger — records every scan hit to CSV + JSONL for offline analysis."""

import csv
import json
import time
from pathlib import Path

from src.scanner.scanner import Mispricing, NearResolvedOpportunity

_CSV_PATH  = Path("logs/opportunities.csv")
_JSON_PATH = Path("logs/opportunities.jsonl")

_CSV_FIELDS = [
    "timestamp", "type", "market", "category",
    "edge_pct", "yes_price", "no_price", "price_sum",
    "volume_24h", "executed", "net_profit_est", "roi_pct",
]


def _ensure_csv_header() -> None:
    Path("logs").mkdir(exist_ok=True)
    if not _CSV_PATH.exists():
        with open(_CSV_PATH, "w", newline="") as f:
            csv.writer(f).writerow(_CSV_FIELDS)


_ensure_csv_header()


_CSV_INJECT_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_cell(value: str) -> str:
    """Strip leading characters that trigger CSV formula injection in spreadsheets."""
    value = str(value)
    while value and value[0] in _CSV_INJECT_PREFIXES:
        value = value[1:]
    return value


def _write(row: dict) -> None:
    safe_row = {k: (_sanitize_cell(v) if isinstance(v, str) else v) for k, v in row.items()}
    with open(_CSV_PATH, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=_CSV_FIELDS).writerow(safe_row)
    with open(_JSON_PATH, "a") as f:
        f.write(json.dumps(row) + "\n")


def log_mispricing(
    opp: Mispricing,
    executed: bool,
    profit_calc: dict | None = None,
    category: str = "",
) -> dict:
    """Log a mispricing opportunity. Returns the row dict."""
    row = {
        "timestamp":      time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "type":           "mispricing",
        "market":         opp.market_question[:80],
        "category":       category,
        "edge_pct":       round(opp.edge_pct, 4),
        "yes_price":      round(opp.yes_price, 4),
        "no_price":       round(opp.no_price, 4),
        "price_sum":      round(opp.price_sum, 4),
        "volume_24h":     round(opp.volume_24h, 2),
        "executed":       executed,
        "net_profit_est": round(profit_calc["net_profit"], 6) if profit_calc else 0.0,
        "roi_pct":        round(profit_calc["roi_pct"], 4) if profit_calc else 0.0,
    }
    _write(row)
    return row


def log_near_resolved(
    opp: NearResolvedOpportunity,
    executed: bool,
    category: str = "",
) -> dict:
    """Log a near-resolved opportunity. Returns the row dict."""
    row = {
        "timestamp":      time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "type":           "near_resolved",
        "market":         opp.market_question[:80],
        "category":       category,
        "edge_pct":       round(opp.return_pct, 4),
        "yes_price":      round(opp.winning_price, 4),
        "no_price":       round(1.0 - opp.winning_price, 4),
        "price_sum":      1.0,
        "volume_24h":     round(opp.volume_24h, 2),
        "executed":       executed,
        "net_profit_est": round(opp.maker_return_pct, 4),
        "roi_pct":        round(opp.maker_return_pct, 4),
    }
    _write(row)
    return row


def get_stats() -> dict:
    """Summary stats from the CSV log — safe to call frequently."""
    try:
        if not _CSV_PATH.exists():
            return {"total_logged": 0, "executed": 0, "avg_edge_pct": 0.0, "recent": []}
        with open(_CSV_PATH, newline="") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return {"total_logged": 0, "executed": 0, "avg_edge_pct": 0.0, "recent": []}
        total    = len(rows)
        executed = sum(1 for r in rows if r.get("executed") == "True")
        edges    = [float(r["edge_pct"]) for r in rows if r.get("edge_pct")]
        avg_edge = round(sum(edges) / len(edges), 3) if edges else 0.0
        recent   = rows[-20:][::-1]
        return {
            "total_logged": total,
            "executed":     executed,
            "avg_edge_pct": avg_edge,
            "recent":       recent,
        }
    except Exception:
        return {"total_logged": 0, "executed": 0, "avg_edge_pct": 0.0, "recent": []}
