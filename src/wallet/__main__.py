"""Wallet creation CLI entry point.

Run: python -m src.wallet
"""

from src.wallet.wallet import create_wallet, load_wallet, wallet_summary, KEYS_FILE


if __name__ == "__main__":
    if KEYS_FILE.exists():
        print(f"⚠️  Wallet already exists at {KEYS_FILE}")
        print("Loading existing wallet...")
        wallet = load_wallet()
    else:
        print("🔑 Creating new Polygon wallet...")
        wallet = create_wallet()

    print()
    print(wallet_summary(wallet))
    print()
    pk = wallet["private_key"]
    print(f"Private Key: {pk[:8]}...{pk[-6:]}")
    print(f"Mnemonic: {'stored in wallet file' if 'mnemonic' in wallet else 'N/A'}")
    print()
    print("Next steps:")
    print("1. Open Replit → Tools → Secrets")
    print("2. Add POLY_PRIVATE_KEY = <your full private key>")
    print("3. Delete config/keys/wallet.json (never commit private keys)")
    print("4. Fund your wallet with USDC.e on Polygon")
    print("5. Start the bot: python -m src.bot")
