"""Robustesse réseau de `fetch` : retries, statuts réels, lectures partielles.

Aucun test ne touche le réseau. Les statuts HTTP sont simulés sous les deux
formes que le code rencontre en production : `requests.HTTPError` (tests
existants) et `PronoteAPIError("Bad request (http status: N)")`, la vraie
forme levée par pronotepy, qui ne porte aucun code structuré.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pronotepy
import pytest
import requests

from app import config, db
from app.pronote import fetch
from app.pronote.normalize import Item


def _api_error(status: int) -> pronotepy.PronoteAPIError:
    """Ce que pronotepy lève vraiment quand PRONOTE répond 429/403/5xx."""
    return pronotepy.PronoteAPIError(f"Bad request (http status: {status})")


def _content(title: str = "", description: str = "") -> SimpleNamespace:
    return SimpleNamespace(title=title, description=description, files=[])


@pytest.fixture
def cfg(tmp_path):
    return config.from_env(
        {"APP_SECRET": "secret-de-test", "DATA_DIR": str(tmp_path / "data")},
        require_token=False,
    )


@pytest.fixture(autouse=True)
def _cache_isolee():
    fetch._CONTENT_CACHE.clear()
    yield
    fetch._CONTENT_CACHE.clear()


# ─────────────────────────── client factice ───────────────────────────


class _Lesson:
    def __init__(self, start: datetime, subject: str, content: object) -> None:
        self.start = start
        self.subject = SimpleNamespace(name=subject)
        self.canceled = False
        self._content = content
        self.reads = 0

    @property
    def content(self):
        self.reads += 1
        if isinstance(self._content, BaseException):
            raise self._content
        return self._content


class _Client:
    """Client minimal : phase `cours` et phase `items`."""

    def __init__(
        self,
        lessons: list | None = None,
        failures: dict | None = None,
        *,
        homework: list | None = None,
        infos: list | None = None,
        discussions: list | None = None,
    ) -> None:
        self._lessons = list(lessons or [])
        self._homework = list(homework or [])
        self._infos = list(infos or [])
        self._discussions = list(discussions or [])
        self.failures = dict(failures or {})
        self.info = SimpleNamespace(class_name="2nde 3")

    def _check(self, name: str) -> None:
        exc = self.failures.get(name)
        if exc is not None:
            raise exc

    def homework(self, start, end) -> list:
        self._check("homework")
        return list(self._homework)

    def information_and_surveys(self, start, end, only_unread: bool = False) -> list:
        self._check("infos")
        return list(self._infos)

    def discussions(self) -> list:
        self._check("discussions")
        return list(self._discussions)

    def lessons(self, start, end) -> list:
        self._check("lessons")
        return list(self._lessons)


class _DiscussionIllisible:
    """Discussion dont la lecture des messages part en erreur réseau."""

    subject = "SVT"
    closed = False

    @property
    def messages(self):
        raise pronotepy.PronoteAPIError("Bad request (http status: 502)")


def _sleeps(monkeypatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(fetch.time, "sleep", recorded.append)
    return recorded


# ─────────────────────────── retry / backoff ───────────────────────────


@pytest.mark.parametrize("status", [429, 403])
def test_un_statut_http_pronotepy_n_est_pas_reessaye(status, monkeypatch):
    """pronotepy a déjà fait refresh()+replay : insister refait un login."""
    sleeps = _sleeps(monkeypatch)
    tries: list[int] = []

    def always():
        tries.append(1)
        raise _api_error(status)

    with pytest.raises(pronotepy.PronoteAPIError):
        fetch._call(always)
    assert len(tries) == 1
    assert sleeps == []


def test_un_500_pronotepy_remonte_sans_pause(monkeypatch):
    sleeps = _sleeps(monkeypatch)

    def boom():
        raise _api_error(500)

    with pytest.raises(pronotepy.PronoteAPIError):
        fetch._call(boom)
    assert sleeps == []


# ─────────────────────────── mapping des erreurs ───────────────────────────


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        pytest.param(401, fetch.ERR_TOKEN, id="http-401"),
        pytest.param(403, fetch.ERR_RATE, id="http-403"),
        pytest.param(429, fetch.ERR_RATE, id="http-429"),
        pytest.param(500, fetch.ERR_UNKNOWN, id="http-500"),
        pytest.param(502, fetch.ERR_UNKNOWN, id="http-502"),
    ],
)
def test_une_erreur_http_pronotepy_est_mappee(status, expected):
    assert fetch._error_of(_api_error(status)) == expected


def test_429_reel_pendant_la_collecte_donne_rate_limited(conn, cfg, make_user, monkeypatch):
    sleeps = _sleeps(monkeypatch)
    user_id = make_user()
    client = _Client([], failures={"lessons": _api_error(429)})

    result = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_COURS)

    assert result.ok is False
    assert result.error == fetch.ERR_RATE
    assert sleeps == [], "pas de pause : pronotepy a déjà rejoué la requête"
    row = conn.execute("SELECT sync_state FROM users WHERE id = ?", (user_id,)).fetchone()
    assert row["sync_state"] == "idle"


# ─────────────────────────── réponses partielles ───────────────────────────


def test_une_lecture_de_contenu_en_echec_ne_casse_pas_la_phase(
    conn, cfg, make_user, monkeypatch
):
    monkeypatch.setattr(fetch.time, "sleep", lambda _seconds: None)
    user_id = make_user()
    start = datetime(2026, 10, 16, 8, 0)
    maths = _Lesson(start, "Mathématiques", _content("Chapitre 3", "Exercices 1 à 5"))
    francais = _Lesson(
        start + timedelta(hours=1), "Français", requests.ConnectionError("coupure")
    )
    histoire = _Lesson(start + timedelta(hours=2), "Histoire", _content("La Révolution", ""))
    client = _Client([maths, francais, histoire])

    result = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_COURS)

    assert result.ok is True
    assert result.error is None
    assert {item.subject for item in result.items} == {"Mathématiques", "Histoire"}
    slots = {slot.split("|", 1)[1] for slot in db.lesson_status(conn, user_id)}
    assert "Français" not in slots, "un cours non lu ne doit pas être marqué traité"

    # Le cours en échec repart au passage suivant, sans relire les autres.
    francais._content = _content("Poème", "")
    second = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_COURS)

    assert second.ok is True
    assert "Français" in {item.subject for item in second.items}
    assert francais.reads == 2
    assert maths.reads == 1


def test_un_429_en_fin_de_passe_ne_perd_pas_les_items_deja_lus(
    conn, cfg, make_user, monkeypatch
):
    """Un cours lu puis une passe avortée : son item doit survivre.

    Le statut « traité » ne doit être posé qu'après `replace_items` ; sinon
    la passe suivante saute le cours et son item n'existe jamais.
    """
    monkeypatch.setattr(fetch.time, "sleep", lambda _seconds: None)
    user_id = make_user()
    start = datetime(2026, 10, 16, 8, 0)
    maths = _Lesson(start, "Mathématiques", _content("Chapitre 3", "Exercices 1 à 5"))
    histoire = _Lesson(start + timedelta(hours=1), "Histoire", fetch.RateLimited())
    client = _Client([maths, histoire])

    first = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_COURS)

    assert first.ok is False
    assert first.error == fetch.ERR_RATE
    assert db.get_items(conn, user_id) == [], "rien n'est persisté sur une passe avortée"
    assert db.lesson_status(conn, user_id) == {}, "aucun statut sans items persistés"

    histoire._content = _content("La Révolution", "")
    second = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_COURS)

    assert second.ok is True
    sujets = {item.subject for item in db.get_items(conn, user_id)}
    assert sujets == {"Mathématiques", "Histoire"}
    assert maths.reads == 1, "le contenu déjà lu ne repart pas sur le réseau"


def test_toutes_les_lectures_en_echec_font_echouer_la_phase(
    conn, cfg, make_user, monkeypatch
):
    """Serveur mort ou session expirée : une panne totale n'est pas un succès."""
    monkeypatch.setattr(fetch.time, "sleep", lambda _seconds: None)
    user_id = make_user()
    start = datetime(2026, 10, 16, 8, 0)
    client = _Client(
        [
            _Lesson(start, "Maths", requests.ConnectionError("down")),
            _Lesson(start + timedelta(hours=1), "Français", requests.Timeout("lent")),
        ]
    )

    result = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_COURS)

    assert result.ok is False
    assert result.error == fetch.ERR_UNREACHABLE
    row = conn.execute(
        "SELECT last_sync_at FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    assert row["last_sync_at"] is None, "pas de marquage sans données lues"


def test_une_discussion_illisible_ne_purge_pas_les_items(conn, cfg, make_user):
    """Une source en échec fait échouer la phase : rien n'est purgé."""
    user_id = make_user()
    ancien = Item(
        key="ancien",
        source="messagerie",
        subject="SVT",
        title="",
        body="DM pour lundi",
        due_date=None,
        kind="devoir",
    )
    db.replace_items(conn, user_id, [ancien])
    client = _Client(discussions=[_DiscussionIllisible()])

    result = fetch.collect(client, conn, user_id, cfg)

    assert result.ok is False
    assert [item.key for item in db.get_items(conn, user_id)] == ["ancien"]


def test_rate_limited_pendant_la_lecture_aborte_la_phase(
    conn, cfg, make_user, monkeypatch
):
    monkeypatch.setattr(fetch.time, "sleep", lambda _seconds: None)
    user_id = make_user()
    client = _Client([_Lesson(datetime(2026, 10, 16, 8, 0), "Maths", fetch.RateLimited())])

    result = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_COURS)

    assert result.ok is False
    assert result.error == fetch.ERR_RATE
    assert db.lesson_status(conn, user_id) == {}


# ─────────────────────────── timeouts ───────────────────────────


class _SessionSansReseau(requests.Session):
    """Vraie `Session` : `send` est court-circuité avant tout paquet."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[object] = []

    def send(self, request, **kwargs):
        self.sent.append(kwargs.get("timeout"))
        raise requests.ConnectionError("hors ligne")


def test_arm_timeout_impose_un_delai_a_chaque_requete():
    session = _SessionSansReseau()
    client = SimpleNamespace(communication=SimpleNamespace(session=session))

    fetch._arm_timeout(client)

    for call in (
        lambda: session.get("http://pronote.invalid/"),
        lambda: session.post("http://pronote.invalid/", json={}),
    ):
        with pytest.raises(requests.ConnectionError):
            call()
    assert session.sent == [fetch.HTTP_TIMEOUT, fetch.HTTP_TIMEOUT]


def test_arm_timeout_respecte_un_delai_explicite_et_reste_idempotent():
    session = _SessionSansReseau()
    client = SimpleNamespace(communication=SimpleNamespace(session=session))

    fetch._arm_timeout(client)
    fetch._arm_timeout(client)

    with pytest.raises(requests.ConnectionError):
        session.get("http://pronote.invalid/", timeout=1)
    assert session.sent == [1]


def test_le_timeout_est_rearme_apres_un_refresh():
    """`refresh()` remplace `communication` : le délai doit revenir."""
    ancienne = _SessionSansReseau()
    client = SimpleNamespace(communication=SimpleNamespace(session=ancienne))
    fetch._arm_timeout(client)
    assert ancienne._timeout_arme is True

    nouvelle = _SessionSansReseau()
    client.communication = SimpleNamespace(session=nouvelle)  # ce que fait refresh()
    fetch._client_call(client, lambda: None)

    assert getattr(nouvelle, "_timeout_arme", False) is True


def test_arm_timeout_sans_client_reel_ne_casse_pas():
    fetch._arm_timeout(SimpleNamespace())
    fetch._arm_timeout(SimpleNamespace(communication=None))
