"""Logging and formatting utilities."""

import json
from datetime import datetime, timezone
from rich.console import Console

console = Console()


def format_usd(amount: float) -> str:
    """Format a number as USD."""
    if abs(amount) >= 1_000_000:
        return f"${amount/1_000_000:.2f}M"
    elif abs(amount) >= 1_000:
        return f"${amount/1_000:.1f}K"
    else:
        return f"${amount:.2f}"


def format_pct(pct: float) -> str:
    """Format a percentage."""
    return f"{pct:.1f}%"


def format_address(address: str) -> str:
    """Format an Ethereum address for display."""
    if not address:
        return "N/A"
    return f"{address[:8]}...{address[-6:]}"


def ts_now() -> str:
    """Current UTC timestamp as ISO string."""
    return datetime.now(timezone.utc).isoformat()


def parse_outcome_prices(raw) -> list[float] | None:
    """Parse outcomePrices field which may be double-encoded JSON."""
    if not raw:
        return None

    if isinstance(raw, str):
        try:
            prices = json.loads(raw)
        except json.JSONDecodeError:
            return None
    elif isinstance(raw, list):
        prices = raw
    else:
        return None

    try:
        return [float(p) for p in prices]
    except (ValueError, TypeError):
        return None


def parse_token_ids(raw) -> dict[str, str]:
    """Parse clobTokenIds field."""
    if not raw:
        return {}

    if isinstance(raw, str):
        try:
            tokens = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    elif isinstance(raw, list):
        tokens = raw
    else:
        return {}

    if len(tokens) >= 2:
        return {"yes": tokens[0], "no": tokens[1]}
    return {}


def save_json(data: dict, filepath: str) -> None:
    """Save data to a JSON file."""
    from pathlib import Path
    Path(filepath).parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2, default=str)