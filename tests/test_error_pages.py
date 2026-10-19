"""Erreurs internes : réponse JSON pour `/api/*`, page HTML sobre sinon.

Le middleware doit rester *à l'intérieur* de celui des en-têtes de sécurité :
la page d'erreur doit porter la CSP et `nosniff` comme n'importe quelle
réponse. Les tests passent par l'application réelle (`server.main.app`) en y
insérant des routes qui explosent, puis les retirent.
"""

from __future__ import annotations

import importlib
import logging
import re

import pytest
from cryptography.fernet import Fernet
from fastapi import Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from server import deps, errors, security

SECRET = Fernet.generate_key().decode()
LLM_KEY = "sk-SENTINEL-ERROR-KEY-42"
PASSWORD = "MotDePasse-SENTINEL-77"
PASSWORD_BODY = "Corps-SENTINEL-88"
EMAIL = "eleve.sentinel@example.org"
TOKEN = "1234567890:SENTINEL_TELEGRAM_TOKEN_abcdefghijklmnop"
TELEPHONE = "0612345678"

ERREUR = (
    f"pour {EMAIL} mot de passe={PASSWORD} clé {LLM_KEY} "
    f"jeton {TOKEN} secret {SECRET} téléphone {TELEPHONE}"
)

INCIDENT_RE = re.compile(r"^[0-9a-f]{8}$")
SENTINELLES = (SECRET, LLM_KEY, PASSWORD, EMAIL, TOKEN, TELEPHONE)


async def _explose():
    raise RuntimeError(ERREUR)


async def _explose_apres_corps(request: Request):
    await request.body()
    raise RuntimeError("échec après lecture du corps")


def _routes():
    """Routes de test, insérées avant le montage statique (qui capte tout)."""
    return [
        APIRoute(
            "/api/test-erreur",
            _explose,
            methods=["GET"],
            include_in_schema=False,
        ),
        APIRoute(
            "/test-erreur",
            _explose,
            methods=["GET"],
            include_in_schema=False,
        ),
        APIRoute(
            "/api/test-erreur-corps",
            _explose_apres_corps,
            methods=["POST"],
            include_in_schema=False,
        ),
        APIRoute(
            "/test-erreur-corps",
            _explose_apres_corps,
            methods=["POST"],
            include_in_schema=False,
        ),
    ]


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Serveur réel isolé : config et base dans `tmp_path`, routes qui explosent."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.setenv("LLM_API_KEY", LLM_KEY)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    for variable in ("LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(deps, "_conn", None)
    monkeypatch.setattr(deps, "_cfg", None)
    monkeypatch.setattr(deps, "_MEMORY", {})
    monkeypatch.setattr(deps, "_SESSIONS", {})

    # Import paresseux : `server.main` ouvre config + base au chargement.
    main = importlib.import_module("server.main")
    ajoutees = _routes()
    for route in ajoutees:
        main.app.router.routes.insert(0, route)
    with TestClient(main.app) as test_client:
        yield test_client
    for route in ajoutees:
        main.app.router.routes.remove(route)

    conn = deps._conn
    if conn is not None:
        conn.close()


def _verifie_entetes(reponse):
    for nom, valeur in security.SECURITY_HEADERS.items():
        assert reponse.headers[nom] == valeur
    assert reponse.headers["cache-control"] == "no-store"


# ── forme de la réponse ────────────────────────────────────────────────


def test_api_renvoie_un_json_generique(client):
    """`/api/*` : JSON stable, sans texte technique ni trace."""
    reponse = client.get("/api/test-erreur")
    assert reponse.status_code == 500
    assert reponse.headers["content-type"].startswith("application/json")
    corps = reponse.json()
    assert corps["detail"] == errors.MESSAGE
    assert INCIDENT_RE.match(corps["incident"])
    assert reponse.headers[errors.INCIDENT_HEADER] == corps["incident"]
    assert "Traceback" not in reponse.text
    assert "RuntimeError" not in reponse.text
    _verifie_entetes(reponse)


def test_reste_du_site_renvoie_une_page_html_sobre(client):
    """Hors API : page lisible, sans script, avec la référence d'incident."""
    reponse = client.get("/test-erreur")
    assert reponse.status_code == 500
    assert reponse.headers["content-type"].startswith("text/html")
    assert errors.TITRE in reponse.text
    assert errors.MESSAGE in reponse.text
    assert "<script" not in reponse.text
    assert "Traceback" not in reponse.text
    assert "RuntimeError" not in reponse.text
    incident = re.search(r"<code>([0-9a-f]{8})</code>", reponse.text)
    assert incident, reponse.text
    assert reponse.headers[errors.INCIDENT_HEADER] == incident.group(1)
    _verifie_entetes(reponse)


def test_chaque_erreur_a_son_incident(client):
    premier = client.get("/api/test-erreur").json()["incident"]
    second = client.get("/api/test-erreur").json()["incident"]
    assert premier != second


def test_les_erreurs_http_ordinaires_ne_sont_pas_detournees(client):
    """Un 404 reste un 404 : le middleware ne remplace que les 500."""
    reponse = client.get("/api/route-qui-n-existe-pas")
    assert reponse.status_code == 404
    assert reponse.json()["detail"] == "Not Found"


# ── journalisation ─────────────────────────────────────────────────────


def test_le_log_porte_la_trace_complete_sans_secret_ni_donnees(client, caplog):
    """Trace utile (fichier, ligne, fonction) mais expurgée."""
    with caplog.at_level(logging.ERROR):
        reponse = client.get(f"/api/test-erreur?email={EMAIL}")
    incident = reponse.json()["incident"]

    assert incident in caplog.text
    assert "GET /api/test-erreur" in caplog.text
    assert "Traceback (most recent call last)" in caplog.text
    assert "_explose" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "***" in caplog.text

    for sentinelle in SENTINELLES:
        assert sentinelle not in caplog.text, sentinelle


def test_le_corps_de_requete_n_est_jamais_journalise(client, caplog):
    """Le bilan d'erreur ne recopie ni corps, ni en-tête, ni cookie."""
    with caplog.at_level(logging.ERROR):
        reponse = client.post(
            "/api/test-erreur-corps",
            json={"password": PASSWORD_BODY, "email": EMAIL},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert reponse.status_code == 500
    assert PASSWORD_BODY not in caplog.text
    assert EMAIL not in caplog.text
    assert TOKEN not in caplog.text


# ── masquage ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "brut",
    [
        f"Bearer {TOKEN}",
        f"Basic {PASSWORD}",
        f"mot de passe = {PASSWORD}",
        f"token: {PASSWORD}",
        "clé " + Fernet.generate_key().decode(),
        "écrire à eleve.sentinel@example.org",
        "chat_id 1234567890",
        "https://pronote.test/eleve.html?login=eleve.sentinel&uuid=abc",
        "https://eleve:motdepasse-sentinel@pronote.test/eleve.html",
        "blocus_session=jeton-session-sentinel",
    ],
)
def test_masquage_des_formes_sensibles(brut):
    masque = errors.redact(brut)
    assert "***" in masque
    for interdit in (
        PASSWORD,
        EMAIL,
        TOKEN,
        "1234567890",
        "eleve.sentinel",
        "motdepasse-sentinel",
        "jeton-session-sentinel",
    ):
        assert interdit not in masque
