"""SQLite persistence — trades, opportunities, and daily P&L snapshots."""

import logging
import sqlite3
import time
from pathlib import Path

_db_log = logging.getLogger("polymarket-bot")

_DB_PATH = Path("logs/trades.db")


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create all tables if they do not exist."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           REAL    NOT NULL,
                timestamp    TEXT    NOT NULL,
                market       TEXT    NOT NULL,
                condition_id TEXT,
                token_id     TEXT,
                side         TEXT,
                price        REAL,
                size         REAL,
                order_type   TEXT,
                status       TEXT,
                fee_estimate REAL,
                category     TEXT,
                strategy     TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS opportunities (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        REAL    NOT NULL,
                timestamp TEXT    NOT NULL,
                strategy  TEXT    NOT NULL,
                market    TEXT    NOT NULL,
                edge      REAL,
                executed  INTEGER DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_pnl (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                REAL    NOT NULL,
                date              TEXT    NOT NULL,
                mispricing_pnl    REAL    DEFAULT 0,
                near_resolved_pnl REAL    DEFAULT 0,
                correlation_pnl   REAL    DEFAULT 0,
                sniper_pnl        REAL    DEFAULT 0,
                total_pnl         REAL    DEFAULT 0
            )
        """)
        c.commit()


# ── Trades ────────────────────────────────────────────────────────────────────

def insert_trade(trade, category: str = "", strategy: str = "") -> None:
    """Persist a Trade dataclass record."""
    try:
        with _conn() as c:
            c.execute(
                """INSERT INTO trades
                   (ts, timestamp, market, condition_id, token_id, side,
                    price, size, order_type, status, fee_estimate, category, strategy)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade.timestamp,
                    time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(trade.timestamp)),
                    (trade.market_question or "")[:100],
                    getattr(trade, "condition_id", ""),
                    getattr(trade, "token_id", ""),
                    trade.side,
                    trade.price,
                    trade.size,
                    getattr(trade, "order_type", "GTC"),
                    trade.status,
                    getattr(trade, "fee_estimate", 0.0),
                    category,
                    strategy,
                ),
            )
            c.commit()
    except Exception as _e:
        _db_log.warning("db.insert_trade failed: %s", _e)


def get_trades(limit: int = 50) -> list[dict]:
    """Return the most recent trades as plain dicts."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def get_db_stats() -> dict:
    """Summary counts from the trades table."""
    try:
        with _conn() as c:
            total    = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            dry      = c.execute("SELECT COUNT(*) FROM trades WHERE status='dry_run'").fetchone()[0]
            executed = total - dry
            by_strategy = {}
            rows = c.execute(
                "SELECT strategy, COUNT(*) as cnt FROM trades GROUP BY strategy"
            ).fetchall()
            for r in rows:
                by_strategy[r["strategy"] or "unknown"] = r["cnt"]
            return {"total": total, "executed": executed, "dry_run": dry, "by_strategy": by_strategy}
    except Exception:
        return {"total": 0, "executed": 0, "dry_run": 0, "by_strategy": {}}


# ── Opportunities ─────────────────────────────────────────────────────────────

def insert_opportunity(strategy: str, market: str, edge: float, executed: bool) -> None:
    try:
        with _conn() as c:
            c.execute(
                """INSERT INTO opportunities (ts, timestamp, strategy, market, edge, executed)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    time.time(),
                    time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                    strategy,
                    market[:100],
                    edge,
                    1 if executed else 0,
                ),
            )
            c.commit()
    except Exception as _e:
        _db_log.warning("db.insert_opportunity failed: %s", _e)


def get_opportunities(limit: int = 100) -> list[dict]:
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT * FROM opportunities ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


# ── Daily P&L Snapshots ───────────────────────────────────────────────────────

def upsert_daily_pnl(
    mispricing: float = 0.0,
    near_resolved: float = 0.0,
    correlation: float = 0.0,
    sniper: float = 0.0,
) -> None:
    """Insert or update today's per-strategy P&L snapshot."""
    import datetime
    today = datetime.date.today().isoformat()
    total = mispricing + near_resolved + correlation + sniper
    try:
        with _conn() as c:
            existing = c.execute(
                "SELECT id FROM daily_pnl WHERE date=?", (today,)
            ).fetchone()
            if existing:
                c.execute(
                    """UPDATE daily_pnl SET
                       ts=?, mispricing_pnl=?, near_resolved_pnl=?,
                       correlation_pnl=?, sniper_pnl=?, total_pnl=?
                       WHERE date=?""",
                    (time.time(), mispricing, near_resolved, correlation, sniper, total, today),
                )
            else:
                c.execute(
                    """INSERT INTO daily_pnl
                       (ts, date, mispricing_pnl, near_resolved_pnl, correlation_pnl, sniper_pnl, total_pnl)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (time.time(), today, mispricing, near_resolved, correlation, sniper, total),
                )
            c.commit()
    except Exception as _e:
        _db_log.warning("db.upsert_daily_pnl failed: %s", _e)


def get_daily_pnl_history(days: int = 30) -> list[dict]:
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT * FROM daily_pnl ORDER BY ts DESC LIMIT ?", (days,)
            ).fetchall()
            return [dict(r) for r in reversed(rows)]
    except Exception:
        return []


def get_strategy_pnl_totals() -> dict:
    """Return cumulative P&L per strategy from daily_pnl table."""
    try:
        with _conn() as c:
            row = c.execute("""
                SELECT
                    SUM(mispricing_pnl)    as mispricing,
                    SUM(near_resolved_pnl) as near_resolved,
                    SUM(correlation_pnl)   as correlation,
                    SUM(sniper_pnl)        as sniper
                FROM daily_pnl
            """).fetchone()
            return {
                "mispricing":    round(float(row["mispricing"]    or 0), 4),
                "near_resolved": round(float(row["near_resolved"] or 0), 4),
                "correlation":   round(float(row["correlation"]   or 0), 4),
                "sniper":        round(float(row["sniper"]        or 0), 4),
            }
    except Exception:
        return {"mispricing": 0.0, "near_resolved": 0.0, "correlation": 0.0, "sniper": 0.0}
