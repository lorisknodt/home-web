"""Password hashing (scrypt), used by app/accounts.py.

The password is never stored in clear: only "scrypt:<salt>:<hash>" is written.
"""
import hashlib
import hmac
import secrets

N, R, P = 2**14, 8, 1


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P)
    # ":" as separator: no "$", which systemd or a shell could interpret
    return f"scrypt:{salt.hex()}:{digest.hex()}"


def verify(password, stored):
    try:
        algo, salt_hex, digest_hex = stored.replace("$", ":").split(":")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=N, r=R, p=P)
    return hmac.compare_digest(digest.hex(), digest_hex)
