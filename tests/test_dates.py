"""Dates françaises : la date du texte prime sur celle du modèle."""

from __future__ import annotations

from datetime import date

from app.pronote.dates import (
    check_llm_date,
    date_explicite,
    prochain_jour,
    resolve_due,
)

SAMEDI = date(2026, 10, 17)
JEUDI = date(2026, 10, 15)
VENDREDI = date(2026, 10, 16)


def test_date_explicite_mois_en_francais():
    assert date_explicite("pour le 21 octobre", base=SAMEDI) == "2026-10-21"
    assert date_explicite("DM pour le 3 novembre", base=SAMEDI) == "2026-11-03"
    assert date_explicite("rien de daté ici", base=SAMEDI) is None


def test_date_explicite_rejette_hors_fenetre():
    # ~46 jours dans le passé : trop vieux pour être une échéance vivante.
    assert date_explicite("1 septembre", base=SAMEDI) is None
    # 2027-05-05 = J+200 : au-delà de la fenêtre de +120 jours.
    assert date_explicite("5 mai", base=SAMEDI) is None


def test_date_explicite_passe_a_l_annee_suivante():
    # « 5 janvier » en décembre = l'an prochain, pas 10 mois dans le passé.
    assert date_explicite("5 janvier", base=date(2026, 12, 20)) == "2027-01-05"


def test_prochain_jour_jamais_le_jour_meme():
    assert prochain_jour("vendredi", base=VENDREDI) == "2026-10-23"
    assert prochain_jour("vendredi", base=JEUDI) == "2026-10-16"
    assert prochain_jour("lundi", base=SAMEDI) == "2026-10-19"
    assert prochain_jour("pas de jour ici", base=SAMEDI) is None


def test_check_llm_date_rejette_une_date_hallucinee():
    assert check_llm_date("2026-10-20", base=SAMEDI) == "2026-10-20"
    assert check_llm_date("2019-01-01", base=SAMEDI) is None
    assert check_llm_date("pas une date", base=SAMEDI) is None
    assert check_llm_date(None, base=SAMEDI) is None


def test_resolve_due_hierarchie():
    # 1. date structurée PRONOTE, même en présence de texte et de LLM.
    assert resolve_due(date(2026, 10, 5), "pour le 21 octobre", "2026-12-01", base=SAMEDI) == date(
        2026, 10, 5
    )
    # 2. date explicite du texte.
    assert resolve_due(None, "pour le 21 octobre", "2026-12-01", base=SAMEDI) == date(
        2026, 10, 21
    )
    # 3. jour de la semaine, au plus tôt demain.
    assert resolve_due(None, "rendez-vous vendredi", "2026-12-01", base=JEUDI) == date(
        2026, 10, 16
    )
    # 4. date du LLM quand le texte n'en donne aucune.
    assert resolve_due(None, "lire le chapitre 4", "2026-10-20", base=SAMEDI) == date(
        2026, 10, 20
    )


def test_resolve_due_jette_la_date_llm_hors_fenetre():
    assert resolve_due(None, "rien", "2019-01-01", base=SAMEDI) is None
    assert resolve_due(None, "rien", "2027-12-31", base=SAMEDI) is None
