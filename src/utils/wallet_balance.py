"""Wallet balance checker for Polygon — shows USDC, pUSD, POL, and other tokens."""

from web3 import Web3
from src.config import config

# Public Polygon RPC endpoints (tried in order, first responsive wins)
POLYGON_RPCS = [
    "https://polygon.drpc.org",
    "https://polygon.llamarpc.com",
    "https://polygon-mainnet.public.blastapi.io",
]

# Known tokens on Polygon mainnet
TOKENS = {
    "POL":         {"address": None,                                         "decimals": 18},
    "USDC_NATIVE": {"address": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "decimals": 6},
    "USDC_E":      {"address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "decimals": 6},
    "pUSD":        {"address": "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB", "decimals": 6},
    "USDT":        {"address": "0xc2132D05D31c914a87C6611C10734AE09705A038", "decimals": 6},
    "DAI":         {"address": "0x8f3Cf7ad23Cd3DaDb19385244216a8c2918AAAC9", "decimals": 18},
    "WPOL":        {"address": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270", "decimals": 18},
    "WETH":        {"address": "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9ef6f", "decimals": 18},
}

# BUG FIX: previous ABI was missing balanceOf — added it so contracts can be
# called properly without needing raw eth.call workarounds.
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def get_w3() -> Web3:
    """Return a connected Web3 instance for Polygon (tries multiple RPCs)."""
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
        dict mapping token name → human-readable balance (float)
    """
    w3 = get_w3()
    address = Web3.to_checksum_address(address)
    balances: dict[str, float] = {}

    # Native POL
    pol_wei = w3.eth.get_balance(address)
    balances["POL"] = float(Web3.from_wei(pol_wei, "ether"))

    # ERC-20 tokens — use the proper ABI now that balanceOf is included
    for name, info in TOKENS.items():
        if name == "POL" or info["address"] is None:
            continue
        try:
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(info["address"]),
                abi=ERC20_ABI,
            )
            raw = contract.functions.balanceOf(address).call()
            balances[name] = raw / (10 ** info["decimals"])
        except Exception:
            balances[name] = 0.0

    return balances


def print_wallet_status(address: str) -> None:
    """Print a formatted wallet status report to the console."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    address = Web3.to_checksum_address(address)

    console.print(f"\n[bold cyan]🔫 Wallet Status — Polygon[/]")
    console.print(f"Address: {address}\n")

    try:
        balances = get_all_balances(address)

        table = Table(title="Token Balances")
        table.add_column("Token", style="cyan")
        table.add_column("Balance", justify="right")
        table.add_column("Notes", justify="left", style="dim")

        for name, balance in sorted(balances.items()):
            note = ""
            if name == "USDC_E":
                note = "← Polymarket collateral"
            elif name == "pUSD":
                note = "← Active Polymarket balance"
            elif name == "POL":
                note = "← Gas token"
            if balance > 0:
                table.add_row(name, f"{balance:.6f}", note)

        # BUG FIX: was add_row("","","") — use add_section() for proper separator
        table.add_section()

        usdc_native = balances.get("USDC_NATIVE", 0)
        usdc_e      = balances.get("USDC_E", 0)
        pusd        = balances.get("pUSD", 0)
        pol         = balances.get("POL", 0)
        total_usdc  = usdc_native + usdc_e + pusd

        # BUG FIX: was "~$" + f"${total_usdc:.2f}" → double dollar sign "~$$10.00"
        table.add_row("Total USDC (all forms)", f"{total_usdc:.4f}", f"≈ ${total_usdc:.2f}")
        table.add_row("POL (gas)",              f"{pol:.8f}",        "")

        console.print(table)

        # Actionable warnings
        if pol < 0.01:
            console.print(
                "\n[yellow]⚠️  Very low POL for gas.[/] "
                "Polymarket's relayer covers trading gas, but redemptions and swaps "
                "require a small amount. Consider sending 0.05 POL to this wallet."
            )

        if usdc_e == 0 and usdc_native > 0:
            console.print(
                f"\n[yellow]⚠️  You have {usdc_native:.2f} native USDC but "
                f"Polymarket uses USDC.e (bridged).[/]\n"
                "Swap via: [cyan]python -m src.utils.swap_usdc --dry-run[/]"
            )

        if usdc_e > 0 and pusd == 0:
            console.print(
                f"\n[yellow]⚠️  You have {usdc_e:.2f} USDC.e not yet deposited into Polymarket.[/]\n"
                "Deposit via: [cyan]python -m src.utils.deposit --dry-run[/]"
            )

    except Exception as e:
        console.print(f"[red]Error checking balances: {e}[/]")


def check_usdc_type(address: str) -> dict:
    """Check USDC holdings and return a readiness summary for Polymarket.

    Returns:
        dict with keys: usdc_native, usdc_e, pusd, total_usdc,
                        pol_for_gas, needs_swap, needs_deposit, has_gas
    """
    balances = get_all_balances(address)
    usdc_native = balances.get("USDC_NATIVE", 0)
    usdc_e      = balances.get("USDC_E", 0)
    pusd        = balances.get("pUSD", 0)
    pol         = balances.get("POL", 0)

    return {
        "usdc_native":    usdc_native,
        "usdc_e":         usdc_e,
        "pusd":           pusd,
        "total_usdc":     usdc_native + usdc_e + pusd,
        "pol_for_gas":    pol,
        "needs_swap":     usdc_native > 0 and usdc_e == 0,
        "needs_deposit":  usdc_e > 0 and pusd == 0,
        "has_gas":        pol > 0.005,
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        addr = sys.argv[1]
    else:
        try:
            from src.config import config
            if config.PRIVATE_KEY:
                from src.wallet.wallet import get_address_from_key
                addr = get_address_from_key(config.PRIVATE_KEY)
            else:
                from src.wallet.wallet import load_wallet
                addr = load_wallet()["address"]
        except Exception:
            print("Usage: python -m src.utils.wallet_balance <address>")
            print("Or set POLY_PRIVATE_KEY in Replit Secrets first.")
            sys.exit(1)

    print_wallet_status(addr)
