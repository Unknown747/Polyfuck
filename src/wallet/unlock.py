"""Unlock and display wallet credentials from encrypted macOS Keychain storage.

This tool only works on the Mac where the wallet was originally created.
On Replit, use POLY_PRIVATE_KEY in Replit Secrets instead.
"""

import json
import subprocess
import sys
from pathlib import Path

WALLET_ENC       = Path(__file__).parent.parent.parent / "config" / "keys" / "wallet.enc"
KEYCHAIN_ACCOUNT = "clawbots-polymarket"
KEYCHAIN_SERVICE = "clawbots-wallet-key"


def get_key_from_keychain() -> str:
    """Retrieve the decryption key from macOS Keychain."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-a", KEYCHAIN_ACCOUNT,
             "-s", KEYCHAIN_SERVICE,
             "-w"],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "The 'security' command is not available. "
            "This tool only works on macOS. "
            "On Replit, add POLY_PRIVATE_KEY to Replit Secrets instead."
        )

    if result.returncode != 0:
        raise RuntimeError(
            "Could not access Keychain. This wallet can only be unlocked on "
            "the Mac where it was created.\n"
            "Error: " + result.stderr.strip()
        )
    return result.stdout.strip()


def unlock_wallet() -> dict:
    """Decrypt and return wallet data from encrypted file."""
    if not WALLET_ENC.exists():
        raise FileNotFoundError(
            f"Encrypted wallet not found at {WALLET_ENC}. "
            "Run 'python -m src.wallet.wallet' to create one."
        )

    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise RuntimeError("cryptography package not installed: pip install cryptography")

    key = get_key_from_keychain()

    with open(WALLET_ENC) as f:
        data = json.load(f)

    fernet    = Fernet(key.encode())
    decrypted = fernet.decrypt(data["encrypted"].encode())
    return json.loads(decrypted)


def main():
    """CLI entry point."""
    try:
        wallet = unlock_wallet()

        print("=" * 50)
        print("🔫 CLAWBOTS WALLET — UNLOCKED")
        print("=" * 50)
        print()
        print(f"  Address:  {wallet['address']}")
        # BUG FIX: wallet dict stores 'chain_id' not 'network' — was KeyError
        chain_id = wallet.get("chain_id", 137)
        network  = wallet.get("network", f"Polygon (chain {chain_id})")
        print(f"  Network:  {network}")
        print()
        pk = wallet['private_key']
        print(f"  Private Key: {pk[:8]}...{pk[-6:]}  (masked — copy full value from Replit Secrets)")
        mn = wallet.get('mnemonic', 'N/A')
        mn_masked = mn[:8] + "..." if mn and mn != 'N/A' else mn
        print(f"  Mnemonic:    {mn_masked}  (masked — store securely offline)")
        print()
        print("⚠️  NEVER share these credentials with anyone.")
        print("⚠️  Copy POLY_PRIVATE_KEY to Replit Secrets, then delete wallet files.")
        print("=" * 50)

    except FileNotFoundError as e:
        print(f"❌ {e}")
        sys.exit(1)
    except RuntimeError as e:
        print(f"🔒 {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
