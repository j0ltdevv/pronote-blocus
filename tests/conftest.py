"""Fixtures partagées : base SQLite isolée, secret Fernet, utilisateur.

Aucun test ne touche le réseau ni le répertoire de l'instance de production.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography.fernet import Fernet  # noqa: E402

from app import db  # noqa: E402

TODAY = "2026-10-17"


@pytest.fixture
def secret() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def other_secret() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "app.db")
    db.init_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def make_user(conn, secret):
    """Crée un compte minimal et rend son `id`."""
    made = 0

    def _make(
        *,
        chat_id: int | None = None,
        password: str = "jeton-initial",
        username: str = "demonstration",
        client_identifier: str | None = "ident-1",
    ) -> str:
        nonlocal made
        made += 1
        return db.create_user(
            conn,
            chat_id=chat_id if chat_id is not None else 1000 + made,
            pronote_url="https://demo.index-education.net/pronote/eleve.html",
            username=username,
            creds={
                "password": password,
                "client_identifier": client_identifier,
                "uuid": "b4f1c2a8-6d3e-4f5a-9c7b-2e8d1a0f6c33",
            },
            secret=secret,
        )

    return _make
