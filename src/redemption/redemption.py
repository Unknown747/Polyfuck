"""Auto-redemption module.

Automatically detects resolved Polymarket markets where the bot holds winning
conditional tokens, and calls the Gnosis CTF redeemPositions() contract to
convert those tokens back to USDC.

On-chain flow:
  1. Fetch positions via Data API, filter is_redeemable=True
  2. For each redeemable position, call CTF.redeemPositions(
         collateralToken=USDC_BRIDGED,
         parentCollectionId=bytes32(0),
         conditionId=<market conditionId>,
         indexSets=[1]  (YES) or [2]  (NO)
     )
  3. Log redeemed amounts and notify the trader module to update exposure.

Dry-run mode skips the on-chain call and logs what would happen.
"""

import time
import logging
from dataclasses import dataclass, field

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
            {"name": "collateralToken",      "type": "address"},
            {"name": "parentCollectionId",   "type": "bytes32"},
            {"name": "conditionId",          "type": "bytes32"},
            {"name": "indexSets",            "type": "uint256[]"},
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
    condition_id:   str
    market_title:   str
    outcome:        str
    shares:         float
    estimated_usdc: float
    status:         str     # "success" | "dry_run" | "failed" | "skipped"
    tx_hash:        str = ""
    error:          str = ""
    timestamp: float = field(default_factory=time.time)

    @property
    def succeeded(self) -> bool:
        return self.status in ("success", "dry_run")


class AutoRedeemer:
    """Detects resolved markets and redeems winning shares → USDC.

    Usage:
        redeemer = AutoRedeemer(address, private_key)
        results  = redeemer.run(positions)
        total    = sum(r.estimated_usdc for r in results if r.succeeded)
    """

    _GAS_LIMIT       = 200_000
    _TX_CONFIRMATIONS = 2

    def __init__(
        self,
        address:     str,
        private_key: str | None = None,
        tracker:     PositionTracker | None = None,
    ):
        self.address     = address
        self.private_key = private_key or config.PRIVATE_KEY
        self.tracker     = tracker
        self._redemption_log:    list[RedemptionResult] = []
        self._total_redeemed_usd: float = 0.0
        self._w3             = None
        self._ctf_contract   = None

    # ── Public API ──────────────────────────────────────────────────────────

    def run(self, positions: list[Position] | None = None) -> list[RedemptionResult]:
        """Scan positions for redeemable markets and process them all.

        Args:
            positions: Pre-fetched list. If None, fetches from API.
        Returns:
            List of RedemptionResult for every processed position.
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
            f"\n[bold cyan]🔄 Auto-Redemption:[/] "
            f"found {len(redeemable)} redeemable position"
            f"{'s' if len(redeemable) != 1 else ''}"
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

    def check_redeemable(self, positions: list[Position] | None = None) -> list[Position]:
        """Return positions ready to redeem without executing anything."""
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

    # ── Private ─────────────────────────────────────────────────────────────

    def _redeem_position(self, pos: Position) -> RedemptionResult:
        """Attempt to redeem a single resolved position."""
        # BUG FIX: was max(current_price, 1.0) which could overestimate if
        # price > 1.0. Winning CTF positions always pay exactly $1.00/share.
        estimated_usdc = pos.size * 1.0

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
            result.error  = "missing condition_id — cannot call redeemPositions"
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
            result.error  = "POLY_PRIVATE_KEY not set in Replit Secrets"
            return result

        try:
            w3  = self._get_web3()
            ctf = self._get_ctf_contract(w3)

            condition_bytes = self._hex_to_bytes32(pos.condition_id)
            # YES = indexSet 1 (binary 01), NO = indexSet 2 (binary 10)
            index_set = 1 if pos.outcome.lower() in ("yes", "1", "true") else 2

            nonce     = w3.eth.get_transaction_count(self.address)
            gas_price = self._get_gas_params(w3)

            tx = ctf.functions.redeemPositions(
                config.USDC_BRIDGED,    # collateralToken (USDC.e on Polygon)
                bytes(32),              # parentCollectionId = bytes32(0)
                condition_bytes,        # conditionId
                [index_set],            # indexSets
            ).build_transaction({
                "from":    self.address,
                "nonce":   nonce,
                "gas":     self._GAS_LIMIT,
                "chainId": config.CHAIN_ID,
                **gas_price,
            })

            signed  = w3.eth.account.sign_transaction(tx, private_key=self.private_key)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            tx_hex  = tx_hash.hex()

            receipt = w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=120, poll_latency=3
            )

            if receipt.status == 1:
                result.status  = "success"
                result.tx_hash = tx_hex
                logger.info(
                    "Redeemed %.2f shares (%s) from %s → $%.2f USDC | tx %s",
                    pos.size, pos.outcome, pos.title[:40], estimated_usdc, tx_hex[:16],
                )
                console.print(
                    f"  [bold green]✅ Redeemed[/] {pos.size:.2f} shares "
                    f"({pos.outcome}) [cyan]{pos.title[:50]}[/] "
                    f"→ [bold green]${estimated_usdc:.2f} USDC[/] "
                    f"| tx {tx_hex[:12]}..."
                )
            else:
                result.status = "failed"
                result.error  = f"transaction reverted (tx {tx_hex[:12]})"
                console.print(f"  [red]❌ Redemption reverted[/] for {pos.title[:40]}")

        except Exception as e:
            result.status = "failed"
            result.error  = str(e)
            logger.error("Redemption failed for %s: %s", pos.condition_id, e)
            console.print(f"  [red]Redemption error:[/] {e}")

        return result

    def _get_web3(self):
        """Return a cached, connected Web3 instance.

        BUG FIX: was using a single RPC (polygon-rpc.com) which is unreachable
        from Replit servers. Now uses the ordered fallback list from config.
        """
        if self._w3 is not None and self._w3.is_connected():
            return self._w3

        from web3 import Web3
        for rpc in config.RPC_FALLBACKS:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
                if w3.is_connected():
                    logger.debug("Web3 connected via %s", rpc)
                    self._w3 = w3
                    # Invalidate cached contract when we get a new Web3 instance
                    self._ctf_contract = None
                    return w3
            except Exception:
                continue

        raise ConnectionError(
            f"Cannot connect to any Polygon RPC. Tried: {config.RPC_FALLBACKS}"
        )

    def _get_ctf_contract(self, w3):
        """Return a cached CTF contract instance."""
        if self._ctf_contract is None:
            from web3 import Web3
            self._ctf_contract = w3.eth.contract(
                address=Web3.to_checksum_address(config.CTF_CONTRACT),
                abi=_CTF_ABI,
            )
        return self._ctf_contract

    def _get_gas_params(self, w3) -> dict:
        """Build EIP-1559 gas params with a 1.3× safety buffer.

        BUG FIX: was using legacy gasPrice field. Polygon supports EIP-1559
        and it's significantly more reliable than legacy pricing. Legacy txs
        can get stuck when base fee spikes.
        """
        try:
            base_fee = w3.eth.gas_price
            return {
                "maxFeePerGas":        int(base_fee * 1.3),
                "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
            }
        except Exception:
            # Fallback to legacy pricing if fee history is unavailable
            return {"gasPrice": 50_000_000_000}   # 50 gwei

    @staticmethod
    def _hex_to_bytes32(hex_str: str) -> bytes:
        """Convert a 0x-prefixed hex string to exactly 32 bytes (left zero-padded)."""
        clean  = hex_str.removeprefix("0x")
        padded = clean.zfill(64)          # left-pad to 64 hex chars = 32 bytes
        return bytes.fromhex(padded)

    def _display_results(self, results: list[RedemptionResult]) -> None:
        if not results:
            return

        table = Table(title="🔄 Redemption Results")
        table.add_column("Market",  style="cyan", max_width=45, no_wrap=True)
        table.add_column("Side",    justify="center")
        table.add_column("Shares",  justify="right")
        table.add_column("USDC",    justify="right", style="green")
        table.add_column("Status",  justify="center")
        table.add_column("TX",      style="dim", max_width=14, no_wrap=True)

        total_usdc = 0.0
        for r in results:
            status_fmt = {
                "success": "[bold green]✅ success[/]",
                "dry_run": "[yellow]🔍 dry run[/]",
                "failed":  "[red]❌ failed[/]",
                "skipped": "[dim]⏭ skipped[/]",
            }.get(r.status, r.status)

            table.add_row(
                r.market_title[:45],
                r.outcome,
                f"{r.shares:.2f}",
                f"${r.estimated_usdc:.2f}",
                status_fmt,
                (r.tx_hash[:12] + "..." if r.tx_hash
                 else r.error[:14] if r.error else "—"),
            )
            if r.succeeded:
                total_usdc += r.estimated_usdc

        console.print(table)
        if total_usdc > 0:
            console.print(f"[bold green]Total redeemable this run: ${total_usdc:.2f} USDC[/]")
