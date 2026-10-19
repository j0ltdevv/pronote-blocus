"""Latence des requêtes : ligne de log par requête et p50/p95 sur cinq minutes.

Aucun corps ni secret ne doit sortir dans les logs ; la fenêtre est vérifiée
avec une horloge fausse, donc les tests ne dorment pas.
"""

from __future__ import annotations

import importlib
import logging
import re

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from server import deps, timing

LIGNE = re.compile(
    r"^requête method=(?P<method>\S+) path=(?P<path>\S+)"
    r" status=(?P<status>\d+) ms=(?P<ms>\d+\.\d+)$"
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Serveur isolé : base et config dans `tmp_path`, magasins vidés."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_SECRET", Fernet.generate_key().decode())
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    for variable in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(deps, "_conn", None)
    monkeypatch.setattr(deps, "_cfg", None)
    monkeypatch.setattr(deps, "_MEMORY", {})
    monkeypatch.setattr(deps, "_SESSIONS", {})
    timing.TIMINGS.clear()

    main = importlib.import_module("server.main")
    with TestClient(main.app) as test_client:
        yield test_client

    conn = deps._conn
    if conn is not None:
        conn.close()
    timing.TIMINGS.clear()


def _lignes(caplog) -> list[re.Match[str]]:
    """Lignes du middleware, déjà découpées en champs."""
    trouvees = []
    for record in caplog.records:
        if record.name != "server.timing":
            continue
        match = LIGNE.match(record.getMessage())
        assert match, f"ligne non structurée : {record.getMessage()!r}"
        trouvees.append(match)
    return trouvees


# ── ligne de log ───────────────────────────────────────────────────────


def test_chaque_requete_laisse_une_ligne(client, caplog):
    caplog.set_level(logging.INFO, logger="server.timing")

    client.get("/health")

    lignes = _lignes(caplog)
    assert len(lignes) == 1
    champs = lignes[0].groupdict()
    assert champs["method"] == "GET"
    assert champs["path"] == "/health"
    assert champs["status"] == "200"
    assert float(champs["ms"]) >= 0.0


def test_le_statut_reel_est_journalise(client, caplog):
    """Un 404 et un 401 sortent avec leur vrai code, pas un 200 générique."""
    caplog.set_level(logging.INFO, logger="server.timing")

    client.get("/introuvable")
    client.get("/api/items")

    statuts = [ligne["status"] for ligne in _lignes(caplog)]
    assert statuts == ["404", "401"]


def test_aucun_secret_dans_la_ligne(client, caplog):
    """La query string et le corps ne sont jamais journalisés."""
    caplog.set_level(logging.INFO, logger="server.timing")

    client.get("/health?token=jeton-tres-secret")
    client.post(
        "/api/auth/pin",
        json={"pin": "1234", "password": "mot-de-passe-secret"},
    )

    dump = "\n".join(record.getMessage() for record in caplog.records)
    assert "jeton-tres-secret" not in dump
    assert "mot-de-passe-secret" not in dump
    assert all("?" not in ligne["path"] for ligne in _lignes(caplog))


def test_le_chemin_suit_le_modele_de_route():
    """Une route paramétrée groupe ses requêtes sous un seul libellé."""
    from starlette.routing import Route

    route = Route("/api/items/{item_id}", lambda request: None)
    assert timing._path({"route": route, "path": "/api/items/42"}) == (
        "/api/items/{item_id}"
    )
    assert timing._path({"path": "/introuvable"}) == "/introuvable"


# ── percentiles ────────────────────────────────────────────────────────


def test_percentiles_sur_une_fenetre_vide():
    durees = timing.Timings()
    assert durees.snapshot() == {
        "window_seconds": 300,
        "count": 0,
        "p50": None,
        "p95": None,
    }


def test_p50_et_p95_par_rang_le_plus_proche():
    durees = timing.Timings()
    for valeur in range(1, 101):
        durees.record(float(valeur))

    assert timing.percentile([1.0], 95) == 1.0
    assert durees.snapshot()["p50"] == 50.0
    assert durees.snapshot()["p95"] == 95.0


def test_les_echantillons_de_plus_de_cinq_minutes_sont_oublies():
    maintenant = [0.0]
    durees = timing.Timings(clock=lambda: maintenant[0])
    durees.record(100.0)

    maintenant[0] = 299.0
    durees.record(200.0)
    assert durees.snapshot()["count"] == 2

    maintenant[0] = 301.0
    assert durees.snapshot() == {
        "window_seconds": 300,
        "count": 1,
        "p50": 200.0,
        "p95": 200.0,
    }


def test_l_historique_est_plafonne():
    """Même sous un pic de trafic, la mémoire reste bornée."""
    durees = timing.Timings(maxlen=3)
    for valeur in range(10):
        durees.record(float(valeur))

    assert durees.snapshot()["count"] == 3
    assert durees.snapshot()["p95"] == 9.0


# ── /status.json ───────────────────────────────────────────────────────


def test_status_json_expose_la_latence(client):
    for _ in range(4):
        client.get("/health")

    donnees = client.get("/status.json").json()

    latence = donnees["latency_ms"]
    assert latence["window_seconds"] == 300
    # Les quatre `/health` mesurés, plus `/status.json` lui-même en cours.
    assert latence["count"] >= 4
    assert latence["p50"] is not None
    assert latence["p95"] >= latence["p50"]
