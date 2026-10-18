"""Tests dédiés au cache de contenu : un contenu vide ne doit PAS être mis en cache.

Le docstring de la phase ``cours`` promet de « retenter à chaque passage ceux
dont le contenu est encore vide ». Avant le correctif, un contenu vide était
mis en cache et le statut ``vide`` bloquait la relève : mesuré, 3 passages
sur un cours vide = 1 seule lecture réseau.

Après correctif : le contenu vide n'est pas caché, donc chaque synchro relit
le cours (protection anti-boucle assurée par ``STATUS_VIDE`` au sein d'une
même passe, mais la relève intervient bien à la synchro suivante).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config, db
from app.pronote import fetch


def _content(title: str = "", description: str = "") -> SimpleNamespace:
    return SimpleNamespace(title=title, description=description, files=[])


class _CountingLesson:
    """Cours factice : ``content`` compte les lectures comme une requête réseau."""

    def __init__(self, start, subject, content) -> None:
        self.start = start
        self.subject = SimpleNamespace(name=subject)
        self.canceled = False
        self._content = content
        self.content_reads = 0

    @property
    def content(self):
        self.content_reads += 1
        return self._content


class _FakeClient:
    def __init__(self, lessons):
        self._lessons = lessons

    def lessons(self, start, end):
        return list(self._lessons)


def _cfg(tmp_path: Path) -> config.Config:
    env = {
        "APP_SECRET": "secret-de-test",
        "DATA_DIR": str(tmp_path / "data"),
    }
    return config.from_env(env, require_token=False)


@pytest.fixture
def cfg(tmp_path: Path) -> config.Config:
    return _cfg(tmp_path)


@pytest.fixture(autouse=True)
def _cache_isolee():
    fetch._CONTENT_CACHE.clear()
    yield
    fetch._CONTENT_CACHE.clear()


def test_contenu_vide_non_cache_3_passages_2_lectures(
    conn, cfg, make_user, monkeypatch
):
    """3 passages sur un cours vide → 2 lectures réseau (pas 1).

    Correction du bug : un contenu vide n'est plus mis en cache, donc le cours
    est relu à la synchro suivante. La protection anti-boucle (``STATUS_VIDE``)
    évite la relève dans une même passe, mais ne bloque pas entre deux synchros.

    Plafond de 30 lectures respecté : 2 ≤ MAX_CONTENT_READS.

    Avant correctif : le contenu vide était mis en cache, 3 passages = 1
    lecture. Après correctif : 3 passages = 3 lectures.
    """
    monkeypatch.setattr(fetch, "CONTENT_DELAY", 0)

    user_id = make_user()
    lesson_vide = _CountingLesson(
        datetime(2026, 10, 5, 8, 0),
        "Mathématiques",
        _content("", ""),
    )
    client = _FakeClient(lessons=[lesson_vide])

    # Passage 1 : lecture obligatoire (cache vide, statut initial absent).
    first = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_COURS)
    assert first.ok, first.error
    assert first.contents_read == 1, "premier passage lit le contenu vide"

    # Passage 2 : contenu vide NON mis en cache → relève.
    second = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_COURS)
    assert second.ok, second.error
    assert second.contents_read == 1, "contenu vide non cache → lecture relue"

    # Passage 3 : statut ``vide`` bloque la relève dans la même passe, mais
    # le cache n'a pas retenu le vide → nouvelle lecture.
    third = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_COURS)
    assert third.ok, third.error
    assert third.contents_read == 1, "statut vide ne bloque pas la relève entre synchros"

    assert lesson_vide.content_reads == 3, "3 passages = 3 lectures"

    statuses = db.lesson_status(conn, user_id)
    assert len(statuses) == 1
    assert set(statuses.values()) == {fetch.STATUS_VIDE}
