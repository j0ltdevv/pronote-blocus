"""Tests de la connexion par mot de passe.

Isolation : base SQLite dans un répertoire temporaire, appels PRONOTE doublés.
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pronotepy.ent
import pytest
from fastapi.testclient import TestClient

from app import db
from app.pronote import auth as pronote_auth
from app.pronote import fetch
from server import deps

from cryptography.fernet import Fernet

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
    """Connexion sqlite du serveur de test."""
    return deps.get_conn()


@pytest.fixture
def pronote_mot_de_passe(monkeypatch):
    """Double `login_with_password` : aucune requête réseau dans les tests."""
    appels: dict[str, object] = {}

    def fake_login(url, username, password, app_uuid, ent=None):  # noqa: ANN001
        appels["url"] = url
        appels["ent"] = ent
        appels["username"] = username
        return (
            SimpleNamespace(logged_in=True),
            {
                "password": password,
                "client_identifier": "ident-1",
                "uuid": app_uuid,
                "username": username,
            },
        )

    monkeypatch.setattr(pronote_auth, "login_with_password", fake_login)
    monkeypatch.setattr(fetch, "class_name_of", lambda client_pronote: "Terminale")
    monkeypatch.setattr(
        fetch, "collect", lambda c, conn, uid, cfg, phase="items": SimpleNamespace(ok=True)
    )
    return appels


def _comptes(conn) -> int:
    """Nombre de comptes créés en base."""
    return conn.execute("select count(*) from users").fetchone()[0]


def test_pronote_url_omis_utilise_default(client, pronote_mot_de_passe):
    """Sans `pronote_url`, la route utilise `cfg.default_pronote_url`."""
    reponse = client.post(
        "/api/auth/password",
        json={
            "username": "eleve",
            "password": "secret",
        },
    )
    assert reponse.status_code == 200, reponse.text
    assert pronote_mot_de_passe["url"] == deps.get_cfg().default_pronote_url


def test_ent_omis_utilise_default(client, pronote_mot_de_passe):
    """Sans `ent`, la route utilise `cfg.password_ent`."""
    reponse = client.post(
        "/api/auth/password",
        json={
            "username": "eleve",
            "password": "secret",
        },
    )
    assert reponse.status_code == 200, reponse.text
    assert pronote_mot_de_passe["ent"] is getattr(pronotepy.ent, deps.get_cfg().password_ent)


def test_ent_inconnu_est_refuse(client, pronote_mot_de_passe):
    """ENT inconnu → 400 et aucun appel à `login_with_password`."""
    reponse = client.post(
        "/api/auth/password",
        json={
            "username": "eleve",
            "password": "secret",
            "ent": "pas-un-ent",
        },
    )
    assert reponse.status_code == 400
    assert "url" not in pronote_mot_de_passe


def test_identifiant_vide_est_refuse(client, pronote_mot_de_passe):
    """Identifiant vide → 400."""
    reponse = client.post(
        "/api/auth/password",
        json={
            "username": "   ",
            "password": "secret",
        },
    )
    assert reponse.status_code == 400
    assert "url" not in pronote_mot_de_passe


def test_echoue_pronote_n_cree_pas_compte(client, conn, monkeypatch):
    """Échec PRONOTE → 401, et aucun compte créé en base."""

    def fake_login_echoue(url, username, password, app_uuid, ent=None):
        raise Exception("Erreur PRONOTE")

    monkeypatch.setattr(pronote_auth, "login_with_password", fake_login_echoue)

    reponse = client.post(
        "/api/auth/password",
        json={
            "username": "eleve",
            "password": "secret",
        },
    )
    assert reponse.status_code == 401
    assert _comptes(conn) == 0


def test_reponse_401_ne_contient_pas_le_mot_de_passe(client, conn, monkeypatch):
    """Le corps de la réponse 401 ne contient jamais le mot de passe soumis."""

    def fake_login_echoue(url, username, password, app_uuid, ent=None):
        raise Exception("Erreur PRONOTE")

    monkeypatch.setattr(pronote_auth, "login_with_password", fake_login_echoue)

    reponse = client.post(
        "/api/auth/password",
        json={
            "username": "eleve",
            "password": "monMotDePasseSecret123",
        },
    )
    assert reponse.status_code == 401
    corps = reponse.json()
    assert "monMotDePasseSecret123" not in str(corps)
    assert "password" not in corps


def test_options_contient_default_url_default_ent_et_ents(client):
    """`GET /api/auth/options` → `default_url`, `default_ent`, et `lyceeconnecte_aquitaine` dans `ents`."""
    reponse = client.get("/api/auth/options")
    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["default_url"] == deps.get_cfg().default_pronote_url
    assert corps["default_ent"] == deps.get_cfg().password_ent
    assert "lyceeconnecte_aquitaine" in corps["ents"]
