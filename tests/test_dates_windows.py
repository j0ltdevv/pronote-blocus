"""Fenêtres de dates : bornes exactes, mois/année, bissextile, libellés.

Complète `test_dates.py` (cas nominaux) en visant les bords de la fenêtre
`PAST`/`FUTURE` de `app/pronote/dates.py`, l'arithmétique calendaire
(changement de mois/année, 29 février) et les variantes de libellés français.

À savoir sur le périmètre réel :

- `app/pronote/dates.py` ne contient AUCUN calcul de fuseau horaire ni
  d'heure d'été : les fonctions reçoivent une `date` nue (calendaire) et
  `base or date.today()`. Les tests « heure d'été » ci-dessous caractérisent
  cette absence d'horloge (aucun décalage au passage à l'heure d'été).
- `WINDOW_START` est une variable d'environnement de `app/config.py` (pas de
  `dates.py`) ; sa validation est déjà couverte par `test_config.py`. Ici on
  caractérise le seul « début de fenêtre » de `dates.py` : `base - PAST`.
- « semaine de /resume » n'est pas dans `dates.py` : le rendu des libellés vit
  dans `app/render.py`. Ces libellés sont couverts en fin de fichier.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.pronote.dates import (
    FUTURE,
    PAST,
    check_llm_date,
    date_explicite,
    prochain_jour,
    resolve_due,
)

# Samedi 17 octobre 2026 : base commune, jamais `today()` réel.
BASE = date(2026, 10, 17)


# ── 1. Bornes de la fenêtre : incluses aux extrémités, exclues au-delà ──────


def test_fenetre_bornes_incluses():
    """`base - PAST` et `base + FUTURE` appartiennent à la fenêtre."""
    bas = BASE - timedelta(days=PAST)
    haut = BASE + timedelta(days=FUTURE)
    assert check_llm_date(bas.isoformat(), base=BASE) == bas.isoformat()
    assert check_llm_date(haut.isoformat(), base=BASE) == haut.isoformat()


def test_fenetre_juste_au_dela_exclue():
    """Un jour de plus de chaque côté sort de la fenêtre."""
    trop_vieux = BASE - timedelta(days=PAST + 1)
    trop_loin = BASE + timedelta(days=FUTURE + 1)
    assert check_llm_date(trop_vieux.isoformat(), base=BASE) is None
    assert check_llm_date(trop_loin.isoformat(), base=BASE) is None


def test_date_explicite_borne_basse_par_le_texte():
    # « 2 octobre » = base - 15 j → pile dans la fenêtre.
    assert date_explicite("2 octobre", base=BASE) == "2026-10-02"
    # « 1 octobre » = base - 16 j → un jour de trop.
    assert date_explicite("1 octobre", base=BASE) is None


def test_date_explicite_borne_haute_par_le_texte():
    # « 14 février » = base + 120 j (l'année suivante) → pile dans la fenêtre.
    assert date_explicite("14 février", base=BASE) == "2027-02-14"
    # « 15 février » = base + 121 j → un jour de trop.
    assert date_explicite("15 février", base=BASE) is None


def test_fenetre_glisse_avec_la_base_pas_avec_aujourd_hui():
    """Une base future ou passée décale la fenêtre entière (aucun `today()`)."""
    future = date(2030, 6, 15)
    assert check_llm_date(future.isoformat(), base=future) == "2030-06-15"
    assert check_llm_date("2030-06-30", base=future) == "2030-06-30"
    assert check_llm_date("2030-08-01", base=future) == "2030-08-01"  # J+47
    assert check_llm_date("2031-01-01", base=future) is None  # J+200, trop loin

    passee = date(2020, 3, 10)
    assert check_llm_date("2020-03-01", base=passee) == "2020-03-01"  # J-9 dans la fenêtre
    assert check_llm_date("2020-01-01", base=passee) is None  # J-69, trop vieux


# ── 2. Changement de mois / d'année ─────────────────────────────────────────


def test_date_explicite_franchit_un_mois():
    # « 31 octobre » vu depuis fin octobre : même année, mois supérieur refusé…
    assert date_explicite("31 octobre", base=date(2026, 10, 30)) == "2026-10-31"
    # … et « 30 novembre » depuis fin novembre.
    assert date_explicite("30 novembre", base=date(2026, 11, 25)) == "2026-11-30"


def test_date_explicite_franchit_une_annee():
    # Vue depuis le 31 décembre, « 2 janvier » est forcément l'an prochain.
    assert date_explicite("2 janvier", base=date(2026, 12, 31)) == "2027-01-02"
    # « 31 décembre » vu la veille : toujours cette année-ci.
    assert date_explicite("31 décembre", base=date(2026, 12, 30)) == "2026-12-31"


def test_date_explicite_hors_fenetre_les_deux_annees():
    # En juin, « 5 janvier » : cette année = passé lointain, l'an prochain = trop loin.
    assert date_explicite("5 janvier", base=date(2026, 6, 1)) is None


# ── 3. Année bissextile ─────────────────────────────────────────────────────


def test_date_explicite_29_fevrier_bissextile():
    assert date_explicite("29 février", base=date(2028, 2, 10)) == "2028-02-29"


def test_date_explicite_29_fevrier_non_bissextile():
    # 2026 et 2027 n'ont pas de 29 février → aucune année ne convient.
    assert date_explicite("29 février", base=date(2026, 2, 10)) is None


def test_date_explicite_29_fevrier_bascule_vers_annee_bissextile():
    # 2027 n'a pas de 29 février, 2028 oui : la date est reportée à l'année
    # suivante (2028-02-29 est à J+71 de la base, donc dans la fenêtre).
    assert date_explicite("29 février", base=date(2027, 12, 20)) == "2028-02-29"


def test_check_llm_date_29_fevrier():
    assert check_llm_date("2028-02-29", base=date(2028, 1, 1)) == "2028-02-29"
    # Date inexistante → `fromisoformat` lève et renvoie None.
    assert check_llm_date("2027-02-29", base=date(2027, 1, 1)) is None


# ── 4. Libellés français (variantes accentuées / non accentuées) ────────────


@pytest.mark.parametrize(
    ("texte", "base", "attendu"),
    [
        ("10 février", date(2027, 2, 1), "2027-02-10"),
        ("10 fevrier", date(2027, 2, 1), "2027-02-10"),
        ("10 août", date(2026, 8, 1), "2026-08-10"),
        ("10 aout", date(2026, 8, 1), "2026-08-10"),
        ("10 décembre", date(2026, 12, 1), "2026-12-10"),
        ("10 decembre", date(2026, 12, 1), "2026-12-10"),
    ],
)
def test_date_explicite_accepte_les_variantes_de_mois(texte, base, attendu):
    assert date_explicite(texte, base=base) == attendu


@pytest.mark.parametrize(
    ("jour", "attendu"),
    [
        ("lundi", "2026-10-19"),
        ("mardi", "2026-10-20"),
        ("mercredi", "2026-10-21"),
        ("jeudi", "2026-10-22"),
        ("vendredi", "2026-10-23"),
        ("samedi", "2026-10-24"),  # même jour de semaine → semaine suivante
        ("dimanche", "2026-10-18"),
    ],
)
def test_prochain_jour_couvre_les_sept_jours(jour, attendu):
    assert prochain_jour(jour, base=BASE) == attendu


def test_prochain_jour_insensible_a_la_casse():
    assert prochain_jour("Vendredi", base=BASE) == "2026-10-23"
    assert prochain_jour("DIMANCHE", base=BASE) == "2026-10-18"


# ── 5. Fuseau / heure d'été : caractérisation d'une absence ──────────────────


def test_heure_d_ete_paris_sans_decalage():
    """`dates.py` ignore le fuseau : une date reste une date calendaire.

    Le passage à l'heure d'été (29 mars 2026) et d'hiver (25 octobre 2026) en
    Europe/Paris ne change RIEN au résultat, faute de logique `zoneinfo`.
    """
    assert date_explicite("29 mars", base=date(2026, 3, 20)) == "2026-03-29"
    assert prochain_jour("dimanche", base=date(2026, 3, 23)) == "2026-03-29"
    assert date_explicite("25 octobre", base=date(2026, 10, 20)) == "2026-10-25"


# ── 6. `resolve_due` : bords non couverts par test_dates.py ─────────────────


def test_resolve_due_garde_une_date_structuree_hors_fenetre():
    """La fenêtre `PAST`/`FUTURE` ne s'applique PAS à la date PRONOTE."""
    vieille = date(2019, 1, 1)
    assert resolve_due(vieille, "pour le 21 octobre", "2026-12-01", base=BASE) == vieille


def test_resolve_due_sans_aucune_date():
    assert resolve_due(None, "", None, base=BASE) is None
    assert resolve_due(None, "lire le chapitre 4", "pas une date", base=BASE) is None


def test_resolve_due_texte_prime_sur_llm_hors_fenetre():
    # Le LLM propose 2019 (hors fenêtre) ; le texte daté gagne avant lui.
    assert resolve_due(None, "pour le 21 octobre", "2019-01-01", base=BASE) == date(
        2026, 10, 21
    )


def test_check_llm_date_tronque_un_horodatage():
    # Seuls les 10 premiers caractères sont lus : un ISO complet passe.
    assert check_llm_date("2026-10-20T12:00:00", base=BASE) == "2026-10-20"
    assert check_llm_date("2026-10-20 reste du texte", base=BASE) == "2026-10-20"


# ── 7. Libellés « semaine de /resume » (rendu, pas `dates.py`) ───────────────


def test_due_label_libelles_francais():
    from app.render import due_label

    assert due_label(None, BASE) == ""
    assert due_label(BASE, BASE) == "aujourd'hui"
    assert due_label(BASE + timedelta(days=1), BASE) == "demain"
    assert due_label(BASE + timedelta(days=5), BASE) == "dans 5 j"
    assert due_label(BASE - timedelta(days=9), BASE) == "9 j de retard"


def test_numbered_lines_ordonne_la_semaine_du_resume():
    from app.pronote.normalize import Item
    from app.pronote.ranking import Row
    from app.render import numbered_lines

    def ligne(key: str, due: date) -> Row:
        item = Item(
            key=key,
            source="demo",
            subject="Maths",
            title="Devoir",
            body="",
            due_date=due,
            kind="devoir",
            attachments=[],
        )
        return Row(item=item, summary="r", action="réviser", effort="moyen")

    rows = [
        ligne("a", BASE),  # aujourd'hui
        ligne("b", BASE + timedelta(days=1)),  # demain
        ligne("c", BASE - timedelta(days=3)),  # en retard
    ]
    lines = numbered_lines(rows, BASE)
    assert lines[0].startswith("1.")
    assert "aujourd'hui" in lines[0]
    assert "demain" in lines[1]
    assert "3 j de retard" in lines[2]
