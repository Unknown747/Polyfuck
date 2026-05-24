"""Auto-redemption module — detects resolved markets and redeems winning shares to USDC."""

from src.redemption.redemption import AutoRedeemer, RedemptionResult

__all__ = ["AutoRedeemer", "RedemptionResult"]
