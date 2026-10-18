"""Synchro PRONOTE hors ligne : cinq sources, erreurs, délais et caches.

Aucun test ne touche le réseau : le client PRONOTE est remplacé par des
objets factices. Les fragments HTML de `tests/fixtures/` reproduisent ce que
pronotepy rend dans `description`, `content()` ou les noms de pièces jointes.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pronotepy
import pytest
import requests

from app import config, db
from app.pronote import auth, fetch

FIXTURES = Path(__file__).parent / "fixtures"

_ANCHOR_RE = re.compile(r"<a\b[^>]*>([^<]+)</a>", re.I)


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _anchors(name: str) -> list[str]:
    return [m.strip() for m in _ANCHOR_RE.findall(_html(name))]


def _files(names: list[str] | None = None) -> list[SimpleNamespace]:
    return [SimpleNamespace(name=name) for name in names or []]


def _cfg(tmp_path: Path, *, llm: bool = False) -> config.Config:
    env = {
        "APP_SECRET": "secret-de-test",
        "DATA_DIR": str(tmp_path / "data"),
    }
    if llm:
        env.update(
            {
                "LLM_BASE_URL": "http://127.0.0.1:9/v1",
                "LLM_API_KEY": "cle-de-test",
                "LLM_MODEL": "modele-de-test",
            }
        )
    return config.from_env(env, require_token=False)


@pytest.fixture
def cfg(tmp_path: Path) -> config.Config:
    return _cfg(tmp_path)


@pytest.fixture
def cfg_llm(tmp_path: Path) -> config.Config:
    return _cfg(tmp_path, llm=True)


@pytest.fixture(autouse=True)
def _cache_isolee():
    fetch._CONTENT_CACHE.clear()
    yield
    fetch._CONTENT_CACHE.clear()


# ─────────────────────────── client factice ───────────────────────────


def _content(
    title: str = "", description: str = "", files: list | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        title=title, description=description, files=list(files or [])
    )


def _homework(
    description: str = "",
    due: date | None = None,
    subject: str = "Mathématiques",
    files: list | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        description=description,
        date=due,
        subject=SimpleNamespace(name=subject),
        files=list(files or []),
    )


def _info(
    title: str = "",
    body: str = "",
    *,
    category: str | None = "Vie scolaire",
    attachments: list | None = None,
    content_raises: bool = False,
    has_content: bool = True,
) -> SimpleNamespace:
    info = SimpleNamespace(
        title=title,
        category=SimpleNamespace(name=category) if category is not None else None,
        attachments=list(attachments or []),
    )
    if content_raises:

        def content():
            raise RuntimeError("panne de lecture du corps")

        info.content = content
    elif has_content:
        info.content = lambda: body
    return info


def _discussion(
    subject: str = "",
    body: str = "",
    *,
    closed: bool = False,
    messages: list | None = None,
) -> SimpleNamespace:
    if messages is None:
        messages = [] if body == "" else [SimpleNamespace(content=body)]
    return SimpleNamespace(subject=subject, closed=closed, messages=messages)


class FakeLesson:
    """Cours factice : `content` compte les lectures comme une requête réseau."""

    def __init__(
        self,
        start: datetime,
        subject: str,
        content: object = None,
        *,
        canceled: bool = False,
    ) -> None:
        self.start = start
        self.subject = SimpleNamespace(name=subject)
        self.canceled = canceled
        self._content = content
        self.content_reads = 0

    @property
    def content(self):
        self.content_reads += 1
        if isinstance(self._content, BaseException):
            raise self._content
        return self._content


class FakeClient:
    def __init__(
        self,
        *,
        homework: list | None = None,
        lessons: list | None = None,
        infos: list | None = None,
        discussions: list | None = None,
        class_name: str | None = "2nde 3",
        failures: dict[str, BaseException] | None = None,
    ) -> None:
        self._homework = list(homework or [])
        self._lessons = list(lessons or [])
        self._infos = list(infos or [])
        self._discussions = list(discussions or [])
        self.failures = dict(failures or {})
        self.info = SimpleNamespace(class_name=class_name)
        self.calls: list[str] = []

    def _check(self, name: str) -> None:
        exc = self.failures.get(name)
        if exc is not None:
            raise exc

    def homework(self, start: date, end: date) -> list:
        self.calls.append("homework")
        self._check("homework")
        return list(self._homework)

    def lessons(self, start: date, end: date) -> list:
        self.calls.append("lessons")
        self._check("lessons")
        return list(self._lessons)

    def information_and_surveys(self, start, end, only_unread: bool = False) -> list:
        self.calls.append("infos")
        self._check("infos")
        return list(self._infos)

    def discussions(self) -> list:
        self.calls.append("discussions")
        self._check("discussions")
        return list(self._discussions)


# ─────────────────────────── cinq onglets ───────────────────────────


def test_travail_a_faire_html(conn, cfg, make_user):
    user_id = make_user()
    client = FakeClient(
        homework=[
            _homework(
                _html("travail_a_faire.html"),
                due=date(2026, 10, 23),
                files=_files(_anchors("pieces_jointes.html")),
            )
        ]
    )

    result = fetch.collect(client, conn, user_id, cfg)

    assert result.ok, result.error
    assert result.class_name == "2nde 3"
    assert result.contents_read == 0
    devoirs = [item for item in result.items if item.source == "travail_a_faire"]
    assert len(devoirs) == 1
    item = devoirs[0]
    assert item.kind == "devoir"
    assert item.subject == "Mathématiques"
    assert "Exercices 12 à 15" in item.body
    assert "page 42" in item.body
    assert "<" not in item.body
    assert item.due_date == date(2026, 10, 23)
    assert item.attachments == ["DM_fractions.pdf", "corrige.pdf"]
    assert [row.key for row in db.get_items(conn, user_id)] == [item.key]


def test_cahier_de_textes_realiste_produit_des_contenus(conn, cfg, make_user, monkeypatch):
    """Une page réaliste, non annulée, doit produire cours et contrôle."""
    monkeypatch.setattr(fetch, "CONTENT_DELAY", 0)
    user_id = make_user()
    fichiers = _files(_anchors("pieces_jointes.html"))
    lundi = FakeLesson(
        datetime(2026, 10, 5, 8, 0),
        "Mathématiques",
        _content(
            "Chapitre 3 — Les fractions",
            _html("cahier_de_textes.html"),
            fichiers,
        ),
    )
    mardi = FakeLesson(
        datetime(2026, 10, 6, 10, 0),
        "Histoire",
        _content("Évaluation", "<p>Évaluation sur la Révolution.</p>"),
    )
    annule = FakeLesson(
        datetime(2026, 10, 7, 9, 0),
        "Anglais",
        _content("Annulé", "<p>Ce cours est annulé.</p>"),
        canceled=True,
    )
    client = FakeClient(lessons=[lundi, mardi, annule])

    result = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_COURS)

    assert result.ok, result.error
    assert result.contents_read == 2
    assert result.contents_read > 0, "la page réaliste doit être lue"
    assert lundi.content_reads == 1
    assert mardi.content_reads == 1
    assert annule.content_reads == 0, "un cours annulé ne coûte aucune requête"

    cours = [item for item in result.items if item.kind == "cours"]
    controles = [item for item in result.items if item.kind == "controle"]
    assert {item.subject for item in cours} == {"Mathématiques", "Histoire"}
    maths = next(item for item in cours if item.subject == "Mathématiques")
    assert maths.source == "cahier_de_textes"
    assert maths.title == "Chapitre 3 — Les fractions"
    assert "Revoir la leçon" in maths.body
    assert "pages 42 et 43" in maths.body
    assert maths.due_date == date(2026, 10, 5)
    assert maths.attachments == ["DM_fractions.pdf", "corrige.pdf"]
    assert len(controles) == 1
    assert controles[0].subject == "Histoire"

    statuses = db.lesson_status(conn, user_id)
    assert len(statuses) == 2
    assert set(statuses.values()) == {fetch.STATUS_TRAITE}


def test_informations_et_sondages_html(conn, cfg, make_user):
    user_id = make_user()
    client = FakeClient(
        infos=[
            _info(
                "Sortie au musée",
                _html("informations.html"),
                category="Vie scolaire",
                attachments=_files(_anchors("pieces_jointes.html")),
            ),
            _info("Titre sans corps", "", category=None),
            _info("Panne du corps", "", content_raises=True),
            _info("", ""),
        ]
    )

    result = fetch.collect(client, conn, user_id, cfg)

    assert result.ok, result.error
    rows = [item for item in result.items if item.source == "informations"]
    assert len(rows) == 3
    musee = next(item for item in rows if item.title == "Sortie au musée")
    assert musee.kind == "info"
    assert musee.subject == "Vie scolaire"
    assert "La sortie est confirmée pour le" in musee.body
    assert "21 octobre" in musee.body
    assert musee.attachments == ["DM_fractions.pdf", "corrige.pdf"]
    assert any(item.subject == "Vie du lycée" for item in rows)


def test_messagerie_html_sujets_doublons_et_fermeture(conn, cfg, make_user):
    user_id = make_user()
    corps = _html("messagerie.html")
    client = FakeClient(
        discussions=[
            _discussion("Mathématiques", corps),
            _discussion("Mathématiques", "<p>doublon ignoré</p>"),
            _discussion("Anglais", corps, closed=True),
            _discussion("SVT", ""),
            _discussion(
                "",
                "",
                messages=[SimpleNamespace(content="<p>Message sans objet.</p>")],
            ),
        ]
    )

    result = fetch.collect(client, conn, user_id, cfg)

    assert result.ok, result.error
    messages = [item for item in result.items if item.source == "messagerie"]
    assert len(messages) == 2
    assert {item.subject for item in messages} == {"Mathématiques", "(sans objet)"}
    maths = next(item for item in messages if item.subject == "Mathématiques")
    assert maths.kind == "message"
    assert maths.title == "Message — Mathématiques"
    assert "contrôle de mathématiques" in maths.body
    assert "exercices 12 à 15" in maths.body


def test_pieces_jointes_entrent_dans_la_cle(conn, cfg, make_user):
    user_id = make_user()
    corps = _html("travail_a_faire.html")
    fichiers = _files(_anchors("pieces_jointes.html"))
    client = FakeClient(
        homework=[
            _homework(corps, due=date(2026, 10, 23)),
            _homework(corps, due=date(2026, 10, 23), files=fichiers),
        ]
    )

    result = fetch.collect(client, conn, user_id, cfg)

    assert result.ok, result.error
    devoirs = [item for item in result.items if item.source == "travail_a_faire"]
    assert len(devoirs) == 2, "un PDF ajouté doit produire un second item"
    sans, avec = sorted(devoirs, key=lambda item: len(item.attachments))
    assert sans.attachments == []
    assert avec.attachments == ["DM_fractions.pdf", "corrige.pdf"]
    assert sans.key != avec.key


# ─────────────────────────── page vide, champs absents ───────────────────────────


def test_champs_absents_et_contenu_vide(conn, cfg, make_user, monkeypatch):
    monkeypatch.setattr(fetch, "CONTENT_DELAY", 0)
    user_id = make_user()
    page_vide = FakeLesson(
        datetime(2026, 10, 5, 8, 0),
        "Mathématiques",
        _content("", _html("page_vide.html")),
    )
    sans_contenu = FakeLesson(datetime(2026, 10, 6, 9, 0), "Histoire", None)
    client = FakeClient(
        homework=[SimpleNamespace()],
        lessons=[page_vide, sans_contenu],
        infos=[_info("Titre seul", "", has_content=False), _info("", "")],
        discussions=[SimpleNamespace(subject="Sans messages", closed=False)],
    )

    items_result = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_ITEMS)

    assert items_result.ok, items_result.error
    assert not [item for item in items_result.items if item.source == "travail_a_faire"]
    assert not [item for item in items_result.items if item.source == "cahier_de_textes"]
    assert not [item for item in items_result.items if item.source == "messagerie"]
    infos = [item for item in items_result.items if item.source == "informations"]
    assert [item.title for item in infos] == ["Titre seul"]

    cours_result = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_COURS)

    assert cours_result.ok, cours_result.error
    assert cours_result.contents_read == 2
    assert not [item for item in cours_result.items if item.source == "cahier_de_textes"]
    statuses = db.lesson_status(conn, user_id)
    assert len(statuses) == 2
    assert set(statuses.values()) == {fetch.STATUS_VIDE}

    # Contenu vide : le cache process ne retient pas le vide, donc la synchro
    # suivante relit le cours (docstring : « retente à chaque passage ceux
    # dont le contenu est encore vide »).
    second = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_COURS)
    assert second.ok, second.error
    assert page_vide.content_reads == 2, "un cours vide doit être relu à la synchro suivante"
    assert sans_contenu.content_reads == 2


def test_page_vide_aucune_source_ne_produit_d_item(conn, cfg, make_user):
    user_id = make_user()
    result = fetch.collect(FakeClient(), conn, user_id, cfg)

    assert result.ok, result.error
    assert result.items == []
    assert result.contents_read == 0
    assert db.get_items(conn, user_id) == []


def test_html_inattendu_ne_casse_pas_la_normalisation(conn, cfg, make_user):
    user_id = make_user()
    client = FakeClient(
        homework=[_homework(_html("html_inattendu.html"), due=date(2026, 10, 23))]
    )

    result = fetch.collect(client, conn, user_id, cfg)

    assert result.ok, result.error
    devoirs = [item for item in result.items if item.source == "travail_a_faire"]
    assert len(devoirs) == 1
    body = devoirs[0].body
    assert "Texte sans fermeture" in body
    assert "cellule" in body
    assert "fin" in body
    assert "<" not in body and ">" not in body


# ─────────────────────────── cours annulés : verdict démo ───────────────────────────


def test_compte_demo_tous_cours_annules_ne_lit_aucun_contenu(conn, cfg, make_user):
    """« contenus lus : 0 » sur la démo = 52 cours annulés, pas un bug.

    Mesuré sur demo.index-education.net : 52 cours, 52 `canceled=True`, et
    leurs contenus restent parfaitement lisibles. `dev_demo` saute les cours
    annulés avant de lire le contenu ; `_fetch_items` ne lit jamais `.content`
    et `_collect_cours` saute aussi les annulés. Le zéro est donc attendu.
    """
    user_id = make_user()
    lessons = [
        FakeLesson(
            datetime(2026, 10, 5, 8, 0) + timedelta(days=jour),
            "Français",
            _content("Séance annulée", "<p>Contenu pourtant lisible.</p>"),
            canceled=True,
        )
        for jour in range(5)
    ]
    client = FakeClient(lessons=lessons)

    items_result = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_ITEMS)
    cours_result = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_COURS)

    assert items_result.ok and cours_result.ok
    assert items_result.contents_read == 0
    assert cours_result.contents_read == 0
    assert not [item for item in cours_result.items if item.source == "cahier_de_textes"]
    assert all(lesson.content_reads == 0 for lesson in lessons)


def test_phase_items_ne_lit_jamais_le_contenu_des_cours(conn, cfg, make_user):
    """La phase rapide ne paie aucune requête `.content` : c'est la phase cours."""
    user_id = make_user()
    lesson = FakeLesson(
        datetime(2026, 10, 5, 8, 0),
        "Mathématiques",
        _content("Chapitre 3", "<p>Revoir la leçon.</p>"),
    )
    client = FakeClient(lessons=[lesson])

    result = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_ITEMS)

    assert result.ok, result.error
    assert result.contents_read == 0
    assert lesson.content_reads == 0
    assert not [item for item in result.items if item.source == "cahier_de_textes"]


# ─────────────────────────── erreurs réseau et HTTP ───────────────────────────


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"HTTP {status}", response=response)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        pytest.param(auth.TokenRefused("jeton révoqué"), fetch.ERR_TOKEN, id="token-refuse"),
        pytest.param(fetch.RateLimited(), fetch.ERR_RATE, id="rate-limited"),
        pytest.param(pronotepy.MFAError("pin"), fetch.ERR_PIN, id="mfa"),
        pytest.param(pronotepy.QRCodeDecryptError("qr"), fetch.ERR_TOKEN, id="qr"),
        pytest.param(pronotepy.CryptoError("crypto"), fetch.ERR_TOKEN, id="crypto"),
        pytest.param(requests.Timeout("lent"), fetch.ERR_TIMEOUT, id="timeout"),
        pytest.param(requests.ConnectionError("down"), fetch.ERR_UNREACHABLE, id="unreachable"),
        pytest.param(_http_error(429), fetch.ERR_RATE, id="http-429"),
        pytest.param(_http_error(401), fetch.ERR_TOKEN, id="http-401"),
        pytest.param(_http_error(403), fetch.ERR_RATE, id="http-403"),
        pytest.param(_http_error(500), fetch.ERR_UNREACHABLE, id="http-500"),
        pytest.param(
            pronotepy.PronoteAPIError("api", pronote_error_code=401),
            fetch.ERR_TOKEN,
            id="api-401",
        ),
        pytest.param(
            pronotepy.PronoteAPIError("api", pronote_error_code=403),
            fetch.ERR_RATE,
            id="api-403",
        ),
        pytest.param(
            pronotepy.PronoteAPIError("api", pronote_error_code=23),
            fetch.ERR_TOKEN,
            id="api-23",
        ),
        pytest.param(
            pronotepy.PronoteAPIError("Unknown error from pronote: 23"),
            fetch.ERR_TOKEN,
            id="api-23-texte",
        ),
        pytest.param(
            pronotepy.PronoteAPIError("api", pronote_error_code=500),
            fetch.ERR_UNKNOWN,
            id="api-inconnu",
        ),
        pytest.param(ValueError("inattendu"), fetch.ERR_UNKNOWN, id="inconnu"),
        pytest.param(RuntimeError("inattendu"), fetch.ERR_UNKNOWN, id="runtime"),
    ],
)
def test_mapping_des_erreurs(exc, expected):
    assert fetch._error_of(exc) == expected


def test_un_timeout_pendant_la_collecte_est_affiche_et_remis_a_idle(conn, cfg, make_user):
    user_id = make_user()
    client = FakeClient(failures={"lessons": requests.Timeout("trop lent")})

    result = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_COURS)

    assert result.ok is False
    assert result.error == fetch.ERR_TIMEOUT
    assert result.items == []
    row = conn.execute(
        "SELECT last_error, sync_state FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    assert row["last_error"] == fetch.ERR_TIMEOUT
    assert row["sync_state"] == "idle"


# ─────────────────────────── retry / backoff ───────────────────────────


def _sleeps(monkeypatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(fetch.time, "sleep", recorded.append)
    return recorded


@pytest.mark.parametrize("status", [429, 403])
def test_un_429_ou_403_est_reessaye_une_seule_fois(status, monkeypatch):
    sleeps = _sleeps(monkeypatch)
    tries: list[int] = []

    def flaky():
        tries.append(1)
        if len(tries) == 1:
            raise _http_error(status)
        return "ok"

    assert fetch._call(flaky) == "ok"
    assert len(tries) == 2, "il faut exactement un nouvel essai"
    assert sleeps == [fetch.BACKOFF_SECONDS]


@pytest.mark.parametrize("status", [429, 403])
def test_deux_echecs_de_suite_levent_rate_limited(status, monkeypatch):
    sleeps = _sleeps(monkeypatch)
    tries: list[int] = []

    def always():
        tries.append(1)
        raise _http_error(status)

    with pytest.raises(fetch.RateLimited):
        fetch._call(always)
    assert len(tries) == 2
    assert sleeps == [fetch.BACKOFF_SECONDS]


def test_une_erreur_non_reessayable_remonte_sans_pause(monkeypatch):
    sleeps = _sleeps(monkeypatch)
    tries: list[int] = []

    def boom():
        tries.append(1)
        raise _http_error(500)

    with pytest.raises(requests.HTTPError):
        fetch._call(boom)
    assert len(tries) == 1
    assert sleeps == []


def test_un_timeout_n_est_pas_reessaye(monkeypatch):
    sleeps = _sleeps(monkeypatch)
    tries: list[int] = []

    def slow():
        tries.append(1)
        raise requests.Timeout("trop lent")

    with pytest.raises(requests.Timeout):
        fetch._call(slow)
    assert len(tries) == 1
    assert sleeps == []


# ─────────────────────────── caches ───────────────────────────


def test_cache_processus_evite_une_seconde_lecture_de_contenu(conn, cfg, make_user):
    user_id = make_user()
    lesson = FakeLesson(
        datetime(2026, 10, 5, 8, 0),
        "Mathématiques",
        _content("Chapitre 3", "<p>Revoir la leçon.</p>"),
    )
    client = FakeClient(lessons=[lesson])

    first = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_COURS)
    assert first.contents_read == 1
    second = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_COURS)
    assert second.contents_read == 0, "un cours traité ne se relit pas"
    assert [item.key for item in second.items] == [item.key for item in first.items]

    # Statut effacé : le cache process sert encore, sans nouvelle requête.
    with conn:
        conn.execute("DELETE FROM lesson_seen WHERE user_id = ?", (user_id,))
    third = fetch.collect(client, conn, user_id, cfg, phase=fetch.PHASE_COURS)
    assert third.contents_read == 0
    assert lesson.content_reads == 1


def test_plafond_de_lecture_des_contenus(conn, cfg, make_user, monkeypatch):
    monkeypatch.setattr(fetch, "MAX_CONTENT_READS", 1)
    monkeypatch.setattr(fetch, "CONTENT_DELAY", 0)
    user_id = make_user()
    lessons = [
        FakeLesson(
            datetime(2026, 10, 5, 8, 0) + timedelta(days=jour),
            f"Matière {jour}",
            _content(f"Titre {jour}", f"<p>Contenu {jour}.</p>"),
        )
        for jour in range(3)
    ]

    result = fetch.collect(
        FakeClient(lessons=lessons), conn, user_id, cfg, phase=fetch.PHASE_COURS
    )

    assert result.ok, result.error
    assert result.contents_read == 1
    assert sum(lesson.content_reads for lesson in lessons) == 1
    assert len(db.lesson_status(conn, user_id)) == 1
    cours = [item for item in result.items if item.kind == "cours"]
    assert len(cours) == 1


def test_extraction_des_messages_est_mise_en_cache_entre_eleves(
    conn, cfg_llm, make_user, monkeypatch
):
    appels: list[tuple[str, str]] = []

    def faux(subject, content, cfg_):
        appels.append((subject, content))
        return (
            {
                "matiere": "Mathématiques",
                "detail": "Faire les exercices 12 à 15",
                "due": "2026-10-21",
            },
            True,
        )

    monkeypatch.setattr("app.llm.extract_devoir_checked", faux)
    discussion = _discussion("Mathématiques", _html("messagerie.html"))
    premier = make_user(chat_id=1)
    second = make_user(chat_id=2)

    first = fetch.collect(FakeClient(discussions=[discussion]), conn, premier, cfg_llm)
    second = fetch.collect(FakeClient(discussions=[discussion]), conn, second, cfg_llm)

    assert len(appels) == 1, "un seul appel modèle pour deux élèves de la même classe"
    for result in (first, second):
        assert result.ok, result.error
        devoirs = [item for item in result.items if item.source == "messagerie"]
        assert len(devoirs) == 1
        assert devoirs[0].kind == "devoir"
        assert devoirs[0].body == "Faire les exercices 12 à 15"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, True),
        (_content(), True),
        (_content("", "<p> </p>"), True),
        (_content("Chapitre", ""), False),
        (_content("", "Texte"), False),
    ],
)
def test_content_is_empty(content, expected):
    assert fetch._content_is_empty(content) is expected
