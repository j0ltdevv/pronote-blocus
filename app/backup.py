"""Sauvegardes cohérentes de la base, à chaud, via l'API `sqlite3.backup`.

`cp app.db` pendant une écriture donne un fichier potentiellement inutilisable :
pages copiées à moitié, `-wal` oublié. L'API de sauvegarde lit un instantané
cohérent page par page, même quand des écritures concurrentes sont en cours.
Le fichier produit est autonome (journal classique), lisible par n'importe
quel `sqlite3`, et créé en 0600 : il contient les jetons PRONOTE chiffrés.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

KEEP_DEFAULT = 5
INTERVAL = 24 * 60 * 60
#: Deux processus (bot et web) partagent `DATA_DIR` : une sauvegarde plus
#: récente que l'intervalle moins cette marge n'est pas doublée.
FRESH_MARGIN = 60 * 60
#: Au démarrage, on saute si une sauvegarde a moins d'une heure : une boucle
#: de redémarrage ne doit pas consommer les cinq places de la rotation.
STARTUP_MIN_AGE = 60 * 60
PREFIX = "app-"
SUFFIX = ".db"
LOCK_NAME = ".backup.lock"


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # `mode` ne s'applique qu'à la création : un dossier existant garde ses
    # permissions, volontairement, pour ne pas surprendre l'administrateur.


def _name(now: datetime) -> str:
    """Nom trié chronologiquement, en UTC (pas de doublon au changement d'heure)."""
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return f"{PREFIX}{stamp}Z{SUFFIX}"


def existing(dest_dir: str | Path) -> list[Path]:
    """Sauvegardes présentes, de la plus ancienne à la plus récente."""
    return sorted(Path(dest_dir).glob(f"{PREFIX}*{SUFFIX}"))


def latest(dest_dir: str | Path) -> Path | None:
    files = existing(dest_dir)
    return files[-1] if files else None


def is_fresh(dest_dir: str | Path, max_age: float, *, now: datetime | None = None) -> bool:
    newest = latest(dest_dir)
    if newest is None:
        return False
    moment = (now or datetime.now(timezone.utc)).timestamp()
    return moment - newest.stat().st_mtime < max_age


def prune(dest_dir: str | Path, keep: int = KEEP_DEFAULT) -> list[Path]:
    """Ne garde que les `keep` sauvegardes les plus récentes."""
    keep = max(1, int(keep))
    removed: list[Path] = []
    for old in existing(dest_dir)[:-keep]:
        try:
            os.remove(old)
            removed.append(old)
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("sauvegarde non supprimée : %s", old)
    return removed


def backup_once(
    db_path: str | Path,
    dest_dir: str | Path,
    keep: int = KEEP_DEFAULT,
    *,
    now: datetime | None = None,
) -> Path:
    """Écrit une sauvegarde complète, puis applique la rotation.

    Le fichier est écrit sous un nom temporaire, passé en 0600, puis renommé :
    une rotation ne voit jamais une sauvegarde partielle.
    """
    source_path = Path(db_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"base introuvable : {source_path}")
    dest_dir = Path(dest_dir)
    _ensure_dir(dest_dir)
    target = dest_dir / _name(now or datetime.now(timezone.utc))
    if target.exists():
        # Deux sauvegardes dans la même microseconde : on ne l'écrase pas.
        target = dest_dir / f"{target.stem}-{os.getpid()}{SUFFIX}"
    tmp = dest_dir / f".{target.name}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        source = sqlite3.connect(str(source_path))
        try:
            dest = sqlite3.connect(str(tmp))
            try:
                source.backup(dest)
                # La base source vit en WAL : journal classique pour que la
                # sauvegarde tienne dans CE fichier, sans -wal à côté.
                dest.execute("PRAGMA journal_mode=DELETE")
            finally:
                dest.close()
        finally:
            source.close()
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        if tmp.exists():
            os.remove(tmp)
        raise
    prune(dest_dir, keep)
    return target


def backup_tick(
    db_path: str | Path,
    dest_dir: str | Path,
    keep: int = KEEP_DEFAULT,
    *,
    min_age: float = 0,
    now: datetime | None = None,
) -> Path | None:
    """Une sauvegarde, sauf si une autre instance la fait ou si elle est fraîche.

    Le `flock` n'est pas bloquant : le second processus repart aussitôt plutôt
    que d'attendre un voisin qui pourrait être bloqué.
    """
    dest_dir = Path(dest_dir)
    _ensure_dir(dest_dir)
    fd = os.open(str(dest_dir / LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.debug("sauvegarde déjà en cours ailleurs : passage ignoré")
            return None
        if min_age and is_fresh(dest_dir, min_age, now=now):
            return None
        return backup_once(db_path, dest_dir, keep, now=now)
    finally:
        os.close(fd)


def backup_at_startup(
    db_path: str | Path, dest_dir: str | Path, keep: int = KEEP_DEFAULT
) -> Path | None:
    """Point de démarrage : une sauvegarde si la plus récente a plus d'une heure."""
    try:
        made = backup_tick(db_path, dest_dir, keep, min_age=STARTUP_MIN_AGE)
    except Exception:  # noqa: BLE001 - une sauvegarde ratée ne bloque pas le démarrage
        log.exception("sauvegarde de démarrage impossible")
        return None
    if made:
        log.info("sauvegarde de la base : %s", made)
    return made


async def backup_loop(
    db_path: str | Path,
    dest_dir: str | Path,
    keep: int = KEEP_DEFAULT,
    interval: float = INTERVAL,
) -> None:
    """Une sauvegarde par `interval` ; aucune exception ne tue la boucle."""
    while True:
        await asyncio.sleep(interval)
        try:
            made = await asyncio.to_thread(
                backup_tick, db_path, dest_dir, keep, min_age=interval - FRESH_MARGIN
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - la boucle doit survivre à un passage raté
            log.exception("boucle de sauvegarde : passage avorté")
            continue
        if made:
            log.info("sauvegarde de la base : %s", made)
