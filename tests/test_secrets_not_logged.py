"""Parcours avec secrets sentinelles : aucun secret ne doit fuir.

Les quatre valeurs sensibles du service (jeton Telegram, APP_SECRET, clé
LLM, mot de passe PRONOTE) sont montées dans les parcours web et bot avec
des marqueurs uniques (SENTINEL_*). Chaque sortie — logs (caplog), corps
de réponses HTTP, textes envoyés par le bot — est vérifiée : aucune ne
doit contenir une de ces valeurs.

Les legs d'échec simulent une dépendance dont le texte d'exception
embarque une valeur sensible : le serveur ne doit jamais relayer ce texte
tel quel, ni dans un log ni dans une réponse.
"""

from __future__ import annotations

import importlib
import logging
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


from app import config, db, llm
from app.pronote import auth, fetch
from server import deps
from tests.test_flow_telegram import patch_login, qr_png, run_flow, SUBJECTS

TG_TOKEN = "1234567890:SENTINEL_TG_TOKEN_qq7"
SECRET = Fernet.generate_key().decode()
LLM_KEY = "sk-SENTINEL-LLM-KEY-42"
PASSWORD = "MotDePasse-SENTINEL-77"

LLM_URL = "http://llm.sentinel.test/v1"
QR_URL = "https://0332722e.index-education.net/pronote/eleve.html"

SENTINELS = (TG_TOKEN, SECRET, LLM_KEY, PASSWORD)


# ─────────────────────────── montage commun ───────────────────────────


class _FakeResponse:
    """Réponse httpx minimale pour le mock du transport LLM."""

    def __init__(self, payload=None) -> None:  # noqa: ANN001
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self):  # noqa: ANN201
        return self._payload


@pytest.fixture
def http(monkeypatch):
    """Transport LLM simulé : succès par défaut, échec programmable."""
    state = SimpleNamespace(
        calls=[],
        error=None,
        payload={"choices": [{"message": {"content": "Réponse simulée."}}]},
    )

    def post(url: str, **kwargs):  # noqa: ANN001
        state.calls.append((url, kwargs))
        if state.error is not None:
            raise state.error
        return _FakeResponse(state.payload)

    monkeypatch.setattr(llm.httpx, "post", post)
    return state


@pytest.fixture
def pronote_web(monkeypatch):
    """Doubles des connexions/collectes PRONOTE côté web (aucun réseau)."""
    state = SimpleNamespace(login_error=None, collect_calls=[])

    class _Client:
        def export_credentials(self):  # noqa: ANN202
            return {
                "username": "demonstration",
                "password": PASSWORD,
                "uuid": "uuid-web",
                "client_identifier": "ident-web",
            }

    def login_with_qr(qr, pin, app_uuid, account_pin=None, **kwargs):  # noqa: ANN001
        if state.login_error is not None:
            raise state.login_error
        return _Client(), {
            "password": PASSWORD,
            "username": "demonstration",
            "uuid": app_uuid,
            "client_identifier": "ident-web",
        }

    def login_with_password(pronote_url, username, password, app_uuid, ent=None):  # noqa: ANN001
        raise RuntimeError("identifiants refusés")

    def collect(client, conn, user_id, cfg, phase="items"):  # noqa: ANN001
        state.collect_calls.append((user_id, phase))
        return fetch.SyncResult(True, None, [], "3A", phase, 0)

    def sync_user(conn, user_id, cfg, phase="items"):  # noqa: ANN001
        return fetch.SyncResult(True, None, [], "3A", phase, 0)

    monkeypatch.setattr(auth, "login_with_qr", login_with_qr)
    monkeypatch.setattr(auth, "login_with_password", login_with_password)
    monkeypatch.setattr(fetch, "class_name_of", lambda client: "3A")
    monkeypatch.setattr(fetch, "subjects_of", lambda client, cfg: [])
    monkeypatch.setattr(fetch, "collect", collect)
    monkeypatch.setattr(fetch, "sync_user", sync_user)
    return state


# ─────────────────────────── parcours web ───────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Serveur web isolé avec les quatre secrets sentinelles en env."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TG_TOKEN)
    monkeypatch.setenv("LLM_BASE_URL", LLM_URL)
    monkeypatch.setenv("LLM_API_KEY", LLM_KEY)
    monkeypatch.setenv("LLM_MODEL", "modele-sentinel")
    monkeypatch.setattr(deps, "_conn", None)
    monkeypatch.setattr(deps, "_cfg", None)
    monkeypatch.setattr(deps, "_MEMORY", {})
    monkeypatch.setattr(deps, "_SESSIONS", {})

    # Import paresseux : `server.main` ouvre config + base au chargement.
    main = importlib.import_module("server.main")
    with TestClient(main.app) as test_client:
        yield test_client

    conn = deps._conn
    if conn is not None:
        conn.close()


def test_parcours_web_aucun_secret_dans_logs_ni_reponses(
    client, pronote_web, http, caplog
):
    """Tout le cycle web tourne avec les sentinelles : ni logs ni réponses ne
    les contiennent, y compris sur les chemins d'échec."""
    reponses = []

    def appel(methode: str, path: str, **kwargs):  # noqa: ANN001
        reponse = getattr(client, methode)(path, **kwargs)
        reponses.append(reponse)
        return reponse

    with caplog.at_level(logging.DEBUG):
        # Publique : santé, options, et routes protégées fermées sans jeton.
        assert appel("get", "/health").status_code == 200
        assert appel("get", "/api/auth/options").status_code == 200
        assert appel("get", "/api/items").status_code == 401

        # QR valide → session PIN.
        reponse = appel("post", "/api/auth/qr", json={"pronote_url": QR_URL})
        assert reponse.status_code == 200
        session_id = reponse.json()["session_id"]

        # Échec PIN : l'exception porte le mot de passe → 502 générique, sans
        # relayer le texte de l'exception.
        pronote_web.login_error = RuntimeError(f"PRONOTE a renvoyé : {PASSWORD}")
        reponse = appel(
            "post",
            "/api/auth/pin",
            json={"session_id": session_id, "pin": "1234"},
        )
        assert reponse.status_code == 502
        assert "PRONOTE impossible" in reponse.json()["detail"]
        pronote_web.login_error = None

        # Parcours nominal : QR → PIN → jeton → données → chat → logout.
        reponse = appel("post", "/api/auth/qr", json={"pronote_url": QR_URL})
        session_id = reponse.json()["session_id"]
        reponse = appel(
            "post",
            "/api/auth/pin",
            json={"session_id": session_id, "pin": "1234"},
        )
        assert reponse.status_code == 200
        jeton = reponse.json()["token"]
        entete = {"Authorization": f"Bearer {jeton}"}

        assert appel("get", "/api/items", headers=entete).status_code == 200
        assert (
            appel(
                "post",
                "/api/items/done",
                headers=entete,
                json={"item_id": "aucun"},
            ).status_code
            == 200
        )
        assert (
            appel("post", "/api/items/refresh", headers=entete).status_code
            == 200
        )

        # Chat : succès d'abord (la clé part bien dans l'en-tête), puis panne
        # dont l'exception porte la clé LLM.
        reponse = appel(
            "post",
            "/api/chat",
            headers=entete,
            json={"question": "Quoi de neuf ?"},
        )
        assert reponse.status_code == 200
        assert http.calls
        assert (
            http.calls[0][1]["headers"]["Authorization"] == f"Bearer {LLM_KEY}"
        ), "la sentinelle LLM doit être la clé réellement envoyée"
        http.error = RuntimeError(f"fournisseur injoignable (clé {LLM_KEY})")
        reponse = appel(
            "post",
            "/api/chat",
            headers=entete,
            json={"question": "Encore ?"},
        )
        assert reponse.status_code == 200
        http.error = None

        # Chemins d'échec publics.
        reponse = appel(
            "post", "/api/auth/qr", json={"pronote_url": "http://127.0.0.1/"}
        )
        assert reponse.status_code == 400
        reponse = appel(
            "post",
            "/api/auth/password",
            json={"username": "", "password": PASSWORD},
        )
        assert reponse.status_code == 400
        # Validation : un corps sans `username` renvoie un 422 — FastAPI
        # reflète l'`input` dans sa réponse par défaut, le handler maison le
        # retire donc le mot de passe ne revient jamais dans le corps.
        reponse = appel(
            "post",
            "/api/auth/password",
            json={"password": PASSWORD},
        )
        assert reponse.status_code == 422
        assert PASSWORD not in reponse.text
        reponse = appel(
            "post",
            "/api/auth/password",
            json={"username": "demonstration", "password": PASSWORD},
        )
        assert reponse.status_code == 401
        reponse = appel(
            "post",
            "/api/auth/pin",
            json={"session_id": "inconnue", "pin": "0000"},
        )
        assert reponse.status_code == 404

        # Déconnexion : plus aucun accès (jeton révoqué, compte supprimé).
        assert (
            appel("post", "/api/auth/logout", headers=entete).status_code
            == 200
        )
        assert appel("get", "/api/items").status_code == 401

    # Aucune sentinelle dans les logs.
    for sentinelle in SENTINELS:
        assert sentinelle not in caplog.text, (
            f"{sentinelle} trouvée dans les logs"
        )

    # Aucune sentinelle dans aucun corps de réponse.
    for reponse in reponses:
        for sentinelle in SENTINELS:
            assert sentinelle not in reponse.text, (
                f"{sentinelle} trouvée dans la réponse {reponse.status_code} "
                f"{reponse.request.method} {reponse.url}"
            )


# ─────────────────────────── parcours bot ───────────────────────────


@pytest.fixture
def pronote_bot(monkeypatch):
    """Doubles réseau PRONOTE pour le bot (mêmes entrées que le parcours web)."""
    state = SimpleNamespace(items=[], subjects=list(SUBJECTS), class_name="3A")

    def class_name_of(client):  # noqa: ANN001
        return state.class_name

    def subjects_of(client, cfg):  # noqa: ANN001
        return list(state.subjects)

    def collect(client, conn, user_id, cfg, phase="items"):  # noqa: ANN001
        db.replace_items(conn, user_id, list(state.items))
        return fetch.SyncResult(True, None, [], state.class_name, phase, 0)

    def sync_user(conn, user_id, cfg, phase="items"):  # noqa: ANN001
        return fetch.SyncResult(True, None, [], state.class_name, phase, 0)

    monkeypatch.setattr(fetch, "class_name_of", class_name_of)
    monkeypatch.setattr(fetch, "subjects_of", subjects_of)
    monkeypatch.setattr(fetch, "collect", collect)
    monkeypatch.setattr(fetch, "sync_user", sync_user)
    return state


def test_parcours_bot_aucun_secret_dans_logs_ni_textes(
    caplog, conn, pronote_bot, http, monkeypatch, tmp_path
):
    """Le bot tourne avec le jeton Telegram sentinelle : ni logs ni textes
    envoyés ne contiennent les secrets, même quand le LLM échoue."""
    cfg = config.from_env(
        {
            "TELEGRAM_BOT_TOKEN": TG_TOKEN,
            "APP_SECRET": SECRET,
            "DATA_DIR": str(tmp_path / "data"),
            "WINDOW_START": "2026-10-01",
            "ADMIN_CHAT_ID": "999999",
            "LLM_BASE_URL": LLM_URL,
            "LLM_API_KEY": LLM_KEY,
            "LLM_MODEL": "modele-sentinel",
        }
    )
    http.error = RuntimeError(f"fournisseur injoignable (clé {LLM_KEY})")
    patch_login(monkeypatch, password=PASSWORD)
    envoyes = []

    async def scenario(rig):
        envoyes.extend(await rig.text("/start"))
        envoyes.extend(await rig.photo(qr_png()))
        envoyes.extend(await rig.text("1234"))
        envoyes.extend(await rig.text("3 1 5"))
        envoyes.extend(await rig.text("Question libre ?"))

    with caplog.at_level(logging.DEBUG):
        run_flow(cfg, conn, scenario)

    # Les loggers qui pourraient porter le jeton dans une URL (PTB, httpx)
    # sont plafonnés par `build_application` : la garantie ne dépend pas du
    # niveau de la racine.
    for logger_name in ("telegram", "httpx", "httpcore"):
        assert logging.getLogger(logger_name).level >= logging.WARNING, (
            f"logger {logger_name} non plafonné"
        )

    for sentinelle in SENTINELS:
        assert sentinelle not in caplog.text, (
            f"{sentinelle} trouvée dans les logs du bot"
        )
        for texte in envoyes:
            assert sentinelle not in texte, (
                f"{sentinelle} trouvée dans un message envoyé"
            )
