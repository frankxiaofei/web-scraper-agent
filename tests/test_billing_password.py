"""Billing password hashing tests."""

from __future__ import annotations

from src.billing.password import hash_password, verify_password


def test_hash_and_verify_roundtrip():
    hashed = hash_password("SecureP@ss1")
    assert hashed != "SecureP@ss1"
    assert verify_password("SecureP@ss1", hashed)
    assert not verify_password("WrongP@ss1", hashed)
