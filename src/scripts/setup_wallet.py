"""Polymarket Wallet On-Chain Setup

Approves all required Polymarket smart contracts to spend pUSD and
operate conditional tokens on behalf of this wallet.

Run once:
    python -m src.scripts.setup_wallet

After success, restart the bot — trades will work immediately.
"""

import os
import sys
import time
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("setup")

# ── Contract addresses (Polygon mainnet, chain 137) ───────────────────────────
PUSD       = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"   # pUSD collateral
CTF        = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"   # Conditional Tokens
EXCHANGE   = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"   # CTF Exchange v1
NEG_RISK   = "0xC5d563A36AE78145C45a50134d48A1215220f80a"   # Neg Risk Exchange
EXCH_V2    = "0xE111180000d2663C0091e4f400237545B87B996B"   # CTF Exchange v2
NR_EXCH_V2 = "0xe2222d279d744050d28e00520010520000310F59"   # Neg Risk Exchange v2
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

ALL_EXCHANGES = [EXCHANGE, NEG_RISK, EXCH_V2, NR_EXCH_V2, NEG_RISK_ADAPTER]

MAX_UINT256 = 2**256 - 1

ERC20_ABI = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"stateMutability":"view","type":"function"},
]

ERC1155_ABI = [
    {"inputs":[{"name":"operator","type":"address"},{"name":"approved","type":"bool"}],"name":"setApprovalForAll","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"account","type":"address"},{"name":"operator","type":"address"}],"name":"isApprovedForAll","outputs":[{"name":"","type":"bool"}],"stateMutability":"view","type":"function"},
]

RPCS = [
    "https://polygon.drpc.org",
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
]


def _connect():
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
    for rpc in RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
            # Polygon is a PoA chain — inject middleware to handle 97-byte extraData
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            if w3.eth.chain_id == 137:
                log.info("Connected to Polygon via %s", rpc)
                return w3
        except Exception:
            pass
    raise RuntimeError("Cannot connect to any Polygon RPC")


def _send_tx(w3, acct, fn, gas=120_000):
    """Build, sign and send a transaction. Return receipt."""
    from web3 import Web3
    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
    priority = w3.to_wei(35, "gwei")
    max_fee  = base_fee * 2 + priority

    tx = fn.build_transaction({
        "from":                 acct.address,
        "nonce":                nonce,
        "gas":                  gas,
        "maxFeePerGas":         max_fee,
        "maxPriorityFeePerGas": priority,
        "chainId":              137,
    })
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    log.info("  tx sent: %s", tx_hash.hex())
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        raise RuntimeError(f"Transaction reverted: {tx_hash.hex()}")
    return receipt


def run():
    from web3 import Web3
    from eth_account import Account

    pk = os.environ.get("POLY_PRIVATE_KEY", "")
    if not pk:
        log.error("POLY_PRIVATE_KEY not set")
        sys.exit(1)

    acct = Account.from_key(pk)
    addr = acct.address
    log.info("Wallet: %s", addr)

    w3 = _connect()

    matic = w3.from_wei(w3.eth.get_balance(addr), "ether")
    log.info("MATIC (gas): %.4f", matic)
    if matic < 0.01:
        log.error("Not enough MATIC for gas. Need at least 0.01 MATIC.")
        sys.exit(1)

    pusd = w3.eth.contract(address=Web3.to_checksum_address(PUSD), abi=ERC20_ABI)
    ctf  = w3.eth.contract(address=Web3.to_checksum_address(CTF),  abi=ERC1155_ABI)

    bal = pusd.functions.balanceOf(addr).call()
    log.info("pUSD balance: %.4f", bal / 1e6)

    exchanges_cs = [Web3.to_checksum_address(e) for e in ALL_EXCHANGES]

    # ── Step 1: pUSD approvals ─────────────────────────────────────────────────
    log.info("\n── Step 1: pUSD (collateral) approvals ──")
    for ex_addr in exchanges_cs:
        current = pusd.functions.allowance(addr, ex_addr).call()
        if current >= MAX_UINT256 // 2:
            log.info("  ✅ %s already approved", ex_addr[:10])
            continue
        log.info("  Approving pUSD → %s ...", ex_addr[:10])
        _send_tx(w3, acct, pusd.functions.approve(ex_addr, MAX_UINT256))
        log.info("  ✅ Done")
        time.sleep(2)

    # ── Step 2: CTF setApprovalForAll ──────────────────────────────────────────
    log.info("\n── Step 2: Conditional Token (CTF) approvals ──")
    for ex_addr in exchanges_cs:
        already = ctf.functions.isApprovedForAll(addr, ex_addr).call()
        if already:
            log.info("  ✅ %s already approved for CTF", ex_addr[:10])
            continue
        log.info("  Setting CTF.setApprovalForAll → %s ...", ex_addr[:10])
        _send_tx(w3, acct, ctf.functions.setApprovalForAll(ex_addr, True))
        log.info("  ✅ Done")
        time.sleep(2)

    # ── Step 3: Notify CLOB to sync on-chain state ─────────────────────────────
    log.info("\n── Step 3: Notify Polymarket CLOB to sync allowances ──")
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
        from src.utils.api import ClobClient
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        clob = ClobClient()
        clob.authenticate()
        r1 = clob._client.update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        log.info("  CLOB sync COLLATERAL: %s", r1)
        r2 = clob._client.update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL)
        )
        log.info("  CLOB sync CONDITIONAL: %s", r2)
    except Exception as e:
        log.warning("  CLOB sync warning (non-fatal): %s", e)

    # ── Summary ────────────────────────────────────────────────────────────────
    log.info("\n═══════════════════════════════════")
    log.info("✅ Wallet setup complete!")
    log.info("Restart the bot — trading is now enabled.")
    log.info("═══════════════════════════════════")


if __name__ == "__main__":
    run()
