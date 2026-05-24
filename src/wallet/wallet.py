"""Wallet creation and key management for Polymarket trading."""

import json
import subprocess
from pathlib import Path
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

try:
    from cryptography.fernet import Fernet
    HAS_FERNET = True
except ImportError:
    HAS_FERNET = False

# Polygon mainnet chain ID
CHAIN_ID = 137

# Key store location
KEYS_DIR = Path(__file__).parent.parent.parent / "config" / "keys"
KEYS_FILE = KEYS_DIR / "wallet.json"
ENCRYPTED_FILE = KEYS_DIR / "wallet.enc"
KEYCHAIN_SERVICE = "clawbots-wallet-key"


def _get_keychain_key() -> str | None:
    """Retrieve encryption key from macOS Keychain."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _decrypt_wallet() -> dict | None:
    """Load and decrypt wallet from wallet.enc using Keychain key."""
    if not ENCRYPTED_FILE.exists() or not HAS_FERNET:
        return None

    key = _get_keychain_key()
    if not key:
        return None

    try:
        with open(ENCRYPTED_FILE) as f:
            enc_data = json.load(f)

        fernet = Fernet(key.encode())
        decrypted = fernet.decrypt(enc_data["encrypted"].encode()).decode()
        return json.loads(decrypted)
    except Exception:
        return None


def create_wallet() -> dict:
    """Create a new random Polygon wallet and save it securely.

    Returns:
        dict with 'address', 'private_key', 'mnemonic' (if available)
    """
    # Generate a new account
    Account.enable_unaudited_hdwallet_features()
    account, mnemonic = Account.create_with_mnemonic()

    wallet_data = {
        "address": account.address,
        "private_key": account.key.hex(),
        "mnemonic": mnemonic,
        "chain_id": CHAIN_ID,
        "created_at": __import__("datetime").datetime.now().isoformat(),
    }

    # Save to file with restricted permissions
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    with open(KEYS_FILE, "w") as f:
        json.dump(wallet_data, f, indent=2)

    # Restrict file permissions (owner read/write only)
    KEYS_FILE.chmod(0o600)

    return wallet_data


def load_wallet() -> dict:
    """Load wallet from key store (encrypted or plain JSON).

    Returns:
        dict with 'address', 'private_key', etc.
    """
    # Try encrypted wallet first (wallet.enc + Keychain)
    wallet = _decrypt_wallet()
    if wallet:
        return wallet

    # Fall back to plain JSON wallet
    if not KEYS_FILE.exists():
        raise FileNotFoundError(
            f"No wallet found at {KEYS_FILE} or {ENCRYPTED_FILE}. "
            f"Run 'python -m src.wallet.create_wallet' first."
        )

    with open(KEYS_FILE) as f:
        return json.load(f)


def sign_message(private_key: str, message: str) -> str:
    """Sign a message with the private key (EIP-191)."""
    account = Account.from_key(private_key)
    signed = account.sign_message(encode_defunct(text=message))
    return signed.signature.hex()


def get_address_from_key(private_key: str) -> str:
    """Derive the Ethereum address from a private key."""
    account = Account.from_key(private_key)
    return account.address


def validate_private_key(key: str) -> bool:
    """Validate that a private key is well-formed."""
    try:
        if not key.startswith("0x"):
            key = "0x" + key
        if len(key) != 66:
            return False
        Account.from_key(key)
        return True
    except Exception:
        return False


def wallet_summary(wallet: dict) -> str:
    """Return a safe summary string for logging (no private key)."""
    return (
        f"Wallet: {wallet['address']}\n"
        f"Chain: Polygon ({wallet['chain_id']})\n"
        f"Created: {wallet['created_at']}\n"
        f"⚠️  Fund this address with USDC on Polygon to start trading."
    )


if __name__ == "__main__":
    import sys

    if ENCRYPTED_FILE.exists() or KEYS_FILE.exists():
        print("Loading existing wallet...")
        wallet = load_wallet()
    else:
        print("🔑 Creating new Polygon wallet...")
        wallet = create_wallet()

    print()
    print(wallet_summary(wallet))
    print()
    print(f"Private Key: {wallet['private_key'][:8]}...{wallet['private_key'][-6:]}")
    print(f"Mnemonic: {'Saved in wallet file' if 'mnemonic' in wallet else 'N/A'}")
    print()
    print(f"✅ Wallet loaded from: {ENCRYPTED_FILE if ENCRYPTED_FILE.exists() else KEYS_FILE}")
    print()
    print("Next steps:")
    print("1. Add POLY_PRIVATE_KEY to config/.env (or use encrypted wallet)")
    print("2. Fund your wallet with USDC on Polygon network")
    print("3. Run the bot in dry-run mode first: python -m src.bot --dry-run")