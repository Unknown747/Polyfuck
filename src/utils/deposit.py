"""Deposit USDC.e into Polymarket CTF system (wrap to pUSD).

Polymarket V2 uses pUSD (Polymarket USD) as collateral, not raw USDC.e.
The deposit flow is:
  1. Approve USDC.e spending by CollateralOnramp
  2. Call wrap(USDC.e, recipient, amount) on CollateralOnramp → mints pUSD

Usage:
  python -m src.utils.deposit              # Deposit all available USDC.e
  python -m src.utils.deposit --amount 10  # Deposit 10 USDC.e
  python -m src.utils.deposit --dry-run    # Preview without executing
  python -m src.utils.deposit --unwrap 5   # Unwrap 5 pUSD back to USDC.e
"""

import argparse
import sys
from web3 import Web3
from eth_account import Account

from src.config import config
from src.wallet.wallet import load_wallet

# Polymarket V2 contract addresses (Polygon mainnet)
COLLATERAL_ONRAMP = Web3.to_checksum_address("0x93070a847efEf7F70739046A929D47a521F5B8ee")
COLLATERAL_OFFRAMP = Web3.to_checksum_address("0x2957922Eb93258b93368531d39fAcCA3B4dC5854")
PUSD_PROXY = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
CTF_EXCHANGE_V2 = Web3.to_checksum_address("0xE111180000d2663C0091e4f400237545B87B996B")
NEG_RISK_CTF_EXCHANGE_V2 = Web3.to_checksum_address("0xe2222d279d744050d28e00520010520000310F59")

# ABIs
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"constant": True, "inputs": [{"name": "", "type": "address"}, {"name": "", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"constant": False, "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "stateMutability": "view", "type": "function"},
]

ONRAMP_ABI = [
    {"constant": False, "inputs": [
        {"name": "_asset", "type": "address"},
        {"name": "_to", "type": "address"},
        {"name": "_amount", "type": "uint256"},
    ], "name": "wrap", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]

OFFRAMP_ABI = [
    {"constant": False, "inputs": [
        {"name": "_asset", "type": "address"},
        {"name": "_to", "type": "address"},
        {"name": "_amount", "type": "uint256"},
    ], "name": "unwrap", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]


def get_w3() -> Web3:
    """Get Web3 instance configured for Polygon."""
    rpcs = [
        "https://polygon.drpc.org",
        "https://polygon.llamarpc.com",
        "https://polygon-mainnet.public.blastapi.io",
    ]
    for rpc in rpcs:
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
        if w3.is_connected():
            return w3
    raise ConnectionError("Cannot connect to any Polygon RPC")


def deposit_usdc_e(amount_usdc: float, dry_run: bool = False) -> dict:
    """Deposit (wrap) USDC.e into pUSD for Polymarket trading.

    Args:
        amount_usdc: Amount of USDC.e to deposit
        dry_run: If True, only preview the transaction

    Returns:
        dict with status and transaction details
    """
    w3 = get_w3()
    wallet = load_wallet()
    account = Account.from_key(wallet["private_key"])
    address = Web3.to_checksum_address(account.address)

    usdc = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
    onramp = w3.eth.contract(address=COLLATERAL_ONRAMP, abi=ONRAMP_ABI)
    pusd = w3.eth.contract(address=PUSD_PROXY, abi=ERC20_ABI)

    # Check balances before
    usdc_balance = usdc.functions.balanceOf(address).call() / 1e6
    pusd_balance = pusd.functions.balanceOf(address).call() / 1e6
    pol_balance = Web3.from_wei(w3.eth.get_balance(address), "ether")

    print(f"=== Deposit USDC.e → pUSD ===")
    print(f"  Wallet:  {address}")
    print(f"  USDC.e:  {usdc_balance:.2f}")
    print(f"  pUSD:    {pusd_balance:.2f}")
    print(f"  POL:     {pol_balance:.4f}")
    print(f"  Amount:  {amount_usdc:.2f} USDC.e")

    amount_wei = int(amount_usdc * 1e6)

    if amount_wei > usdc.functions.balanceOf(address).call():
        return {"status": "error", "message": f"Insufficient USDC.e balance: {usdc_balance:.2f}"}

    # Check allowance
    allowance = usdc.functions.allowance(address, COLLATERAL_ONRAMP).call()
    if allowance < amount_wei:
        if dry_run:
            print(f"\n  ⚠️  Need to approve USDC.e for CollateralOnramp")
            print(f"     Current allowance: {allowance / 1e6:.2f}")
            print(f"     Required:          {amount_usdc:.2f}")
            return {"status": "dry_run", "action": "approve + wrap", "amount": amount_usdc}

        print(f"\n  Approving USDC.e for CollateralOnramp...")
        max_uint256 = 2**256 - 1
        tx = usdc.functions.approve(COLLATERAL_ONRAMP, max_uint256).build_transaction({
            "from": address,
            "nonce": w3.eth.get_transaction_count(address),
            "gas": 100000,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": Web3.to_wei(30, "gwei"),
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        print(f"  ✅ Approved in block {receipt['blockNumber']} (gas: {receipt['gasUsed']})")

    if dry_run:
        print(f"\n  [DRY RUN] Would wrap {amount_usdc:.2f} USDC.e → pUSD")
        return {"status": "dry_run", "action": "wrap", "amount": amount_usdc}

    # Execute wrap
    print(f"\n  Wrapping {amount_usdc:.2f} USDC.e → pUSD...")
    tx = onramp.functions.wrap(USDC_E, address, amount_wei).build_transaction({
        "from": address,
        "nonce": w3.eth.get_transaction_count(address),
        "gas": 200000,
        "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": Web3.to_wei(30, "gwei"),
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  TX: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt["status"] == 1:
        # Check balances after
        usdc_after = usdc.functions.balanceOf(address).call() / 1e6
        pusd_after = pusd.functions.balanceOf(address).call() / 1e6

        print(f"  ✅ Wrapped successfully in block {receipt['blockNumber']} (gas: {receipt['gasUsed']})")
        print(f"\n  === Updated Balances ===")
        print(f"  USDC.e: {usdc_balance:.2f} → {usdc_after:.2f}")
        print(f"  pUSD:   {pusd_balance:.2f} → {pusd_after:.2f}")

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
        print(f"  ❌ Transaction failed!")
        return {"status": "failed", "tx_hash": tx_hash.hex(), "receipt": dict(receipt)}


def unwrap_pusd(amount_pusd: float, dry_run: bool = False) -> dict:
    """Unwrap pUSD back to USDC.e (withdraw from Polymarket).

    Args:
        amount_pusd: Amount of pUSD to unwrap
        dry_run: If True, only preview the transaction

    Returns:
        dict with status and transaction details
    """
    w3 = get_w3()
    wallet = load_wallet()
    account = Account.from_key(wallet["private_key"])
    address = Web3.to_checksum_address(account.address)

    pusd = w3.eth.contract(address=PUSD_PROXY, abi=ERC20_ABI)
    offramp = w3.eth.contract(address=COLLATERAL_OFFRAMP, abi=OFFRAMP_ABI)
    usdc = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)

    # Check balances before
    pusd_balance = pusd.functions.balanceOf(address).call() / 1e6
    usdc_balance = usdc.functions.balanceOf(address).call() / 1e6

    print(f"=== Unwrap pUSD → USDC.e ===")
    print(f"  Wallet:  {address}")
    print(f"  pUSD:    {pusd_balance:.2f}")
    print(f"  USDC.e:  {usdc_balance:.2f}")
    print(f"  Amount:  {amount_pusd:.2f} pUSD")

    amount_wei = int(amount_pusd * 1e6)

    if amount_wei > pusd.functions.balanceOf(address).call():
        return {"status": "error", "message": f"Insufficient pUSD balance: {pusd_balance:.2f}"}

    # Check pUSD allowance for offramp
    allowance = pusd.functions.allowance(address, COLLATERAL_OFFRAMP).call()
    if allowance < amount_wei:
        if dry_run:
            print(f"\n  ⚠️  Need to approve pUSD for CollateralOfframp")
            return {"status": "dry_run", "action": "approve + unwrap", "amount": amount_pusd}

        print(f"\n  Approving pUSD for CollateralOfframp...")
        max_uint256 = 2**256 - 1
        tx = pusd.functions.approve(COLLATERAL_OFFRAMP, max_uint256).build_transaction({
            "from": address,
            "nonce": w3.eth.get_transaction_count(address),
            "gas": 100000,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": Web3.to_wei(30, "gwei"),
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        print(f"  ✅ Approved in block {receipt['blockNumber']} (gas: {receipt['gasUsed']})")

    if dry_run:
        print(f"\n  [DRY RUN] Would unwrap {amount_pusd:.2f} pUSD → USDC.e")
        return {"status": "dry_run", "action": "unwrap", "amount": amount_pusd}

    # Execute unwrap
    print(f"\n  Unwrapping {amount_pusd:.2f} pUSD → USDC.e...")
    tx = offramp.functions.unwrap(USDC_E, address, amount_wei).build_transaction({
        "from": address,
        "nonce": w3.eth.get_transaction_count(address),
        "gas": 200000,
        "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": Web3.to_wei(30, "gwei"),
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  TX: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt["status"] == 1:
        pusd_after = pusd.functions.balanceOf(address).call() / 1e6
        usdc_after = usdc.functions.balanceOf(address).call() / 1e6

        print(f"  ✅ Unwrapped successfully in block {receipt['blockNumber']} (gas: {receipt['gasUsed']})")
        print(f"\n  === Updated Balances ===")
        print(f"  pUSD:   {pusd_balance:.2f} → {pusd_after:.2f}")
        print(f"  USDC.e: {usdc_balance:.2f} → {usdc_after:.2f}")

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
        print(f"  ❌ Transaction failed!")
        return {"status": "failed", "tx_hash": tx_hash.hex(), "receipt": dict(receipt)}


def check_balances() -> dict:
    """Check all relevant balances (USDC.e, pUSD, POL)."""
    w3 = get_w3()
    wallet = load_wallet()
    address = Web3.to_checksum_address(wallet["address"])

    usdc = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
    pusd = w3.eth.contract(address=PUSD_PROXY, abi=ERC20_ABI)

    usdc_balance = usdc.functions.balanceOf(address).call() / 1e6
    pusd_balance = pusd.functions.balanceOf(address).call() / 1e6
    pol_balance = float(Web3.from_wei(w3.eth.get_balance(address), "ether"))

    # Check allowances
    onramp_allowance = usdc.functions.allowance(address, COLLATERAL_ONRAMP).call() / 1e6
    offramp_pusd_allowance = pusd.functions.allowance(address, COLLATERAL_OFFRAMP).call() / 1e6

    print(f"=== Wallet Balances ===")
    print(f"  Wallet:   {address}")
    print(f"  USDC.e:   {usdc_balance:.2f}")
    print(f"  pUSD:     {pusd_balance:.2f}")
    print(f"  POL:      {pol_balance:.4f}")
    print(f"\n  Allowances:")
    print(f"  USDC.e → Onramp:  {'∞' if onramp_allowance > 1e12 else f'{onramp_allowance:.2f}'}")
    print(f"  pUSD   → Offramp: {'∞' if offramp_pusd_allowance > 1e12 else f'{offramp_pusd_allowance:.2f}'}")

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
    parser.add_argument("--amount", type=float, help="Amount to deposit/unwrap (default: all available)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without executing")
    parser.add_argument("--unwrap", type=float, metavar="AMOUNT", help="Unwrap pUSD back to USDC.e")
    parser.add_argument("--balances", action="store_true", help="Check all balances")
    args = parser.parse_args()

    if args.balances or (not args.unwrap and args.amount is None and not args.dry_run):
        if not args.dry_run and not args.unwrap:
            check_balances()

    if args.unwrap:
        amount = args.unwrap
        result = unwrap_pusd(amount, dry_run=args.dry_run)
    elif args.amount is not None or args.dry_run:
        from src.utils.swap_usdc import get_w3 as get_w3_swap
        w3 = get_w3_swap()
        usdc = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
        wallet = load_wallet()
        usdc_balance = usdc.functions.balanceOf(Web3.to_checksum_address(wallet["address"])).call() / 1e6
        amount = args.amount if args.amount else usdc_balance
        result = deposit_usdc_e(amount, dry_run=args.dry_run)