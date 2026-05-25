"""SQLite trade history — persistent across restarts."""

import sqlite3
import time
from pathlib import Path

_DB_PATH = Path("logs/trades.db")


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they do not exist."""
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
        c.commit()


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
                    trade.condition_id,
                    trade.token_id,
                    trade.side,
                    trade.price,
                    trade.size,
                    trade.order_type,
                    trade.status,
                    trade.fee_estimate,
                    category,
                    strategy,
                ),
            )
            c.commit()
    except Exception:
        pass


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
            return {"total": total, "executed": executed, "dry_run": dry}
    except Exception:
        return {"total": 0, "executed": 0, "dry_run": 0}
