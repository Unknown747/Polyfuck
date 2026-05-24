"""Auto-redemption module.

Automatically detects resolved Polymarket markets where the bot holds winning
conditional tokens, and calls the Gnosis CTF redeemPositions() contract function
to convert those tokens back to USDC.

On-chain flow:
  1. Fetch positions via Data API, filter is_redeemable=True
  2. For each redeemable position, call CTF.redeemPositions(
         collateralToken=USDC_BRIDGED,
         parentCollectionId=bytes32(0),
         conditionId=<market conditionId>,
         indexSets=[1]  (YES) or [2]  (NO)
     )
  3. Log redeemed amounts and notify the trader module to update exposure.

Dry-run mode skips the on-chain call and just prints what would happen.
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.table import Table

from src.config import config
from src.positions.positions import Position, PositionTracker

logger = logging.getLogger("polymarket-bot")
console = Console()

# Gnosis Conditional Token Framework ABI (minimal — only redeemPositions)
_CTF_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "name": "payoutDenominator",
        "type": "function",
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]

# ERC-20 balanceOf ABI for checking post-redemption USDC balance
_ERC20_BALANCE_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    }
]


@dataclass
class RedemptionResult:
    """Result of a single redemption attempt."""
    condition_id: str
    market_title: str
    outcome: str
    shares: float
    estimated_usdc: float
    status: str         # "success", "dry_run", "failed", "skipped"
    tx_hash: str = ""
    error: str = ""
    timestamp: float = field(default_factory=time.time)

    @property
    def succeeded(self) -> bool:
        return self.status in ("success", "dry_run")


class AutoRedeemer:
    """Detects resolved markets and redeems winning shares back to USDC.

    Usage:
        redeemer = AutoRedeemer(address, private_key)
        results = redeemer.run(positions)
        total_usd = sum(r.estimated_usdc for r in results if r.succeeded)
    """

    # Number of blocks to wait for tx confirmation
    _TX_CONFIRMATIONS = 2
    # Gas limit for redeemPositions (empirically safe upper bound)
    _GAS_LIMIT = 200_000

    def __init__(
        self,
        address: str,
        private_key: str | None = None,
        tracker: PositionTracker | None = None,
    ):
        self.address = address
        self.private_key = private_key or config.PRIVATE_KEY
        self.tracker = tracker
        self._redemption_log: list[RedemptionResult] = []
        self._total_redeemed_usd: float = 0.0
        self._w3 = None
        self._ctf_contract = None

    def run(self, positions: list[Position] | None = None) -> list[RedemptionResult]:
        """Scan positions for redeemable markets and process them.

        Args:
            positions: Pre-fetched positions list. If None, fetches from API.

        Returns:
            List of RedemptionResult for each processed position.
        """
        if positions is None:
            if not self.tracker:
                console.print("[yellow]AutoRedeemer: no tracker or positions provided.[/]")
                return []
            positions = self.tracker.refresh_positions(force=True)

        redeemable = [p for p in positions if p.is_redeemable and p.size > 0]

        if not redeemable:
            console.print("[dim]AutoRedeemer: no redeemable positions found.[/]")
            return []

        console.print(
            f"\n[bold cyan]🔄 Auto-Redemption:[/] found {len(redeemable)} "
            f"redeemable position{'s' if len(redeemable) != 1 else ''}"
        )

        results: list[RedemptionResult] = []

        for pos in redeemable:
            result = self._redeem_position(pos)
            results.append(result)

            if result.succeeded:
                self._total_redeemed_usd += result.estimated_usdc
                self._redemption_log.append(result)

        self._display_results(results)
        return results

    def check_redeemable(
        self, positions: list[Position] | None = None
    ) -> list[Position]:
        """Return positions that are ready to redeem without executing."""
        if positions is None:
            if not self.tracker:
                return []
            positions = self.tracker.refresh_positions()
        return [p for p in positions if p.is_redeemable and p.size > 0]

    def get_total_redeemed(self) -> float:
        """Total USDC recovered via redemption this session."""
        return self._total_redeemed_usd

    def get_redemption_log(self) -> list[RedemptionResult]:
        return self._redemption_log.copy()

    # === Private ===

    def _redeem_position(self, pos: Position) -> RedemptionResult:
        """Attempt to redeem a single resolved position."""
        # Winning side pays $1.00 per share; losing side pays $0.00
        # current_price on a resolved market is 1.0 (win) or 0.0 (loss).
        # We only reach here if is_redeemable=True, so price should be ~1.0
        estimated_usdc = pos.size * max(pos.current_price, 1.0)

        result = RedemptionResult(
            condition_id=pos.condition_id,
            market_title=pos.title,
            outcome=pos.outcome,
            shares=pos.size,
            estimated_usdc=estimated_usdc,
            status="skipped",
        )

        if not pos.condition_id:
            result.status = "failed"
            result.error = "Missing condition_id"
            return result

        if config.DRY_RUN:
            console.print(
                f"  [yellow]DRY RUN[/] Would redeem {pos.size:.2f} shares "
                f"({pos.outcome}) from [cyan]{pos.title[:50]}[/] "
                f"≈ [green]${estimated_usdc:.2f} USDC[/]"
            )
            result.status = "dry_run"
            return result

        if not self.private_key:
            result.status = "failed"
            result.error = "No private key configured"
            return result

        try:
            w3 = self._get_web3()
            ctf = self._get_ctf_contract(w3)

            # conditionId must be bytes32
            condition_bytes = self._hex_to_bytes32(pos.condition_id)

            # Determine indexSet: YES=1 (binary 01), NO=2 (binary 10)
            index_set = 1 if pos.outcome.lower() in ("yes", "1", "true") else 2

            # Build transaction
            nonce = w3.eth.get_transaction_count(self.address)
            gas_price = self._get_gas_price(w3)

            tx = ctf.functions.redeemPositions(
                config.USDC_BRIDGED,          # collateralToken (USDC.e)
                bytes(32),                    # parentCollectionId = bytes32(0)
                condition_bytes,              # conditionId
                [index_set],                  # indexSets
            ).build_transaction({
                "from": self.address,
                "nonce": nonce,
                "gas": self._GAS_LIMIT,
                "gasPrice": gas_price,
                "chainId": config.CHAIN_ID,
            })

            # Sign and send
            signed = w3.eth.account.sign_transaction(tx, private_key=self.private_key)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            tx_hex = tx_hash.hex()

            # Wait for confirmation
            receipt = w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=120, poll_latency=3
            )

            if receipt.status == 1:
                result.status = "success"
                result.tx_hash = tx_hex
                logger.info(
                    "Redeemed %.2f shares (%s) from %s → ~$%.2f USDC | tx: %s",
                    pos.size, pos.outcome, pos.title[:40], estimated_usdc, tx_hex[:16]
                )
                console.print(
                    f"  [bold green]✅ Redeemed[/] {pos.size:.2f} shares "
                    f"({pos.outcome}) from [cyan]{pos.title[:50]}[/] "
                    f"→ [bold green]${estimated_usdc:.2f} USDC[/] "
                    f"| tx: {tx_hex[:12]}..."
                )
            else:
                result.status = "failed"
                result.error = f"Transaction reverted (tx: {tx_hex[:12]})"
                console.print(f"  [red]Redemption reverted[/] for {pos.title[:40]}")

        except Exception as e:
            result.status = "failed"
            result.error = str(e)
            logger.error("Redemption failed for %s: %s", pos.condition_id, e)
            console.print(f"  [red]Redemption error:[/] {e}")

        return result

    def _get_web3(self):
        """Return a cached Web3 instance."""
        if self._w3 is None:
            from web3 import Web3
            self._w3 = Web3(Web3.HTTPProvider(config.RPC_URL, request_kwargs={"timeout": 30}))
            if not self._w3.is_connected():
                raise ConnectionError(f"Cannot connect to RPC at {config.RPC_URL}")
        return self._w3

    def _get_ctf_contract(self, w3):
        """Return a cached CTF contract instance."""
        if self._ctf_contract is None:
            from web3 import Web3
            self._ctf_contract = w3.eth.contract(
                address=Web3.to_checksum_address(config.CTF_CONTRACT),
                abi=_CTF_ABI,
            )
        return self._ctf_contract

    def _get_gas_price(self, w3) -> int:
        """Get current gas price with a small safety buffer (1.1×)."""
        try:
            base = w3.eth.gas_price
            return int(base * 1.1)
        except Exception:
            return 50_000_000_000  # 50 gwei fallback

    @staticmethod
    def _hex_to_bytes32(hex_str: str) -> bytes:
        """Convert a 0x-prefixed hex string to a 32-byte value."""
        clean = hex_str.removeprefix("0x")
        padded = clean.zfill(64)
        return bytes.fromhex(padded)

    def _display_results(self, results: list[RedemptionResult]) -> None:
        """Display redemption results as a Rich table."""
        if not results:
            return

        table = Table(title="🔄 Redemption Results")
        table.add_column("Market", style="cyan", max_width=45, no_wrap=True)
        table.add_column("Side", justify="center")
        table.add_column("Shares", justify="right")
        table.add_column("USDC", justify="right", style="green")
        table.add_column("Status", justify="center")
        table.add_column("TX", style="dim", max_width=14, no_wrap=True)

        total_usdc = 0.0
        for r in results:
            status_fmt = {
                "success": "[bold green]✅ success[/]",
                "dry_run": "[yellow]🔍 dry run[/]",
                "failed": "[red]❌ failed[/]",
                "skipped": "[dim]⏭ skipped[/]",
            }.get(r.status, r.status)

            table.add_row(
                r.market_title[:45],
                r.outcome,
                f"{r.shares:.2f}",
                f"${r.estimated_usdc:.2f}",
                status_fmt,
                r.tx_hash[:12] + "..." if r.tx_hash else (r.error[:12] if r.error else "—"),
            )
            if r.succeeded:
                total_usdc += r.estimated_usdc

        console.print(table)
        if total_usdc > 0:
            console.print(f"[bold green]Total redeemed this run: ${total_usdc:.2f} USDC[/]")
