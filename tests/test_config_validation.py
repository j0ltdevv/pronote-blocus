"""Validation exhaustive des entrées de `app.config.from_env`.

Complète `tests/test_config.py` (qui couvre la désactivation de LLM_MESSAGES, la
clé Fernet invalide et la date WINDOW_START) en insistant sur :

1. les variables obligatoires absentes ou vides ;
2. ADMIN_CHAT_ID et MAX_CONCURRENT_SYNCS (entiers invalides ou hors bornes) ;
3. les booléens LLM_MESSAGES sous toutes leurs formes ;
4. les valeurs par défaut ;
5. les chaînes transmises telles quelles (URL malformée, base URL, DATA_DIR).

Les tests de clamps enregistrent le comportement réel du code (caractérisation) :
`MAX_CONCURRENT_SYNCS` est ramené à 1 au minimum et à 4 si non entier, plutôt que
de lever une erreur.
"""

from __future__ import annotations

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


# ── 1. Variables obligatoires ───────────────────────────────────────────────


def test_token_manquant_leve_une_erreur():
    env = _env()
    del env["TELEGRAM_BOT_TOKEN"]
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN manquant"):
        config.from_env(env)


@pytest.mark.parametrize("valeur", ["", "   "])
def test_token_vide_ou_blanc_est_manquant(valeur):
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN manquant"):
        config.from_env(_env(TELEGRAM_BOT_TOKEN=valeur))


def test_secret_manquant_leve_une_erreur():
    env = _env()
    del env["APP_SECRET"]
    with pytest.raises(RuntimeError, match="APP_SECRET manquant"):
        config.from_env(env)


def test_secret_blanc_est_manquant():
    with pytest.raises(RuntimeError, match="APP_SECRET manquant"):
        config.from_env(_env(APP_SECRET="   "))


def test_require_token_false_accepte_un_environnement_vide(tmp_path):
    # Zéro-config : sans APP_SECRET, le mode web génère une clé et la
    # persiste dans DATA_DIR/app_secret. Le test isole DATA_DIR pour ne pas
    # polluer le dépôt et vérifie que la clé est une vraie Fernet.
    cfg = config.from_env({"DATA_DIR": str(tmp_path / "data")}, require_token=False)
    assert cfg.telegram_token == ""
    assert cfg.app_secret != ""
    # doit être une clé Fernet valide
    Fernet(cfg.app_secret.encode())
    assert cfg.window_start == "2026-10-01"
    # second appel = même clé réutilisée depuis le fichier
    cfg2 = config.from_env({"DATA_DIR": str(tmp_path / "data")}, require_token=False)
    assert cfg2.app_secret == cfg.app_secret


def test_secret_fernet_valide_conserve_la_cle():
    cle = Fernet.generate_key().decode()
    cfg = config.from_env(_env(APP_SECRET=cle))
    assert cfg.app_secret == cle


def test_les_valeurs_sont_strippees():
    cfg = config.from_env(_env(TELEGRAM_BOT_TOKEN="  x:y  "))
    assert cfg.telegram_token == "x:y"


# ── 2. ADMIN_CHAT_ID ────────────────────────────────────────────────────────


def test_admin_chat_id_absent_vaut_none():
    cfg = config.from_env(_env())
    assert cfg.admin_chat_id is None


@pytest.mark.parametrize("valeur", ["", "   "])
def test_admin_chat_id_blanc_vaut_none(valeur):
    cfg = config.from_env(_env(ADMIN_CHAT_ID=valeur))
    assert cfg.admin_chat_id is None


def test_admin_chat_id_entier_est_lu():
    cfg = config.from_env(_env(ADMIN_CHAT_ID=" 999999 "))
    assert cfg.admin_chat_id == 999999


def test_admin_chat_id_negatif_est_accepte():
    cfg = config.from_env(_env(ADMIN_CHAT_ID="-42"))
    assert cfg.admin_chat_id == -42


@pytest.mark.parametrize("valeur", ["abc", "12.5", "1,2", "9e3"])
def test_admin_chat_id_non_entier_leve_une_erreur(valeur):
    with pytest.raises(RuntimeError, match="ADMIN_CHAT_ID invalide"):
        config.from_env(_env(ADMIN_CHAT_ID=valeur))


# ── 3. MAX_CONCURRENT_SYNCS ─────────────────────────────────────────────────


def test_max_concurrent_syncs_defaut_vaut_4():
    cfg = config.from_env(_env())
    assert cfg.max_concurrent_syncs == 4


def test_max_concurrent_syncs_valeur_valide():
    cfg = config.from_env(_env(MAX_CONCURRENT_SYNCS=" 8 "))
    assert cfg.max_concurrent_syncs == 8


@pytest.mark.parametrize("valeur", ["0", "-3", "-999"])
def test_max_concurrent_syncs_hors_bornes_est_ramene_a_1(valeur):
    cfg = config.from_env(_env(MAX_CONCURRENT_SYNCS=valeur))
    assert cfg.max_concurrent_syncs == 1


def test_max_concurrent_syncs_1_reste_1():
    cfg = config.from_env(_env(MAX_CONCURRENT_SYNCS="1"))
    assert cfg.max_concurrent_syncs == 1


@pytest.mark.parametrize("valeur", ["abc", "4.5", "", "  "])
def test_max_concurrent_syncs_invalide_retombe_sur_4(valeur):
    cfg = config.from_env(_env(MAX_CONCURRENT_SYNCS=valeur))
    assert cfg.max_concurrent_syncs == 4


# ── 4. LLM_MESSAGES (booléens sous toutes leurs formes) ─────────────────────


@pytest.mark.parametrize("valeur", ["1", "true", "TRUE", "True", "yes", "YES", "on", "oui"])
def test_llm_messages_formes_actives(valeur):
    cfg = config.from_env(_env(LLM_MESSAGES=valeur))
    assert cfg.llm_messages is True


@pytest.mark.parametrize("valeur", ["1", "true", "yes"])
def test_llm_messages_actif_valeur_avec_espaces(valeur):
    cfg = config.from_env(_env(LLM_MESSAGES=f"  {valeur}  "))
    assert cfg.llm_messages is True


@pytest.mark.parametrize("valeur", ["0", "false", "no", "   "])
def test_llm_messages_formes_desactivees(valeur):
    cfg = config.from_env(_env(LLM_MESSAGES=valeur))
    assert cfg.llm_messages is False


def test_llm_messages_chaine_vide_equivaut_a_non_defini():
    # Caractérisation : "" est traité comme absent, donc retombe sur "1" → True.
    cfg = config.from_env(_env(LLM_MESSAGES=""))
    assert cfg.llm_messages is True


# ── 5. Valeurs par défaut et dérivations ────────────────────────────────────


def test_valeurs_par_defaut():
    cfg = config.from_env(_env())
    assert cfg.window_start == "2026-10-01"
    assert cfg.default_pronote_url == config.DEFAULT_PRONOTE_URL
    assert cfg.mobile_query == config.DEFAULT_MOBILE_QUERY
    assert cfg.llm_base_url == ""
    assert cfg.llm_api_key == ""
    assert cfg.llm_model == ""
    assert cfg.llm_enabled is False


def test_data_dir_par_defaut_et_db_path():
    cfg = config.from_env(_env())
    assert cfg.data_dir == Path("data")
    assert cfg.db_path == Path("data") / "app.db"


def test_data_dir_personnalise_derive_db_uploads_locks():
    cfg = config.from_env(_env(DATA_DIR="/tmp/instance-pronote"))
    assert cfg.data_dir == Path("/tmp/instance-pronote")
    assert cfg.db_path == Path("/tmp/instance-pronote") / "app.db"
    assert cfg.uploads_dir == Path("/tmp/instance-pronote") / "uploads"
    assert cfg.locks_dir == Path("/tmp/instance-pronote") / "locks"


def test_data_dir_expanduser():
    cfg = config.from_env(_env(DATA_DIR="~"))
    assert cfg.data_dir == Path.home()


def test_llm_enabled_vrai_quand_les_trois_champs_sont_presents():
    cfg = config.from_env(
        _env(LLM_BASE_URL="http://127.0.0.1:9/v1", LLM_API_KEY="k", LLM_MODEL="m")
    )
    assert cfg.llm_enabled is True


def test_llm_enabled_faux_si_un_champ_manque():
    cfg = config.from_env(_env(LLM_BASE_URL="http://127.0.0.1:9/v1", LLM_API_KEY="k"))
    assert cfg.llm_enabled is False


def test_window_start_date_convertit_la_chaine():
    cfg = config.from_env(_env(WINDOW_START="2026-09-15"))
    assert cfg.window_start_date().isoformat() == "2026-09-15"


# ── 6. Chaînes transmises telles quelles ────────────────────────────────────


def test_llm_base_url_sans_slash_final():
    cfg = config.from_env(_env(LLM_BASE_URL="http://127.0.0.1:9/v1/"))
    assert cfg.llm_base_url == "http://127.0.0.1:9/v1"


def test_url_pronote_malformee_est_transmise_telle_quelle():
    # Caractérisation : from_env ne valide pas les URL, seule la forme est rangée.
    cfg = config.from_env(_env(DEFAULT_PRONOTE_URL="pas une url"))
    assert cfg.default_pronote_url == "pas une url"


def test_mobile_query_personnalisee():
    cfg = config.from_env(_env(PRONOTE_MOBILE_QUERY="?fd=2"))
    assert cfg.mobile_query == "?fd=2"
