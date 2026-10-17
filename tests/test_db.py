"""Rotation du jeton, listes numérotées, quota LLM, suppression en cascade.

`save_credentials` est le point de rupture en production : si la nouvelle
valeur n'est pas écrite, ou si elle l'est en laissant une seconde ligne, le
compte devient inaccessible sans nouveau QR.
"""

from __future__ import annotations

import pytest

from app import crypto, db
from app.pronote.normalize import Item


def test_save_credentials_rotates_in_place(conn, secret, make_user):
    uid = make_user(password="jeton-1", client_identifier="ident-1")

    db.save_credentials(
        conn, uid, {"password": "jeton-2", "client_identifier": "ident-2"}, secret
    )

    rows = conn.execute("SELECT * FROM users").fetchall()
    assert len(rows) == 1, "une rotation ne doit jamais insérer une seconde ligne"
    row = rows[0]
    assert row["id"] == uid
    assert crypto.decrypt(row["token_enc"], secret) == "jeton-2"
    assert row["client_identifier"] == "ident-2"
    assert db.credentials_for(conn, uid, secret)["password"] == "jeton-2"


def test_save_credentials_keeps_identity_fields(conn, secret):
    uid = db.create_user(
        conn,
        chat_id=4242,
        pronote_url="https://etab.index-education.net/pronote/eleve.html",
        username="eleve",
        creds={"password": "v1", "client_identifier": "id-a", "uuid": "uuid-fixe"},
        secret=secret,
        class_name="1G3",
    )

    db.save_credentials(conn, uid, {"password": "v2"}, secret)

    row = db.get_user(conn, uid)
    assert row["chat_id"] == 4242
    assert row["username"] == "eleve"
    assert row["app_uuid"] == "uuid-fixe", "l'app_uuid ne change jamais"
    assert row["class_name"] == "1G3"
    assert row["client_identifier"] == "id-a", "absent du creds = inchangé"
    assert crypto.decrypt(row["token_enc"], secret) == "v2"


def test_save_credentials_stores_account_pin_encrypted(conn, secret, make_user):
    uid = make_user()

    db.save_credentials(conn, uid, {"password": "jeton-2", "account_pin": "9876"}, secret)

    row = db.get_user(conn, uid)
    assert row["account_pin_enc"], "account_pin fourni doit être persisté"
    assert crypto.decrypt(row["account_pin_enc"], secret) == "9876"
    assert db.credentials_for(conn, uid, secret)["account_pin"] == "9876"


def test_save_credentials_without_password_changes_nothing(conn, secret, make_user):
    uid = make_user(password="ancien-jeton", client_identifier="ident-1")
    before = db.get_user(conn, uid)
    before_token = bytes(before["token_enc"])

    with pytest.raises(ValueError):
        db.save_credentials(
            conn, uid, {"client_identifier": "ident-2", "account_pin": "1234"}, secret
        )

    after = db.get_user(conn, uid)
    assert bytes(after["token_enc"]) == before_token
    assert after["client_identifier"] == "ident-1"
    assert after["account_pin_enc"] is None
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_save_credentials_unknown_user_raises(conn, secret, make_user):
    make_user()
    with pytest.raises(KeyError):
        db.save_credentials(conn, "pas-un-id", {"password": "jeton-2"}, secret)
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_decrypt_with_changed_secret_is_loud(conn, secret, other_secret, make_user):
    uid = make_user(password="jeton-secret")
    blob = bytes(db.get_user(conn, uid)["token_enc"])

    with pytest.raises(crypto.SecretError):
        crypto.decrypt(blob, other_secret)
    with pytest.raises(crypto.SecretError):
        db.credentials_for(conn, uid, other_secret)


def test_list_ref_resolves_and_replaces(conn, make_user):
    uid = make_user()
    db.write_list_ref(conn, uid, ["cle-a", "cle-b", "cle-c"])

    assert db.resolve_list_ref(conn, uid, 1) == "cle-a"
    assert db.resolve_list_ref(conn, uid, 3) == "cle-c"
    assert db.resolve_list_ref(conn, uid, 4) is None

    db.write_list_ref(conn, uid, ["cle-x", "cle-y"])

    assert db.resolve_list_ref(conn, uid, 1) == "cle-x"
    assert db.resolve_list_ref(conn, uid, 2) == "cle-y"
    assert db.resolve_list_ref(conn, uid, 3) is None, "aucun résidu de l'ancienne liste"
    assert conn.execute("SELECT COUNT(*) FROM list_ref").fetchone()[0] == 2


def test_bump_llm_count_caps_then_resets_with_new_day(conn, make_user):
    uid = make_user()

    assert [db.bump_llm_count(conn, uid, "2026-10-17", 10) for _ in range(10)] == [True] * 10
    assert db.bump_llm_count(conn, uid, "2026-10-17", 10) is False, "11e question refusée"

    assert db.bump_llm_count(conn, uid, "2026-10-18", 10) is True
    row = db.get_user(conn, uid)
    assert row["llm_day"] == "2026-10-18"
    assert row["llm_count"] == 1


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


def test_delete_user_removes_every_trace(conn, secret, make_user):
    uid = make_user()
    survivor = make_user()
    db.set_priorities(conn, uid, ["Maths", "SVT"])
    db.set_priorities(conn, survivor, ["Histoire"])
    db.replace_items(conn, uid, [_item("k1"), _item("k2")])
    db.replace_items(conn, survivor, [_item("k3")])
    db.mark_done(conn, uid, "k1")
    db.mark_done(conn, survivor, "k3")
    db.write_list_ref(conn, uid, ["k1", "k2"])
    db.write_list_ref(conn, survivor, ["k3"])

    db.delete_user(conn, uid)

    assert db.get_user(conn, uid) is None
    for table in ("priorities", "items", "list_ref", "done_local"):
        remaining = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (uid,)
        ).fetchone()[0]
        assert remaining == 0, table
        kept = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (survivor,)
        ).fetchone()[0]
        assert kept > 0, f"{table} : les données d'un autre élève sont intactes"
