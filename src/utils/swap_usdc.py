"""Swap native USDC → USDC.e (bridged) on Polygon via Uniswap V3.

Both tokens have 6 decimals and trade ~1:1 on the 0.01% fee tier pool.

Usage:
    python -m src.utils.swap_usdc --dry-run          # Preview without executing
    python -m src.utils.swap_usdc --amount 10        # Swap 10 native USDC
    python -m src.utils.swap_usdc                    # Swap all native USDC
"""

import sys
import time
import argparse
from web3 import Web3

from src.config import config

# ── Polygon mainnet addresses ─────────────────────────────────────────────────
USDC_NATIVE       = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
USDC_E            = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
UNIV3_ROUTER      = Web3.to_checksum_address("0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45")  # SwapRouter02

# Fee tiers to try in order (cheapest first)
FEE_TIERS = [100, 500, 3000]

# ── ABIs ─────────────────────────────────────────────────────────────────────
ERC20_ABI = [
    {"constant": True,  "inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True,  "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_spender", "type": "address"},
                                    {"name": "_value", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True,  "inputs": [{"name": "_owner", "type": "address"},
                                    {"name": "_spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "remaining", "type": "uint256"}], "type": "function"},
]

UNIV3_ROUTER_ABI = [
    {
        "inputs": [{
            "components": [
                {"name": "tokenIn",           "type": "address"},
                {"name": "tokenOut",          "type": "address"},
                {"name": "fee",               "type": "uint24"},
                {"name": "recipient",         "type": "address"},
                {"name": "amountIn",          "type": "uint256"},
                {"name": "amountOutMinimum",  "type": "uint256"},
                {"name": "sqrtPriceLimitX96", "type": "uint160"},
            ],
            "name": "params", "type": "tuple",
        }],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable", "type": "function",
    }
]

_POLYGON_RPCS = [
    "https://polygon.drpc.org",
    "https://polygon.llamarpc.com",
    "https://polygon-mainnet.public.blastapi.io",
]


def get_w3() -> Web3:
    for rpc in _POLYGON_RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    raise ConnectionError("Could not connect to Polygon")


def get_balances(w3: Web3, address: str) -> dict:
    address = Web3.to_checksum_address(address)
    native_c  = w3.eth.contract(address=USDC_NATIVE, abi=ERC20_ABI)
    bridged_c = w3.eth.contract(address=USDC_E,      abi=ERC20_ABI)
    return {
        "usdc_native":     native_c.functions.balanceOf(address).call()  / 1e6,
        "usdc_e":          bridged_c.functions.balanceOf(address).call() / 1e6,
        "pol":             float(Web3.from_wei(w3.eth.get_balance(address), "ether")),
        "usdc_native_raw": native_c.functions.balanceOf(address).call(),
        "usdc_e_raw":      bridged_c.functions.balanceOf(address).call(),
    }


def _approve_if_needed(w3: Web3, pk: str, token_addr: str, spender: str, amount: int) -> None:
    from eth_account import Account
    account = Account.from_key(pk)
    token   = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
    current = token.functions.allowance(account.address, Web3.to_checksum_address(spender)).call()
    if current >= amount:
        print(f"  ✅ Allowance already sufficient ({current / 1e6:.2f})")
        return
    print(f"  Approving USDC native for Uniswap V3 Router...")
    tx = token.functions.approve(Web3.to_checksum_address(spender), 2**256 - 1).build_transaction({
        "from":                account.address,
        "nonce":               w3.eth.get_transaction_count(account.address),
        "gas":                 120_000,
        "maxFeePerGas":        int(w3.eth.gas_price * 1.4),
        "maxPriorityFeePerGas": Web3.to_wei(30, "gwei"),
        "chainId":             137,
    })
    signed  = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  Approval TX: {tx_hash.hex()} — waiting...")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt["status"] != 1:
        raise RuntimeError("Approval transaction failed!")
    print(f"  ✅ Approved (block {receipt['blockNumber']})")


def swap_usdc_native_to_bridged(
    amount_usdc: float, private_key: str, dry_run: bool = False
) -> dict:
    """Swap native USDC → USDC.e on Polygon via Uniswap V3 (tries fee tiers 0.01%, 0.05%, 0.3%)."""
    from eth_account import Account

    w3      = get_w3()
    account = Account.from_key(private_key)
    address = Web3.to_checksum_address(account.address)

    balances   = get_balances(w3, address)
    amount_raw = int(amount_usdc * 1e6)
    min_out    = int(amount_usdc * 0.995 * 1e6)  # 0.5% max slippage

    print(f"\n🔫 Wallet: {address}")
    print(f"  Native USDC : {balances['usdc_native']:.4f}")
    print(f"  USDC.e      : {balances['usdc_e']:.4f}")
    print(f"  POL         : {balances['pol']:.4f}")
    print(f"\n💱 Swap: {amount_usdc:.4f} USDC.native → USDC.e (Uniswap V3)")
    print(f"  Min output  : {min_out / 1e6:.4f} USDC.e (0.5% slippage guard)")

    if balances["usdc_native"] < amount_usdc:
        raise ValueError(f"Insufficient native USDC: have {balances['usdc_native']:.4f}, need {amount_usdc:.4f}")

    if dry_run:
        print(f"\n  [DRY RUN] Would swap {amount_usdc:.4f} USDC.native → USDC.e via Uniswap V3")
        return {"dry_run": True, "amount_in": amount_usdc, "fee_tiers_tried": FEE_TIERS}

    # Step 1: Approve
    print(f"\n1️⃣  Checking approval...")
    _approve_if_needed(w3, private_key, USDC_NATIVE, UNIV3_ROUTER, amount_raw)

    # Step 2: Swap — try each fee tier until one succeeds
    router   = w3.eth.contract(address=UNIV3_ROUTER, abi=UNIV3_ROUTER_ABI)
    deadline = int(time.time()) + 600

    last_error = None
    for fee in FEE_TIERS:
        print(f"\n2️⃣  Trying Uniswap V3 fee tier {fee/10000:.2f}%...")
        try:
            swap_tx = router.functions.exactInputSingle((
                USDC_NATIVE,  # tokenIn
                USDC_E,       # tokenOut
                fee,          # fee
                address,      # recipient
                amount_raw,   # amountIn
                min_out,      # amountOutMinimum
                0,            # sqrtPriceLimitX96
            )).build_transaction({
                "from":                address,
                "nonce":               w3.eth.get_transaction_count(address),
                "gas":                 350_000,
                "maxFeePerGas":        int(w3.eth.gas_price * 1.4),
                "maxPriorityFeePerGas": Web3.to_wei(30, "gwei"),
                "chainId":             137,
                "value":               0,
            })

            signed  = account.sign_transaction(swap_tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            print(f"  TX: {tx_hash.hex()} — waiting for confirmation...")
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt["status"] == 1:
                new_bals = get_balances(w3, address)
                print(f"\n✅ Swap successful! Block {receipt['blockNumber']}, gas {receipt['gasUsed']}")
                print(f"  Native USDC : {balances['usdc_native']:.4f} → {new_bals['usdc_native']:.4f}")
                print(f"  USDC.e      : {balances['usdc_e']:.4f} → {new_bals['usdc_e']:.4f}")
                print(f"\n🎯 Sekarang jalankan: python -m src.utils.deposit")
                return {
                    "success":            True,
                    "tx_hash":            tx_hash.hex(),
                    "block":              receipt["blockNumber"],
                    "gas_used":           receipt["gasUsed"],
                    "fee_tier":           fee,
                    "usdc_native_before": balances["usdc_native"],
                    "usdc_e_before":      balances["usdc_e"],
                    "usdc_native_after":  new_bals["usdc_native"],
                    "usdc_e_after":       new_bals["usdc_e"],
                }
            else:
                print(f"  ❌ Fee tier {fee} reverted, trying next...")
                last_error = f"TX reverted: {tx_hash.hex()}"

        except Exception as exc:
            print(f"  ❌ Fee tier {fee} error: {exc}")
            last_error = str(exc)
            continue

    print(f"\n❌ All fee tiers failed. Last error: {last_error}")
    return {"success": False, "error": last_error}


def main():
    parser = argparse.ArgumentParser(description="Swap native USDC → USDC.e via Uniswap V3 on Polygon")
    parser.add_argument("--amount",  type=float, help="Amount of USDC to swap (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without executing")
    args = parser.parse_args()

    if config.PRIVATE_KEY:
        private_key = config.PRIVATE_KEY
        from src.wallet.wallet import get_address_from_key
        address = get_address_from_key(private_key)
    else:
        try:
            from src.wallet.wallet import load_wallet
            wallet      = load_wallet()
            private_key = wallet["private_key"]
            address     = wallet["address"]
        except FileNotFoundError:
            print("❌ No private key found. Add POLY_PRIVATE_KEY to Replit Secrets.")
            sys.exit(1)

    w3       = get_w3()
    balances = get_balances(w3, address)
    amount   = args.amount or balances["usdc_native"]

    if amount <= 0:
        print("No native USDC to swap.")
        sys.exit(0)

    if args.amount is None and amount > 0.01:
        amount -= 0.01  # Leave dust for safety
        print(f"Swapping {amount:.4f} USDC (reserving 0.01 for rounding safety)")

    result = swap_usdc_native_to_bridged(amount, private_key, dry_run=args.dry_run)
    print(f"\nResult: {result}")


if __name__ == "__main__":
    main()
