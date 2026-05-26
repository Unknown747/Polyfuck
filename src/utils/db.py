"""SQLite persistence — trades, opportunities, and daily P&L snapshots.

Every row is tagged with `mode` ('live') so all stats reflect real trades.
"""

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
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    """Create all tables if they do not exist, and migrate existing schemas."""
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
                strategy     TEXT,
                mode         TEXT    NOT NULL DEFAULT 'live'
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
                executed  INTEGER DEFAULT 0,
                mode      TEXT    NOT NULL DEFAULT 'live'
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_pnl (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                REAL    NOT NULL,
                date              TEXT    NOT NULL,
                mode              TEXT    NOT NULL DEFAULT 'live',
                mispricing_pnl    REAL    DEFAULT 0,
                near_resolved_pnl REAL    DEFAULT 0,
                correlation_pnl   REAL    DEFAULT 0,
                sniper_pnl        REAL    DEFAULT 0,
                total_pnl         REAL    DEFAULT 0
            )
        """)

        # ── Migrate existing databases that lack the `mode` column ──────────
        for table in ("trades", "opportunities", "daily_pnl"):
            try:
                c.execute(
                    f"ALTER TABLE {table} ADD COLUMN mode TEXT NOT NULL DEFAULT 'live'"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists — safe to ignore


# ── Trades ────────────────────────────────────────────────────────────────────

def insert_trade(
    trade,
    category: str = "",
    strategy: str = "",
    mode: str = "live",
) -> None:
    """Persist a Trade dataclass record tagged with `mode`."""
    try:
        with _conn() as c:
            c.execute(
                """INSERT INTO trades
                   (ts, timestamp, market, condition_id, token_id, side,
                    price, size, order_type, status, fee_estimate, category, strategy, mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    mode,
                ),
            )
            c.commit()
    except Exception as _e:
        _db_log.warning("db.insert_trade failed: %s", _e)


def get_trades(limit: int = 50, mode: str | None = None) -> list[dict]:
    """Return the most recent trades, optionally filtered by mode."""
    try:
        with _conn() as c:
            if mode:
                rows = c.execute(
                    "SELECT * FROM trades WHERE mode=? ORDER BY ts DESC LIMIT ?",
                    (mode, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def get_db_stats(mode: str | None = None) -> dict:
    """Summary counts from the trades table, optionally filtered by mode."""
    try:
        with _conn() as c:
            where = "WHERE mode=?" if mode else ""
            params = (mode,) if mode else ()

            total = c.execute(
                f"SELECT COUNT(*) FROM trades {where}", params
            ).fetchone()[0]

            strat_where = "WHERE mode=?" if mode else ""
            rows = c.execute(
                f"SELECT strategy, COUNT(*) as cnt FROM trades {strat_where} GROUP BY strategy",
                params,
            ).fetchall()
            by_strategy = {r["strategy"] or "unknown": r["cnt"] for r in rows}

            return {
                "total": total,
                "executed": total,
                "by_strategy": by_strategy,
                "mode": mode or "all",
            }
    except Exception:
        return {"total": 0, "executed": 0, "by_strategy": {}, "mode": mode or "all"}


# ── Opportunities ─────────────────────────────────────────────────────────────

def insert_opportunity(
    strategy: str,
    market: str,
    edge: float,
    executed: bool,
    mode: str = "live",
) -> None:
    try:
        with _conn() as c:
            c.execute(
                """INSERT INTO opportunities (ts, timestamp, strategy, market, edge, executed, mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    time.time(),
                    time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                    strategy,
                    market[:100],
                    edge,
                    1 if executed else 0,
                    mode,
                ),
            )
            c.commit()
    except Exception as _e:
        _db_log.warning("db.insert_opportunity failed: %s", _e)


def get_opportunities(limit: int = 100, mode: str | None = None) -> list[dict]:
    try:
        with _conn() as c:
            if mode:
                rows = c.execute(
                    "SELECT * FROM opportunities WHERE mode=? ORDER BY ts DESC LIMIT ?",
                    (mode, limit),
                ).fetchall()
            else:
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
    mode: str = "live",
) -> None:
    """Insert or update today's per-strategy P&L snapshot."""
    import datetime
    today = datetime.date.today().isoformat()
    total = mispricing + near_resolved + correlation + sniper
    try:
        with _conn() as c:
            existing = c.execute(
                "SELECT id FROM daily_pnl WHERE date=? AND mode=?", (today, mode)
            ).fetchone()
            if existing:
                c.execute(
                    """UPDATE daily_pnl SET
                       ts=?, mispricing_pnl=?, near_resolved_pnl=?,
                       correlation_pnl=?, sniper_pnl=?, total_pnl=?
                       WHERE date=? AND mode=?""",
                    (time.time(), mispricing, near_resolved, correlation, sniper, total, today, mode),
                )
            else:
                c.execute(
                    """INSERT INTO daily_pnl
                       (ts, date, mode, mispricing_pnl, near_resolved_pnl,
                        correlation_pnl, sniper_pnl, total_pnl)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (time.time(), today, mode, mispricing, near_resolved, correlation, sniper, total),
                )
            c.commit()
    except Exception as _e:
        _db_log.warning("db.upsert_daily_pnl failed: %s", _e)


def get_daily_pnl_history(days: int = 30, mode: str | None = None) -> list[dict]:
    try:
        with _conn() as c:
            if mode:
                rows = c.execute(
                    "SELECT * FROM daily_pnl WHERE mode=? ORDER BY ts DESC LIMIT ?",
                    (mode, days),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM daily_pnl ORDER BY ts DESC LIMIT ?", (days,)
                ).fetchall()
            return [dict(r) for r in reversed(rows)]
    except Exception:
        return []


def get_strategy_pnl_totals(mode: str | None = None) -> dict:
    """Return cumulative P&L per strategy, optionally restricted to one mode."""
    try:
        with _conn() as c:
            where = "WHERE mode=?" if mode else ""
            params = (mode,) if mode else ()
            row = c.execute(
                f"""SELECT
                    SUM(mispricing_pnl)    as mispricing,
                    SUM(near_resolved_pnl) as near_resolved,
                    SUM(correlation_pnl)   as correlation,
                    SUM(sniper_pnl)        as sniper
                FROM daily_pnl {where}""",
                params,
            ).fetchone()
            return {
                "mispricing":    round(float(row["mispricing"]    or 0), 4),
                "near_resolved": round(float(row["near_resolved"] or 0), 4),
                "correlated":    round(float(row["correlation"]   or 0), 4),
                "sniper":        round(float(row["sniper"]        or 0), 4),
            }
    except Exception:
        return {"mispricing": 0.0, "near_resolved": 0.0, "correlated": 0.0, "sniper": 0.0}
