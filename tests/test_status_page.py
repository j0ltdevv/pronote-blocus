"""Page de statut publique : lisible sans jeton, sans donnée d'élève.

La base vit dans `tmp_path` ; aucun appel PRONOTE.
"""

from __future__ import annotations

import importlib
import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import db
from app.pronote import fetch
from server import deps
from server.routes import status

SECRET = Fernet.generate_key().decode()
PRONOTE_URL = "https://demo.index-education.net/pronote/eleve.html"


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

    main = importlib.import_module("server.main")
    with TestClient(main.app) as test_client:
        yield test_client

    conn = deps._conn
    if conn is not None:
        conn.close()


@pytest.fixture
def conn(client):
    return deps.get_conn()


def _compte(conn, *, chat_id: int, username: str, classe: str | None = None) -> str:
    user_id = db.create_user(
        conn,
        chat_id=chat_id,
        pronote_url=PRONOTE_URL,
        username=username,
        creds={
            "password": "jeton-pronote-secret",
            "client_identifier": "ident-1",
            "uuid": "b4f1c2a8-6d3e-4f5a-9c7b-2e8d1a0f6c33",
        },
        secret=SECRET,
        class_name=classe,
    )
    return user_id


# ── routes publiques ───────────────────────────────────────────────────

def test_status_et_status_json_sans_authentification(client):
    page = client.get("/status")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "Pronote Blocus" in page.text

    reponse = client.get("/status.json")
    assert reponse.status_code == 200
    assert reponse.headers["content-type"].startswith("application/json")
    donnees = json.loads(reponse.text)
    assert set(donnees) == {"uptime_seconds", "last_sync_at", "users", "last_error"}


def test_status_utilise_une_feuille_externe(client):
    """La CSP de production interdit `style-src 'unsafe-inline'`."""
    page = client.get("/status")
    assert '<link rel="stylesheet" href="/status.css">' in page.text
    assert "<style" not in page.text

    css = client.get("/status.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert "body" in css.text


def test_status_vide_ne_ment_pas(client):
    donnees = client.get("/status.json").json()
    assert donnees["users"] == 0
    assert donnees["last_sync_at"] is None
    assert donnees["last_error"] is None
    assert donnees["uptime_seconds"] >= 0

    page = client.get("/status").text
    assert "jamais" in page
    assert "aucune" in page


def test_status_reflete_la_base(client, conn):
    premier = _compte(conn, chat_id=101, username="eleve-a")
    _compte(conn, chat_id=102, username="eleve-b")
    db.mark_synced(conn, premier)
    db.set_last_error(conn, premier, fetch.ERR_TIMEOUT)
    attendu = conn.execute(
        "SELECT last_sync_at FROM users WHERE id = ?", (premier,)
    ).fetchone()["last_sync_at"]

    donnees = client.get("/status.json").json()
    assert donnees["users"] == 2
    assert donnees["last_sync_at"] == attendu
    assert donnees["last_error"] == fetch.ERR_TIMEOUT

    page = client.get("/status").text
    assert fetch.ERR_TIMEOUT in page
    assert "2" in page


def test_status_ne_divulgue_ni_jeton_ni_eleve(client, conn):
    username = "Élève Dupont"
    user_id = _compte(conn, chat_id=201, username=username, classe="2nde B")
    token = deps.issue_token(user_id, conn)
    # Erreur volontairement polluée : même un texte libre ne doit pas sortir.
    db.set_last_error(conn, user_id, f"{username} {token} jeton-pronote-secret")

    for url in ("/status", "/status.json"):
        rendu = client.get(url).text
        assert token not in rendu
        assert username not in rendu
        assert "jeton-pronote-secret" not in rendu
        assert "2nde B" not in rendu
        assert status.MASKED_ERROR in rendu
