"""Password hashing — bcrypt cost=12."""

from __future__ import annotations

import bcrypt

_ROUNDS = 12


def hash_password(password: str) -> str:
    digest = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=_ROUNDS))
    return digest.decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False
