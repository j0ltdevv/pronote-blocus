"""Sonde `/healthz` : 200 si la base répond, 503 si elle est injoignable.

Isolation : base SQLite dans `tmp_path`, aucun appel réseau. La panne est
simulée en fermant la connexion du serveur (fichier disparu, disque plein…).
"""

from __future__ import annotations

import importlib

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from server import deps

SECRET = Fernet.generate_key().decode()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Serveur isolé : config et base dans `tmp_path`, magasins mémoire vidés."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    for variable in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(deps, "_conn", None)
    monkeypatch.setattr(deps, "_cfg", None)
    monkeypatch.setattr(deps, "_MEMORY", {})
    monkeypatch.setattr(deps, "_SESSIONS", {})

    # Import paresseux : `server.main` ouvre la base au chargement, il doit donc
    # voir le DATA_DIR du test (jamais celui de l'instance de production).
    main = importlib.import_module("server.main")
    with TestClient(main.app) as test_client:
        yield test_client

    conn = deps._conn
    if conn is not None:
        conn.close()


# ── sonde de vivacité ──────────────────────────────────────────────────


def test_healthz_base_ok(client):
    """Base joignable : 200 et JSON minimal, sans jeton requis."""
    reponse = client.get("/healthz")
    assert reponse.status_code == 200
    assert reponse.json() == {"status": "ok", "db": "ok"}


def test_healthz_base_inaccessible(client):
    """Base morte (connexion fermée) : 503, sans chemin ni trace technique."""
    chemin = str(deps.get_cfg().db_path)
    deps.get_conn().close()

    reponse = client.get("/healthz")
    assert reponse.status_code == 503
    assert reponse.json() == {"status": "degraded", "db": "unavailable"}
    # Aucune fuite : ni emplacement de la base, ni détail de l'exception.
    assert chemin not in reponse.text
    assert "Traceback" not in reponse.text
    assert "closed" not in reponse.text