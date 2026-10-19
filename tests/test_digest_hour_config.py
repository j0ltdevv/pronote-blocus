"""`DIGEST_HOUR` : heure locale du résumé quotidien.

Défaut documenté : 18 (même valeur que l'ancienne colonne `users.digest_hour`,
donc un déploiement qui ne définit pas `DIGEST_HOUR` garde son comportement).
La valeur est validée au démarrage : une heure hors 0-23 ferait tourner la
boucle de résumé à contretemps, ou jamais.
"""

from __future__ import annotations

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

def test_defaut_vaut_18():
    assert config.from_env(_env()).digest_hour == 18

@pytest.mark.parametrize("valeur", ["0", "1", "7", "18", "22", "23"])
def test_valeur_valide_est_acceptee(valeur):
    assert config.from_env(_env(DIGEST_HOUR=valeur)).digest_hour == int(valeur)

def test_valeur_avec_espaces_est_strippee():
    assert config.from_env(_env(DIGEST_HOUR="  9 ")).digest_hour == 9

@pytest.mark.parametrize("valeur", ["", "   "])
def test_valeur_vide_retombe_sur_le_defaut(valeur):
    assert config.from_env(_env(DIGEST_HOUR=valeur)).digest_hour == 18

@pytest.mark.parametrize("valeur", ["24", "25", "-1", "-999"])
def test_valeur_hors_bornes_leve_une_erreur(valeur):
    with pytest.raises(RuntimeError, match="DIGEST_HOUR invalide"):
        config.from_env(_env(DIGEST_HOUR=valeur))

@pytest.mark.parametrize("valeur", ["abc", "7h", "18.5", "1,8", "0x10"])
def test_valeur_non_entieres_leve_une_erreur(valeur):
    with pytest.raises(RuntimeError, match="DIGEST_HOUR invalide"):
        config.from_env(_env(DIGEST_HOUR=valeur))
