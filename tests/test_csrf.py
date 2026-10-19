"""Tests de la protection CSRF des écritures web.

Le cookie de session (httpOnly) suffit à authentifier, donc une page tierce
pourrait déclencher `POST /api/...` à l'insu de l'élève. Chaque écriture doit
donc présenter un jeton dérivé de sa session, en en-tête ou en champ caché.
"""

from __future__ import annotations

import importlib
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
ORIGIN = "http://localhost:8000"


# ── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Serveur isolé : config et base dans `tmp_path`, magasins vidés."""
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


@pytest.fixture
def pronote_hors_ligne(monkeypatch):
    """Double `login_with_qr` / `collect` : la connexion PIN sans réseau."""
    appels: dict[str, object] = {}

    def fake_login(qr, pin, app_uuid, account_pin=None):  # noqa: ANN001
        appels["pin"] = pin
        client_pronote = SimpleNamespace(logged_in=True)
        creds = {
            "password": "jeton-pronote",
            "client_identifier": "ident-1",
            "uuid": app_uuid,
            "username": qr.get("login", ""),
        }
        return client_pronote, creds

    def fake_collect(client_pronote, conn, user_id, cfg, phase="items"):  # noqa: ANN001
        appels["collecte"] = (user_id, phase)
        return SimpleNamespace(ok=True, items=[])

    monkeypatch.setattr(pronote_auth, "login_with_qr", fake_login)
    monkeypatch.setattr(fetch, "class_name_of", lambda client_pronote: "Terminale")
    monkeypatch.setattr(fetch, "collect", fake_collect)
    return appels


def _compte(conn, *, chat_id: int = 1) -> tuple[str, str]:
    """Crée un compte minimal et rend `(user_id, jeton)`."""
    user_id = db.create_user(
        conn,
        chat_id=chat_id,
        pronote_url=PRONOTE_URL,
        username=f"eleve{chat_id}",
        creds={
            "password": "jeton-initial",
            "client_identifier": "ident-1",
            "uuid": "b4f1c2a8-6d3e-4f5a-9c7b-2e8d1a0f6c33",
        },
        secret=SECRET,
    )
    return user_id, deps.issue_token(user_id, conn)


def _session_par_cookie(client, jeton: str) -> None:
    """Authentifie le client par le seul cookie, comme un navigateur relancé."""
    client.cookies.clear()
    client.cookies.set(deps.COOKIE_NAME, jeton, domain="")


def _entete(jeton: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {jeton}"}


def _csrf(jeton: str) -> dict[str, str]:
    return {deps.CSRF_HEADER_NAME: deps.csrf_token(jeton)}


# ── écritures protégées ────────────────────────────────────────────────


def test_requete_sans_jeton_csrf_est_refusee(client, conn):
    """Cookie de session seul : l'écriture est refusée, explicitement."""
    _, jeton = _compte(conn)
    _session_par_cookie(client, jeton)

    reponse = client.post("/api/chat", json={"question": ""})

    assert reponse.status_code == 403
    assert "CSRF" in reponse.json()["detail"]


def test_jeton_csrf_valide_laisse_passer(client, conn):
    """Le jeton dérivé de la session courante ouvre l'écriture."""
    _, jeton = _compte(conn)
    _session_par_cookie(client, jeton)

    reponse = client.post(
        "/api/chat", json={"question": ""}, headers=_csrf(jeton)
    )

    assert reponse.status_code == 200


def test_jeton_d_une_autre_session_est_refuse(client, conn):
    """Le jeton de B ne vaut pas pour la session A (aucune écriture)."""
    user_a, jeton_a = _compte(conn, chat_id=1)
    _, jeton_b = _compte(conn, chat_id=2)
    _session_par_cookie(client, jeton_a)

    reponse = client.post(
        "/api/items/done",
        json={"item_id": "devoir:x"},
        headers=_csrf(jeton_b),
    )

    assert reponse.status_code == 403
    assert "invalide" in reponse.json()["detail"]
    assert db.done_keys(conn, user_a) == set()


def test_jeton_non_ascii_est_refuse_proprement(client, conn):
    """Un jeton hostile ne doit pas provoquer une erreur 500."""
    _, jeton = _compte(conn)
    _session_par_cookie(client, jeton)

    reponse = client.post(
        "/api/auth/logout", data={deps.CSRF_FIELD_NAME: "é"}
    )

    assert reponse.status_code == 403


def test_le_champ_cache_d_un_formulaire_suffit(client, conn):
    """Les formulaires classiques passent par `csrf_token`, sans en-tête."""
    user_id, jeton = _compte(conn)
    _session_par_cookie(client, jeton)

    reponse = client.post(
        "/api/auth/logout", data={deps.CSRF_FIELD_NAME: deps.csrf_token(jeton)}
    )

    assert reponse.status_code == 200
    assert db.get_user(conn, user_id) is None


def test_l_en_tete_bearer_n_exige_pas_de_jeton_csrf(client, conn):
    """Un client qui présente le jeton en en-tête ne peut pas subir de CSRF."""
    _, jeton = _compte(conn)

    reponse = client.post(
        "/api/chat", json={"question": ""}, headers=_entete(jeton)
    )

    assert reponse.status_code == 200


# ── jeton rendu et cookie ──────────────────────────────────────────────


def test_csrf_est_lie_a_la_session(client, conn):
    """`/api/auth/csrf` rend le jeton de la session, dans un cookie Lax."""
    _, jeton_a = _compte(conn, chat_id=1)
    _, jeton_b = _compte(conn, chat_id=2)
    assert deps.csrf_token(jeton_a) != deps.csrf_token(jeton_b)

    _session_par_cookie(client, jeton_a)
    reponse = client.get("/api/auth/csrf")

    assert reponse.status_code == 200
    assert reponse.json()["csrf_token"] == deps.csrf_token(jeton_a)
    assert client.cookies.get(deps.CSRF_COOKIE_NAME) == deps.csrf_token(jeton_a)
    cookie = reponse.headers.get("set-cookie", "").lower()
    assert "blocus_csrf" in cookie
    assert "samesite=lax" in cookie


def test_cookie_mort_ne_bloque_pas_la_reconnexion(client, conn):
    """Cookie expiré ou révoqué : le jeton de pré-connexion rouvre le login."""
    _, jeton = _compte(conn)
    deps.revoke_token(jeton, conn)
    assert deps.resolve_token(jeton, conn) is None
    _session_par_cookie(client, jeton)

    prealable = client.get("/api/auth/csrf").json()["csrf_token"]
    reponse = client.post(
        "/api/auth/qr",
        json={"pronote_url": PRONOTE_URL},
        headers={"Origin": ORIGIN, deps.CSRF_HEADER_NAME: prealable},
    )

    assert reponse.status_code == 200
    assert reponse.json()["session_id"]


def test_login_depuis_un_navigateur_exige_le_jeton_de_pre_connexion(client, conn):
    """Un POST de connexion venu d'une page (Origin) doit porter le jeton."""
    refus = client.post(
        "/api/auth/qr", json={"pronote_url": PRONOTE_URL}, headers={"Origin": ORIGIN}
    )
    assert refus.status_code == 403

    jeton = client.get("/api/auth/csrf").json()["csrf_token"]
    accepte = client.post(
        "/api/auth/qr",
        json={"pronote_url": PRONOTE_URL},
        headers={"Origin": ORIGIN, deps.CSRF_HEADER_NAME: jeton},
    )
    assert accepte.status_code == 200
    assert accepte.json()["session_id"]


def test_login_rend_le_jeton_csrf_de_la_session(client, conn, pronote_hors_ligne):
    """`/pin` pose le cookie et rend le jeton qui autorise la suite."""
    qr = client.post("/api/auth/qr", json={"pronote_url": PRONOTE_URL}).json()
    corps = client.post(
        "/api/auth/pin", json={"session_id": qr["session_id"], "pin": "1234"}
    ).json()

    jeton = corps["token"]
    assert corps["csrf_token"] == deps.csrf_token(jeton)
    assert client.cookies.get(deps.CSRF_COOKIE_NAME) == corps["csrf_token"]
    assert client.cookies.get(deps.COOKIE_NAME) == jeton

    # Cookie seul, sans jetons : refus.
    assert client.post("/api/auth/logout").status_code == 403

    # Le jeton rendu par le login autorise la déconnexion.
    reponse = client.post(
        "/api/auth/logout", headers={deps.CSRF_HEADER_NAME: corps["csrf_token"]}
    )
    assert reponse.status_code == 200
    assert db.get_user(conn, corps["user_id"]) is None
