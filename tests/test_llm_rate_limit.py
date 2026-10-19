"""Non-régression du rate limiting de `app.llm` : semaphore et pause partagée.

Aucun réseau : `httpx.post` est remplacé par un faux qui enregistre chaque
requête. Les propriétés fixées le 18/10/2026 sont vérifiées ici :

1. `_CALL_SLOTS` borne à `MAX_CONCURRENT_CALLS` les appels *simultanés* ;
2. un refus 429 pose `PAUSE_SECONDS` de pause *partagée* sans ouvrir le
   circuit (`_LLM_DOWN_UNTIL` reste à 0) ;
3. 400/413/422/503 suivent exactement le même chemin que 429.

L'état global (circuit + pause + semaphore) est réinitialisé autour de chaque
test : ce sont des objets de module, pas des fixtures par test.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from app import config, llm

#: Paramètres de production fixés dans `app/llm.py`.
MAX_CONCURRENT_CALLS = llm.MAX_CONCURRENT_CALLS
PAUSE_SECONDS = llm.PAUSE_SECONDS
PAUSE_CODES = llm.PAUSE_CODES

URL_LLM = "http://127.0.0.1:9/v1/chat/completions"


# ─────────────────────────── montage ───────────────────────────


def _env(tmp_path, secret: str) -> dict[str, str]:
    return {
        "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
        "APP_SECRET": secret,
        "DATA_DIR": str(tmp_path / "data"),
        "WINDOW_START": "2026-10-01",
        "LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "LLM_API_KEY": "cle-de-test",
        "LLM_MODEL": "modele-de-test",
    }


@pytest.fixture
def cfg(tmp_path, secret) -> config.Config:
    return config.from_env(_env(tmp_path, secret))


class _Response:
    """Réponse httpx minimale : ni socket, ni encodage réel."""

    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", URL_LLM),
                response=self,
            )

    def json(self) -> dict:  # noqa: D102
        if self._payload is None:
            raise AssertionError("raise_for_status a dû lever avant json()")
        return self._payload


def _reponse_ok() -> _Response:
    """Une vraie réussite : `_chat` doit renvoyer le contenu, pas None."""
    return _Response(
        200,
        {"choices": [{"message": {"content": "réponse simulée"}}]},
    )


def _erreur_statut(status_code: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        f"HTTP {status_code}",
        request=httpx.Request("POST", URL_LLM),
        response=_Response(status_code),
    )


@pytest.fixture
def http(monkeypatch):
    """Remplace l'unique sortie réseau de `app.llm` et rend le journal.

    - `state.hold` (Event) : quand *non* mis, chaque appel attend qu'on le
      libère — c'est ce qui permet de mesurer la concordance réelle.
    - `state.in_flight` / `state.peak` : appels entrés et non sortis ; c'est
      exactement la grandeur que le semaphore doit borner.
    """
    state = SimpleNamespace(
        calls=0,
        in_flight=0,
        peak=0,
        started=threading.Event(),
        hold=threading.Event(),
        response=_reponse_ok(),
        error=None,
    )
    state.hold.set()  # par défaut : les appels terminent immédiatement
    lock = threading.Lock()

    def post(url: str, **kwargs):  # noqa: ANN001, ANN003
        with lock:
            state.calls += 1
            state.in_flight += 1
            state.peak = max(state.peak, state.in_flight)
        state.started.set()
        state.hold.wait(timeout=10)
        with lock:
            state.in_flight -= 1
        if state.error is not None:
            raise state.error
        return state.response

    monkeypatch.setattr(llm.httpx, "post", post)
    return state


@pytest.fixture(autouse=True)
def _etat_global_reinitialise(monkeypatch):
    """Circuit, pause et semaphore : tout est global, tout est remis à zéro.

    Sans cela, la pause posée par un test 429 ferait rendre `None` à tous les
    tests suivants, et un slot fuité par une assertion échouée laisserait les
    appels suivants en repli pour toute la suite.
    """
    _reset_etat()
    yield
    _reset_etat()


def _reset_etat() -> None:
    llm._reset_down()
    llm._PAUSE_UNTIL = 0.0
    llm._CALL_SLOTS._value = MAX_CONCURRENT_CALLS  # noqa: SLF001


def _start_workers(cfg: config.Config, count: int) -> list[threading.Thread]:
    """Démarre `count` appels `_chat` en parallèle (aucune attente de slot)."""
    barrier = threading.Barrier(count)

    def worker() -> None:
        barrier.wait()
        llm._chat(
            cfg,
            [{"role": "user", "content": "question"}],
            max_tokens=8,
            timeout=30,
        )

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for thread in threads:
        thread.start()
    return threads


# ─────────────────────────── 1. semaphore ───────────────────────────


def test_le_semaphore_plafonne_a_4_appels_simultanes(cfg, http):
    """`MAX_CONCURRENT_CALLS` est la borne stricte des appels simultanés.

    Huit threads lancent un appel en même temps ; le transport les retient
    tous jusqu'à libération. Au plus `MAX_CONCURRENT_CALLS` peuvent être
    *entrés* dans `httpx.post` à la fois — les autres attendent un slot.
    """
    http.hold.clear()  # les appels entrés ne sortent plus
    workers = _start_workers(cfg, 2 * MAX_CONCURRENT_CALLS)

    try:
        assert http.started.wait(timeout=10), "aucun appel n'est parti au transport"
        # La borne doit être atteinte, puis plus franchie.
        deadline = time.monotonic() + 2.0
        while http.calls < MAX_CONCURRENT_CALLS and time.monotonic() < deadline:
            time.sleep(0.01)
        assert http.calls == MAX_CONCURRENT_CALLS, (
            f"{http.calls} appels entrés dans le transport, "
            f"le plafond est {MAX_CONCURRENT_CALLS}"
        )
        # Aucun slot ne se libère : la pointe de concordance vaut la borne.
        time.sleep(0.3)
        assert http.peak == MAX_CONCURRENT_CALLS, (
            f"{http.peak} appels simultanés observés, "
            f"le plafond est {MAX_CONCURRENT_CALLS}"
        )
    finally:
        http.hold.set()
        for thread in workers:
            thread.join(timeout=10)
            assert not thread.is_alive(), "un thread LLM est resté bloqué"

    # Les 4 appels en attente ont pris les slots libérés, un par un.
    assert http.calls == 2 * MAX_CONCURRENT_CALLS, (
        "les appels en attente doivent partir dès qu'un slot se libère"
    )


def test_le_slot_est_rendu_aux_appels_suivants(cfg, http):
    """Un appel terminé rend son slot : l'appel suivant part immédiatement.

    C'est le complément indispensable du plafond : si le slot n'était pas
    rendu, le 5e appel attendrait `SLOT_WAIT_SECONDS` puis passerait en repli.
    """
    messages = [{"role": "user", "content": "un"}]
    assert llm._chat(cfg, messages, max_tokens=8) == "réponse simulée"
    assert llm._chat(cfg, messages, max_tokens=8) == "réponse simulée"

    assert http.calls == 2, "chaque appel réussi doit rendre exactement un slot"
    assert http.peak == 1


# ─────────────────────────── 2. pause 429 ───────────────────────────


def test_un_429_declenche_la_pause_partagee_sans_circuit(cfg, monkeypatch):
    """Le chemin exact d'un 429 : pause partagée, jamais de circuit ouvert.

    Sans pause : le second appel partirait quand même et aggraverait le quota.
    Sans circuit : la pénalité ne dure que `PAUSE_SECONDS`, pas `DOWN_SECONDS`.
    """
    calls: list[int] = []

    def post(url: str, **kwargs):  # noqa: ANN001, ANN003
        calls.append(1)
        raise _erreur_statut(429)

    monkeypatch.setattr(llm.httpx, "post", post)

    assert llm._chat(cfg, [{"role": "user", "content": "x"}], max_tokens=8) is None
    assert calls == [1], "le 429 doit atteindre le transport"

    # Pause posée, et pour la bonne durée.
    assert llm._is_paused() is True, "la pause doit être active"
    restante = llm._PAUSE_UNTIL - time.monotonic()
    assert 0 < restante <= PAUSE_SECONDS, (
        f"pause de {restante:.1f}s, attendu (0, {PAUSE_SECONDS:.0f}s]"
    )

    # Circuit ouvert : interdit — 429 n'est pas une panne.
    assert llm._is_down() is False, "un 429 ne doit pas ouvrir le circuit"

    # La pause est *partagée* : un autre appel (autre thread, autre endpoint)
    # ne repart pas tant qu'elle n'a pas expiré.
    assert llm._chat(cfg, [{"role": "user", "content": "y"}], max_tokens=8) is None
    assert calls == [1], "la pause doit bloquer les appels suivants"

    # Une fois la pause expirée, le transport redevient joignable.
    monkeypatch.setattr(llm, "_PAUSE_UNTIL", time.monotonic() - 0.01)
    assert llm._is_paused() is False
    assert llm._chat(cfg, [{"role": "user", "content": "z"}], max_tokens=8) is None
    assert calls == [1, 1], "après expiration la requête doit repartir"

    # Le circuit est resté fermé du début à la fin.
    assert llm._is_down() is False, "le circuit ne doit jamais s'ouvrir sur un 429"


# ─────────────────────────── 3. autres codes de pause ───────────────────────────


@pytest.mark.parametrize("status", sorted(PAUSE_CODES - {429}))
def test_les_autres_codes_de_pause_suivent_le_meme_chemin(status, cfg, monkeypatch):
    """400/413/422/503 : pause partagée de `PAUSE_SECONDS`, sans circuit ouvert.

    400 parce que Groq répond `json_validate_failed` sur un JSON non conforme :
    le fournisseur répond, il refuse seulement la forme de la requête.
    """
    assert status in PAUSE_CODES

    def post(url: str, **kwargs):  # noqa: ANN001, ANN003
        raise _erreur_statut(status)

    monkeypatch.setattr(llm.httpx, "post", post)

    assert llm._chat(cfg, [{"role": "user", "content": "x"}], max_tokens=8) is None
    assert llm._is_paused() is True, f"le code {status} doit poser la pause"
    assert llm._is_down() is False, f"le code {status} ne doit pas ouvrir le circuit"

    restante = llm._PAUSE_UNTIL - time.monotonic()
    assert 0 < restante <= PAUSE_SECONDS


def test_un_echec_franc_ouvre_le_circuit_pas_la_pause(cfg, monkeypatch):
    """Caractérisation : 500 n'est PAS un code de pause — circuit ouvert.

    C'est la frontière qui rend les tests (b) et (c) informatifs : si tout
    échec ouvrait le circuit, un simple 429 couperait le service 10 minutes.
    """
    calls: list[int] = []

    def post(url: str, **kwargs):  # noqa: ANN001, ANN003
        calls.append(1)
        raise _erreur_statut(500)

    monkeypatch.setattr(llm.httpx, "post", post)

    assert llm._chat(cfg, [{"role": "user", "content": "x"}], max_tokens=8) is None
    assert llm._is_down() is True, "un 500 doit ouvrir le circuit"
    assert llm._is_paused() is False, "un 500 ne doit pas poser de pause"
    assert llm._chat(cfg, [{"role": "user", "content": "y"}], max_tokens=8) is None
    assert calls == [1], "le circuit doit couper le second appel"
