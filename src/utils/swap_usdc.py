"""Swap native USDC → USDC.e (bridged) on Polygon via QuickSwap.

Both tokens have 6 decimals and trade 1:1, so slippage should be minimal.

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

# Contracts on Polygon mainnet
USDC_NATIVE      = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
USDC_E           = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
WMATIC           = Web3.to_checksum_address("0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270")
UNISWAP_V3_ROUTER = Web3.to_checksum_address("0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45")
QUICKSWAP_ROUTER  = Web3.to_checksum_address("0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff")

ERC20_ABI = [
    {"constant": True,  "inputs": [{"name": "_owner",   "type": "address"}],                                          "name": "balanceOf", "outputs": [{"name": "balance",    "type": "uint256"}], "type": "function"},
    {"constant": True,  "inputs": [],                                                                                  "name": "decimals",  "outputs": [{"name": "",           "type": "uint8"}],   "type": "function"},
    {"constant": True,  "inputs": [],                                                                                  "name": "symbol",    "outputs": [{"name": "",           "type": "string"}],  "type": "function"},
    {"constant": False, "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}],   "name": "approve",   "outputs": [{"name": "",           "type": "bool"}],    "type": "function"},
    {"constant": True,  "inputs": [{"name": "_owner",   "type": "address"}, {"name": "_spender", "type": "address"}],"name": "allowance", "outputs": [{"name": "remaining",  "type": "uint256"}], "type": "function"},
]

QUICKSWAP_ABI = [
    {"constant": True,  "inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "path", "type": "address[]"}], "name": "getAmountsOut",              "outputs": [{"name": "amounts", "type": "uint256[]"}], "type": "function"},
    {"constant": False, "inputs": [
        {"name": "amountIn",    "type": "uint256"},
        {"name": "amountOutMin","type": "uint256"},
        {"name": "path",        "type": "address[]"},
        {"name": "to",          "type": "address"},
        {"name": "deadline",    "type": "uint256"},
    ], "name": "swapExactTokensForTokens", "outputs": [{"name": "amounts", "type": "uint256[]"}], "type": "function"},
]

_POLYGON_RPCS = [
    "https://polygon.drpc.org",
    "https://polygon.llamarpc.com",
    "https://polygon-mainnet.public.blastapi.io",
]


def get_w3() -> Web3:
    """Return a connected Web3 instance for Polygon."""
    for rpc in _POLYGON_RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    raise ConnectionError("Could not connect to Polygon")


def get_balances(w3: Web3, address: str) -> dict:
    """Return USDC balances for an address."""
    address             = Web3.to_checksum_address(address)
    usdc_native_contract = w3.eth.contract(address=USDC_NATIVE, abi=ERC20_ABI)
    usdc_e_contract      = w3.eth.contract(address=USDC_E,       abi=ERC20_ABI)

    native_bal  = usdc_native_contract.functions.balanceOf(address).call()
    bridged_bal = usdc_e_contract.functions.balanceOf(address).call()
    pol_bal     = w3.eth.get_balance(address)

    return {
        "usdc_native":     native_bal  / 1e6,
        "usdc_e":          bridged_bal / 1e6,
        "pol":             float(Web3.from_wei(pol_bal, "ether")),
        "usdc_native_raw": native_bal,
        "usdc_e_raw":      bridged_bal,
    }


def approve_token(w3: Web3, private_key: str, token_address: str, spender: str, amount: int) -> str:
    """Approve a token spend. Returns tx hash or 'already_approved'."""
    from eth_account import Account
    account = Account.from_key(private_key)
    address = account.address

    contract = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)

    current_allowance = contract.functions.allowance(
        address, Web3.to_checksum_address(spender)
    ).call()
    if current_allowance >= amount:
        print(f"  Allowance already sufficient: {current_allowance / 1e6:.4f}")
        return "already_approved"

    tx = contract.functions.approve(
        Web3.to_checksum_address(spender),
        2 ** 256 - 1,
    ).build_transaction({
        "from":                address,
        "nonce":               w3.eth.get_transaction_count(address),
        "gas":                 100_000,
        "maxFeePerGas":        int(w3.eth.gas_price * 1.3),
        "maxPriorityFeePerGas": Web3.to_wei(30, "gwei"),
    })

    signed  = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  Approval tx: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    print(f"  Confirmed in block {receipt['blockNumber']}")
    return tx_hash.hex()


def swap_usdc_native_to_bridged(
    amount_usdc: float, private_key: str, dry_run: bool = False
) -> dict:
    """Swap native USDC → USDC.e on Polygon via QuickSwap.

    Args:
        amount_usdc: Amount of native USDC to swap
        private_key: Wallet private key (0x-prefixed)
        dry_run: Show quote without executing

    Returns:
        dict with swap result
    """
    from eth_account import Account

    w3      = get_w3()
    account = Account.from_key(private_key)
    address = Web3.to_checksum_address(account.address)

    balances = get_balances(w3, address)
    print(f"\n🔫 Wallet: {address}")
    print(f"  Native USDC: {balances['usdc_native']:.4f}")
    print(f"  USDC.e:      {balances['usdc_e']:.4f}")
    print(f"  POL:         {balances['pol']:.6f}")

    if balances["usdc_native"] < amount_usdc:
        raise ValueError(
            f"Insufficient native USDC: have {balances['usdc_native']:.4f}, "
            f"need {amount_usdc:.4f}"
        )

    amount_raw     = int(amount_usdc * 1e6)
    # 0.1% slippage tolerance — appropriate for a stablecoin-to-stablecoin swap
    min_amount_out = int(amount_usdc * 0.999 * 1e6)

    print(f"\n💱 Swap: {amount_usdc:.4f} USDC.native → USDC.e")
    print(f"  Min output: {min_amount_out / 1e6:.4f} USDC.e (0.1% slippage)")

    router   = w3.eth.contract(address=QUICKSWAP_ROUTER, abi=QUICKSWAP_ABI)
    expected_out = 0
    quote_path   = [USDC_NATIVE, USDC_E]

    try:
        amounts_out  = router.functions.getAmountsOut(amount_raw, [USDC_NATIVE, USDC_E]).call()
        expected_out = amounts_out[-1]
        print(f"  QuickSwap quote (direct): {expected_out / 1e6:.4f} USDC.e")
    except Exception as e:
        print(f"  Direct pool unavailable ({e}), trying WMATIC route...")
        try:
            amounts_out  = router.functions.getAmountsOut(
                amount_raw, [USDC_NATIVE, WMATIC, USDC_E]
            ).call()
            expected_out = amounts_out[-1]
            quote_path   = [USDC_NATIVE, WMATIC, USDC_E]
            print(f"  QuickSwap WMATIC route: {expected_out / 1e6:.4f} USDC.e")
        except Exception as e2:
            raise ValueError(f"Could not find any swap route: {e2}")

    if dry_run:
        print(f"\n  [DRY RUN] Would swap {amount_usdc:.4f} → ~{expected_out / 1e6:.4f} USDC.e")
        return {"dry_run": True, "amount_in": amount_usdc, "expected_out": expected_out / 1e6}

    # Step 1: Approve
    print(f"\n1️⃣  Approving USDC.native spend...")
    approve_token(w3, private_key, USDC_NATIVE, QUICKSWAP_ROUTER, amount_raw)

    # Step 2: Swap
    print(f"\n2️⃣  Executing swap...")
    deadline = int(time.time()) + 600  # 10 minute window

    swap_tx = router.functions.swapExactTokensForTokens(
        amount_raw, min_amount_out, quote_path, address, deadline
    ).build_transaction({
        "from":                address,
        "nonce":               w3.eth.get_transaction_count(address),
        "gas":                 300_000,
        "maxFeePerGas":        int(w3.eth.gas_price * 1.3),
        "maxPriorityFeePerGas": Web3.to_wei(30, "gwei"),
    })

    signed  = account.sign_transaction(swap_tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  TX: {tx_hash.hex()} — waiting for confirmation...")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt["status"] == 1:
        new_balances = get_balances(w3, address)
        print(f"\n✅ Swap successful! Block {receipt['blockNumber']}, gas {receipt['gasUsed']}")
        print(f"  Native USDC: {balances['usdc_native']:.4f} → {new_balances['usdc_native']:.4f}")
        print(f"  USDC.e:      {balances['usdc_e']:.4f} → {new_balances['usdc_e']:.4f}")
        return {
            "success":           True,
            "tx_hash":           tx_hash.hex(),
            "block":             receipt["blockNumber"],
            "gas_used":          receipt["gasUsed"],
            "usdc_native_before": balances["usdc_native"],
            "usdc_e_before":     balances["usdc_e"],
            "usdc_native_after": new_balances["usdc_native"],
            "usdc_e_after":      new_balances["usdc_e"],
        }
    else:
        print(f"\n❌ Swap failed! TX: {tx_hash.hex()}")
        return {"success": False, "tx_hash": tx_hash.hex()}


def main():
    parser = argparse.ArgumentParser(description="Swap native USDC → USDC.e on Polygon")
    parser.add_argument("--amount",  type=float, help="Amount of USDC to swap (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without executing")
    args = parser.parse_args()

    # BUG FIX: was always calling load_wallet() which requires a wallet file.
    # Use POLY_PRIVATE_KEY from Replit Secrets first, fall back to file.
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
        return

    # Leave 0.01 USDC for rounding safety
    if args.amount is None and amount > 0.01:
        amount -= 0.01
        print(f"Swapping {amount:.4f} USDC (reserving 0.01 for rounding)")

    result = swap_usdc_native_to_bridged(amount, private_key, dry_run=args.dry_run)
    print(f"\nResult: {result}")


if __name__ == "__main__":
    main()
