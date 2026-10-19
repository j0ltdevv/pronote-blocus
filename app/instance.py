"""Verrou mono-instance : un seul processus par `DATA_DIR`.

SQLite encaisse plusieurs connexions, pas deux boucles de polling qui
écrivent les mêmes lignes (`database is locked`, jetons PRONOTE invalidés
dans le désordre). On prend donc un `flock` exclusif **non bloquant** sur
`DATA_DIR/locks/instance.lock` avant d'ouvrir la base : le second processus
meurt tout de suite, sans rien toucher.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    """Un autre processus tient déjà le verrou de cette instance."""


class InstanceLock:
    """`flock` exclusif sur un fichier, relâché par `release()` ou à la mort.

    Le noyau libère le verrou quand le descripteur se ferme : un `kill -9`
    ne laisse donc pas de fichier bloquant derrière lui.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        """Prend le verrou ou lève `AlreadyRunning` sans rien modifier."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise AlreadyRunning(str(self.path)) from None
        try:
            # PID lisible pour l'humain qui se demande « qui tient le verrou ».
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
