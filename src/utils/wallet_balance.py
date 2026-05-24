"""Wallet balance checker and USDC conversion utilities."""

from web3 import Web3
from src.config import config

# Polygon RPC fallbacks
POLYGON_RPCS = [
    "https://polygon.drpc.org",
    "https://polygon.llamarpc.com",
    "https://polygon-mainnet.public.blastapi.io",
]

# Known token contracts on Polygon
TOKENS = {
    "POL": {"address": None, "decimals": 18},  # Native token
    "USDC_NATIVE": {"address": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "decimals": 6},
    "USDC_E": {"address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "decimals": 6},
    "USDT": {"address": "0xc2132D05D31c914a87C6611C10734AE09705A038", "decimals": 6},
    "DAI": {"address": "0x8f3Cf7ad23Cd3DaDb19385244216a8c2918AAAC9", "decimals": 18},
    "WPOL": {"address": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270", "decimals": 18},
    "WETH": {"address": "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9ef6f", "decimals": 18},
}

# ERC20 balanceOf(address) + decimals() + symbol() ABI fragments
ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
]


def get_w3() -> Web3:
    """Get a connected Web3 instance for Polygon."""
    for rpc in POLYGON_RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    raise ConnectionError("Could not connect to any Polygon RPC")


def get_all_balances(address: str) -> dict[str, float]:
    """Get all token balances for an address on Polygon.

    Returns:
        dict mapping token name to human-readable balance
    """
    w3 = get_w3()
    address = Web3.to_checksum_address(address)
    balances = {}

    # POL (native)
    pol_wei = w3.eth.get_balance(address)
    balances["POL"] = float(Web3.from_wei(pol_wei, "ether"))

    # ERC20 tokens
    padded_addr = address[2:].zfill(64)
    for name, info in TOKENS.items():
        if name == "POL" or info["address"] is None:
            continue
        try:
            contract = Web3.to_checksum_address(info["address"])
            result = w3.eth.call({"to": contract, "data": f"0x70a08231{padded_addr}"})
            raw = int(result.hex(), 16)
            balances[name] = raw / (10 ** info["decimals"])
        except Exception:
            balances[name] = 0.0

    return balances


def print_wallet_status(address: str) -> None:
    """Print a formatted wallet status report."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    address = Web3.to_checksum_address(address)

    console.print(f"\n[bold cyan]🔫 Wallet Status[/]")
    console.print(f"Address: {address}")

    try:
        balances = get_all_balances(address)

        table = Table(title="Token Balances (Polygon)")
        table.add_column("Token", style="cyan")
        table.add_column("Balance", justify="right")
        table.add_column("Value (est.)", justify="right")

        for token, balance in sorted(balances.items()):
            if balance > 0:
                style = "bold green" if balance > 0 else "dim"
                table.add_row(token, f"{balance:.6f}", "-", style=style)

        # Show tokens with balance
        table.add_row("", "", "")

        # Check specifically what Polymarket needs
        usdc_native = balances.get("USDC_NATIVE", 0)
        usdc_e = balances.get("USDC_E", 0)
        pol = balances.get("POL", 0)

        total_usdc = usdc_native + usdc_e
        table.add_row("Total USDC", f"{total_usdc:.2f}", "~$"+f"{total_usdc:.2f}", style="bold yellow")
        table.add_row("POL (gas)", f"{pol:.8f}", "", style="bold")

        console.print(table)

        # Warnings
        if pol < 0.01:
            console.print("\n[yellow]⚠️  Wallet has no POL for gas. Polymarket's Relayer covers gas for trading, but you may need a small amount for certain operations.[/]")

        if usdc_e == 0 and usdc_native > 0:
            console.print(f"\n[yellow]⚠️  You have {usdc_native:.2f} native USDC but Polymarket uses USDC.e (bridged).[/]")
            console.print("[yellow]You need to swap native USDC → USDC.e to trade on Polymarket.[/]")
            console.print("[cyan]Options:[/]")
            console.print("  1. Use QuickSwap/Uniswap on Polygon to swap (needs ~0.01 POL for gas)")
            console.print("  2. Polymarket may auto-convert when depositing (check their UI)")
            console.print("  3. Send a tiny bit of POL (0.01-0.05) to this wallet for gas, then swap")

    except Exception as e:
        console.print(f"[red]Error checking balances: {e}[/]")


def check_usdc_type(address: str) -> dict:
    """Check which USDC type the wallet holds and return details.

    Returns:
        dict with 'usdc_native', 'usdc_e', 'needs_swap' fields
    """
    balances = get_all_balances(address)
    usdc_native = balances.get("USDC_NATIVE", 0)
    usdc_e = balances.get("USDC_E", 0)

    return {
        "usdc_native": usdc_native,
        "usdc_e": usdc_e,
        "total_usdc": usdc_native + usdc_e,
        "pol_for_gas": balances.get("POL", 0),
        "needs_swap": usdc_native > 0 and usdc_e == 0,
        "has_gas": balances.get("POL", 0) > 0.005,
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        addr = sys.argv[1]
    else:
        # Try to load from wallet
        try:
            from src.wallet.wallet import load_wallet
            wallet = load_wallet()
            addr = wallet["address"]
        except FileNotFoundError:
            print("Usage: python -m src.utils.wallet_balance <address>")
            print("Or create a wallet first: python -m src.wallet.create_wallet")
            sys.exit(1)

    print_wallet_status(addr)