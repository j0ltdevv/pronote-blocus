"""Le cache d'extraction des messages : un appel de modèle par corps, pas par élève."""

from __future__ import annotations

import json

from app import config, db
from app.pronote import fetch


def _cfg() -> config.Config:
    return config.from_env(
        {
            "APP_SECRET": "unused-ici",
            "LLM_BASE_URL": "http://127.0.0.1:9/v1",
            "LLM_API_KEY": "cle-de-test",
            "LLM_MODEL": "modele-de-test",
        },
        require_token=False,
    )


def _rows(conn) -> list:
    return list(conn.execute("SELECT key, model, summary FROM summaries"))


def test_extraction_identiques_ne_sollicite_le_modele_qu_une_fois(conn, monkeypatch):
    cfg = _cfg()
    appels: list[tuple[str, str]] = []

    def faux(subject, content, cfg_):
        appels.append((subject, content))
        return ({"matiere": "Maths", "detail": "Exercices 12 à 15", "due": "2026-10-21"}, True)

    monkeypatch.setattr("app.llm.extract_devoir_checked", faux)

    premier = fetch._extract(conn, "Devoirs", "Exercices 12 à 15 pour vendredi", cfg)
    # Deuxième élève de la même classe : même sujet, même corps.
    second = fetch._extract(conn, "Devoirs", "Exercices 12 à 15 pour vendredi", cfg)

    assert premier == second == {
        "matiere": "Maths",
        "detail": "Exercices 12 à 15",
        "due": "2026-10-21",
    }
    assert len(appels) == 1, f"le modèle a été appelé {len(appels)} fois"

    rows = _rows(conn)
    assert len(rows) == 1
    assert rows[0]["model"] == fetch.EXTRACT_MODEL
    assert json.loads(rows[0]["summary"])["detail"] == "Exercices 12 à 15"


def test_un_corps_different_repart_au_modele(conn, monkeypatch):
    cfg = _cfg()
    appels: list[str] = []

    def faux(subject, content, cfg_):
        appels.append(content)
        return ({"matiere": "", "detail": content[:20], "due": None}, True)

    monkeypatch.setattr("app.llm.extract_devoir_checked", faux)
    fetch._extract(conn, "Devoirs", "corps A", cfg)
    fetch._extract(conn, "Devoirs", "corps B", cfg)
    fetch._extract(conn, "Devoirs", "corps A", cfg)
    assert len(appels) == 2
    assert len(_rows(conn)) == 2


def test_un_null_est_mis_en_cache_mais_une_panne_non(conn, monkeypatch):
    """Un « pas de devoir » stable coûte un appel ; une panne ne fige rien."""
    cfg = _cfg()
    etat = {"mode": "null", "calls": 0}

    def faux(subject, content, cfg_):
        etat["calls"] += 1
        if etat["mode"] == "panne":
            return (None, False)
        return (None, True)

    monkeypatch.setattr("app.llm.extract_devoir_checked", faux)

    assert fetch._extract(conn, "Info", "message sans travail", cfg) is None
    assert fetch._extract(conn, "Info", "message sans travail", cfg) is None
    assert etat["calls"] == 1, "le null relisible aurait dû être servi par le cache"
    assert len(_rows(conn)) == 1

    etat["mode"] = "panne"
    corps = "autre message"
    assert fetch._extract(conn, "Info", corps, cfg) is None
    assert fetch._extract(conn, "Info", corps, cfg) is None
    assert etat["calls"] == 3, "une panne a été mise en cache"
    assert len(_rows(conn)) == 1, "la panne a écrit une ligne dans summaries"


def test_sans_llm_aucun_appel_ni_ecriture(conn, monkeypatch):
    cfg = config.from_env({"APP_SECRET": "x"}, require_token=False)
    appels: list[int] = []
    monkeypatch.setattr(
        "app.llm.extract_devoir_checked", lambda *a: appels.append(1) or (None, True)
    )
    assert fetch._extract(conn, "Devoirs", "du travail", cfg) is None
    assert appels == []
    assert _rows(conn) == []
