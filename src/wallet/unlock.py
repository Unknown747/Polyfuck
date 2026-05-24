"""Unlock and display wallet credentials from encrypted storage.

Only works on this Mac - the decryption key is in the macOS Keychain.
"""

import json
import subprocess
from pathlib import Path

from cryptography.fernet import Fernet

WALLET_ENC = Path(__file__).parent.parent.parent / "config" / "keys" / "wallet.enc"
KEYCHAIN_ACCOUNT = "clawbots-polymarket"
KEYCHAIN_SERVICE = "clawbots-wallet-key"


def get_key_from_keychain() -> str:
    """Retrieve the decryption key from macOS Keychain."""
    result = subprocess.run(
        ["security", "find-generic-password",
         "-a", KEYCHAIN_ACCOUNT,
         "-s", KEYCHAIN_SERVICE,
         "-w"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Could not access keychain. This wallet can only be unlocked on "
            "the Mac where it was created. Error: " + result.stderr.strip()
        )
    return result.stdout.strip()


def unlock_wallet() -> dict:
    """Decrypt and return wallet data."""
    if not WALLET_ENC.exists():
        raise FileNotFoundError(
            f"Encrypted wallet not found at {WALLET_ENC}. "
            "Run 'python -m src.wallet.create_wallet' first."
        )

    # Get decryption key from Keychain
    key = get_key_from_keychain()

    # Read encrypted data
    with open(WALLET_ENC) as f:
        data = json.load(f)

    # Decrypt
    fernet = Fernet(key.encode())
    decrypted = fernet.decrypt(data["encrypted"].encode())
    return json.loads(decrypted)


def main():
    """CLI entry point."""
    try:
        wallet = unlock_wallet()

        print("=" * 50)
        print("🔫 LIL MUTANTS WALLET - UNLOCKED")
        print("=" * 50)
        print()
        print(f"  Address:   {wallet['address']}")
        print(f"  Network:   {wallet['network']} (Chain {wallet['chain_id']})")
        print()
        print(f"  Private Key: {wallet['private_key']}")
        print(f"  Mnemonic:     {wallet['mnemonic']}")
        print()
        print("⚠️  NEVER share these credentials with anyone.")
        print("⚠️  This output is NOT logged by the bot.")
        print("=" * 50)

    except FileNotFoundError as e:
        print(f"❌ {e}")
    except RuntimeError as e:
        print(f"🔒 {e}")
    except Exception as e:
        print(f"❌ Error: {e}")


if __name__ == "__main__":
    main()