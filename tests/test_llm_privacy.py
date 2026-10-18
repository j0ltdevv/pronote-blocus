"""Tests de confidentialité et de panne pour `app.llm`.

Couverts :
- Bug 1 : `weekly_synthesis` doit filtrer les rows de messagerie quand
  `LLM_MESSAGES=0`, sinon le corps d'un message de prof quitte le serveur
  via `_row_line` → `row.summary` (fallback `body[:140]`).
- Bug 2 : un LLM injoignable doit activer un circuit ouvert (`_LLM_DOWN_UNTIL`)
  mémorisé 10 minutes, sortir tôt et ne pas refaire de requête à chaque appel.
"""

from __future__ import annotations

import json
import time
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from app import config, llm
from app.pronote import ranking
from app.pronote.normalize import make_item

CHAT = 4242

# ─────────────────────────── montage ───────────────────────────


def _env(tmp_path, secret: str, **overrides: str) -> dict[str, str]:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
        "APP_SECRET": secret,
        "DATA_DIR": str(tmp_path / "data"),
        "WINDOW_START": "2026-10-01",
        "LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "LLM_API_KEY": "cle-de-test",
        "LLM_MODEL": "modele-de-test",
    }
    env.update(overrides)
    return env


@pytest.fixture
def cfg(tmp_path, secret) -> config.Config:
    return config.from_env(_env(tmp_path, secret))


@pytest.fixture
def cfg_no_messages(tmp_path, secret) -> config.Config:
    return config.from_env(_env(tmp_path, secret, LLM_MESSAGES="0"))


class _Response:
    """Réponse httpx minimale."""

    def __init__(self, payload=None) -> None:  # noqa: ANN001
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self):  # noqa: ANN201
        return self._payload


@pytest.fixture
def http(monkeypatch):
    state = SimpleNamespace(calls=[], response=None, error=None)

    def post(url: str, **kwargs):  # noqa: ANN001
        state.calls.append((url, kwargs))
        if state.error is not None:
            raise state.error
        assert state.response is not None, "aucune réponse programmée"
        return state.response

    monkeypatch.setattr(llm.httpx, "post", post)
    return state


def _message_payload(content=None, reasoning=None) -> dict:  # noqa: ANN201
    message: dict[str, str] = {}
    if content is not None:
        message["content"] = content
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {"choices": [{"message": message}]}


def _item(subject: str, body: str, *, kind: str, source: str):
    return make_item(
        kind=kind,
        source=source,
        subject=subject,
        body=body,
        due_date=date(2026, 10, 20),
    )


def _row(*, subject: str, body: str, kind: str, source: str) -> object:
    item = _item(subject, body, kind=kind, source=source)
    return ranking.to_row(item, None)


# ─────────────────────────── 1. LLM_MESSAGES & weekly_synthesis ───────────────────────────


def test_weekly_synthesis_filtre_les_messages_quand_llm_messages_0(
    cfg_no_messages, http
):
    """Bug 1 — Le corps d'un message de prof ne doit jamais quitter le serveur.

    `weekly_synthesis` appelle `_row_line`, qui lit `row.summary`. Sans résumé,
    `Row.summary` vaut `body[:140]` (fallback). Si la synthèse envoie une ligne
    construite sur un message de prof, le corps du prof part au LLM.
    """
    # Un message de prof (messagerie) + un devoir normal (travail_a_faire).
    message_row = _row(
        subject="Mathématiques",
        body="CORPS CONFIDENTIEL DU PROF — ne doit pas quitter",
        kind="message",
        source="messagerie",
    )
    devoir_row = _row(
        subject="SVT",
        body="Lire le chapitre 2",
        kind="devoir",
        source="travail_a_faire",
    )

    http.response = _Response(
        _message_payload(content="Synthèse hebdo\nligne 2")
    )

    synthesis = llm.weekly_synthesis([message_row, devoir_row], cfg_no_messages)

    # Le corps du message de prof n'est jamais envoyé au LLM.
    envoye = json.dumps(http.calls[0][1]["json"], ensure_ascii=False)
    assert "CORPS CONFIDENTIEL DU PROF" not in envoye, (
        "le corps d'un message de prof a quitté le serveur malgré LLM_MESSAGES=0"
    )
    # La synthèse est bien produite à partir du seul devoir.
    assert synthesis is not None
    assert "Lire le chapitre 2" in http.calls[0][1]["json"]["messages"][1]["content"]


def test_weekly_synthesis_temoin_envoie_bien_le_message_quand_llm_messages_1(
    cfg, http
):
    """Témoin : avec le drapeau actif, le corps du message est bien transmis.

    Cela prouve que l'assertion ci-dessus échouerait sans le filtrage.
    """
    message_row = _row(
        subject="Mathématiques",
        body="CORPS CONFIDENTIEL DU PROF",
        kind="message",
        source="messagerie",
    )

    http.response = _Response(_message_payload(content="Synthèse"))

    llm.weekly_synthesis([message_row], cfg)

    envoye = json.dumps(http.calls[0][1]["json"], ensure_ascii=False)
    assert "CORPS CONFIDENTIEL DU PROF" in envoye


# ─────────────────────────── 2. Circuit ouvert anti-panne ───────────────────────────


def test_llm_down_memorise_un_echec_et_sort_tôt(cfg, monkeypatch):
    """Bug 2 — Un LLM injoignable doit activer un circuit ouvert mémorisé.

    Sans le circuit : chaque appel refait une requête (timeout 15 s) et l'échec
    n'est jamais enregistré → le lot repart intact à chaque affichage.
    """
    # Circuit ouvert simulé : _LLM_DOWN_UNTIL fixé dans le passé lointain.
    future = time.monotonic() + 60  # même horloge que `_is_down`
    monkeypatch.setattr(llm, "_LLM_DOWN_UNTIL", future)

    # Même si le transport est reprogrammé pour réussir, le circuit doit
    # sortir tôt : aucune requête ne doit être émise.
    def interdit_post(*args, **kwargs):  # noqa: ANN001
        raise AssertionError(
            "le LLM est marqué down : aucune requête ne devait être émise"
        )

    monkeypatch.setattr(llm.httpx, "post", interdit_post)

    # _chat doit rendre None sans appeler de transport.
    assert llm._chat(cfg, [{"role": "user", "content": "x"}], max_tokens=8) is None


def test_llm_down_se_déclenche_après_un_echec_et_dure_10_minutes(cfg, monkeypatch):
    """Le circuit s'ouvre après le premier échec et tient 10 minutes."""
    calls: list = []

    def post(url: str, **kwargs):  # noqa: ANN001
        calls.append((url, kwargs))
        raise httpx.ConnectError("réseau coupé")

    monkeypatch.setattr(llm.httpx, "post", post)

    # Réinitialise le circuit pour le test.
    monkeypatch.setattr(llm, "_LLM_DOWN_UNTIL", 0.0)

    # Premier appel : échec, circuit s'ouvre.
    assert llm._chat(cfg, [{"role": "user", "content": "x"}], max_tokens=8) is None
    assert len(calls) == 1, "le premier appel doit tenter le transport"
    assert llm._LLM_DOWN_UNTIL > time.monotonic(), "le circuit doit rester ouvert"

    # Deuxième appel : circuit ouvert, aucun appel réseau.
    assert llm._chat(cfg, [{"role": "user", "content": "x"}], max_tokens=8) is None
    assert len(calls) == 1, "le circuit ouvert doit empêcher un second appel"

    # Le plafond est ~10 minutes (600 s). On ne force pas l'attente réelle :
    # on vérifie simplement que le plafond est dans la fenêtre attendue.
    remaining = llm._LLM_DOWN_UNTIL - time.monotonic()
    assert 500 <= remaining <= 600, f"circuit ouvert {remaining:.0f}s, pas ~600s"
