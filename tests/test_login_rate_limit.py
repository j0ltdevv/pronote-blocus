"""Limitation des tentatives de connexion web : 5 échecs par IP / 5 minutes.

Le compteur est un global de module : chaque test le remplace par une
instance neuve à horloge simulée, sinon les échecs d'un test bloqueraient
le suivant. Aucun réseau : `login_with_password` est doublé.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.pronote import auth as pronote_auth
from app.pronote import fetch
from server import deps, ratelimit

SECRET = Fernet.generate_key().decode()
IDENTIFIANTS = {"username": "eleve", "password": "secret"}

@pytest.fixture
def horloge(monkeypatch):
    """Limiteur neuf + temps simulé : rend `(limiteur, [instant])`."""
    maintenant = [1000.0]
    limiteur = ratelimit.LoginLimiter(horloge=lambda: maintenant[0])
    monkeypatch.setattr(ratelimit, "LIMITER", limiteur)
    return limiteur, maintenant

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

def _login_double(monkeypatch, etat: dict) -> None:
    """Double `login_with_password` ; `etat["echoue"]` pilote le résultat."""

    def fake_login(url, username, password, app_uuid, ent=None):  # noqa: ANN001
        etat["n"] += 1
        if etat["echoue"]:
            raise Exception("Identifiants refusés")
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

def _etat(echoue: bool = True) -> dict:
    return {"echoue": echoue, "n": 0}

def _tentative(client):
    return client.post("/api/auth/password", json=IDENTIFIANTS)

def test_cinq_echecs_puis_429(client, horloge, monkeypatch):
    """Les 5 premiers échecs passent en 401 ; le 6e est refusé sans appel réseau."""
    etat = _etat()
    _login_double(monkeypatch, etat)

    for _ in range(ratelimit.MAX_ECHECS):
        assert _tentative(client).status_code == 401

    refus = _tentative(client)
    assert refus.status_code == 429
    assert refus.headers["retry-after"] == str(ratelimit.FENETRE_SECONDS)
    assert etat["n"] == ratelimit.MAX_ECHECS

def test_succes_non_bloque_et_non_compte(client, horloge, monkeypatch):
    """Un succès passe même après des échecs, et n'alimente pas le compteur."""
    etat = _etat()
    _login_double(monkeypatch, etat)
    for _ in range(ratelimit.MAX_ECHECS - 1):
        assert _tentative(client).status_code == 401

    etat["echoue"] = False
    assert _tentative(client).status_code == 200

    # Le succès ne compte pas : il reste 4 échecs, donc cet échec-ci passe.
    etat["echoue"] = True
    assert _tentative(client).status_code == 401
    appels_avant_refus = etat["n"]
    assert _tentative(client).status_code == 429
    assert etat["n"] == appels_avant_refus

def test_pin_est_limite_aussi(client, horloge, monkeypatch):
    """Le code PIN est un autre chemin de connexion : même compteur."""
    def fake_login_qr(*args, **kwargs):  # noqa: ANN002, ANN003
        raise pronote_auth.QrExpired()

    monkeypatch.setattr(pronote_auth, "login_with_qr", fake_login_qr)

    def tentative_pin():
        deps.sessions()["sess-test"] = {
            "qr": {"login": "eleve", "jeton": "jeton", "url": "https://demo.example/"},
            "pronote_url": "https://demo.example/",
        }
        return client.post(
            "/api/auth/pin", json={"session_id": "sess-test", "pin": "1234"}
        )

    for _ in range(ratelimit.MAX_ECHECS):
        assert tentative_pin().status_code == 401
    assert tentative_pin().status_code == 429

def test_purge_apres_fenetre(client, horloge, monkeypatch):
    """Passé la fenêtre, l'IP repart à zéro et n'est plus bloquée."""
    limiteur, maintenant = horloge
    _login_double(monkeypatch, _etat())

    for _ in range(ratelimit.MAX_ECHECS):
        assert _tentative(client).status_code == 401
    assert _tentative(client).status_code == 429

    maintenant[0] += ratelimit.FENETRE_SECONDS + 1
    assert _tentative(client).status_code == 401
    assert all(len(tirs) == 1 for tirs in limiteur._echecs.values())

def test_client_ip_derriere_le_proxy():
    """Loopback : la dernière entrée XFF fait foi ; hors loopback, non."""
    def requete(hote, forwarded=None):
        entetes = {} if forwarded is None else {"x-forwarded-for": forwarded}
        return SimpleNamespace(
            client=SimpleNamespace(host=hote), headers=entetes
        )

    assert ratelimit.client_ip(requete("127.0.0.1", "203.0.113.9, 198.51.100.7")) == (
        "198.51.100.7"
    )
    assert ratelimit.client_ip(requete("::ffff:127.0.0.1", "203.0.113.9")) == "203.0.113.9"
    assert ratelimit.client_ip(requete("127.0.0.1")) == "127.0.0.1"
    assert ratelimit.client_ip(requete("198.51.100.7", "203.0.113.9")) == "198.51.100.7"

def test_purge_oublie_les_ip_inactives(horloge):
    """Les IP sans échec récent disparaissent : pas de fuite mémoire."""
    limiteur, maintenant = horloge
    for i in range(100):
        limiteur.echec(f"10.0.0.{i}")

    maintenant[0] += ratelimit.FENETRE_SECONDS + 1
    limiteur.verifier("10.0.0.1")
    assert limiteur._echecs == {}
