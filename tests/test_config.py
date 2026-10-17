"""Validation de `from_env` : les mauvais secrets et dates cassent au démarrage,
pas une fois le bot en marche.

1. LLM_MESSAGES=False → cfg.llm_messages False
2. APP_SECRET non-Fernet (+ require_token=True) → RuntimeError
3. WINDOW_START invalide → RuntimeError
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app import config


# ── 1. LLM_MESSAGES ─────────────────────────────────────────────────────────


def test_llm_messages_false_disables_messages():
    env = {
        "TELEGRAM_BOT_TOKEN": "x:y",
        "APP_SECRET": Fernet.generate_key().decode(),
        "LLM_MESSAGES": "false",
    }
    cfg = config.from_env(env)
    assert cfg.llm_messages is False


@pytest.mark.parametrize("val", ["0", "no", "FALSE"])
def test_llm_messages_variants_disable_messages(val):
    env = {
        "TELEGRAM_BOT_TOKEN": "x:y",
        "APP_SECRET": Fernet.generate_key().decode(),
        "LLM_MESSAGES": val,
    }
    cfg = config.from_env(env)
    assert cfg.llm_messages is False


def test_llm_messages_true_when_unset():
    env = {
        "TELEGRAM_BOT_TOKEN": "x:y",
        "APP_SECRET": Fernet.generate_key().decode(),
    }
    cfg = config.from_env(env)
    assert cfg.llm_messages is True


# ── 2. APP_SECRET non-Fernet ────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["pas-une-cle", "totally wrong", "a" * 44, "not_base64!!!"])
def test_app_secret_non_fernet_raises(bad):
    env = {
        "TELEGRAM_BOT_TOKEN": "x:y",
        "APP_SECRET": bad,
    }
    with pytest.raises(RuntimeError, match="APP_SECRET invalide"):
        config.from_env(env, require_token=True)


def test_app_secret_non_fernet_with_require_token_false_does_not_raise():
    env = {
        "TELEGRAM_BOT_TOKEN": "x:y",
        "APP_SECRET": "pas-une-cle",
    }
    cfg = config.from_env(env, require_token=False)
    assert cfg.app_secret == "pas-une-cle"


# ── 3. WINDOW_START invalide ────────────────────────────────────────────────


@pytest.mark.parametrize("bad_date", ["2026-13-01", "not-a-date", "01/10/2026", "2026/10/01"])
def test_window_start_invalide_raises(bad_date):
    env = {
        "TELEGRAM_BOT_TOKEN": "x:y",
        "APP_SECRET": Fernet.generate_key().decode(),
        "WINDOW_START": bad_date,
    }
    with pytest.raises(RuntimeError, match="WINDOW_START invalide"):
        config.from_env(env)
