"""Sauvegardes : fichier créé, contenu lisible, rotation, permissions.

La base de production contient les jetons PRONOTE chiffrés : une sauvegarde
doit être complète, autonome (sans `-wal`), et lisible seulement par le
service.
"""

from __future__ import annotations

import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import backup, config

T0 = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)


def _stamp(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_sauvegarde_cree_un_fichier_complet(conn, make_user, tmp_path):
    uid = make_user(chat_id=4242, username="eleve")
    dest = tmp_path / "backups"

    made = backup.backup_once(tmp_path / "app.db", dest, 5, now=T0)

    assert made.is_file()
    assert made.parent == dest
    assert made.name.startswith("app-") and made.suffix == ".db"
    # Autonome : pas de -wal à côté, pas de fichier temporaire qui traîne.
    assert not Path(f"{made}-wal").exists()
    assert list(dest.glob(".app-*")) == []

    copie = sqlite3.connect(made)
    try:
        assert copie.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copie.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        row = copie.execute("SELECT id, chat_id FROM users").fetchone()
        assert row == (uid, 4242)
        assert copie.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    finally:
        copie.close()


def test_base_vide_reste_lisible(conn, tmp_path):
    made = backup.backup_once(tmp_path / "app.db", tmp_path / "backups", 5, now=T0)

    copie = sqlite3.connect(made)
    try:
        tables = {
            ligne[0]
            for ligne in copie.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        copie.close()
    assert "users" in tables


def test_rotation_garde_les_plus_recentes(conn, tmp_path):
    dest = tmp_path / "backups"
    made = [
        backup.backup_once(tmp_path / "app.db", dest, keep=3, now=_stamp(i * 60))
        for i in range(5)
    ]

    restantes = sorted(dest.glob("app-*.db"))
    assert restantes == made[-3:]
    assert made[0].exists() is False
    assert made[1].exists() is False


def test_permissions_du_fichier_et_du_dossier(conn, tmp_path):
    dest = tmp_path / "backups"

    made = backup.backup_once(tmp_path / "app.db", dest, 5, now=T0)

    assert stat.S_IMODE(made.stat().st_mode) == 0o600
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700


def test_passage_periodique_ne_double_pas_une_sauvegarde_fraiche(conn, tmp_path):
    source = tmp_path / "app.db"
    dest = tmp_path / "backups"

    premier = backup.backup_tick(source, dest, 5)
    assert premier is not None
    assert backup.backup_tick(source, dest, 5, min_age=23 * 3600) is None

    second = backup.backup_tick(source, dest, 5)
    assert second is not None
    assert second != premier
    assert len(list(dest.glob("app-*.db"))) == 2


def test_demarrage_ne_consomme_pas_la_rotation(conn, tmp_path):
    source = tmp_path / "app.db"
    dest = tmp_path / "backups"

    premier = backup.backup_at_startup(source, dest, 5)
    assert premier is not None
    # Redémarrage immédiat (boucle de restart) : on ne réécrit pas le même état.
    assert backup.backup_at_startup(source, dest, 5) is None
    assert list(dest.glob("app-*.db")) == [premier]


@pytest.mark.parametrize(
    ("valeur", "attendu"),
    [
        (None, 5),
        ("2", 2),
        ("0", 1),
        ("-3", 1),
        ("abc", 5),
        ("9999", config.MAX_BACKUPS),
    ],
)
def test_backup_keep_depuis_l_environnement(tmp_path, valeur, attendu):
    env = {"DATA_DIR": str(tmp_path / "data")}
    if valeur is not None:
        env["BACKUP_KEEP"] = valeur

    cfg = config.from_env(env, require_token=False)

    assert cfg.backup_keep == attendu
    assert cfg.backups_dir == tmp_path / "data" / "backups"
