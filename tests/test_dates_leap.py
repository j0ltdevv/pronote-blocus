"""29 février et bascule d'année dans `date_explicite`.

Régression : quand la date n'existe pas dans l'année de base (29 février
d'une année non bissextile), l'implémentation rendait `None` immédiatement
sans tenter l'année suivante. On vérifie ici la bascule, l'absence de
décalage pour les 365 dates valides, et le cas « invalide dans les deux
années ».
"""

from __future__ import annotations

import calendar
from datetime import date

import pytest

from app.config import MOIS_FR
from app.pronote.dates import date_explicite, resolve_due

# Noms de mois français indexés par numéro (accents complets : 1..12).
NOMS_MOIS = {
    1: "janvier",
    2: "février",
    3: "mars",
    4: "avril",
    5: "mai",
    6: "juin",
    7: "juillet",
    8: "août",
    9: "septembre",
    10: "octobre",
    11: "novembre",
    12: "décembre",
}


# ── 1. 29 février : année de base, année suivante ───────────────────────────


def test_29_fevrier_annee_bissextile_meme_annee():
    assert date_explicite("29 février", base=date(2028, 2, 10)) == "2028-02-29"


def test_29_fevrier_bascule_vers_annee_suivante_bissextile():
    # 2027 n'a pas de 29 février ; 2028-02-29 est à J+71, dans la fenêtre.
    assert date_explicite("29 février", base=date(2027, 12, 20)) == "2028-02-29"
    # Variante sans accent, comme dans les messages Telegram.
    assert date_explicite("29 fevrier", base=date(2027, 12, 20)) == "2028-02-29"


def test_29_fevrier_bascule_aussi_depuis_debut_annee_non_bissextile():
    # Base 2027-01-31 : le 29/02/2027 n'existe pas, le 29/02/2028 est à
    # J+394 → hors fenêtre (+120), donc rien à proposer.
    assert date_explicite("29 février", base=date(2027, 1, 31)) is None
    # Base 2027-11-01 : 2028-02-29 est à J+120, borne haute incluse.
    assert date_explicite("29 février", base=date(2027, 11, 1)) == "2028-02-29"


def test_29_fevrier_invalide_dans_les_deux_annees():
    # 2026 et 2027 sont non bissextiles : aucune année ne convient.
    assert date_explicite("29 février", base=date(2026, 2, 10)) is None
    assert date_explicite("29 fevrier", base=date(2026, 12, 20)) is None


def test_29_fevrier_hors_fenetre_les_deux_annees():
    # 2028-02-29 est 265 jours avant la base → trop vieux ; 2029 n'en a pas.
    assert date_explicite("29 février", base=date(2028, 12, 20)) is None


def test_resolve_due_bascule_le_29_fevrier():
    assert resolve_due(None, "contrôle pour le 29 février", None, base=date(2027, 12, 20)) == date(
        2028, 2, 29
    )


# ── 2. Bords d'année ────────────────────────────────────────────────────────


def test_31_decembre():
    # Dans la fenêtre de l'année de base : pas de bascule.
    assert date_explicite("31 décembre", base=date(2026, 12, 1)) == "2026-12-31"
    # Hors fenêtre en décembre 2027 (~J+360) et en décembre 2028 : rien.
    assert date_explicite("31 décembre", base=date(2027, 1, 5)) is None


def test_1er_janvier():
    # 2026-01-01 est 364 jours avant la base : la bascule donne 2027-01-01.
    assert date_explicite("1 janvier", base=date(2026, 12, 31)) == "2027-01-01"
    assert date_explicite("01 janvier", base=date(2026, 12, 20)) == "2027-01-01"
    # Déjà dans la fenêtre : aucune bascule.
    assert date_explicite("1 janvier", base=date(2027, 1, 2)) == "2027-01-01"


# ── 3. Date inexistante dans les deux années ────────────────────────────────


def test_31_fevrier_inexistant_partout():
    assert date_explicite("31 février", base=date(2026, 2, 10)) is None
    assert date_explicite("31 fevrier", base=date(2028, 2, 10)) is None
    assert date_explicite("30 février", base=date(2027, 2, 10)) is None
    assert date_explicite("32 mars", base=date(2026, 3, 10)) is None
    assert date_explicite("0 mars", base=date(2026, 3, 10)) is None


# ── 4. Non-régression : les 365 jours valides ne bougent pas ────────────────


def _jours_de_annee(annee: int) -> list[tuple[int, int]]:
    return [
        (mois, jour)
        for mois in range(1, 13)
        for jour in range(1, calendar.monthrange(annee, mois)[1] + 1)
    ]


def test_365_jours_valides_non_decales():
    jours = _jours_de_annee(2026)  # année commune : exactement 365 dates
    assert len(jours) == 365
    for mois, jour in jours:
        base = date(2026, mois, jour)
        texte = f"{jour} {NOMS_MOIS[mois]}"
        assert date_explicite(texte, base=base) == base.isoformat(), texte


def test_366_jours_bissextiles_non_decales():
    jours = _jours_de_annee(2028)
    assert len(jours) == 366
    for mois, jour in jours:
        base = date(2028, mois, jour)
        texte = f"{jour} {NOMS_MOIS[mois]}"
        assert date_explicite(texte, base=base) == base.isoformat(), texte


def test_non_regression_chaque_jour_reste_dans_sa_fenetre():
    # Une date valide l'année de base et dans la fenêtre doit rester dans
    # l'année de base, jamais projetée à l'année suivante.
    for mois, jour in _jours_de_annee(2026):
        base = date(2026, mois, jour)
        attendu = date(2026, mois, jour)
        for variante in (NOMS_MOIS[mois], NOMS_MOIS[mois].upper()):
            assert date_explicite(f"{jour} {variante}", base=base) == attendu.isoformat()


@pytest.mark.parametrize("mois_extrait", sorted(MOIS_FR))
def test_chaque_mois_accepte_reste_stable(mois_extrait):
    # Garde-fou : toutes les orthographes de `MOIS_FR` restent reconnues.
    mois = MOIS_FR[mois_extrait]
    base = date(2026, mois, 15)
    assert date_explicite(f"10 {mois_extrait}", base=base) == date(2026, mois, 10).isoformat()
