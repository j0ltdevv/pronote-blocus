"""Quota du chat web : 10 questions par jour et par compte, côté serveur.

Le compteur vit en SQLite (`users.llm_day` / `users.llm_count`) : il doit donc
refuser une 11e question même si le serveur a redémarré entre-temps. Sans LLM,
la réponse est locale et ne consomme rien (même règle que le bot).
"""

from __future__ import annotations

import importlib
from datetime import date

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import db, llm
from server import deps
from server.routes import chat as chat_route

SECRET = Fernet.generate_key().decode()
PRONOTE_URL = "https://demo.index-education.net/pronote/eleve.html"
PLAFOND = 10
JOUR = date(2026, 10, 20)
LENDEMAIN = date(2026, 10, 21)
LLM_ENV = {
    "LLM_BASE_URL": "https://llm.example/v1",
    "LLM_API_KEY": "cle-test",
    "LLM_MODEL": "modele-test",
}
REPONSE_LLM = "Réponse simulée."


# ── serveur relançable ────────────────────────────────────────────────


class Serveur:
    """Serveur de test qu'on peut arrêter puis relancer sur la même base."""

    def __init__(self) -> None:
        self.main = importlib.import_module("server.main")
        self.client: TestClient | None = None
        self._demarre()

    def _demarre(self) -> None:
        self.client = TestClient(self.main.app)
        self.client.__enter__()

    def redemarre(self) -> None:
        """Simule un redémarrage : plus rien en mémoire, seul SQLite reste."""
        assert self.client is not None
        self.client.__exit__(None, None, None)
        self.client = None
        conn = deps._conn
        if conn is not None:
            conn.close()
        deps._conn = None
        deps._cfg = None
        deps._MEMORY.clear()
        deps._SESSIONS.clear()
        self._demarre()

    @property
    def conn(self):
        return deps.get_conn()

    def ferme(self) -> None:
        if self.client is not None:
            self.client.__exit__(None, None, None)
            self.client = None
        conn = deps._conn
        if conn is not None:
            conn.close()
        deps._conn = None
        deps._cfg = None


def _prepare(tmp_path, monkeypatch, *, llm_actif: bool) -> Serveur:
    """Serveur isolé, jour figé, LLM doublé ou coupé selon le test."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    for variable, valeur in LLM_ENV.items():
        if llm_actif:
            monkeypatch.setenv(variable, valeur)
        else:
            monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(deps, "_conn", None)
    monkeypatch.setattr(deps, "_cfg", None)
    monkeypatch.setattr(deps, "_MEMORY", {})
    monkeypatch.setattr(deps, "_SESSIONS", {})
    # Jour fixe : la remise à zéro ne doit pas dépendre de l'heure du test.
    monkeypatch.setattr(chat_route, "_today", lambda: JOUR)
    if llm_actif:
        # Aucun appel réseau : la réponse du modèle est doublée.
        monkeypatch.setattr(llm, "chat_answer", lambda question, block, cfg: REPONSE_LLM)
    return Serveur()


@pytest.fixture
def serveur(tmp_path, monkeypatch):
    """Serveur isolé avec un LLM configuré (réponse doublée)."""
    instance = _prepare(tmp_path, monkeypatch, llm_actif=True)
    yield instance
    instance.ferme()


@pytest.fixture
def serveur_sans_llm(tmp_path, monkeypatch):
    """Serveur isolé sans LLM : la réponse est la liste locale."""
    instance = _prepare(tmp_path, monkeypatch, llm_actif=False)
    yield instance
    instance.ferme()


# ── helpers ────────────────────────────────────────────────────────────


def _compte(serveur: Serveur, chat_id: int) -> tuple[str, str]:
    """Crée un compte et rend `(user_id, jeton)` ; le jeton est persistant."""
    user_id = db.create_user(
        serveur.conn,
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
    return user_id, deps.issue_token(user_id, serveur.conn)


def _poser(serveur: Serveur, jeton: str, question: str = "quoi de neuf ?"):
    assert serveur.client is not None
    return serveur.client.post(
        "/api/chat",
        json={"question": question},
        headers={"Authorization": f"Bearer {jeton}"},
    )


# ── quota ──────────────────────────────────────────────────────────────


def test_onzieme_question_refusee_avec_un_message_clair(serveur):
    """Dix réponses, puis un 429 explicite : le serveur ne sert plus rien."""
    user_id, jeton = _compte(serveur, chat_id=1)

    for numero in range(PLAFOND):
        assert _poser(serveur, jeton).status_code == 200, f"question {numero + 1}"

    refusee = _poser(serveur, jeton, "onzième question")
    assert refusee.status_code == 429
    detail = refusee.json()["detail"]
    assert "10" in detail and "reviens demain" in detail
    assert db.get_user(serveur.conn, user_id)["llm_count"] == PLAFOND


def test_quota_refuse_apres_redemarrage(serveur):
    """Le plafond survit au redémarrage : le 11e appel reste refusé."""
    user_id, jeton = _compte(serveur, chat_id=1)
    for _ in range(PLAFOND):
        assert _poser(serveur, jeton).status_code == 200

    serveur.redemarre()

    refusee = _poser(serveur, jeton, "après redémarrage")
    assert refusee.status_code == 429, "le compteur doit venir de SQLite, pas de la mémoire"
    row = db.get_user(serveur.conn, user_id)
    assert (row["llm_day"], row["llm_count"]) == (JOUR.isoformat(), PLAFOND)


def test_quota_independant_pour_chaque_compte(serveur):
    """Un élève qui a épuisé son quota ne ferme pas celui d'un autre."""
    _, jeton_a = _compte(serveur, chat_id=1)
    _, jeton_b = _compte(serveur, chat_id=2)

    for _ in range(PLAFOND):
        assert _poser(serveur, jeton_a).status_code == 200
    assert _poser(serveur, jeton_a).status_code == 429

    assert _poser(serveur, jeton_b).status_code == 200


def test_le_quota_repart_le_lendemain(serveur, monkeypatch):
    """Jour épuisé, puis nouveau jour : la première question repasse."""
    _, jeton = _compte(serveur, chat_id=1)
    for _ in range(PLAFOND):
        assert _poser(serveur, jeton).status_code == 200
    assert _poser(serveur, jeton).status_code == 429

    monkeypatch.setattr(chat_route, "_today", lambda: LENDEMAIN)

    assert _poser(serveur, jeton, "nouveau jour").status_code == 200


def test_sans_llm_aucune_question_consommee(serveur_sans_llm):
    """Sans LLM, la liste locale est servie sans jamais toucher au quota."""
    user_id, jeton = _compte(serveur_sans_llm, chat_id=1)

    for _ in range(PLAFOND + 2):
        assert _poser(serveur_sans_llm, jeton).status_code == 200

    row = db.get_user(serveur_sans_llm.conn, user_id)
    assert (row["llm_day"], row["llm_count"]) == (None, 0)
