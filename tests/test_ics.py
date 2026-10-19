"""Export iCalendar : structure, échappement, UID stable, pliage RFC 5545."""

from __future__ import annotations

import importlib
from datetime import date

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import db
from app.pronote.normalize import make_item
from server import deps

SECRET = Fernet.generate_key().decode()
PRONOTE_URL = "https://demo.index-education.net/pronote/eleve.html"


# ── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Serveur isolé : config et base dans `tmp_path`, magasins vidés."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    for variable in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(deps, "_conn", None)
    monkeypatch.setattr(deps, "_cfg", None)
    monkeypatch.setattr(deps, "_MEMORY", {})
    monkeypatch.setattr(deps, "_SESSIONS", {})

    main = importlib.import_module("server.main")
    with TestClient(main.app) as test_client:
        yield test_client

    conn = deps._conn
    if conn is not None:
        conn.close()


@pytest.fixture
def conn(client):
    return deps.get_conn()


# ── helpers ────────────────────────────────────────────────────────────


def _compte(conn) -> tuple[str, str]:
    """Crée un compte minimal et rend `(user_id, jeton)`."""
    user_id = db.create_user(
        conn,
        chat_id=1,
        pronote_url=PRONOTE_URL,
        username="eleve1",
        creds={
            "password": "jeton-initial",
            "client_identifier": "ident-1",
            "uuid": "b4f1c2a8-6d3e-4f5a-9c7b-2e8d1a0f6c33",
        },
        secret=SECRET,
    )
    return user_id, deps.issue_token(user_id, conn)


def _devoir(subject: str, body: str, due: date | None):
    return make_item(
        kind="devoir", source="travail_a_faire", subject=subject, body=body, due_date=due
    )


def _entete(jeton: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {jeton}"}


def _lignes(corps: str) -> list[str]:
    """Déplie (RFC 5545 §3.1) puis découpe en lignes logiques."""
    return corps.replace("\r\n ", "").split("\r\n")


# ── tests ──────────────────────────────────────────────────────────────


def test_structure_vcalendar_et_un_vevent_par_devoir(client, conn):
    user_id, jeton = _compte(conn)
    db.replace_items(
        conn,
        user_id,
        [
            _devoir("Maths", "Exo 1", date(2026, 10, 20)),
            make_item(
                kind="controle",
                source="cahier_de_textes",
                subject="SVT",
                body="Contrôle chapitre 2",
                due_date=date(2026, 10, 22),
            ),
            # Sans date, ni un cours : rien à placer dans un calendrier.
            _devoir("Lecture", "Chapitre 3", None),
            make_item(
                kind="cours",
                source="cahier_de_textes",
                subject="Anglais",
                body="Leçon du jour",
                due_date=date(2026, 10, 21),
            ),
            # Ni une info : une date lue dans le texte n'en fait pas un devoir.
            make_item(
                kind="info",
                source="informations",
                subject="Vie du lycée",
                body="Sortie le 25 octobre",
                due_date=date(2026, 10, 25),
            ),
        ],
    )

    reponse = client.get("/calendar.ics", headers=_entete(jeton))

    assert reponse.status_code == 200
    assert reponse.headers["content-type"] == "text/calendar; charset=utf-8"
    corps = reponse.text
    assert corps.startswith("BEGIN:VCALENDAR\r\n")
    assert corps.endswith("END:VCALENDAR\r\n")
    assert corps.count("\r\nBEGIN:VEVENT\r\n") == 2
    assert corps.count("\r\nEND:VEVENT\r\n") == 2
    assert "DTSTART;VALUE=DATE:20261020\r\n" in corps
    assert "DTSTART;VALUE=DATE:20261022\r\n" in corps
    assert "SUMMARY:Maths — Exo 1\r\n" in corps
    assert "SUMMARY:SVT — Contrôle chapitre 2\r\n" in corps
    assert "Vie du lycée" not in corps


def test_echappement_des_virgules_points_virgules_et_antislashs(client, conn):
    user_id, jeton = _compte(conn)
    db.replace_items(
        conn,
        user_id,
        [
            make_item(
                kind="devoir",
                source="travail_a_faire",
                subject="Maths",
                body="Exo 1",
                title="Exo 1, p. 2; C:\\perso\\fiche\nsuite",
                due_date=date(2026, 10, 20),
            )
        ],
    )

    corps = client.get("/calendar.ics", headers=_entete(jeton)).text

    assert (
        "SUMMARY:Maths — Exo 1\\, p. 2\\; C:\\\\perso\\\\fiche\\nsuite\r\n" in corps
    )


def test_uid_stable_entre_deux_appels(client, conn):
    user_id, jeton = _compte(conn)
    item = _devoir("Maths", "Exo 1", date(2026, 10, 20))
    db.replace_items(conn, user_id, [item])

    premier = _lignes(client.get("/calendar.ics", headers=_entete(jeton)).text)
    second = _lignes(client.get("/calendar.ics", headers=_entete(jeton)).text)

    uids = [
        [ligne for ligne in corps if ligne.startswith("UID:")]
        for corps in (premier, second)
    ]
    assert uids[0] == uids[1] == [f"UID:{item.key}@pronote-blocus"]


def test_pliage_a_75_octets_sans_couper_un_caractere(client, conn):
    user_id, jeton = _compte(conn)
    item = _devoir("Mathématiques", "Évaluation " * 20, date(2026, 10, 20))
    db.replace_items(conn, user_id, [item])

    reponse = client.get("/calendar.ics", headers=_entete(jeton))

    # `reponse.text` décode le corps en UTF-8 : un accent coupé en deux par le
    # pliage ferait échouer cette étape.
    corps = reponse.content.decode("utf-8")
    physiques = corps.split("\r\n")
    assert all(len(ligne.encode("utf-8")) <= 75 for ligne in physiques)
    assert any(ligne.startswith(" ") for ligne in physiques)

    lignes = _lignes(corps)
    assert f"SUMMARY:Mathématiques — {item.title}" in lignes


def test_calendrier_exige_un_jeton(client):
    assert client.get("/calendar.ics").status_code == 401
