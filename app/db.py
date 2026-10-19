"""SQLite : schéma, ouverture, et l'UNIQUE point d'écriture des identifiants.

`save_credentials` est le point de rupture en production : chaque login
réussi invalide le jeton précédent côté PRONOTE. Si la nouvelle valeur n'est
pas écrite dans la même transaction que la réponse de login, le compte devient
inaccessible et il faut un nouveau QR.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import crypto


class _Materialized:
    """Résultat d'une requête, déjà lu SOUS verrou.

    Un curseur sqlite3 n'exécute la requête qu'au premier `fetch`/`next` : le
    relâcher hors verrou laisse deux threads marcher sur le même curseur
    (mesuré : `InterfaceError: bad parameter or other API misuse` sous charge).
    On matérialise donc les lignes avant de rendre la main.
    """

    __slots__ = ("rows", "rowcount", "lastrowid")

    def __init__(self, cursor) -> None:  # noqa: ANN001
        self.rows = cursor.fetchall()
        self.rowcount = cursor.rowcount
        self.lastrowid = cursor.lastrowid

    def __iter__(self):
        return iter(self.rows)

    def fetchone(self):  # noqa: ANN201
        return self.rows[0] if self.rows else None

    def fetchall(self):  # noqa: ANN201
        return list(self.rows)

    def __len__(self) -> int:
        return len(self.rows)


class LockedConnection(sqlite3.Connection):
    """Connexion unique partagée par l'event loop ET jusqu'à `MAX_CONCURRENT_SYNCS`
    threads de synchro.

    Sans sérialisation, deux threads entrant dans `with conn:` lèvent
    `ProgrammingError: cannot start a transaction within a transaction`, et un
    commit concurrent peut figer une rotation de jeton à moitié écrite. Le
    verrou couvre donc la TRANSACTION (via `__enter__`) ET chaque requête.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def execute(self, sql, parameters=(), /):  # noqa: ANN001, ANN201
        with self._lock:
            return _Materialized(super().execute(sql, parameters))

    def executemany(self, sql, seq_of_parameters, /):  # noqa: ANN001, ANN201
        with self._lock:
            return _Materialized(super().executemany(sql, seq_of_parameters))

    def executescript(self, sql_script, /):  # noqa: ANN001, ANN201
        with self._lock:
            return super().executescript(sql_script)

    def commit(self) -> None:
        with self._lock:
            super().commit()

    def rollback(self) -> None:
        with self._lock:
            super().rollback()

    def __enter__(self):  # noqa: ANN204
        self._lock.acquire()
        try:
            super().__enter__()
        except BaseException:
            self._lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN204
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self._lock.release()

@contextmanager
def _write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Transaction d'écriture qui prend le verrou SQLite AVANT le premier SELECT.

    `with conn:` ne démarre la transaction qu'au premier INSERT/UPDATE/DELETE :
    un SELECT en tête lit hors transaction. Deux connexions lisent alors le
    même compteur et la seconde écriture écrase la première (mesuré : plafond
    de 10, 20 questions acceptées, compteur à 10). `BEGIN IMMEDIATE` réserve
    l'écriture tout de suite ; les autres connexions attendent `busy_timeout`
    au lieu de bâtir leur décision sur un état périmé.

    Si l'appelant a déjà ouvert une transaction, on s'y rattache : le verrou
    de `LockedConnection` sérialise alors les threads de la même connexion.
    """
    with conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        yield conn

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL UNIQUE,
    pronote_url TEXT NOT NULL,
    username TEXT NOT NULL,
    app_uuid TEXT NOT NULL,
    token_enc BLOB NOT NULL,
    client_identifier TEXT,
    account_pin_enc BLOB,
    class_name TEXT,
    created_at TEXT NOT NULL,
    last_sync_at TEXT,
    last_error TEXT,
    last_error_at TEXT,
    sync_state TEXT NOT NULL DEFAULT 'idle',
    allow_password INTEGER NOT NULL DEFAULT 0,
    include_messages INTEGER NOT NULL DEFAULT 1,
    digest_hour INTEGER NOT NULL DEFAULT 18,
    digest_day TEXT,
    llm_day TEXT,
    llm_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS priorities (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    rank INTEGER NOT NULL,
    PRIMARY KEY (user_id, subject)
);
CREATE TABLE IF NOT EXISTS items (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    source TEXT NOT NULL,
    subject TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    due_date TEXT,
    kind TEXT NOT NULL,
    attachments TEXT NOT NULL DEFAULT '[]',
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS summaries (
    key TEXT NOT NULL,
    model TEXT NOT NULL,
    summary TEXT NOT NULL,
    action TEXT NOT NULL,
    effort TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (key, model)
);
CREATE TABLE IF NOT EXISTS done_local (
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS list_ref (
    user_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, idx)
);
CREATE TABLE IF NOT EXISTS lesson_seen (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    slot TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, slot)
);
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS items_user_due ON items(user_id, due_date);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    parent = path.parent
    if not parent.exists():
        raise RuntimeError(
            f"Le répertoire de la base n'existe pas : {parent}"
        )
    conn = sqlite3.connect(
        str(path), timeout=30, check_same_thread=False, factory=LockedConnection
    )
    conn.row_factory = sqlite3.Row
    _configure(conn)
    return conn

#: Le passage en WAL exige un verrou exclusif bref que `busy_timeout` ne
#: couvre pas : deux ouvertures simultanées d'une base neuve se répondent
#: `database is locked` (mesuré sous quatre threads). On réessaie ~1 s.
_WAL_ATTEMPTS = 20
_WAL_BACKOFF = 0.05

def _configure(conn: sqlite3.Connection) -> None:
    """Pose les pragmas de connexion, en réessayant le passage en WAL.

    Une fois WAL posé, la demande suivante ne prend plus de verrou : la
    contention ne concerne que la toute première ouverture.
    """
    # `busy_timeout` d'abord : les écritures qui suivent doivent attendre.
    conn.execute("PRAGMA busy_timeout=30000")
    for essai in range(_WAL_ATTEMPTS):
        try:
            row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or essai == _WAL_ATTEMPTS - 1:
                raise
        else:
            if row is not None and str(row[0]).lower() == "wal":
                break
            if essai == _WAL_ATTEMPTS - 1:
                mode = row[0] if row else "?"
                raise RuntimeError(f"impossible d'activer WAL : {mode}")
        time.sleep(_WAL_BACKOFF)
    conn.execute("PRAGMA foreign_keys=ON")


# Colonnes attendues dans `users` pour la migration idempotente.
# L'ordre doit refléter le SCHEMA ci-dessus (définitions → défauts inclus).
_USERS_COLUMNS: list[tuple[str, str, str | None]] = [
    ("id", "TEXT PRIMARY KEY", None),
    ("chat_id", "INTEGER NOT NULL UNIQUE", None),
    ("pronote_url", "TEXT NOT NULL", None),
    ("username", "TEXT NOT NULL", None),
    ("app_uuid", "TEXT NOT NULL", None),
    ("token_enc", "BLOB NOT NULL", None),
    ("client_identifier", "TEXT", None),
    ("account_pin_enc", "BLOB", None),
    ("class_name", "TEXT", None),
    ("created_at", "TEXT NOT NULL", None),
    ("last_sync_at", "TEXT", None),
    ("last_error", "TEXT", None),
    ("last_error_at", "TEXT", None),
    ("sync_state", "TEXT NOT NULL DEFAULT 'idle'", None),
    ("allow_password", "INTEGER NOT NULL DEFAULT 0", None),
    ("include_messages", "INTEGER NOT NULL DEFAULT 1", None),
    ("digest_hour", "INTEGER NOT NULL DEFAULT 18", None),
    ("digest_day", "TEXT", None),
    ("llm_day", "TEXT", None),
    ("llm_count", "INTEGER NOT NULL DEFAULT 0", None),
]


def init_schema(conn: sqlite3.Connection) -> None:
    # `executescript` commite avant de lancer le script et les CREATE TABLE
    # sont idempotents : la contention y est absorbée par `busy_timeout`.
    conn.executescript(SCHEMA)
    # Les ALTER, eux, doivent être lus ET écrits sous le même verrou. En
    # autocommit (les DDL n'ouvrent aucune transaction implicite), le bot et
    # le serveur web démarrant ensemble sur une base ancienne lisent tous
    # deux `table_info` avant l'ALTER de l'autre, et le second échoue sur
    # `duplicate column name` (mesuré).
    with _write_tx(conn):
        existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(users)")
        }
        for name, col_type, _default in _USERS_COLUMNS:
            if name not in existing:
                conn.execute(
                    f"ALTER TABLE users ADD COLUMN {name} {col_type}"
                )
                existing.add(name)


# ─────────────────────────── identifiants ───────────────────────────


def save_credentials(
    conn: sqlite3.Connection,
    user_id: str,
    creds: dict,
    secret: str,
) -> None:
    """Écrit le jeton tourné en UNE transaction.

    `creds` est le dict rendu par `client.export_credentials()` : il contient
    le nouveau mot de passe-jeton (`password`) et éventuellement un
    `client_identifier` rafraîchi.
    """
    password = creds.get("password")
    if not password:
        raise ValueError("export_credentials() ne contient pas de jeton")
    account_pin = creds.get("account_pin")
    fields = ["token_enc = ?"]
    values: list[object] = [crypto.encrypt(password, secret)]
    client_identifier = creds.get("client_identifier")
    if client_identifier:
        fields.append("client_identifier = ?")
        values.append(client_identifier)
    if account_pin:
        fields.append("account_pin_enc = ?")
        values.append(crypto.encrypt(account_pin, secret))
    values.append(user_id)
    with conn:
        cur = conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
        if cur.rowcount != 1:
            raise KeyError(f"utilisateur inconnu : {user_id}")


def credentials_for(conn: sqlite3.Connection, user_id: str, secret: str) -> dict:
    row = conn.execute(
        "SELECT username, token_enc, client_identifier, account_pin_enc, pronote_url,"
        " app_uuid FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"utilisateur inconnu : {user_id}")
    pin_enc = row["account_pin_enc"]
    return {
        "username": row["username"],
        "password": crypto.decrypt(row["token_enc"], secret),
        "client_identifier": row["client_identifier"],
        "account_pin": crypto.decrypt(pin_enc, secret) if pin_enc else None,
        "pronote_url": row["pronote_url"],
        "uuid": row["app_uuid"],
    }


# ─────────────────────────── utilisateurs ───────────────────────────


def get_user_by_chat(conn: sqlite3.Connection, chat_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()


def get_user(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def create_user(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    pronote_url: str,
    username: str,
    creds: dict,
    secret: str,
    class_name: str | None = None,
    account_pin: str | None = None,
) -> str:
    """Crée le compte et range les credentials dans la MÊME transaction."""
    import uuid as _uuid

    user_id = str(_uuid.uuid4())
    account_pin = account_pin or creds.get("account_pin")
    with conn:
        conn.execute(
            "INSERT INTO users (id, chat_id, pronote_url, username, app_uuid, token_enc,"
            " client_identifier, account_pin_enc, class_name, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                user_id,
                chat_id,
                pronote_url,
                username or creds.get("username", ""),
                creds.get("uuid") or crypto.new_app_uuid(),
                crypto.encrypt(creds["password"], secret),
                creds.get("client_identifier"),
                crypto.encrypt(account_pin, secret) if account_pin else None,
                class_name,
                now_iso(),
            ),
        )
    return user_id


def reauth_user(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    creds: dict,
    secret: str,
    pronote_url: str | None = None,
    username: str | None = None,
    class_name: str | None = None,
    account_pin: str | None = None,
) -> None:
    """Nouveau QR pour un compte EXISTANT : jeton, identifiant et UUID d'appareil
    sont remplacés en une transaction, `last_error` est levé.

    Un nouveau QR = un nouvel appareil : l'`app_uuid` doit suivre, sinon
    PRONOTE refuse le jeton au login suivant.
    """
    fields = [
        "token_enc = ?",
        "app_uuid = ?",
        "last_error = NULL",
        "last_error_at = NULL",
        "sync_state = 'idle'",
    ]
    values: list[object] = [
        crypto.encrypt(creds["password"], secret),
        creds.get("uuid") or crypto.new_app_uuid(),
    ]
    if account_pin:
        fields.append("account_pin_enc = ?")
        values.append(crypto.encrypt(account_pin, secret))
    if creds.get("client_identifier"):
        fields.append("client_identifier = ?")
        values.append(creds["client_identifier"])
    if pronote_url:
        fields.append("pronote_url = ?")
        values.append(pronote_url)
    if username:
        fields.append("username = ?")
        values.append(username)
    if class_name:
        fields.append("class_name = ?")
        values.append(class_name)
    values.append(user_id)
    with conn:
        cur = conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
        if cur.rowcount != 1:
            raise KeyError(f"utilisateur inconnu : {user_id}")


def delete_user(conn: sqlite3.Connection, user_id: str) -> None:
    with conn:
        conn.execute("DELETE FROM priorities WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM list_ref WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM done_local WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM lesson_seen WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM items WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def set_sync_state(conn: sqlite3.Connection, user_id: str, state: str) -> None:
    with conn:
        conn.execute("UPDATE users SET sync_state = ? WHERE id = ?", (state, user_id))


def set_last_error(
    conn: sqlite3.Connection, user_id: str, error: str | None
) -> None:
    """Range l'erreur ET son horodatage : `last_error` seul ne dit pas
    laquelle est la plus récente quand plusieurs comptes échouent."""
    with conn:
        conn.execute(
            "UPDATE users SET last_error = ?, last_error_at = ? WHERE id = ?",
            (error, now_iso() if error else None, user_id),
        )


def mark_synced(conn: sqlite3.Connection, user_id: str) -> None:
    with conn:
        conn.execute(
            "UPDATE users SET last_sync_at = ?, last_error = NULL,"
            " last_error_at = NULL WHERE id = ?",
            (now_iso(), user_id),
        )


def set_class_name(conn: sqlite3.Connection, user_id: str, class_name: str | None) -> None:
    with conn:
        conn.execute("UPDATE users SET class_name = ? WHERE id = ?", (class_name, user_id))


# ─────────────────────────── priorités ───────────────────────────


def set_priorities(conn: sqlite3.Connection, user_id: str, subjects: list[str]) -> None:
    with conn:
        conn.execute("DELETE FROM priorities WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO priorities (user_id, subject, rank) VALUES (?,?,?)",
            [(user_id, s, i) for i, s in enumerate(subjects)],
        )


def get_priorities(conn: sqlite3.Connection, user_id: str) -> dict[str, int]:
    return {
        row["subject"]: row["rank"]
        for row in conn.execute(
            "SELECT subject, rank FROM priorities WHERE user_id = ?", (user_id,)
        )
    }


# ─────────────────────────── items & cache ───────────────────────────


def replace_items(conn: sqlite3.Connection, user_id: str, items: list) -> int:
    """Réécrit les items d'un élève ; les `done_local` survivent.

    Les items des sources lues à chaque passe (travail à faire, informations,
    messagerie) sont *purgés* : la clé d'un item est un hash de son contenu,
    donc un devoir corrigé par le prof ou disparu de la fenêtre produirait
    sinon une seconde ligne que l'élève ne verrait jamais se nettoyer.

    Les items `cahier_de_textes` sont épargnés : la phase cours ne lit qu'un
    lot de leçons et fusionne avec l'existant, purger sur `fetched_at` y
    effacerait des cours simplement non relus cette fois-ci.
    """
    import json

    stamp = now_iso()
    with conn:
        conn.executemany(
            "INSERT INTO items (user_id, key, source, subject, title, body, due_date,"
            " kind, attachments, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(user_id, key) DO UPDATE SET source=excluded.source,"
            " subject=excluded.subject, title=excluded.title, body=excluded.body,"
            " due_date=excluded.due_date, kind=excluded.kind,"
            " attachments=excluded.attachments, fetched_at=excluded.fetched_at",
            [
                (
                    user_id,
                    it.key,
                    it.source,
                    it.subject,
                    it.title,
                    it.body,
                    it.due_date.isoformat() if it.due_date else None,
                    it.kind,
                    json.dumps(it.attachments, ensure_ascii=False),
                    stamp,
                )
                for it in items
            ],
        )
        # Purge par CLÉ, pas par horodatage : `now_iso()` est à la seconde,
        # donc deux passes rapprochées partagent le même stamp et le
        # `fetched_at < stamp` ne supprimait jamais rien (doublons à vie).
        keys = [it.key for it in items]
        if keys:
            marks = ",".join("?" * len(keys))
            conn.execute(
                "DELETE FROM items WHERE user_id = ? AND source <> 'cahier_de_textes'"
                f" AND key NOT IN ({marks})",
                (user_id, *keys),
            )
        else:
            conn.execute(
                "DELETE FROM items WHERE user_id = ? AND source <> 'cahier_de_textes'",
                (user_id,),
            )
    return len(items)


def get_items(conn: sqlite3.Connection, user_id: str) -> list:
    from .pronote.normalize import Item

    out = []
    for row in conn.execute("SELECT * FROM items WHERE user_id = ?", (user_id,)):
        import json
        from datetime import date as _date

        out.append(
            Item(
                key=row["key"],
                source=row["source"],
                subject=row["subject"],
                title=row["title"],
                body=row["body"],
                due_date=_date.fromisoformat(row["due_date"]) if row["due_date"] else None,
                kind=row["kind"],
                attachments=json.loads(row["attachments"] or "[]"),
            )
        )
    return out


def done_keys(conn: sqlite3.Connection, user_id: str) -> set[str]:
    return {
        row["key"]
        for row in conn.execute(
            "SELECT key FROM done_local WHERE user_id = ?", (user_id,)
        )
    }


def mark_done(conn: sqlite3.Connection, user_id: str, key: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO done_local (user_id, key) VALUES (?,?)", (user_id, key)
        )


def get_summaries(conn: sqlite3.Connection, keys: list[str], model: str) -> dict[str, dict]:
    if not keys:
        return {}
    out: dict[str, dict] = {}
    chunk = 400
    for i in range(0, len(keys), chunk):
        part = keys[i : i + chunk]
        marks = ",".join("?" * len(part))
        for row in conn.execute(
            f"SELECT key, summary, action, effort FROM summaries"
            f" WHERE model = ? AND key IN ({marks})",
            [model, *part],
        ):
            out[row["key"]] = {
                "summary": row["summary"],
                "action": row["action"],
                "effort": row["effort"],
            }
    return out


def put_summaries(conn: sqlite3.Connection, model: str, rows: dict[str, dict]) -> None:
    stamp = now_iso()
    with conn:
        conn.executemany(
            "INSERT INTO summaries (key, model, summary, action, effort, created_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(key, model) DO UPDATE SET"
            " summary=excluded.summary, action=excluded.action, effort=excluded.effort,"
            " created_at=excluded.created_at",
            [
                (k, model, v["summary"], v["action"], v["effort"], stamp)
                for k, v in rows.items()
            ],
        )


def purge_old_summaries(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("DELETE FROM summaries WHERE created_at < date('now','-7 days')")


# ─────────────────────────── listes numérotées ───────────────────────────


def write_list_ref(conn: sqlite3.Connection, user_id: str, keys: list[str]) -> None:
    stamp = now_iso()
    with conn:
        conn.execute("DELETE FROM list_ref WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO list_ref (user_id, idx, key, created_at) VALUES (?,?,?,?)",
            [(user_id, i + 1, k, stamp) for i, k in enumerate(keys)],
        )


def resolve_list_ref(conn: sqlite3.Connection, user_id: str, idx: int) -> str | None:
    row = conn.execute(
        "SELECT key FROM list_ref WHERE user_id = ? AND idx = ?", (user_id, idx)
    ).fetchone()
    return row["key"] if row else None


# ─────────────────────────── leçons lues ───────────────────────────


def lesson_status(conn: sqlite3.Connection, user_id: str) -> dict[str, str]:
    return {
        row["slot"]: row["status"]
        for row in conn.execute(
            "SELECT slot, status FROM lesson_seen WHERE user_id = ?", (user_id,)
        )
    }


def set_lesson_status(
    conn: sqlite3.Connection, user_id: str, slot: str, status: str
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO lesson_seen (user_id, slot, status, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(user_id, slot) DO UPDATE SET status=excluded.status,"
            " updated_at=excluded.updated_at",
            (user_id, slot, status, now_iso()),
        )


# ─────────────────────────── quota LLM ───────────────────────────


def bump_llm_count(
    conn: sqlite3.Connection, user_id: str, day: str, limit: int
) -> bool:
    """Renvoie True si la question est autorisée (et la compte).

    Un `day` strictement antérieur à `llm_day` stocké est refusé sans rien
    écrire : un appel attardé au passage de minuit ne doit pas faire reculer la
    date du compteur, ce qui rouvrirait le quota du jour courant.
    """
    with _write_tx(conn):
        row = conn.execute(
            "SELECT llm_day, llm_count FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return False
        stored_day = row["llm_day"]
        if stored_day is not None and day < stored_day:
            return False
        count = row["llm_count"] if stored_day == day else 0
        if count >= limit:
            return False
        conn.execute(
            "UPDATE users SET llm_day = ?, llm_count = ? WHERE id = ?",
            (day, count + 1, user_id),
        )
    return True


# ─────────────────────── sessions web persistantes ───────────────────────
#
# Le jeton est stocké **haché** : une fuite de la base ne donne aucune session
# utilisable. Il survit aux redémarrages du serveur, ce qui évite à l'élève de
# refaire un QR à chaque déploiement.


def create_web_session(conn: sqlite3.Connection, token_hash: str, user_id: str, expires_at: int) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO web_sessions (token_hash, user_id, created_at, expires_at)"
            " VALUES (?,?,?,?)",
            (token_hash, user_id, now_iso(), expires_at),
        )


def get_web_session(conn: sqlite3.Connection, token_hash: str, now: int) -> str | None:
    row = conn.execute(
        "SELECT user_id FROM web_sessions WHERE token_hash = ? AND expires_at > ?",
        (token_hash, now),
    ).fetchone()
    return row["user_id"] if row is not None else None


def delete_web_session(conn: sqlite3.Connection, token_hash: str) -> None:
    with conn:
        conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))

def delete_user_web_sessions(conn: sqlite3.Connection, user_id: str) -> None:
    """Révoque toutes les sessions web d'un compte (rotation après connexion)."""
    with conn:
        conn.execute("DELETE FROM web_sessions WHERE user_id = ?", (user_id,))


def purge_web_sessions(conn: sqlite3.Connection, now: int) -> None:
    with conn:
        conn.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (now,))
