"""Verrou mono-instance : un seul processus par `DATA_DIR`.

Deux processus sur le même `DATA_DIR` se marchent dessus sur SQLite. Le
`flock` doit donc refuser le second, et ne jamais rester coincé après la mort
du premier.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app import config, instance, main as main_module

ROOT = Path(__file__).resolve().parents[1]


def _lock(tmp_path: Path) -> instance.InstanceLock:
    return instance.InstanceLock(tmp_path / "locks" / "instance.lock")


def _cfg(tmp_path: Path) -> config.Config:
    return config.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "123:fake",
            "APP_SECRET": Fernet.generate_key().decode(),
            "DATA_DIR": str(tmp_path),
        }
    )


def test_second_acquirer_fails(tmp_path):
    first, second = _lock(tmp_path), _lock(tmp_path)

    first.acquire()
    try:
        assert (tmp_path / "locks" / "instance.lock").is_file()
        with pytest.raises(instance.AlreadyRunning):
            second.acquire()
    finally:
        first.release()

    # Le perdant n'a laissé aucun descripteur : il peut réessayer.
    second.acquire()
    second.release()


def test_lock_released_after_release(tmp_path):
    lock = _lock(tmp_path)
    lock.acquire()
    lock.release()

    again = _lock(tmp_path)
    again.acquire()
    again.release()


def test_lock_released_after_process_exit(tmp_path):
    path = tmp_path / "locks" / "instance.lock"
    script = (
        "import sys\n"
        "from app.instance import InstanceLock\n"
        "InstanceLock(sys.argv[1]).acquire()\n"
        "print('locked', flush=True)\n"
        "sys.stdin.readline()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(instance.AlreadyRunning):
            _lock(tmp_path).acquire()
    finally:
        assert child.stdin is not None
        child.stdin.write("\n")
        child.stdin.flush()
        assert child.wait(timeout=30) == 0

    # Processus mort : le noyau a rendu le verrou.
    after = _lock(tmp_path)
    after.acquire()
    after.release()


def test_main_exits_2_before_opening_db(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    cfg.ensure_dirs()
    holder = instance.InstanceLock(cfg.locks_dir / "instance.lock")
    holder.acquire()
    monkeypatch.setattr(main_module.config, "from_env", lambda *a, **k: cfg)
    monkeypatch.setattr(sys, "argv", ["app.main"])

    def _no_db(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("le second processus ne doit pas ouvrir la base")

    monkeypatch.setattr(main_module.db, "connect", _no_db)

    try:
        with pytest.raises(SystemExit) as excinfo:
            main_module.main()
    finally:
        holder.release()

    assert excinfo.value.code == 2
    assert str(cfg.data_dir) in capsys.readouterr().err
    assert not cfg.db_path.exists()
