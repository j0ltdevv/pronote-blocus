"""Fixtures E2E : une instance isolée du serveur (DB temp + LLM optionnel) et
Chrome système via pytest-playwright (canal "chrome", aucun téléchargement).

Le serveur est monté en sous-processus sur un port libre, avec une DB de
démonstration seedée (1 utilisateur, 5 devoirs, 1 session web valide).
"""
from __future__ import annotations

import hashlib
import os
import socket
import sqlite3
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
import urllib.request

REPO = Path(__file__).resolve().parents[2]
SERVE = REPO / "scripts" / "serve.sh"
LLM_ENV_FILE = Path.home() / ".config" / "pronote-blocus.env"
TOKEN = "e2e-token"
USER_ID = "u1"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _llm_env() -> dict[str, str]:
    env = {}
    if LLM_ENV_FILE.exists():
        for line in LLM_ENV_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _seed(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    now = "2026-10-20T12:00:00"
    overdue = (date.today() - timedelta(days=1)).isoformat()
    upcoming = (date.today() + timedelta(days=2)).isoformat()
    cur.execute(
        "INSERT INTO users (id, chat_id, pronote_url, username, app_uuid, token_enc, created_at, last_sync_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (USER_ID, 9990001, "https://demo.pronote.fr", "demo", "uuid-demo",
         b"\x00", now, now),
    )
    digest = hashlib.sha256(TOKEN.encode()).hexdigest()
    cur.execute(
        "INSERT INTO web_sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
        (digest, USER_ID, now, int(time.time()) + 7200),
    )
    items = [
        ("k1", "Maths", "Exercices sur les suites", "controle", overdue),
        ("k2", "Physique", "Compte rendu TP électricité", "devoir", upcoming),
        ("k3", "Anglais", "Dissertation AI and society", "devoir", None),
        ("k4", "Histoire", "Frise chronologique 1848", "devoir", upcoming),
        ("k5", "SVT", "Schéma de la cellule", "devoir", None),
    ]
    cur.executemany(
        "INSERT INTO items (user_id, key, source, subject, title, body, due_date, kind, fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [(USER_ID, k, "pronote", s, t, "", d, kind, now) for k, s, t, kind, d in items],
    )
    cur.execute("INSERT INTO done_local (user_id, key) VALUES (?,?)", (USER_ID, "k4"))
    con.commit()
    con.close()


@pytest.fixture(scope="session")
def pb_server(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("pb-e2e")
    port = _free_port()
    env = os.environ.copy()
    env.update(_llm_env())
    env.update({"DATA_DIR": str(data_dir), "PORT": str(port), "HOST": "127.0.0.1"})
    proc = subprocess.Popen([str(SERVE)], cwd=str(REPO), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/status.json", timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.4)
    else:
        proc.terminate()
        raise RuntimeError("serveur E2E pas monté")
    _seed(data_dir / "app.db")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def browser_type_launch_args():
    # Utilise le Chrome système : pas de téléchargement Chromium (~100 Mo).
    return {"channel": "chrome"}


@pytest.fixture
def has_llm() -> bool:
    return bool(_llm_env().get("LLM_BASE_URL"))
