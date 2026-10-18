"""Schéma SQLite : création initiale, migrations rejouées, contraintes, intégrité.

`app/db.py` ne versionne pas son schéma (`PRAGMA user_version` reste à 0) :
la migration tient à deux mécanismes, `CREATE TABLE IF NOT EXISTS` (le SCHEMA
complet) puis `ALTER TABLE ... ADD COLUMN` piloté par `db._USERS_COLUMNS` pour
les colonnes absentes d'une base plus ancienne.

Ce fichier couvre ce que les autres tests ne couvrent pas — `test_db.py`
concerne les écritures métier (rotation de jeton, quota, listes numérotées) et
`test_concurrency.py` les écritures concurrentes :

- l'état du schéma juste après ouverture d'une base neuve ;
- l'idempotence de `init_schema` et la préservation des données ;
- une base au format ANCIEN (colonnes et tables manquantes) ;
- les contraintes (UNIQUE, NOT NULL, clés primaires composites) et les clés
  étrangères ;
- l'intégrité après suppression de compte, y compris ce qui SURVIT ;
- la lecture depuis plusieurs threads.

Aucun réseau, aucun temporisateur : chaque test ouvre sa propre base.
"""

from __future__ import annotations

import re
import sqlite3
import threading

import pytest

from app import db
from app.pronote.normalize import Item

#: Tables créées par `db.SCHEMA`.
TABLES = {
    "users",
    "priorities",
    "items",
    "summaries",
    "done_local",
    "list_ref",
    "lesson_seen",
}

URL = "https://demo.index-education.net/pronote/eleve.html"
APP_UUID = "b4f1c2a8-6d3e-4f5a-9c7b-2e8d1a0f6c33"
STAMP = "2026-10-17T18:00:00+00:00"

_INSERT_USER = (
    "INSERT INTO users (id, chat_id, pronote_url, username, app_uuid, token_enc,"
    " client_identifier, created_at) VALUES (?,?,?,?,?,?,?,?)"
)

#: Dans `_USERS_COLUMNS`, le défaut est écrit DANS la définition de colonne.
_DEFAULT_RE = re.compile(r"DEFAULT\s+(\S+)")


def _user_params(
    user_id: str = "u1",
    chat_id: int = 1,
    *,
    pronote_url: str | None = URL,
    created_at: str | None = STAMP,
) -> tuple:
    return (
        user_id,
        chat_id,
        pronote_url,
        "demonstration",
        APP_UUID,
        b"jeton",
        "ident-1",
        created_at,
    )


def _item(key: str) -> Item:
    return Item(
        key=key,
        source="travail_a_faire",
        subject="Maths",
        title="Exercices",
        body="Exercices page 12",
        due_date=None,
        kind="devoir",
        attachments=[],
    )


def _structure(conn: sqlite3.Connection) -> list[tuple]:
    """Tout ce que SQLite garde du schéma : tables, index, déclencheurs, SQL."""
    return [
        (row["type"], row["name"], row["sql"])
        for row in conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        )
    ]


def _colonnes(conn: sqlite3.Connection) -> list[str]:
    return [row["name"] for row in conn.execute("PRAGMA table_info(users)")]


@pytest.fixture
def peuple(conn):
    """Base neuve plus une ligne dans chaque table rattachée à `u1`."""
    conn.execute(_INSERT_USER, _user_params())
    conn.execute(
        "INSERT INTO priorities (user_id, subject, rank) VALUES ('u1','Maths',0)"
    )
    conn.execute(
        "INSERT INTO items (user_id, key, source, subject, title, body, due_date,"
        " kind, attachments, fetched_at)"
        " VALUES ('u1','k1','travail_a_faire','Maths','Exercices','p.12',NULL,"
        "'devoir','[]',?)",
        (STAMP,),
    )
    conn.execute(
        "INSERT INTO summaries (key, model, summary, action, effort, created_at)"
        " VALUES ('k1','modele','resume','relire','faible',?)",
        (STAMP,),
    )
    conn.execute("INSERT INTO done_local (user_id, key) VALUES ('u1','k1')")
    conn.execute(
        "INSERT INTO list_ref (user_id, idx, key, created_at) VALUES ('u1',1,'k1',?)",
        (STAMP,),
    )
    conn.execute(
        "INSERT INTO lesson_seen (user_id, slot, status, updated_at)"
        " VALUES ('u1','2026-10-17-08h00','lu',?)",
        (STAMP,),
    )
    conn.commit()
    return conn


# ─────────────────────────── création initiale ───────────────────────────


def test_schema_neuf_cree_toutes_les_tables_et_l_index(conn):
    """Une base neuve contient les sept tables et l'index de tri des devoirs."""
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert TABLES <= tables

    assert (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
            " AND name = 'items_user_due'"
        ).fetchone()
        is not None
    )
    assert [
        row["name"] for row in conn.execute("PRAGMA index_info(items_user_due)")
    ] == ["user_id", "due_date"]
    assert conn.execute("PRAGMA index_list(items)").fetchone()["unique"] == 0


def test_colonnes_users_conformes_au_schema(conn):
    """`users` doit porter exactement `db._USERS_COLUMNS`, type et défaut compris.

    C'est le contrat dont dépendent la migration et le code applicatif : une
    colonne ajoutée au SCHEMA mais oubliée dans `_USERS_COLUMNS` ne serait
    jamais migrée sur une base existante.
    """
    info = list(conn.execute("PRAGMA table_info(users)"))
    assert [row["name"] for row in info] == [name for name, _, _ in db._USERS_COLUMNS]

    for row, (name, col_type, _default) in zip(info, db._USERS_COLUMNS):
        assert row["type"] == col_type.split()[0], name
        assert bool(row["notnull"]) == ("NOT NULL" in col_type), name
        attendu = _DEFAULT_RE.search(col_type)
        assert row["dflt_value"] == (attendu.group(1) if attendu else None), name


def test_init_schema_rejoue_est_idempotent(conn, secret, make_user):
    """Trois `init_schema` de suite laissent schéma et données strictement identiques."""
    user_id = make_user()
    db.set_priorities(conn, user_id, ["Maths", "SVT"])
    db.replace_items(conn, user_id, [_item("k1"), _item("k2")])
    db.mark_done(conn, user_id, "k1")
    db.write_list_ref(conn, user_id, ["k1", "k2"])
    db.set_lesson_status(conn, user_id, "2026-10-17-08h00", "lu")

    structure = _structure(conn)
    colonnes = _colonnes(conn)

    for _ in range(3):
        db.init_schema(conn)
        assert _structure(conn) == structure
        assert _colonnes(conn) == colonnes

    # Caractérisation : la migration ne se versionne pas, elle se contente
    # d'être rejouable.
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0

    assert db.get_user(conn, user_id) is not None
    assert len(db.get_items(conn, user_id)) == 2
    assert db.get_priorities(conn, user_id) == {"Maths": 0, "SVT": 1}
    assert db.done_keys(conn, user_id) == {"k1"}
    assert db.resolve_list_ref(conn, user_id, 2) == "k2"
    assert db.lesson_status(conn, user_id) == {"2026-10-17-08h00": "lu"}


def test_base_fermee_puis_rouverte_garde_ses_donnees(tmp_path, secret):
    """Fermer puis rouvrir la base ne perd rien et ne réécrit pas le schéma."""
    chemin = tmp_path / "app.db"
    premiere = db.connect(chemin)
    try:
        db.init_schema(premiere)
        user_id = db.create_user(
            premiere,
            chat_id=7,
            pronote_url=URL,
            username="demonstration",
            creds={"password": "jeton-reouverture", "uuid": APP_UUID},
            secret=secret,
        )
        structure = _structure(premiere)
    finally:
        premiere.close()

    seconde = db.connect(chemin)
    try:
        db.init_schema(seconde)
        row = db.get_user(seconde, user_id)
        assert row is not None
        assert row["chat_id"] == 7
        assert db.credentials_for(seconde, user_id, secret)["password"] == (
            "jeton-reouverture"
        )
        assert _structure(seconde) == structure
    finally:
        seconde.close()


# ─────────────────────────── base dans un ancien format ───────────────────────────

_BASE_ANCIENNE = """
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL UNIQUE,
    pronote_url TEXT NOT NULL,
    username TEXT NOT NULL,
    app_uuid TEXT NOT NULL,
    token_enc BLOB NOT NULL,
    client_identifier TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE priorities (
    user_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    rank INTEGER NOT NULL,
    PRIMARY KEY (user_id, subject)
);
"""


def _base_ancienne(chemin) -> None:
    """Écrit à la main une base d'avant les colonnes de réglages (sans `items`)."""
    ancien = sqlite3.connect(chemin)
    try:
        ancien.executescript(_BASE_ANCIENNE)
        ancien.execute(_INSERT_USER, _user_params())
        ancien.execute(
            "INSERT INTO priorities (user_id, subject, rank) VALUES ('u1','Maths',0)"
        )
        ancien.commit()
    finally:
        ancien.close()


def test_migration_complete_une_base_ancienne(tmp_path, secret):
    """Sur une base ancienne : colonnes ajoutées, défauts posés, données gardées."""
    chemin = tmp_path / "app.db"
    _base_ancienne(chemin)

    conn = db.connect(chemin)
    try:
        db.init_schema(conn)

        assert set(_colonnes(conn)) == {name for name, _, _ in db._USERS_COLUMNS}
        # L'ordre physique suit l'ancienne table, puis les colonnes ajoutées :
        # l'ordre de `_USERS_COLUMNS` n'est donc PAS garanti après migration.
        assert _colonnes(conn) != [name for name, _, _ in db._USERS_COLUMNS]
        assert _colonnes(conn)[:8] == [
            "id",
            "chat_id",
            "pronote_url",
            "username",
            "app_uuid",
            "token_enc",
            "client_identifier",
            "created_at",
        ]

        row = conn.execute("SELECT * FROM users WHERE id = 'u1'").fetchone()
        assert row["chat_id"] == 1
        assert row["token_enc"] == b"jeton"
        assert row["client_identifier"] == "ident-1"
        # Les colonnes ajoutées prennent le défaut déclaré.
        assert row["sync_state"] == "idle"
        assert row["allow_password"] == 0
        assert row["include_messages"] == 1
        assert row["digest_hour"] == 18
        assert row["llm_count"] == 0
        for vide in ("class_name", "account_pin_enc", "last_sync_at", "last_error",
                     "digest_day", "llm_day"):
            assert row[vide] is None, vide

        # Tables absentes de l'ancienne base : créées telles quelles.
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert TABLES <= tables
        assert conn.execute("SELECT COUNT(*) FROM priorities").fetchone()[0] == 1

        # La base migrée est utilisable : compte neuf, état de synchro, items.
        other = db.create_user(
            conn,
            chat_id=2,
            pronote_url=URL,
            username="demonstration",
            creds={"password": "jeton-neuf", "uuid": APP_UUID},
            secret=secret,
        )
        db.set_sync_state(conn, other, "syncing")
        db.replace_items(conn, other, [_item("k9")])
        assert db.get_user(conn, other)["sync_state"] == "syncing"
        assert [item.key for item in db.get_items(conn, other)] == ["k9"]

        # Rejouer la migration sur la base déjà migrée ne bouge plus rien.
        structure = _structure(conn)
        db.init_schema(conn)
        assert _structure(conn) == structure
    finally:
        conn.close()


def test_migration_ne_repare_pas_une_colonne_divergente(tmp_path):
    """Caractérisation : seules les colonnes ABSENTES sont ajoutées.

    Une colonne présente avec un autre type ou sans contrainte est laissée en
    l'état : `init_schema` ne détecte aucune divergence de schéma.
    """
    chemin = tmp_path / "app.db"
    ancien = sqlite3.connect(chemin)
    try:
        ancien.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL UNIQUE,
                pronote_url TEXT NOT NULL,
                username TEXT NOT NULL,
                app_uuid TEXT NOT NULL,
                token_enc BLOB NOT NULL,
                client_identifier TEXT,
                created_at TEXT NOT NULL,
                sync_state TEXT
            );
            """
        )
        ancien.execute(
            "INSERT INTO users (id, chat_id, pronote_url, username, app_uuid,"
            " token_enc, client_identifier, created_at)"
            " VALUES ('u1','chat-en-texte',?,?,?,?,?,?)",
            (URL, "demonstration", APP_UUID, b"jeton", None, STAMP),
        )
        ancien.execute("UPDATE users SET sync_state = 'syncing'")
        ancien.commit()
    finally:
        ancien.close()

    conn = db.connect(chemin)
    try:
        db.init_schema(conn)
        info = {row["name"]: row for row in conn.execute("PRAGMA table_info(users)")}
        # `chat_id` reste TEXT alors que `_USERS_COLUMNS` déclare INTEGER.
        assert info["chat_id"]["type"] == "TEXT"
        # `sync_state` reste nullable et sans défaut : un insert neuf y met NULL.
        assert info["sync_state"]["notnull"] == 0
        assert info["sync_state"]["dflt_value"] is None
        assert conn.execute("SELECT sync_state FROM users").fetchone()[0] == "syncing"

        conn.execute(_INSERT_USER, _user_params("u2", 2))
        assert conn.execute(
            "SELECT sync_state FROM users WHERE id = 'u2'"
        ).fetchone()[0] is None
    finally:
        conn.close()


# ─────────────────────────── contraintes ───────────────────────────

CONFLITS: dict[str, tuple[str, tuple]] = {
    "users.chat_id déjà pris": (_INSERT_USER, _user_params("u2", 1)),
    "users.id déjà pris": (_INSERT_USER, _user_params("u1", 2)),
    "users.pronote_url NULL": (
        _INSERT_USER,
        _user_params("u3", 3, pronote_url=None),
    ),
    "users.created_at NULL": (
        _INSERT_USER,
        _user_params("u4", 4, created_at=None),
    ),
    "priorities utilisateur inconnu": (
        "INSERT INTO priorities (user_id, subject, rank) VALUES ('inconnu','Maths',0)",
        (),
    ),
    "priorities (user_id, subject) en double": (
        "INSERT INTO priorities (user_id, subject, rank) VALUES ('u1','Maths',5)",
        (),
    ),
    "items utilisateur inconnu": (
        "INSERT INTO items (user_id, key, source, subject, title, body, kind,"
        " fetched_at) VALUES ('inconnu','k9','travail_a_faire','Maths','T','B','devoir',?)",
        (STAMP,),
    ),
    "items (user_id, key) en double": (
        "INSERT INTO items (user_id, key, source, subject, title, body, kind,"
        " fetched_at) VALUES ('u1','k1','travail_a_faire','Maths','T','B','devoir',?)",
        (STAMP,),
    ),
    "lesson_seen utilisateur inconnu": (
        "INSERT INTO lesson_seen (user_id, slot, status, updated_at)"
        " VALUES ('inconnu','2026-10-17-09h00','lu',?)",
        (STAMP,),
    ),
    "lesson_seen (user_id, slot) en double": (
        "INSERT INTO lesson_seen (user_id, slot, status, updated_at)"
        " VALUES ('u1','2026-10-17-08h00','vu',?)",
        (STAMP,),
    ),
    "list_ref (user_id, idx) en double": (
        "INSERT INTO list_ref (user_id, idx, key, created_at) VALUES ('u1',1,'k2',?)",
        (STAMP,),
    ),
    "summaries (key, model) en double": (
        "INSERT INTO summaries (key, model, summary, action, effort, created_at)"
        " VALUES ('k1','modele','autre','autre','fort',?)",
        (STAMP,),
    ),
}


@pytest.mark.parametrize("libelle", sorted(CONFLITS))
def test_contraintes_rejettent_les_ecritures_invalides(peuple, libelle):
    """Chaque écriture interdite par le schéma lève `IntegrityError`."""
    sql, params = CONFLITS[libelle]
    with pytest.raises(sqlite3.IntegrityError):
        peuple.execute(sql, params)
    peuple.rollback()


def test_connect_active_wal_cles_etrangeres_et_row_factory(conn):
    """`db.connect` pose les pragmas dont dépendent cascade et concurrence."""
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    assert conn.row_factory is sqlite3.Row
    assert isinstance(conn, db.LockedConnection)


def test_cles_etrangeres_inactives_sur_une_connexion_brute(tmp_path):
    """Caractérisation : seul `db.connect` active `PRAGMA foreign_keys`.

    Une connexion `sqlite3` brute sur le même fichier accepte une ligne
    orpheline : l'intégrité référentielle dépend du chemin d'ouverture.
    """
    chemin = tmp_path / "app.db"
    propre = db.connect(chemin)
    try:
        db.init_schema(propre)
        assert propre.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        propre.close()

    brute = sqlite3.connect(chemin)
    try:
        assert brute.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        brute.execute(
            "INSERT INTO priorities (user_id, subject, rank) VALUES ('inconnu','Maths',0)"
        )
        brute.commit()
        assert brute.execute("SELECT COUNT(*) FROM priorities").fetchone()[0] == 1
    finally:
        brute.close()


def test_connect_refuse_un_repertoire_absent(tmp_path):
    """Créer la base dans un répertoire manquant échoue au lieu de l'inventer."""
    with pytest.raises(RuntimeError, match="n'existe pas"):
        db.connect(tmp_path / "absent" / "app.db")


# ─────────────────────────── intégrité après suppression ───────────────────────────


def test_suppression_brute_cascade_les_tables_declarees(peuple):
    """Un simple `DELETE FROM users` suffit pour les tables déclarées en FK."""
    peuple.execute("DELETE FROM users WHERE id = 'u1'")

    for table in ("priorities", "items", "lesson_seen"):
        assert peuple.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table

    # Caractérisation : `done_local` et `list_ref` ne déclarent AUCUNE clé
    # étrangère, leurs lignes survivent et `foreign_key_check` ne les voit pas
    # (aucune contrainte à violer). C'est pourquoi `db.delete_user` les
    # supprime explicitement.
    assert peuple.execute("SELECT COUNT(*) FROM done_local").fetchone()[0] == 1
    assert peuple.execute("SELECT COUNT(*) FROM list_ref").fetchone()[0] == 1
    assert peuple.execute("PRAGMA foreign_key_check").fetchall() == []


def test_delete_user_ne_laisse_aucun_orphelin_mais_garde_les_resumes(peuple):
    """Après `delete_user` : plus rien de l'élève, sauf les résumés partagés.

    Les résumés sont indexés par `key` (hash de contenu) et partagés entre
    élèves : la suppression d'un compte ne les touche pas, seul
    `purge_old_summaries` les enlève après sept jours.
    """
    db.delete_user(peuple, "u1")

    assert peuple.execute("PRAGMA foreign_key_check").fetchall() == []
    for table in ("priorities", "items", "done_local", "list_ref", "lesson_seen"):
        assert peuple.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert peuple.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 1


# ─────────────────────────── lecture concurrente ───────────────────────────

LECTEURS = 6
ITERATIONS = 40


def test_lectures_concurrentes_ne_voient_jamais_d_instantane_partiel(conn, make_user):
    """Six threads de lecture pendant qu'un septième écrit : aucune vue partielle.

    Chaque lecteur doit toujours voir les huit items d'un lot réécrit en une
    transaction, et un `sync_state` parmi les deux valeurs écrites.
    """
    user_id = make_user()
    db.replace_items(conn, user_id, [_item(f"k{i}") for i in range(8)])
    db.set_sync_state(conn, user_id, "idle")

    depart = threading.Barrier(LECTEURS + 2)  # six lecteurs, un écrivain, ce test
    erreurs: list[BaseException] = []
    lectures: list[int] = []

    def lecteur() -> None:
        try:
            depart.wait()
            for _ in range(ITERATIONS):
                keys = {item.key for item in db.get_items(conn, user_id)}
                assert keys == {f"k{i}" for i in range(8)}, keys
                assert (
                    conn.execute(
                        "SELECT COUNT(*) FROM users WHERE id = ?", (user_id,)
                    ).fetchone()[0]
                    == 1
                )
                etat = conn.execute(
                    "SELECT sync_state FROM users WHERE id = ?", (user_id,)
                ).fetchone()["sync_state"]
                assert etat in {"idle", "syncing"}, etat
                lectures.append(len(keys))
        except BaseException as exc:  # noqa: BLE001 - rapporté au thread principal
            erreurs.append(exc)

    def ecrivain() -> None:
        try:
            depart.wait()
            for i in range(ITERATIONS * LECTEURS):
                db.set_sync_state(conn, user_id, "syncing" if i % 2 else "idle")
        except BaseException as exc:  # noqa: BLE001
            erreurs.append(exc)

    threads = [threading.Thread(target=lecteur) for _ in range(LECTEURS)]
    threads.append(threading.Thread(target=ecrivain))
    for thread in threads:
        thread.start()
    depart.wait()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "un thread est resté bloqué"

    assert not erreurs, f"exceptions concurrentes : {erreurs!r}"
    assert lectures == [8] * (ITERATIONS * LECTEURS)


def test_resultat_de_requete_materiel_reutilisable(conn, make_user):
    """Les lignes sont lues SOUS verrou : le résultat ne dépend plus du curseur.

    Un curseur `sqlite3` n'exécute la requête qu'au premier accès ; réutiliser
    le résultat hors verrou doit donc rester possible et stable.
    """
    make_user()
    make_user()

    resultat = conn.execute("SELECT id FROM users ORDER BY id")
    assert not isinstance(resultat, sqlite3.Cursor)
    assert len(resultat) == 2
    premiers = [row["id"] for row in resultat]
    assert [row["id"] for row in resultat] == premiers
    assert resultat.fetchone()["id"] == premiers[0]
    assert len(resultat.fetchall()) == 2
    assert resultat.fetchall() is not resultat.rows

    insere = conn.execute(_INSERT_USER, _user_params("u9", 99))
    assert insere.rowcount == 1
    assert isinstance(insere.lastrowid, int) and insere.lastrowid > 0
