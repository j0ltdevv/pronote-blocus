"""Durcissement de l'authentification web : cookies, rotation, rate limit.

Couvre les points de l'audit de sécurité :
  - cookie de session : HttpOnly, SameSite=lax, Path, Max-Age, Secure (HTTPS) ;
  - expiration des jetons (base et mémoire) et des sessions QR→PIN ;
  - rotation de session après connexion (l'ancien jeton meurt) ;
  - limitation des tentatives de connexion (mot de passe et PIN) ;
  - jetons jamais conservés en clair (digest seul, comparaison à temps constant).

Aucun réseau : PRONOTE est doublé, la base vit dans `tmp_path`.
"""

from __future__ import annotations

import importlib
import re
from http.cookies import SimpleCookie
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import db
from app.pronote import auth as pronote_auth
from app.pronote import fetch
from server import deps

SECRET = Fernet.generate_key().decode()
PRONOTE_URL = "https://demo.index-education.net/pronote/eleve.html"
PIN_VALIDE = "1234"


# ── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Serveur isolé, comme dans test_server_auth.py."""
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
        # Jeton CSRF de pré-connexion : les routes d'écriture l'exigent.
        jeton = test_client.get("/api/auth/csrf").json()["csrf_token"]
        test_client.headers.update({"X-CSRF-Token": jeton, "Origin": "http://testserver"})
        yield test_client

    conn = deps._conn
    if conn is not None:
        conn.close()


@pytest.fixture
def conn(client):
    """Connexion sqlite du serveur de test."""
    return deps.get_conn()


@pytest.fixture
def pronote_pin(monkeypatch):
    """Double `login_with_qr` : PIN `1234` accepté, sinon échec (pas de réseau)."""
    def fake_login(qr, pin, app_uuid, account_pin=None):  # noqa: ANN001
        if pin != PIN_VALIDE:
            return SimpleNamespace(logged_in=False), {}
        creds = {
            "password": "jeton-pronote",
            "client_identifier": "ident-1",
            "uuid": app_uuid,
            "username": qr.get("login", ""),
        }
        return SimpleNamespace(logged_in=True), creds

    monkeypatch.setattr(pronote_auth, "login_with_qr", fake_login)
    monkeypatch.setattr(fetch, "class_name_of", lambda client_pronote: "Terminale")
    monkeypatch.setattr(
        fetch, "collect", lambda c, conn, uid, cfg, phase="items": SimpleNamespace(ok=True)
    )


@pytest.fixture
def pronote_mot_de_passe(monkeypatch):
    """Double `login_with_password` : `secret` accepté, sinon échec."""
    def fake_login(url, username, password, app_uuid, ent=None):  # noqa: ANN001
        if password != "secret":
            return SimpleNamespace(logged_in=False), {}
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


def _rafraichit_csrf(client) -> None:
    """Le jeton CSRF est lié à la session : on le redemande avant chaque écriture."""
    client.headers["X-CSRF-Token"] = client.get("/api/auth/csrf").json()["csrf_token"]


def _nouveau_qr(client) -> str:
    """Crée une session QR et rend son `session_id`."""
    _rafraichit_csrf(client)
    qr = client.post("/api/auth/qr", json={"pronote_url": PRONOTE_URL})
    assert qr.status_code == 200
    return qr.json()["session_id"]


def _connexion_pin(client, *, pin: str | None = None, session_id: str | None = None):
    if session_id is None:
        session_id = _nouveau_qr(client)
    return client.post(
        "/api/auth/pin",
        json={"session_id": session_id, "pin": pin or PIN_VALIDE},
    )


def _connexion_password(client, *, username: str = "eleve", password: str = "secret"):
    _rafraichit_csrf(client)
    return client.post(
        "/api/auth/password",
        json={"pronote_url": PRONOTE_URL, "username": username, "password": password},
    )


def _cookies(reponse) -> SimpleCookie:
    """Toutes les en-têtes `Set-Cookie` (session + CSRF) dans un seul pot."""
    cookie = SimpleCookie()
    for entete in reponse.headers.get_list("set-cookie"):
        cookie.load(entete)
    return cookie


def _entete(jeton: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {jeton}"}


# ── cookie de session ──────────────────────────────────────────────────


@pytest.mark.parametrize("route", ["pin", "password"])
def test_cookie_http_only_samesite_lax_path_et_max_age(client, pronote_pin, pronote_mot_de_passe, route):
    """Le cookie de session est HttpOnly, SameSite=lax, Path=/ et Max-Age=TTL."""
    reponse = (
        _connexion_pin(client) if route == "pin" else _connexion_password(client)
    )
    assert reponse.status_code == 200, reponse.text

    morsure = _cookies(reponse)[deps.COOKIE_NAME]
    assert morsure["httponly"] is True
    assert morsure["samesite"] == "lax"
    assert morsure["path"] == "/"
    assert int(morsure["max-age"]) == deps.TOKEN_TTL_SECONDS
    # Jamais de `Secure` en HTTP simple (l'instance LAN).
    assert "secure" not in morsure or not morsure["secure"]


def test_cookie_secure_posé_quand_la_requête_arrive_en_https(client, pronote_pin):
    """Avec `X-Forwarded-Proto: https`, le cookie reçoit le drapeau `Secure`."""
    session_id = _nouveau_qr(client)
    reponse = client.post(
        "/api/auth/pin",
        json={"session_id": session_id, "pin": PIN_VALIDE},
        headers={"x-forwarded-proto": "https"},
    )
    assert reponse.status_code == 200, reponse.text
    morsure = _cookies(reponse)[deps.COOKIE_NAME]
    assert morsure["secure"] is True


# ── expiration ─────────────────────────────────────────────────────────


def test_jeton_expire_en_base_est_refuse(client, conn):
    """Une ligne `web_sessions` au-delà de `expires_at` ne vaut plus rien."""
    user_id = db.create_user(
        conn,
        chat_id=1,
        pronote_url=PRONOTE_URL,
        username="eleve",
        creds={"password": "x", "client_identifier": "i", "uuid": "u"},
        secret=SECRET,
    )
    now = int(__import__("time").time())
    conn.execute(
        "INSERT INTO web_sessions (token_hash, user_id, created_at, expires_at)"
        " VALUES (?,?,?,?)",
        (deps._hash("jeton-vieillot"), user_id, "2026-01-01T00:00:00+00:00", now - 1),
    )
    conn.commit()
    assert deps.resolve_token("jeton-vieillot", conn) is None


def test_session_qr_expiree_est_refusee_et_purgee(client, pronote_pin, monkeypatch):
    """Une session QR au-delà de son TTL est refusée (404) puis purgée."""
    # TTL négatif : la session naît déjà expirée.
    monkeypatch.setattr(deps, "QR_SESSION_TTL_SECONDS", -60)
    session_id = _nouveau_qr(client)
    assert session_id in deps.sessions()

    reponse = client.post(
        "/api/auth/pin",
        json={"session_id": session_id, "pin": PIN_VALIDE},
    )
    assert reponse.status_code == 404

    # Le prochain usage du store purge les sessions mortes.
    _nouveau_qr(client)
    assert session_id not in deps.sessions()


# ── rotation de session ────────────────────────────────────────────────


def test_rotation_apres_connexion_pin(client, conn, pronote_pin, monkeypatch):
    """Une nouvelle connexion PIN tue l'ancien jeton (mémoire + base)."""
    # Même élève aux deux connexions : le login du QR doit être stable.
    def qr_stable(url):  # noqa: ANN001
        return {"login": "eleve-stable", "jeton": "x" * 32, "url": url}

    monkeypatch.setattr("server.routes.auth._qr_from_url", qr_stable)

    premier = _connexion_pin(client)
    assert premier.status_code == 200
    jeton_ancien = premier.json()["token"]
    assert client.get("/api/items", headers=_entete(jeton_ancien)).status_code == 200

    second = _connexion_pin(client)
    assert second.status_code == 200
    jeton_frais = second.json()["token"]

    # L'ancien jeton est mort, le nouveau vit.
    assert client.get("/api/items", headers=_entete(jeton_ancien)).status_code == 401
    assert client.get("/api/items", headers=_entete(jeton_frais)).status_code == 200

    # Une seule session web survivante pour ce compte.
    user_id = second.json()["user_id"]
    lignes = conn.execute(
        "SELECT count(*) FROM web_sessions WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    assert lignes == 1


def test_rotation_apres_connexion_mot_de_passe(client, conn, pronote_mot_de_passe):
    """Une nouvelle connexion par mot de passe tue l'ancien jeton."""
    premier = _connexion_password(client)
    assert premier.status_code == 200
    jeton_ancien = premier.json()["token"]

    second = _connexion_password(client)
    assert second.status_code == 200
    jeton_frais = second.json()["token"]

    assert client.get("/api/items", headers=_entete(jeton_ancien)).status_code == 401
    assert client.get("/api/items", headers=_entete(jeton_frais)).status_code == 200


# ── limitation des tentatives ──────────────────────────────────────────


def test_rate_limit_password_bloque_apres_cinq_echecs(client, conn, pronote_mot_de_passe):
    """5 échecs sur (IP, identifiant) → la 6e tentative est rejetée (429)."""
    for _ in range(deps.LOGIN_MAX_ATTEMPTS):
        reponse = _connexion_password(client, password="mauvais")
        assert reponse.status_code == 401

    # Même avec les bons identifiants, la route est verrouillée.
    bloque = _connexion_password(client)
    assert bloque.status_code == 429

    # Un autre identifiant n'est pas affecté : la clé contient l'username.
    autre = _connexion_password(client, username="autre-eleve")
    assert autre.status_code == 200


def test_rate_limit_pin_bloque_apres_cinq_echecs(client, conn, pronote_pin):
    """5 PIN refusés depuis la même IP → la 6e tentative est rejetée (429)."""
    for _ in range(deps.LOGIN_MAX_ATTEMPTS):
        reponse = _connexion_pin(client, pin="0000")
        assert reponse.status_code == 401

    # Un QR frais + le bon PIN ne suffisent plus : le verrou est par IP.
    bloque = _connexion_pin(client)
    assert bloque.status_code == 429


# ── secrets : jamais en clair, comparaison à temps constant ────────────


def test_jeton_jamais_en_clair_en_memoire(client, conn):
    """La mémoire ne retient que le digest : aucun jeton brut réutilisable."""
    user_id = db.create_user(
        conn,
        chat_id=2,
        pronote_url=PRONOTE_URL,
        username="eleve",
        creds={"password": "x", "client_identifier": "i", "uuid": "u"},
        secret=SECRET,
    )
    jeton = deps.issue_token(user_id)

    assert jeton not in deps._MEMORY
    assert all(re.fullmatch(r"[0-9a-f]{64}", clé) for clé in deps._MEMORY)
    assert deps.resolve_token(jeton) == user_id

    # Un jeton modifié est rejeté : la vérification passe par le digest.
    assert deps.resolve_token(jeton + "x") is None

    # La comparaison à temps constant porte sur le digest stocké : si celui-ci
    # diverge (corruption), le jeton est rejeté même avec la bonne clé.
    deps._MEMORY[deps._hash(jeton)] = (user_id, float("inf"), "0" * 64)
    assert deps.resolve_token(jeton) is None


def test_jeton_revoque_disparait_de_la_memoire(client, conn):
    """`revoke_token` efface le digest : le jeton ne résout plus rien."""
    user_id = db.create_user(
        conn,
        chat_id=3,
        pronote_url=PRONOTE_URL,
        username="eleve",
        creds={"password": "x", "client_identifier": "i", "uuid": "u"},
        secret=SECRET,
    )
    jeton = deps.issue_token(user_id)
    assert deps.resolve_token(jeton) == user_id

    deps.revoke_token(jeton)
    assert deps.resolve_token(jeton) is None
    assert deps._hash(jeton) not in deps._MEMORY
