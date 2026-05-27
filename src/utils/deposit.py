"""Deposit USDC.e into Polymarket (wrap to pUSD) and withdraw (unwrap).

Polymarket V2 uses pUSD (Polymarket USD) as collateral, not raw USDC.e.
The deposit flow is:
  1. Approve USDC.e spending by CollateralOnramp
  2. Call wrap(USDC.e, recipient, amount) on CollateralOnramp → mints pUSD

The withdrawal flow is the reverse:
  1. Approve pUSD spending by CollateralOfframp
  2. Call unwrap(USDC.e, recipient, amount) → burns pUSD, returns USDC.e

Usage:
  python -m src.utils.deposit --balances   # Check balances
  python -m src.utils.deposit --dry-run    # Preview deposit
  python -m src.utils.deposit --amount 10  # Deposit 10 USDC.e
  python -m src.utils.deposit --unwrap 5   # Unwrap 5 pUSD → USDC.e
"""

import argparse
import sys
from web3 import Web3
from eth_account import Account

from src.config import config

# Polymarket V2 contract addresses (Polygon mainnet)
COLLATERAL_ONRAMP  = Web3.to_checksum_address("0x93070a847efEf7F70739046A929D47a521F5B8ee")
COLLATERAL_OFFRAMP = Web3.to_checksum_address("0x2957922Eb93258b93368531d39fAcCA3B4dC5854")
PUSD_PROXY         = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
USDC_E             = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

ERC20_ABI = [
    {"inputs": [{"name": "", "type": "address"}],                                              "name": "balanceOf",  "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view",        "type": "function"},
    {"inputs": [{"name": "", "type": "address"}, {"name": "", "type": "address"}],             "name": "allowance",  "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view",        "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],"name": "approve",    "outputs": [{"name": "", "type": "bool"}],    "stateMutability": "nonpayable",  "type": "function"},
    {"inputs": [],                                                                              "name": "decimals",   "outputs": [{"name": "", "type": "uint8"}],   "stateMutability": "view",        "type": "function"},
    {"inputs": [],                                                                              "name": "symbol",     "outputs": [{"name": "", "type": "string"}],  "stateMutability": "view",        "type": "function"},
]

ONRAMP_ABI = [
    {"inputs": [
        {"name": "_asset",  "type": "address"},
        {"name": "_to",     "type": "address"},
        {"name": "_amount", "type": "uint256"},
    ], "name": "wrap", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]

OFFRAMP_ABI = [
    {"inputs": [
        {"name": "_asset",  "type": "address"},
        {"name": "_to",     "type": "address"},
        {"name": "_amount", "type": "uint256"},
    ], "name": "unwrap", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]

_POLYGON_RPCS = [
    "https://polygon.drpc.org",
    "https://polygon.llamarpc.com",
    "https://polygon-mainnet.public.blastapi.io",
]


def get_w3() -> Web3:
    """Get a Web3 instance for Polygon (tries multiple RPCs)."""
    for rpc in _POLYGON_RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    raise ConnectionError("Cannot connect to any Polygon RPC")


def _get_private_key() -> str:
    """Return private key from Replit Secrets or fall back to wallet file."""
    # BUG FIX: was always calling load_wallet() which requires a file on disk.
    # On Replit the key lives in env vars (Secrets), so check there first.
    if config.PRIVATE_KEY:
        return config.PRIVATE_KEY
    try:
        from src.wallet.wallet import load_wallet
        return load_wallet()["private_key"]
    except Exception:
        raise RuntimeError(
            "No private key found. Add POLY_PRIVATE_KEY to Replit Secrets."
        )


def _build_tx_base(w3: Web3, address: str) -> dict:
    """Build EIP-1559 transaction base with safe gas pricing."""
    # BUG FIX: was using 2× gas multiplier — wasteful on Polygon. Use 1.3×.
    return {
        "from": address,
        "nonce": w3.eth.get_transaction_count(address),
        "gas": 200_000,
        "maxFeePerGas": int(w3.eth.gas_price * 1.3),
        "maxPriorityFeePerGas": Web3.to_wei(30, "gwei"),
    }


def deposit_usdc_e(amount_usdc: float, dry_run: bool = False) -> dict:
    """Wrap USDC.e → pUSD for Polymarket trading.

    Args:
        amount_usdc: Amount of USDC.e to deposit
        dry_run: Preview the transaction without executing

    Returns:
        dict with status and transaction details
    """
    w3       = get_w3()
    pk       = _get_private_key()
    account  = Account.from_key(pk)
    address  = Web3.to_checksum_address(account.address)

    usdc   = w3.eth.contract(address=USDC_E,            abi=ERC20_ABI)
    onramp = w3.eth.contract(address=COLLATERAL_ONRAMP, abi=ONRAMP_ABI)
    pusd   = w3.eth.contract(address=PUSD_PROXY,        abi=ERC20_ABI)

    usdc_balance = usdc.functions.balanceOf(address).call() / 1e6
    pusd_balance = pusd.functions.balanceOf(address).call() / 1e6
    pol_balance  = float(Web3.from_wei(w3.eth.get_balance(address), "ether"))

    print(f"=== Deposit USDC.e → pUSD ===")
    print(f"  Wallet:  {address}")
    print(f"  USDC.e:  {usdc_balance:.4f}")
    print(f"  pUSD:    {pusd_balance:.4f}")
    print(f"  POL:     {pol_balance:.4f}")
    print(f"  Amount:  {amount_usdc:.4f} USDC.e")

    amount_wei = int(amount_usdc * 1e6)

    if amount_wei > usdc.functions.balanceOf(address).call():
        return {"status": "error", "message": f"Insufficient USDC.e: have {usdc_balance:.4f}"}

    # Check and set allowance if needed
    allowance = usdc.functions.allowance(address, COLLATERAL_ONRAMP).call()
    if allowance < amount_wei:
        if dry_run:
            print(f"\n  [DRY RUN] Would approve USDC.e for CollateralOnramp")
            print(f"  [DRY RUN] Would wrap {amount_usdc:.4f} USDC.e → pUSD")
            return {"status": "dry_run", "action": "approve + wrap", "amount": amount_usdc}

        print(f"\n  Approving USDC.e for CollateralOnramp...")
        tx = usdc.functions.approve(COLLATERAL_ONRAMP, 2 ** 256 - 1).build_transaction(
            {**_build_tx_base(w3, address), "gas": 100_000}
        )
        signed  = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        print(f"  ✅ Approved (block {receipt['blockNumber']}, gas {receipt['gasUsed']})")
    elif dry_run:
        print(f"\n  [DRY RUN] Allowance already sufficient ({allowance / 1e6:.4f})")
        print(f"  [DRY RUN] Would wrap {amount_usdc:.4f} USDC.e → pUSD")
        return {"status": "dry_run", "action": "wrap", "amount": amount_usdc}

    # Execute wrap
    print(f"\n  Wrapping {amount_usdc:.4f} USDC.e → pUSD...")
    tx = onramp.functions.wrap(USDC_E, address, amount_wei).build_transaction(
        _build_tx_base(w3, address)
    )
    signed  = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  TX: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt["status"] == 1:
        usdc_after = usdc.functions.balanceOf(address).call() / 1e6
        pusd_after = pusd.functions.balanceOf(address).call() / 1e6
        print(f"  ✅ Wrapped (block {receipt['blockNumber']}, gas {receipt['gasUsed']})")
        print(f"\n  USDC.e: {usdc_balance:.4f} → {usdc_after:.4f}")
        print(f"  pUSD:   {pusd_balance:.4f} → {pusd_after:.4f}")
        return {
            "status": "success",
            "tx_hash": tx_hash.hex(),
            "block_number": receipt["blockNumber"],
            "gas_used": receipt["gasUsed"],
            "usdc_e_before": usdc_balance,
            "usdc_e_after": usdc_after,
            "pusd_before": pusd_balance,
            "pusd_after": pusd_after,
        }
    else:
        print(f"  ❌ Transaction reverted!")
        return {"status": "failed", "tx_hash": tx_hash.hex()}


def unwrap_pusd(amount_pusd: float, dry_run: bool = False) -> dict:
    """Unwrap pUSD → USDC.e (withdraw from Polymarket).

    Args:
        amount_pusd: Amount of pUSD to unwrap
        dry_run: Preview without executing

    Returns:
        dict with status and transaction details
    """
    w3      = get_w3()
    pk      = _get_private_key()
    account = Account.from_key(pk)
    address = Web3.to_checksum_address(account.address)

    pusd    = w3.eth.contract(address=PUSD_PROXY,         abi=ERC20_ABI)
    offramp = w3.eth.contract(address=COLLATERAL_OFFRAMP, abi=OFFRAMP_ABI)
    usdc    = w3.eth.contract(address=USDC_E,             abi=ERC20_ABI)

    pusd_balance = pusd.functions.balanceOf(address).call() / 1e6
    usdc_balance = usdc.functions.balanceOf(address).call() / 1e6

    print(f"=== Unwrap pUSD → USDC.e ===")
    print(f"  Wallet:  {address}")
    print(f"  pUSD:    {pusd_balance:.4f}")
    print(f"  USDC.e:  {usdc_balance:.4f}")
    print(f"  Amount:  {amount_pusd:.4f} pUSD")

    amount_wei = int(amount_pusd * 1e6)

    if amount_wei > pusd.functions.balanceOf(address).call():
        return {"status": "error", "message": f"Insufficient pUSD: have {pusd_balance:.4f}"}

    allowance = pusd.functions.allowance(address, COLLATERAL_OFFRAMP).call()
    if allowance < amount_wei:
        if dry_run:
            print(f"\n  [DRY RUN] Would approve pUSD for CollateralOfframp")
            print(f"  [DRY RUN] Would unwrap {amount_pusd:.4f} pUSD → USDC.e")
            return {"status": "dry_run", "action": "approve + unwrap", "amount": amount_pusd}

        print(f"\n  Approving pUSD for CollateralOfframp...")
        tx = pusd.functions.approve(COLLATERAL_OFFRAMP, 2 ** 256 - 1).build_transaction(
            {**_build_tx_base(w3, address), "gas": 100_000}
        )
        signed  = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        print(f"  ✅ Approved (block {receipt['blockNumber']}, gas {receipt['gasUsed']})")
    elif dry_run:
        print(f"\n  [DRY RUN] Would unwrap {amount_pusd:.4f} pUSD → USDC.e")
        return {"status": "dry_run", "action": "unwrap", "amount": amount_pusd}

    # Execute unwrap
    print(f"\n  Unwrapping {amount_pusd:.4f} pUSD → USDC.e...")
    tx = offramp.functions.unwrap(USDC_E, address, amount_wei).build_transaction(
        _build_tx_base(w3, address)
    )
    signed  = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  TX: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt["status"] == 1:
        pusd_after = pusd.functions.balanceOf(address).call() / 1e6
        usdc_after = usdc.functions.balanceOf(address).call() / 1e6
        print(f"  ✅ Unwrapped (block {receipt['blockNumber']}, gas {receipt['gasUsed']})")
        print(f"\n  pUSD:   {pusd_balance:.4f} → {pusd_after:.4f}")
        print(f"  USDC.e: {usdc_balance:.4f} → {usdc_after:.4f}")
        return {
            "status": "success",
            "tx_hash": tx_hash.hex(),
            "block_number": receipt["blockNumber"],
            "gas_used": receipt["gasUsed"],
            "pusd_before": pusd_balance,
            "pusd_after": pusd_after,
            "usdc_e_before": usdc_balance,
            "usdc_e_after": usdc_after,
        }
    else:
        print(f"  ❌ Transaction reverted!")
        return {"status": "failed", "tx_hash": tx_hash.hex()}


def check_balances() -> dict:
    """Check all relevant balances (USDC.e, pUSD, POL) and print a summary."""
    w3      = get_w3()
    pk      = _get_private_key()
    address = Web3.to_checksum_address(Account.from_key(pk).address)

    usdc = w3.eth.contract(address=USDC_E,     abi=ERC20_ABI)
    pusd = w3.eth.contract(address=PUSD_PROXY, abi=ERC20_ABI)

    usdc_balance = usdc.functions.balanceOf(address).call() / 1e6
    pusd_balance = pusd.functions.balanceOf(address).call() / 1e6
    pol_balance  = float(Web3.from_wei(w3.eth.get_balance(address), "ether"))

    onramp_allowance        = usdc.functions.allowance(address, COLLATERAL_ONRAMP).call()  / 1e6
    offramp_pusd_allowance  = pusd.functions.allowance(address, COLLATERAL_OFFRAMP).call() / 1e6

    print(f"=== Wallet Balances ===")
    print(f"  Wallet:   {address}")
    print(f"  USDC.e:   {usdc_balance:.4f}")
    print(f"  pUSD:     {pusd_balance:.4f}")
    print(f"  POL:      {pol_balance:.4f}")
    print(f"\n  Allowances:")
    print(f"  USDC.e → Onramp:  {'∞' if onramp_allowance > 1e12 else f'{onramp_allowance:.4f}'}")
    print(f"  pUSD   → Offramp: {'∞' if offramp_pusd_allowance > 1e12 else f'{offramp_pusd_allowance:.4f}'}")

    return {
        "address": address,
        "usdc_e": usdc_balance,
        "pusd": pusd_balance,
        "pol": pol_balance,
        "onramp_allowance": onramp_allowance,
        "offramp_pusd_allowance": offramp_pusd_allowance,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deposit/withdraw USDC.e ↔ pUSD for Polymarket")
    parser.add_argument("--amount",   type=float, help="Amount to deposit (default: all available USDC.e)")
    parser.add_argument("--dry-run",  action="store_true", help="Preview without executing")
    parser.add_argument("--unwrap",   type=float, metavar="AMOUNT", help="Unwrap pUSD → USDC.e")
    parser.add_argument("--balances", action="store_true", help="Show balances and exit")
    args = parser.parse_args()

    if args.balances:
        check_balances()
        sys.exit(0)

    if args.unwrap is not None:
        result = unwrap_pusd(args.unwrap, dry_run=args.dry_run)
    else:
        # BUG FIX: was importing swap_usdc.get_w3 unnecessarily here.
        # Use deposit's own get_w3 to find the available USDC.e balance.
        w3     = get_w3()
        pk     = _get_private_key()
        addr   = Web3.to_checksum_address(Account.from_key(pk).address)
        usdc_c = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
        avail  = usdc_c.functions.balanceOf(addr).call() / 1e6
        amount = args.amount if args.amount else avail
        if amount <= 0:
            print("No USDC.e available to deposit.")
            sys.exit(0)
        result = deposit_usdc_e(amount, dry_run=args.dry_run)

    print(f"\nResult: {result}")
