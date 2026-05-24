"""Wallet creation CLI entry point."""

from src.wallet.wallet import create_wallet, load_wallet, KEYS_FILE

if __name__ == "__main__":
    import sys

    if KEYS_FILE.exists():
        print(f"⚠️  Wallet already exists at {KEYS_FILE}")
        print("Loading existing wallet...")
        wallet = load_wallet()
    else:
        print("🔑 Creating new Polygon wallet...")
        wallet = create_wallet()

    from src.wallet.wallet import wallet_summary
    print()
    print(wallet_summary(wallet))
    print()
    print(f"Private Key: {wallet['private_key'][:8]}...{wallet['private_key'][-6:]}")
    print(f"Mnemonic: {'Saved in wallet file' if 'mnemonic' in wallet else 'N/A'}")
    print()
    print(f"✅ Wallet saved to: {KEYS_FILE}")
    print()
    print("Next steps:")
    print("1. Add POLY_PRIVATE_KEY to config/.env")
    print("2. Fund your wallet with USDC on Polygon network")
    print("3. Run the bot in dry-run mode first: python -m src.bot --dry-run")