"""Position tracking and P&L management."""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from rich.console import Console
from rich.table import Table

from src.utils.api import DataClient

console = Console()


@dataclass
class Position:
    """An open position on Polymarket."""
    condition_id: str
    title: str
    outcome: str  # "Yes" or "No"
    size: float
    avg_price: float
    current_price: float
    initial_value: float
    current_value: float
    cash_pnl: float
    percent_pnl: float
    slug: str = ""
    token_id: str = ""
    outcome_index: int = 0
    is_redeemable: bool = False
    is_mergeable: bool = False

    @property
    def unrealized_pnl(self) -> float:
        return self.current_value - self.initial_value

    @property
    def roi_pct(self) -> float:
        return self.percent_pnl

    def to_dict(self) -> dict:
        return {
            "condition_id": self.condition_id,
            "title": self.title,
            "outcome": self.outcome,
            "size": self.size,
            "avg_price": self.avg_price,
            "current_price": self.current_price,
            "initial_value": self.initial_value,
            "current_value": self.current_value,
            "cash_pnl": self.cash_pnl,
            "percent_pnl": self.percent_pnl,
        }


class PositionTracker:
    """Tracks open positions and calculates P&L."""

    def __init__(self, address: str, data_client: DataClient | None = None):
        self.address = address
        self.data = data_client or DataClient()
        self._position_cache: dict[str, Position] = {}
        self._last_refresh: float = 0

    def refresh_positions(self, force: bool = False) -> list[Position]:
        """Fetch current positions from Polymarket Data API."""
        # Cache for 60 seconds
        if not force and time.time() - self._last_refresh < 60 and self._position_cache:
            return list(self._position_cache.values())

        try:
            raw_positions = self.data.get_positions(self.address, limit=500)
            positions = []

            for p in raw_positions:
                try:
                    pos = Position(
                        condition_id=p.get("conditionId", ""),
                        title=p.get("title", ""),
                        outcome=p.get("outcome", ""),
                        size=float(p.get("size", 0) or 0),
                        avg_price=float(p.get("avgPrice", 0) or 0),
                        current_price=float(p.get("curPrice", 0) or 0),
                        initial_value=float(p.get("initialValue", 0) or 0),
                        current_value=float(p.get("currentValue", 0) or 0),
                        cash_pnl=float(p.get("cashPnl", 0) or 0),
                        percent_pnl=float(p.get("percentPnl", 0) or 0),
                        slug=p.get("slug", ""),
                        token_id=p.get("asset", ""),
                        outcome_index=int(p.get("outcomeIndex", 0) or 0),
                        is_redeemable=bool(p.get("redeemable", False)),
                        is_mergeable=bool(p.get("mergeable", False)),
                    )

                    if pos.size > 0:
                        positions.append(pos)
                        self._position_cache[pos.condition_id] = pos

                except (ValueError, TypeError) as e:
                    console.print(f"[yellow]Skipping malformed position: {e}[/]")
                    continue

            self._last_refresh = time.time()
            return positions

        except Exception as e:
            console.print(f"[red]Error fetching positions: {e}[/]")
            return list(self._position_cache.values())

    def get_portfolio_value(self) -> dict:
        """Get total portfolio value and P&L."""
        try:
            value_data = self.data.get_value(self.address)
            return {
                "total_value": float(value_data.get("value", 0) or 0),
                "total_pnl": float(value_data.get("pnl", 0) or 0),
            }
        except Exception as e:
            console.print(f"[red]Error fetching portfolio value: {e}[/]")
            return {"total_value": 0.0, "total_pnl": 0.0}

    def get_closed_positions(self, limit: int = 50) -> list[dict]:
        """Get closed/settled positions."""
        try:
            return self.data.get_closed_positions(self.address, limit=limit)
        except Exception as e:
            console.print(f"[red]Error fetching closed positions: {e}[/]")
            return []

    def get_position(self, condition_id: str) -> Position | None:
        """Get a specific position by condition ID."""
        self.refresh_positions()
        return self._position_cache.get(condition_id)

    def save_snapshot(self, filepath: str = "logs/positions_snapshot.json") -> None:
        """Save current positions to a JSON file."""
        positions = self.refresh_positions(force=True)
        data = {
            "timestamp": time.time(),
            "address": self.address,
            "positions": [p.to_dict() for p in positions],
            "portfolio": self.get_portfolio_value(),
        }

        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2, default=str)

        console.print(f"[green]Position snapshot saved to {filepath}[/]")

    def display_positions(self, positions: list[Position] | None = None) -> None:
        """Display positions in a rich table."""
        if positions is None:
            positions = self.refresh_positions(force=True)

        if not positions:
            console.print("[yellow]No open positions.[/]")
            return

        table = Table(title="📊 Open Positions")
        table.add_column("Market", style="cyan", max_width=40, no_wrap=True)
        table.add_column("Side", justify="center")
        table.add_column("Size", justify="right")
        table.add_column("Avg Price", justify="right")
        table.add_column("Current", justify="right")
        table.add_column("Value", justify="right")
        table.add_column("P&L", justify="right")
        table.add_column("ROI", justify="right")

        total_value = 0.0
        total_pnl = 0.0

        for pos in positions:
            pnl_style = "green" if pos.cash_pnl >= 0 else "red"
            table.add_row(
                pos.title[:40],
                pos.outcome,
                f"{pos.size:.1f}",
                f"${pos.avg_price:.3f}",
                f"${pos.current_price:.3f}",
                f"${pos.current_value:.2f}",
                f"[{pnl_style}]${pos.cash_pnl:+.2f}[/{pnl_style}]",
                f"[{pnl_style}]{pos.percent_pnl:+.1f}%[/{pnl_style}]",
            )
            total_value += pos.current_value
            total_pnl += pos.cash_pnl

        console.print(table)
        console.print(
            f"\n[bold]Total Value:[/] ${total_value:.2f} | "
            f"[bold]Total P&L:[/] ${total_pnl:+.2f}"
        )