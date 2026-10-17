"""Stress SQLite : écritures concurrentes sans réseau.

Le seul verrou d'écriture est `db.LockedConnection`. Ces tests vérifient deux
propriétés que la production ne peut pas se permettre de perdre :

- aucune exception ne sort de `save_credentials` / `replace_items` quand
  plusieurs threads de synchro écrivent en même temps ;
- le couple (jeton chiffré, `client_identifier`) reste celui d'UNE seule
  écriture : jamais un jeton neuf avec l'identifiant d'appareil précédent.

Durée volontairement bornée à 3 s (contrainte de suite rapide).
"""

from __future__ import annotations

import threading
import time

from app import db
from app.pronote.normalize import Item

#: Six threads d'écriture, comme `MAX_CONCURRENT_SYNCS` en production.
WORKERS = 6
#: Assez long pour croiser plusieurs rotations de jeton, assez court pour la CI.
DURATION = 3.0
KEYS = [f"item-{i}" for i in range(8)]


def _items(tag: str) -> list[Item]:
    return [
        Item(
            key=key,
            source="devoir",
            subject="Mathématiques",
            title=tag,
            body=tag,
            due_date=None,
            kind="devoir",
        )
        for key in KEYS
    ]


def test_ecritures_concurrentes_jeton_et_identifiant_atomiques(conn, secret, make_user):
    user_id = make_user(chat_id=1)
    db.save_credentials(
        conn,
        user_id,
        {"password": "tok-init", "client_identifier": "ident-init"},
        secret,
    )
    start = threading.Barrier(WORKERS + 1)
    stop = threading.Event()
    errors: list[BaseException] = []
    iterations = [0] * WORKERS

    def worker(thread_id: int) -> None:
        seq = 0
        try:
            start.wait()
            while not stop.is_set():
                seq += 1
                tag = f"{thread_id}:{seq}"
                db.save_credentials(
                    conn,
                    user_id,
                    {
                        "password": f"tok-{tag}",
                        "client_identifier": f"ident-{tag}",
                    },
                    secret,
                )
                db.replace_items(conn, user_id, _items(tag))
            iterations[thread_id] = seq
        except BaseException as exc:  # noqa: BLE001 - on veut TOUT remonter
            errors.append(exc)
            stop.set()

    threads = [
        threading.Thread(target=worker, args=(thread_id,), daemon=True)
        for thread_id in range(WORKERS)
    ]
    for thread in threads:
        thread.start()

    try:
        start.wait()
        deadline = time.monotonic() + DURATION
        snapshots = 0
        while time.monotonic() < deadline and not stop.is_set():
            creds = db.credentials_for(conn, user_id, secret)
            token = str(creds["password"]).removeprefix("tok-")
            ident = str(creds["client_identifier"]).removeprefix("ident-")
            assert token == ident, (
                "couple incohérent : jeton "
                f"{creds['password']!r} avec client_identifier "
                f"{creds['client_identifier']!r}"
            )

            titles = {
                row["title"]
                for row in conn.execute(
                    "SELECT title FROM items WHERE user_id = ?", (user_id,)
                )
            }
            # `replace_items` écrit les 8 lignes dans UNE transaction : un
            # instantané ne doit jamais mélanger deux écrivains.
            assert len(titles) <= 1, f"items mélangés entre écrivains : {titles}"
            if titles:
                assert len(db.get_items(conn, user_id)) == len(KEYS)
            snapshots += 1
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive(), "un thread d'écriture est resté bloqué"

    assert not errors, f"exceptions concurrentes : {errors!r}"
    assert snapshots > 0
    assert sum(iterations) > 0

    creds = db.credentials_for(conn, user_id, secret)
    assert creds["password"].removeprefix("tok-") == creds[
        "client_identifier"
    ].removeprefix("ident-")
    titles = {
        row["title"]
        for row in conn.execute(
            "SELECT title FROM items WHERE user_id = ?", (user_id,)
        )
    }
    assert len(titles) == 1, f"état final incohérent : {titles}"
    assert len(db.get_items(conn, user_id)) == len(KEYS)
