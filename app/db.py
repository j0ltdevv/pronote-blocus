"""SQLite : schéma, ouverture, et l'UNIQUE point d'écriture des identifiants.

`save_credentials` est le point de rupture en production : chaque login
réussi invalide le jeton précédent côté PRONOTE. Si la nouvelle valeur n'est
pas écrite dans la même transaction que la réponse de login, le compte devient
inaccessible et il faut un nouveau QR.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

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
CREATE INDEX IF NOT EXISTS items_user_due ON items(user_id, due_date);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path), timeout=30, check_same_thread=False, factory=LockedConnection
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(SCHEMA)


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
        "account_pin_enc = ?",
        "last_error = NULL",
        "sync_state = 'idle'",
    ]
    values: list[object] = [
        crypto.encrypt(creds["password"], secret),
        creds.get("uuid") or crypto.new_app_uuid(),
        crypto.encrypt(account_pin, secret) if account_pin else None,
    ]
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
    with conn:
        conn.execute("UPDATE users SET last_error = ? WHERE id = ?", (error, user_id))


def mark_synced(conn: sqlite3.Connection, user_id: str) -> None:
    with conn:
        conn.execute(
            "UPDATE users SET last_sync_at = ?, last_error = NULL WHERE id = ?",
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
    """Réécrit les items d'un élève ; les `done_local` survivent."""
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
            [(user_id, i + 1, k, stamp) for i, k in enumerate(keys)][:60],
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
    """Renvoie True si la question est autorisée (et la compte)."""
    row = conn.execute(
        "SELECT llm_day, llm_count FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if row is None:
        return False
    count = row["llm_count"] if row["llm_day"] == day else 0
    if count >= limit:
        return False
    with conn:
        conn.execute(
            "UPDATE users SET llm_day = ?, llm_count = ? WHERE id = ?",
            (day, count + 1, user_id),
        )
    return True
