"""Deux connexions SQLite simultanées sur le même fichier.

`tests/test_concurrency.py` et `tests/test_db_quota.py` partagent UNE
connexion : `LockedConnection` y sérialise les threads, donc le verrou de
SQLite n'est jamais sollicité. Ici chaque thread ouvre la sienne — c'est le
cas réel du bot (event loop) et du serveur web quand ils tournent côte à
côte — et c'est `busy_timeout` + WAL qui doivent absorber la contention.

Propriétés vérifiées :

- aucune écriture ne remonte `database is locked` ;
- le quota LLM ne se laisse pas dépasser par un read-modify-write mené sur
  deux connexions (le SELECT initial doit être couvert par le verrou
  d'écriture) ;
- une lecture ne bloque pas derrière une écriture non validée.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from app import db

JOUR = "2026-10-17"
PLAFOND = 10
#: Assez d'itérations pour croiser plusieurs fois les deux écrivains.
TOURS = 40

@pytest.fixture
def deux(tmp_path):
    """Deux connexions indépendantes sur la même base, schéma déjà posé."""
    premiere = db.connect(tmp_path / "app.db")
    db.init_schema(premiere)
    seconde = db.connect(tmp_path / "app.db")
    yield premiere, seconde
    premiere.close()
    seconde.close()

def _utilisateur(conn: sqlite3.Connection, secret: str, chat_id: int = 1) -> str:
    return db.create_user(
        conn,
        chat_id=chat_id,
        pronote_url="https://demo.index-education.net/pronote/eleve.html",
        username="demonstration",
        creds={
            "password": "jeton-initial",
            "client_identifier": "ident-1",
            "uuid": "b4f1c2a8-6d3e-4f5a-9c7b-2e8d1a0f6c33",
        },
        secret=secret,
    )

def _lance(connexions, job) -> list:
    """Un thread par connexion, tous libérés en même temps.

    Rend les résultats ; toute exception remontée par un thread fait échouer le
    test (c'est précisément ce qu'on veut interdire).
    """
    barriere = threading.Barrier(len(connexions))
    erreurs: list[BaseException] = []
    resultats: list = [None] * len(connexions)

    def runner(i: int) -> None:
        try:
            barriere.wait(timeout=10)
            resultats[i] = job(i, connexions[i])
        except BaseException as exc:  # noqa: BLE001 - on veut TOUT remonter
            erreurs.append(exc)

    threads = [
        threading.Thread(target=runner, args=(i,), daemon=True)
        for i in range(len(connexions))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive(), "une connexion est restée bloquée"

    assert not erreurs, f"exceptions concurrentes : {erreurs!r}"
    return resultats

def test_chaque_connexion_porte_wal_et_busy_timeout(deux):
    """Le réglage est par connexion : la seconde doit l'avoir aussi."""
    for conn in deux:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000

def test_ecritures_simultanees_sans_database_is_locked(deux, secret):
    """Deux connexions qui écrivent en boucle : aucune exception, état cohérent."""
    premiere, seconde = deux
    user_id = _utilisateur(premiere, secret)

    def job(i: int, conn: sqlite3.Connection) -> int:
        tag = "ab"[i]
        for tour in range(TOURS):
            db.set_sync_state(conn, user_id, f"{tag}-{tour}")
            db.set_priorities(conn, user_id, [f"{tag}{tour}"])
            db.write_list_ref(conn, user_id, [f"{tag}-{tour}"])
            db.mark_done(conn, user_id, f"{tag}-{tour}")
            db.put_summaries(
                conn,
                "modele",
                {f"{tag}-{tour}": {"summary": "s", "action": "a", "effort": "faible"}},
            )
            db.create_web_session(conn, f"{tag}-{tour}", user_id, 2**40)
        return TOURS

    _lance((premiere, seconde), job)

    attendues = {f"{tag}-{tour}" for tag in "ab" for tour in range(TOURS)}
    assert db.done_keys(premiere, user_id) == attendues
    assert len(db.get_priorities(premiere, user_id)) == 1
    assert db.resolve_list_ref(premiere, user_id, 1) in {"a-39", "b-39"}
    assert db.get_user(premiere, user_id)["sync_state"] in {"a-39", "b-39"}
    assert premiere.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == len(
        attendues
    )
    assert premiere.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0] == len(
        attendues
    )

def test_plafond_llm_ne_se_depasse_pas_entre_deux_connexions(deux, secret):
    """Plafond 10, 20 questions réparties sur deux connexions : 10 acceptées.

    Sans `BEGIN IMMEDIATE`, les deux connexions lisent le compteur hors
    transaction et la seconde écriture écrase la première (mesuré : 20
    acceptées, compteur à 10).
    """
    premiere, seconde = deux
    user_id = _utilisateur(premiere, secret)
    premiere.execute(
        "UPDATE users SET llm_day = ?, llm_count = 0 WHERE id = ?", (JOUR, user_id)
    )
    premiere.commit()

    def job(i: int, conn: sqlite3.Connection) -> int:
        return sum(
            db.bump_llm_count(conn, user_id, JOUR, PLAFOND) for _ in range(PLAFOND)
        )

    acceptees = _lance((premiere, seconde), job)

    assert sum(acceptees) == PLAFOND
    assert db.get_user(premiere, user_id)["llm_count"] == PLAFOND

def test_lecture_pendant_une_ecriture_non_validee(deux, secret):
    """WAL : la seconde connexion lit l'ancienne valeur au lieu d'attendre."""
    premiere, seconde = deux
    user_id = _utilisateur(premiere, secret)
    db.set_sync_state(premiere, user_id, "idle")

    premiere.execute("BEGIN IMMEDIATE")
    premiere.execute(
        "UPDATE users SET sync_state = 'sync' WHERE id = ?", (user_id,)
    )
    try:
        assert db.get_user(seconde, user_id)["sync_state"] == "idle"
    finally:
        premiere.rollback()

    assert db.get_user(seconde, user_id)["sync_state"] == "idle"

def test_init_schema_simultane_sur_un_fichier_vierge(tmp_path):
    """Quatre ouvertures concurrentes d'une base neuve : pas de course au schéma.

    Rejoué 20 fois : sur un seul essai, un `database is locked` peut ne pas
    tomber selon l'ordonnancement des threads.
    """
    for tour in range(20):
        chemin = tmp_path / f"neuf-{tour}.db"
        erreurs: list[BaseException] = []
        barriere = threading.Barrier(4)

        def ouvrir() -> None:
            try:
                conn = db.connect(chemin)
                try:
                    barriere.wait(timeout=10)
                    db.init_schema(conn)
                finally:
                    conn.close()
            except BaseException as exc:  # noqa: BLE001
                erreurs.append(exc)

        threads = [threading.Thread(target=ouvrir, daemon=True) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            assert not thread.is_alive(), "une ouverture est restée bloquée"

        assert not erreurs, f"exceptions concurrentes (tour {tour}) : {erreurs!r}"

    conn = db.connect(tmp_path / "neuf-19.db")
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"users", "items", "web_sessions", "summaries"} <= tables
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


# ─────────────────── migration sur une base d'un ancien format ───────────────────

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
"""

def _base_ancienne(chemin) -> None:
    """Base d'avant les colonnes de réglages, avec un compte dedans.

    La ligne existante est ce qui rend la course visible : sans elle, un
    `ALTER TABLE ... NOT NULL` ne prouve rien.
    """
    ancien = sqlite3.connect(chemin)
    try:
        ancien.executescript(_BASE_ANCIENNE)
        ancien.execute(
            "INSERT INTO users (id, chat_id, pronote_url, username, app_uuid,"
            " token_enc, client_identifier, created_at)"
            " VALUES ('u1',1,'https://demo.index-education.net/pronote/eleve.html',"
            " 'demonstration','b4f1c2a8-6d3e-4f5a-9c7b-2e8d1a0f6c33',X'00','ident-1',"
            " '2026-10-17T18:00:00+00:00')"
        )
        ancien.commit()
    finally:
        ancien.close()

def test_migration_simultanee_sur_une_base_ancienne(tmp_path):
    """Bot et serveur web démarrant ensemble : une seule migration gagne.

    La fenêtre est forcée par un `trace_callback` qui endort la première
    connexion juste avant son premier `ALTER`. Sans verrou d'écriture, la
    seconde lit `table_info` pendant ce sommeil, voit les mêmes colonnes
    absentes, et l'une des deux lève `duplicate column name` (mesuré).
    """
    chemin = tmp_path / "app.db"
    _base_ancienne(chemin)
    premiere = db.connect(chemin)
    seconde = db.connect(chemin)
    erreurs: list[BaseException] = []

    endormi = []

    def ralentir(sql: str) -> None:
        if "ALTER TABLE users ADD COLUMN" in sql and not endormi:
            endormi.append(1)
            time.sleep(0.3)

    premiere.set_trace_callback(ralentir)

    def migrer(conn: sqlite3.Connection) -> None:
        try:
            db.init_schema(conn)
        except BaseException as exc:  # noqa: BLE001 - on veut TOUT remonter
            erreurs.append(exc)

    a = threading.Thread(target=migrer, args=(premiere,), daemon=True)
    b = threading.Thread(target=migrer, args=(seconde,), daemon=True)
    a.start()
    time.sleep(0.05)
    b.start()
    a.join(timeout=20)
    b.join(timeout=20)
    assert not a.is_alive() and not b.is_alive(), "une migration est restée bloquée"
    premiere.set_trace_callback(None)

    try:
        assert not erreurs, f"migrations concurrentes : {erreurs!r}"
        colonnes = {
            row["name"] for row in premiere.execute("PRAGMA table_info(users)")
        }
        assert colonnes == {name for name, _, _ in db._USERS_COLUMNS}
        assert premiere.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    finally:
        premiere.close()
        seconde.close()

def test_migration_est_atomique(monkeypatch, tmp_path):
    """Un `ALTER` qui échoue ne laisse pas la moitié des colonnes ajoutées.

    Les DDL n'ouvrent pas de transaction implicite en mode legacy : sans
    `BEGIN IMMEDIATE`, chaque colonne resterait en place après l'échec.
    """
    chemin = tmp_path / "app.db"
    _base_ancienne(chemin)
    colonnes = list(db._USERS_COLUMNS) + [("colonne_impossible", "TEXT NOT NULL", None)]
    monkeypatch.setattr(db, "_USERS_COLUMNS", colonnes)

    conn = db.connect(chemin)
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.init_schema(conn)
        restantes = {
            row["name"] for row in conn.execute("PRAGMA table_info(users)")
        }
        assert restantes == {
            "id",
            "chat_id",
            "pronote_url",
            "username",
            "app_uuid",
            "token_enc",
            "client_identifier",
            "created_at",
        }
    finally:
        conn.close()

def test_ecriture_attend_le_verrou_au_lieu_d_echouer(deux, secret):
    """Une écriture concurrente patiente le temps de la transaction en cours."""
    premiere, seconde = deux
    user_id = _utilisateur(premiere, secret)

    premiere.execute("BEGIN IMMEDIATE")
    premiere.execute(
        "UPDATE users SET sync_state = 'sync' WHERE id = ?", (user_id,)
    )

    erreurs: list[BaseException] = []
    attente: list[float] = []

    def ecrire() -> None:
        try:
            debut = time.monotonic()
            db.set_sync_state(seconde, user_id, "fin")
            attente.append(time.monotonic() - debut)
        except BaseException as exc:  # noqa: BLE001
            erreurs.append(exc)

    thread = threading.Thread(target=ecrire, daemon=True)
    thread.start()
    time.sleep(0.2)
    premiere.commit()
    thread.join(timeout=20)

    assert not thread.is_alive(), "l'écriture est restée bloquée"
    assert not erreurs, f"écriture concurrente : {erreurs!r}"
    assert attente and attente[0] >= 0.15, "l'écriture n'a pas attendu le verrou"
    assert db.get_user(premiere, user_id)["sync_state"] == "fin"
