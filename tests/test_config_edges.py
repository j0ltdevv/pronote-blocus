"""Cas limites de `app.config.from_env` : valeurs vides ou blanches, entiers
hors bornes, ports invalides et chemins relatifs.

Chaque cas doit soit lever une erreur explicite (`RuntimeError` avec le nom de
la variable), soit retomber sur un défaut documenté dans le README. Les tests
de clamp enregistrent les bornes réelles : `MAX_CONCURRENT_SYNCS` reste dans
1..64 et `ADMIN_CHAT_ID` doit tenir sur 64 bits.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app import config


def _env(**overrides: str) -> dict[str, str]:
    """Environnement minimal valide, surchargé par `overrides`."""
    env = {
        "TELEGRAM_BOT_TOKEN": "x:y",
        "APP_SECRET": Fernet.generate_key().decode(),
    }
    env.update(overrides)
    return env


# ── 1. Valeurs vides ou blanches → défaut documenté ─────────────────────────


@pytest.mark.parametrize("valeur", ["", "   ", "\t\n"])
def test_data_dir_vide_ou_blanc_vaut_data(valeur):
    cfg = config.from_env(_env(DATA_DIR=valeur))
    assert cfg.data_dir == Path("data")
    assert cfg.db_path == Path("data") / "app.db"


@pytest.mark.parametrize("valeur", ["", "   "])
def test_url_pronote_vide_ou_blanche_vaut_defaut(valeur):
    cfg = config.from_env(_env(DEFAULT_PRONOTE_URL=valeur))
    assert cfg.default_pronote_url == config.DEFAULT_PRONOTE_URL


@pytest.mark.parametrize("valeur", ["", "   "])
def test_mobile_query_vide_ou_blanche_vaut_defaut(valeur):
    cfg = config.from_env(_env(PRONOTE_MOBILE_QUERY=valeur))
    assert cfg.mobile_query == config.DEFAULT_MOBILE_QUERY


@pytest.mark.parametrize("valeur", ["", "   "])
def test_password_ent_vide_ou_blanc_vaut_defaut(valeur):
    cfg = config.from_env(_env(PASSWORD_ENT=valeur))
    assert cfg.password_ent == "lyceeconnecte_aquitaine"


@pytest.mark.parametrize("valeur", ["", "   "])
def test_window_start_vide_ou_blanc_vaut_defaut(valeur):
    cfg = config.from_env(_env(WINDOW_START=valeur))
    assert cfg.window_start == "2026-10-01"


def test_base_url_blanche_desactive_le_llm():
    cfg = config.from_env(_env(LLM_BASE_URL="   ", LLM_API_KEY="k", LLM_MODEL="m"))
    assert cfg.llm_base_url == ""
    assert cfg.llm_enabled is False


def test_llm_messages_blanc_reste_desactive():
    # Caractérisation : "" vaut « non défini » (activé), mais une valeur
    # blanche est traitée comme une chaîne non reconnue → désactivé.
    assert config.from_env(_env(LLM_MESSAGES="")).llm_messages is True
    assert config.from_env(_env(LLM_MESSAGES="   ")).llm_messages is False


@pytest.mark.parametrize("valeur", ["0", "false", "FALSE", "False", "no", "NO"])
def test_allow_password_login_desactive_sans_casse(valeur):
    cfg = config.from_env(_env(ALLOW_PASSWORD_LOGIN=valeur))
    assert cfg.allow_password_login is False


@pytest.mark.parametrize("valeur", ["1", "true", "TRUE", "", "   "])
def test_allow_password_login_actif_par_defaut(valeur):
    cfg = config.from_env(_env(ALLOW_PASSWORD_LOGIN=valeur))
    assert cfg.allow_password_login is True


# ── 2. Entiers hors bornes ──────────────────────────────────────────────────


@pytest.mark.parametrize("valeur", ["64", "65", "1000", "999999999999999999999"])
def test_max_concurrent_syncs_plafonne_a_64(valeur):
    cfg = config.from_env(_env(MAX_CONCURRENT_SYNCS=valeur))
    assert cfg.max_concurrent_syncs == config.MAX_SYNCS_CEILING


def test_max_concurrent_syncs_borne_haute_exacte():
    cfg = config.from_env(_env(MAX_CONCURRENT_SYNCS="63"))
    assert cfg.max_concurrent_syncs == 63


@pytest.mark.parametrize(
    "valeur",
    ["9223372036854775808", "-9223372036854775809", "9" * 40],
)
def test_admin_chat_id_hors_64_bits_leve_une_erreur(valeur):
    with pytest.raises(RuntimeError, match="ADMIN_CHAT_ID invalide"):
        config.from_env(_env(ADMIN_CHAT_ID=valeur))


@pytest.mark.parametrize("valeur", ["9223372036854775807", "-9223372036854775808"])
def test_admin_chat_id_aux_bornes_64_bits_est_accepte(valeur):
    cfg = config.from_env(_env(ADMIN_CHAT_ID=valeur))
    assert cfg.admin_chat_id == int(valeur)


# ── 3. Ports invalides → erreur au démarrage ────────────────────────────────


@pytest.mark.parametrize(
    "valeur",
    [
        "http://127.0.0.1:abc/v1",
        "http://127.0.0.1:70000/v1",
        "http://127.0.0.1:-1/v1",
        "http://127.0.0.1:0/v1",
    ],
)
def test_llm_base_url_port_invalide_leve_une_erreur(valeur):
    with pytest.raises(RuntimeError, match="LLM_BASE_URL invalide"):
        config.from_env(_env(LLM_BASE_URL=valeur))


@pytest.mark.parametrize(
    "valeur",
    ["https://pronote.example:99999/eleve.html", "https://pronote.example:abc/"],
)
def test_url_pronote_port_invalide_leve_une_erreur(valeur):
    with pytest.raises(RuntimeError, match="DEFAULT_PRONOTE_URL invalide"):
        config.from_env(_env(DEFAULT_PRONOTE_URL=valeur))


def test_llm_base_url_port_valide_est_conserve():
    cfg = config.from_env(_env(LLM_BASE_URL="http://127.0.0.1:8080/v1/"))
    assert cfg.llm_base_url == "http://127.0.0.1:8080/v1"


def test_url_sans_schema_reste_transmise_telle_quelle():
    # Compatibilité : seules les URL pourvues d'un hôte sont inspectées.
    cfg = config.from_env(_env(DEFAULT_PRONOTE_URL="pronote.local/eleve"))
    assert cfg.default_pronote_url == "pronote.local/eleve"


# ── 4. Chemins relatifs et expansion ────────────────────────────────────────


def test_data_dir_relatif_reste_relatif():
    cfg = config.from_env(_env(DATA_DIR="instance/pronote"))
    assert cfg.data_dir == Path("instance/pronote")
    assert cfg.db_path == Path("instance/pronote") / "app.db"
    assert cfg.uploads_dir == Path("instance/pronote") / "uploads"
    assert cfg.locks_dir == Path("instance/pronote") / "locks"


@pytest.mark.parametrize("valeur", ["./data", "  ./data  "])
def test_data_dir_point_slash_est_normalise(valeur):
    cfg = config.from_env(_env(DATA_DIR=valeur))
    assert cfg.data_dir == Path("data")


def test_data_dir_absolu_conserve():
    cfg = config.from_env(_env(DATA_DIR="/var/lib/pronote-blocus"))
    assert cfg.data_dir == Path("/var/lib/pronote-blocus")
    assert cfg.db_path == Path("/var/lib/pronote-blocus") / "app.db"


def test_data_dir_expanduser():
    cfg = config.from_env(_env(DATA_DIR="~/pronote-blocus-test"))
    assert cfg.data_dir == Path.home() / "pronote-blocus-test"


@pytest.mark.skipif(os.name == "nt", reason="lookup pwd indisponible sous Windows")
def test_data_dir_utilisateur_inconnu_leve_une_erreur():
    with pytest.raises(RuntimeError, match="DATA_DIR invalide"):
        config.from_env(_env(DATA_DIR="~utilisateur-inexistant-pronote/data"))
