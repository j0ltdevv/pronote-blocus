"""Chiffrement au repos des identifiants PRONOTE.

Le jeton n'est jamais écrit en clair, jamais loggué, jamais renvoyé dans un
message Telegram. Seule la base (et son backup) contient le chiffré.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class SecretError(RuntimeError):
    """APP_SECRET absent ou mal formé."""


def _fernet(secret: str) -> Fernet:
    if not secret:
        raise SecretError("APP_SECRET vide")
    try:
        return Fernet(secret.encode())
    except (ValueError, TypeError) as exc:
        raise SecretError(
            "APP_SECRET doit être une clé Fernet (44 caractères base64 url-safe) : "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        ) from exc


def encrypt(plain: str, secret: str) -> bytes:
    return _fernet(secret).encrypt(plain.encode())


def decrypt(blob: bytes, secret: str) -> str:
    try:
        return _fernet(secret).decrypt(bytes(blob)).decode()
    except (InvalidToken, TypeError, ValueError) as exc:
        raise SecretError(
            "impossible de déchiffrer : APP_SECRET a changé ou la donnée est corrompue"
        ) from exc


def new_app_uuid() -> str:
    """Identifiant d'appareil PRONOTE, généré UNE fois par compte."""
    import uuid

    return str(uuid.uuid4())
