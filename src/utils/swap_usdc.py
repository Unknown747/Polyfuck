"""Swap native USDC to USDC.e (bridged) on Polygon via Uniswap V3.

This script swaps USDC.native → USDC.e so the funds can be used on Polymarket.
Both tokens have 6 decimals and trade 1:1, so the swap should have minimal slippage.

Usage:
    python -m src.utils.swap_usdc                    # Swap all native USDC
    python -m src.utils.swap_usdc --amount 10        # Swap specific amount
    python -m src.utils.swap_usdc --dry-run          # Preview without executing
"""

import sys
import time
import argparse
from web3 import Web3

from src.config import config

# Contracts on Polygon
USDC_NATIVE = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

# Uniswap V3 SwapRouter02 on Polygon
UNISWAP_V3_ROUTER = Web3.to_checksum_address("0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45")

# QuickSwap Router (fallback)
QUICKSWAP_ROUTER = Web3.to_checksum_address("0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff")

# ERC20 ABI - just the functions we need
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "remaining", "type": "uint256"}], "type": "function"},
]

# Uniswap V3 SwapRouter ABI (exactInputSingle)
SWAP_ROUTER_ABI = [
    {
        "inputs": [
            {"components": [
                {"name": "tokenIn", "type": "address"},
                {"name": "tokenOut", "type": "address"},
                {"name": "fee", "type": "uint24"},
                {"name": "recipient", "type": "address"},
                {"name": "amountIn", "type": "uint256"},
                {"name": "amountOutMinimum", "type": "uint256"},
                {"name": "sqrtPriceLimitX96", "type": "uint160"},
            ], "name": "params", "type": "tuple"},
        ],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "type": "function",
    },
]

# QuickSwap Router ABI (swapTokensForExactTokens / swapExactTokensForTokens)
QUICKSWAP_ABI = [
    {"constant": True, "inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "path", "type": "address[]"}], "name": "getAmountsOut", "outputs": [{"name": "amounts", "type": "uint256[]"}], "type": "function"},
    {"constant": False, "inputs": [
        {"name": "amountIn", "type": "uint256"},
        {"name": "amountOutMin", "type": "uint256"},
        {"name": "path", "type": "address[]"},
        {"name": "to", "type": "address"},
        {"name": "deadline", "type": "uint256"},
    ], "name": "swapExactTokensForTokens", "outputs": [{"name": "amounts", "type": "uint256[]"}], "type": "function"},
]

POLYGON_RPCS = [
    "https://polygon.drpc.org",
    "https://polygon.llamarpc.com",
    "https://polygon-mainnet.public.blastapi.io",
]


def get_w3() -> Web3:
    """Get connected Web3 instance."""
    for rpc in POLYGON_RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    raise ConnectionError("Could not connect to Polygon")


def get_balances(w3: Web3, address: str) -> dict:
    """Get USDC balances for an address."""
    address = Web3.to_checksum_address(address)

    usdc_native_contract = w3.eth.contract(address=USDC_NATIVE, abi=ERC20_ABI)
    usdc_e_contract = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)

    native_bal = usdc_native_contract.functions.balanceOf(address).call()
    bridged_bal = usdc_e_contract.functions.balanceOf(address).call()
    pol_bal = w3.eth.get_balance(address)

    return {
        "usdc_native": native_bal / 1e6,
        "usdc_e": bridged_bal / 1e6,
        "pol": float(Web3.from_wei(pol_bal, "ether")),
        "usdc_native_raw": native_bal,
        "usdc_e_raw": bridged_bal,
    }


def approve_token(w3: Web3, private_key: str, token_address: str, spender: str, amount: int) -> str:
    """Approve a token spend. Returns tx hash."""
    from eth_account import Account
    account = Account.from_key(private_key)

    contract = w3.eth.contract(address=token_address, abi=ERC20_ABI)

    # Check current allowance
    current_allowance = contract.functions.allowance(account.address, Web3.to_checksum_address(spender)).call()
    if current_allowance >= amount:
        print(f"  Allowance already sufficient: {current_allowance / 1e6:.2f} USDC")
        return "already_approved"

    # Approve max uint256
    max_approval = 2**256 - 1

    tx = contract.functions.approve(
        Web3.to_checksum_address(spender),
        max_approval
    ).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": 100000,
        # BUG FIX: 2x gas multiplier is too high; use 1.3x for Polygon
        "maxFeePerGas": int(w3.eth.gas_price * 1.3),
        "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
    })

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

    print(f"  Approval tx sent: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    print(f"  Approval confirmed in block {receipt['blockNumber']}")
    return tx_hash.hex()


def swap_usdc_native_to_bridged(amount_usdc: float, private_key: str, dry_run: bool = False) -> dict:
    """Swap native USDC → USDC.e on Polygon via QuickSwap.

    Args:
        amount_usdc: Amount of native USDC to swap
        private_key: Wallet private key (with 0x prefix)
        dry_run: If True, just show quote without executing

    Returns:
        dict with swap details
    """
    from eth_account import Account

    w3 = get_w3()
    account = Account.from_key(private_key)
    address = Web3.to_checksum_address(account.address)

    # Check balances
    balances = get_balances(w3, address)
    print(f"\n🔫 Wallet: {address}")
    print(f"  Native USDC: {balances['usdc_native']:.2f}")
    print(f"  USDC.e: {balances['usdc_e']:.2f}")
    print(f"  POL: {balances['pol']:.6f}")

    if balances['usdc_native'] < amount_usdc:
        raise ValueError(f"Insufficient native USDC: have {balances['usdc_native']:.2f}, need {amount_usdc:.2f}")

    amount_raw = int(amount_usdc * 1e6)  # 6 decimals
    # BUG FIX: 1.5% slippage is far too high for a stablecoin-to-stablecoin swap.
    # Use 0.1% max slippage to avoid MEV leakage on Polygon.
    min_amount_out = int(amount_usdc * 0.999 * 1e6)  # 0.1% slippage tolerance

    # Use QuickSwap for the swap (USDC.native → USDC.e pool should exist)
    print(f"\n💱 Swap: {amount_usdc:.2f} USDC.native → USDC.e")
    print(f"  Minimum output: {min_amount_out / 1e6:.4f} USDC.e (0.1% slippage)")

    # BUG FIX: define WMATIC at module scope so it's available in all branches.
    WMATIC = Web3.to_checksum_address("0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270")

    # Get quote from QuickSwap
    router_contract = w3.eth.contract(address=QUICKSWAP_ROUTER, abi=QUICKSWAP_ABI)

    quote_path = [USDC_NATIVE, USDC_E]
    expected_out = 0
    try:
        # Try direct pool: USDC.native → USDC.e
        amounts_out = router_contract.functions.getAmountsOut(
            amount_raw,
            [USDC_NATIVE, USDC_E]
        ).call()
        expected_out = amounts_out[-1]
        print(f"  QuickSwap quote: {expected_out / 1e6:.4f} USDC.e")
    except Exception as e:
        print(f"  QuickSwap direct pool error: {e}")
        print("  Trying WMATIC route...")

        # Try via WMATIC: USDC.native → WMATIC → USDC.e
        try:
            amounts_out = router_contract.functions.getAmountsOut(
                amount_raw,
                [USDC_NATIVE, WMATIC, USDC_E]
            ).call()
            expected_out = amounts_out[-1]
            quote_path = [USDC_NATIVE, WMATIC, USDC_E]
            print(f"  QuickSwap WMATIC route quote: {expected_out / 1e6:.4f} USDC.e")
        except Exception as e2:
            raise ValueError(f"Could not find swap route: {e2}")

    if dry_run:
        print(f"\n  [DRY RUN] Would swap {amount_usdc:.2f} USDC.native → ~{expected_out / 1e6:.4f} USDC.e")
        return {"dry_run": True, "amount_in": amount_usdc, "expected_out": expected_out / 1e6}

    # Step 1: Approve USDC.native spending
    print(f"\n1️⃣  Approving USDC.native spend...")
    approve_tx = approve_token(w3, private_key, USDC_NATIVE, QUICKSWAP_ROUTER, amount_raw)

    # Step 2: Execute swap using the same path determined during quote
    print(f"\n2️⃣  Executing swap...")
    path = quote_path

    deadline = int(time.time()) + 600  # 10 minutes

    swap_tx = router_contract.functions.swapExactTokensForTokens(
        amount_raw,             # amountIn
        min_amount_out,         # amountOutMin
        path,                   # path
        address,                # to
        deadline,               # deadline
    ).build_transaction({
        "from": address,
        "nonce": w3.eth.get_transaction_count(address),
        "gas": 300000,  # Generous gas limit
        # BUG FIX: 3x gas multiplier is wasteful; 1.3x is sufficient for Polygon
        "maxFeePerGas": int(w3.eth.gas_price * 1.3),
        "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
    })

    signed = account.sign_transaction(swap_tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

    print(f"  Swap tx sent: {tx_hash.hex()}")
    print(f"  Waiting for confirmation...")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt["status"] == 1:
        new_balances = get_balances(w3, address)
        print(f"\n✅ Swap successful!")
        print(f"  Block: {receipt['blockNumber']}")
        print(f"  Gas used: {receipt['gasUsed']}")
        print(f"  Native USDC: {balances['usdc_native']:.2f} → {new_balances['usdc_native']:.2f}")
        print(f"  USDC.e: {balances['usdc_e']:.2f} → {new_balances['usdc_e']:.2f}")
        return {
            "success": True,
            "tx_hash": tx_hash.hex(),
            "block": receipt["blockNumber"],
            "gas_used": receipt["gasUsed"],
            "usdc_native_before": balances["usdc_native"],
            "usdc_e_before": balances["usdc_e"],
            "usdc_native_after": new_balances["usdc_native"],
            "usdc_e_after": new_balances["usdc_e"],
        }
    else:
        print(f"\n❌ Swap failed! TX: {tx_hash.hex()}")
        return {"success": False, "tx_hash": tx_hash.hex()}


def main():
    parser = argparse.ArgumentParser(description="Swap native USDC → USDC.e on Polygon")
    parser.add_argument("--amount", type=float, help="Amount of USDC to swap (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without executing")
    args = parser.parse_args()

    # Load wallet
    try:
        from src.wallet.wallet import load_wallet
        wallet = load_wallet()
        private_key = wallet["private_key"]
        address = wallet["address"]
    except FileNotFoundError:
        print("❌ No wallet found. Run: python -m src.wallet.create_wallet")
        sys.exit(1)

    w3 = get_w3()
    balances = get_balances(w3, address)

    amount = args.amount or balances["usdc_native"]

    if amount <= 0:
        print("No native USDC to swap!")
        return

    # Leave 0.01 USDC for rounding
    if amount == balances["usdc_native"] and amount > 0.01:
        amount = amount - 0.01
        print(f"Swapping {amount:.2f} USDC (leaving 0.01 for rounding)")

    result = swap_usdc_native_to_bridged(amount, private_key, dry_run=args.dry_run)
    print(f"\nResult: {result}")


if __name__ == "__main__":
    main()