"""WINDOW_START est relu à chaque cycle de synchro, sans redémarrer.

Un process qui tourne ne doit plus dépendre de la valeur figée au démarrage :
changer `WINDOW_START` doit modifier la fenêtre de rattrapage dès le cycle
suivant. La relecture vit au bord de la synchro (`fetch._window`), seul point
par lequel passent le bot et le serveur web ; `Config` reste la photographie
validée au démarrage (autoritaire, même si l'env ambiant a changé).
"""

from __future__ import annotations

from cryptography.fernet import Fernet

from app import config
from app.pronote import fetch


def _cfg(window_start: str):
    return config.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "x:y",
            "APP_SECRET": Fernet.generate_key().decode(),
            "WINDOW_START": window_start,
        }
    )


def test_fenetre_relue_au_cycle_suivant_sans_restart(monkeypatch):
    monkeypatch.delenv("WINDOW_START", raising=False)
    cfg = _cfg("2026-10-01")

    debut, _, _ = fetch._window(cfg)
    assert debut.isoformat() == "2026-10-01"

    # La variable change pendant que le process tourne : le cycle suivant
    # doit voir la nouvelle valeur, sans restart.
    monkeypatch.setenv("WINDOW_START", "2026-09-15")
    debut, _, _ = fetch._window(cfg)
    assert debut.isoformat() == "2026-09-15"


def test_config_reste_autoritaire_sur_l_env_ambiant(monkeypatch):
    """`from_env(env={...})` prime : le snapshot validé au boot n'est pas
    écrasé par l'env ambiant — seule la fenêtre de synchro est relue."""
    monkeypatch.setenv("WINDOW_START", "2026-09-15")
    cfg = _cfg("2026-10-01")

    assert cfg.window_start_date().isoformat() == "2026-10-01"
    debut, _, _ = fetch._window(cfg)
    assert debut.isoformat() == "2026-09-15"


def test_variable_absente_retombe_sur_la_valeur_de_demarrage(monkeypatch):
    monkeypatch.delenv("WINDOW_START", raising=False)
    cfg = _cfg("2026-10-01")

    debut, _, _ = fetch._window(cfg)
    assert debut.isoformat() == "2026-10-01"


def test_invalide_a_chaud_ignore_et_repli(monkeypatch):
    """Une valeur mal formée en cours de route ne casse pas la synchro :
    log + repli sur la valeur validée au démarrage."""
    monkeypatch.setenv("WINDOW_START", "hier")
    cfg = _cfg("2026-10-01")

    debut, _, _ = fetch._window(cfg)
    assert debut.isoformat() == "2026-10-01"
