"""
filecrypt.py -- encryption at rest for stored contracts (built 2026-10-24).

Brokerage archive PDFs are kept 5 years on the Railway volume. With
FILE_ENCRYPTION_KEY set, each file is written as AES-256-GCM ciphertext and
only decrypted in memory when a signed-in broker downloads it. Someone who
gets a copy of the volume or a backup gets unreadable files; someone who
controls the running app (and so the key) does not -- this is not a
substitute for the app's own access checks.

Format: MAGIC + 12-byte nonce + ciphertext (with GCM tag). Files without
MAGIC are legacy plaintext and are returned as-is, so existing archives keep
working until tools/encrypt_archive.py converts them.

Key: 32 random bytes, urlsafe base64. Make one with
    python3 -c "import filecrypt; print(filecrypt.new_key())"
Losing or changing the key makes every encrypted file unreadable for good --
keep a copy outside Railway (e.g. a password manager).

Without the key, new files are written as plaintext (feature off) and a
warning is printed at startup; reading an encrypted file without the right
key raises FileCryptError instead of returning garbage.
"""
import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"TXOENC1\n"
_NONCE = 12


class FileCryptError(Exception):
    pass


def new_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def _key():
    raw = os.environ.get("FILE_ENCRYPTION_KEY", "").strip()
    if not raw:
        return None
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception:
        raise FileCryptError("FILE_ENCRYPTION_KEY is not valid base64.")
    if len(key) != 32:
        raise FileCryptError("FILE_ENCRYPTION_KEY must decode to 32 bytes.")
    return key


def enabled() -> bool:
    return _key() is not None


def is_encrypted(data: bytes) -> bool:
    return data[:len(MAGIC)] == MAGIC


def encrypt(data: bytes) -> bytes:
    """Ciphertext when the key is set, else the plaintext unchanged."""
    key = _key()
    if key is None or is_encrypted(data):
        return data
    nonce = os.urandom(_NONCE)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, MAGIC)


def decrypt(data: bytes) -> bytes:
    """Plaintext for both encrypted and legacy plaintext files."""
    if not is_encrypted(data):
        return data
    key = _key()
    if key is None:
        raise FileCryptError("File is encrypted but FILE_ENCRYPTION_KEY is not set.")
    body = data[len(MAGIC):]
    try:
        return AESGCM(key).decrypt(body[:_NONCE], body[_NONCE:], MAGIC)
    except InvalidTag:
        raise FileCryptError("File could not be decrypted (wrong key or damaged file).")


def write_file(path: str, data: bytes):
    """Write (encrypted when enabled) atomically: a crash never leaves half a file."""
    tmp = f"{path}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    with open(tmp, "wb") as f:
        f.write(encrypt(data))
    os.replace(tmp, path)


def read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return decrypt(f.read())


def startup_check():
    """Called once at import of app.py: say loudly if the key is missing or bad."""
    try:
        if not enabled():
            print("[filecrypt] FILE_ENCRYPTION_KEY not set -- brokerage archive files are stored unencrypted.")
    except FileCryptError as e:
        print(f"[filecrypt] {e} New archive files cannot be saved until it is fixed.")
