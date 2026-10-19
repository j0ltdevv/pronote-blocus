"""Mode diagnostic `--doctor` : config, base et schéma, sans démarrer le bot.

Chaque test échoue si le comportement correspondant régresse :

- le jeton Telegram ne doit PAS être exigé ;
- une config incomplète (APP_SECRET invalide, WINDOW_START…) doit sortir en 1 avec la cause ;
- un APP_SECRET absent est généré par le mode zero-config et sort en 0 ;
- une base absente ou un schéma incomplet doit sortir en 1 ;
- `--doctor` ne doit construire aucune Application Telegram ;
- sans `--doctor`, `main()` garde son comportement d'origine.

Hors réseau, base SQLite dans `tmp_path`.
"""

from __future__ import annotations

import sqlite3
import sys

import pytest

from app import db
from app import main as main_module


def _run(env: dict[str, str]) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = main_module.doctor(env, out=lines.append)
    return code, lines


def _env(tmp_path, secret: str, **extra: str) -> dict[str, str]:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:JETON-DE-TEST",
        "APP_SECRET": secret,
        "DATA_DIR": str(tmp_path / "data"),
    }
    env.update(extra)
    return env


def _created_db(tmp_path) -> None:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    conn = db.connect(tmp_path / "data" / "app.db")
    db.init_schema(conn)
    conn.close()


def _empty_db(tmp_path) -> None:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    sqlite3.connect(tmp_path / "data" / "app.db").close()


def _ko(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("KO")]


# ─────────────────────────── cas nominal ───────────────────────────


def test_doctor_ok_returns_zero_with_prefixed_lines(tmp_path, secret):
    _created_db(tmp_path)

    code, lines = _run(_env(tmp_path, secret))

    assert code == 0, lines
    assert lines
    for line in lines:
        assert line[:2] in ("OK", "KO"), line
        assert line[2:4] == "  ", f"préfixe non aligné : {line!r}"
    assert _ko(lines) == []
    assert any("configuration chargée" in line for line in lines)
    assert any("APP_SECRET valide" in line for line in lines)
    assert any("base ouverte" in line for line in lines)
    assert any("schéma complet" in line for line in lines)


def test_doctor_never_builds_the_telegram_application(tmp_path, secret, monkeypatch):
    _created_db(tmp_path)

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("--doctor ne doit pas démarrer le bot")

    monkeypatch.setattr(main_module, "build_application", _boom)

    code, lines = _run(_env(tmp_path, secret))

    assert code == 0, lines


# ─────────────────────── jeton Telegram facultatif ───────────────────────


def test_doctor_runs_without_telegram_token(tmp_path, secret):
    _created_db(tmp_path)
    env = _env(tmp_path, secret)
    env.pop("TELEGRAM_BOT_TOKEN")

    code, lines = _run(env)

    assert code == 0, lines
    token_lines = [line for line in lines if "TELEGRAM_BOT_TOKEN" in line]
    assert token_lines, lines
    assert token_lines[0].startswith("OK")
    assert "non requis" in token_lines[0]


def test_doctor_with_telegram_token_reports_it_present(tmp_path, secret):
    _created_db(tmp_path)

    code, lines = _run(_env(tmp_path, secret))

    assert code == 0, lines
    assert any(
        line.startswith("OK") and "TELEGRAM_BOT_TOKEN présent" in line
        for line in lines
    )


# ─────────────────────── configuration incomplète ───────────────────────


def test_doctor_auto_generates_app_secret_and_exits_zero(tmp_path):
    _created_db(tmp_path)
    env = _env(tmp_path, secret="")
    env.pop("APP_SECRET")

    code, lines = _run(env)

    # Depuis le mode web zero-config, `from_env(require_token=False)` génère
    # et persiste APP_SECRET : absent n'est plus une erreur.
    assert code == 0, lines
    assert not _ko(lines), lines
    assert any("généré" in line for line in lines), lines
    assert (tmp_path / "data" / "app_secret").is_file()


def test_doctor_invalid_app_secret_exits_1_with_cause(tmp_path):
    _created_db(tmp_path)
    env = _env(tmp_path, "", APP_SECRET="pas-une-cle")

    code, lines = _run(env)

    assert code == 1, lines
    ko = _ko(lines)
    assert len(ko) == 1, lines
    assert "APP_SECRET invalide" in ko[0]


def test_doctor_invalid_window_start_exits_1_immediately(tmp_path, secret):
    env = _env(tmp_path, secret, WINDOW_START="2026-13-01")

    code, lines = _run(env)

    assert code == 1, lines
    assert len(lines) == 1, lines
    assert lines[0].startswith("KO")
    assert "WINDOW_START invalide" in lines[0]
    assert not any("base ouverte" in line for line in lines)


# ─────────────────────────── base et schéma ───────────────────────────


def test_doctor_missing_data_dir_exits_1_with_cause(tmp_path, secret):
    env = _env(tmp_path, secret, DATA_DIR=str(tmp_path / "absent"))

    code, lines = _run(env)

    assert code == 1, lines
    ko = _ko(lines)
    assert len(ko) == 1, lines
    assert "base inaccessible" in ko[0]
    assert "répertoire" in ko[0]


def test_doctor_empty_database_reports_missing_tables(tmp_path, secret):
    _empty_db(tmp_path)

    code, lines = _run(_env(tmp_path, secret))

    assert code == 1, lines
    assert any("base ouverte" in line for line in lines)
    ko = _ko(lines)
    assert len(ko) == 1, lines
    assert "schéma incomplet" in ko[0]
    assert "users" in ko[0]


def test_doctor_does_not_create_the_schema(tmp_path, secret):
    _empty_db(tmp_path)

    code, _lines = _run(_env(tmp_path, secret))

    assert code == 1
    conn = db.connect(tmp_path / "data" / "app.db")
    try:
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert "users" not in names, "--doctor diagnostique, il ne migre pas"


# ─────────────────────── aiguillage de `main()` ───────────────────────


def test_main_dispatches_doctor_without_starting_bot(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["app.main", "--doctor"])
    monkeypatch.setattr(main_module, "doctor", lambda: 1)

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("le bot ne doit pas démarrer avec --doctor")

    monkeypatch.setattr(main_module.config, "from_env", _boom)

    with pytest.raises(SystemExit) as excinfo:
        main_module.main()

    assert excinfo.value.code == 1


def test_main_without_doctor_keeps_previous_behaviour(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["app.main"])

    def _never(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("doctor ne doit pas être appelé sans --doctor")

    monkeypatch.setattr(main_module, "doctor", _never)

    def _no_token(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("TELEGRAM_BOT_TOKEN manquant dans l'environnement")

    monkeypatch.setattr(main_module.config, "from_env", _no_token)

    with pytest.raises(SystemExit) as excinfo:
        main_module.main()

    assert str(excinfo.value.code).startswith("Configuration incomplète")
