"""Classement : arêtes (urgences croissantes, égalités, doublons, liste vide).

Complète `test_ranking.py` sans le dupliquer : ces tests ciblent les cas
limites de `sort_key` (départages, stabilité) et de `split_sections`.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.pronote.normalize import Item
from app.pronote.ranking import NO_PRIORITY, split_sections

TODAY = date(2026, 10, 17)


def _item(key: str, subject: str, kind: str, due: date | None, body: str = "") -> Item:
    return Item(
        key=key,
        source="cahier_de_textes" if kind == "cours" else "travail_a_faire",
        subject=subject,
        title=f"titre {key}",
        body=body or f"corps {key}",
        due_date=due,
        kind=kind,
        attachments=[],
    )


def test_liste_vide_rend_deux_listes_vides():
    overdue, upcoming = split_sections([], {}, {"Maths": 0}, TODAY)

    assert overdue == []
    assert upcoming == []


def test_urgence_croissante_le_plus_ancien_d_abord():
    items = [
        _item("recent", "Maths", "devoir", TODAY - timedelta(days=1)),
        _item("vieux", "Maths", "devoir", TODAY - timedelta(days=12)),
        _item("moyen", "Maths", "devoir", TODAY - timedelta(days=4)),
    ]

    overdue, _ = split_sections(items, {}, {}, TODAY)

    assert [r.key for r in overdue] == ["vieux", "moyen", "recent"]
    ages = [(TODAY - r.item.due_date).days for r in overdue]
    assert ages == [12, 4, 1], "l'ancienneté décroît, l'urgence croît"


def test_egalite_de_date_departagee_par_taille_de_corps():
    items = [
        _item("long", "Maths", "devoir", TODAY + timedelta(days=1), body="x" * 60),
        _item("court", "Maths", "devoir", TODAY + timedelta(days=1), body="x" * 5),
    ]

    _, upcoming = split_sections(items, {}, {}, TODAY)

    assert [r.key for r in upcoming] == ["court", "long"], "le plus court d'abord"


def test_ordre_stable_a_cle_de_tri_egale():
    a = _item("z1", "Maths", "devoir", TODAY + timedelta(days=1))
    b = _item("z2", "Maths", "devoir", TODAY + timedelta(days=1))

    _, upcoming = split_sections([a, b], {}, {}, TODAY)
    assert [r.key for r in upcoming] == ["z1", "z2"]

    _, inverses = split_sections([b, a], {}, {}, TODAY)
    assert [r.key for r in inverses] == ["z2", "z1"], "à clé égale, l'ordre d'entrée est gardé"


def test_matiere_prioritaire_passe_devant_une_echeance_plus_proche():
    items = [
        _item("anglais_proche", "Anglais", "devoir", TODAY + timedelta(days=1)),
        _item("maths_loin", "Maths", "devoir", TODAY + timedelta(days=10)),
    ]

    _, upcoming = split_sections(items, {}, {"Maths": 0, "Anglais": 1}, TODAY)

    assert [r.key for r in upcoming] == ["maths_loin", "anglais_proche"], (
        "pour un devoir à venir, la priorité de matière passe avant la date"
    )


def test_matrice_de_priorites_incomplete_descend_la_matiere_absente():
    items = [
        _item("absent", "Anglais", "devoir", TODAY + timedelta(days=1)),
        _item("svt", "SVT", "devoir", TODAY + timedelta(days=1)),
        _item("maths", "Maths", "devoir", TODAY + timedelta(days=1)),
    ]

    _, upcoming = split_sections(items, {}, {"Maths": 0, "SVT": 1}, TODAY)

    assert [r.key for r in upcoming] == ["maths", "svt", "absent"]
    assert NO_PRIORITY == 99, "valeur de repli documentée"


def test_item_sans_date_va_en_a_venir_et_arrive_en_dernier():
    items = [
        _item("sans_date", "Maths", "devoir", None),
        _item("date_proche", "Maths", "devoir", TODAY + timedelta(days=1)),
        _item("en_retard", "Maths", "devoir", TODAY - timedelta(days=1)),
    ]

    overdue, upcoming = split_sections(items, {}, {}, TODAY)

    assert [r.key for r in overdue] == ["en_retard"], "sans date n'est jamais en retard"
    assert [r.key for r in upcoming] == ["date_proche", "sans_date"]


def test_doublons_meme_cle_conserves_tels_quels():
    # Caractérisation : `split_sections` ne dédoublonne pas par `key`
    # (le dédoublonnage se fait en amont, dans `build_items`).
    items = [
        _item("dup", "Maths", "devoir", TODAY - timedelta(days=2)),
        _item("dup", "Maths", "devoir", TODAY - timedelta(days=2)),
    ]

    overdue, _ = split_sections(items, {}, {}, TODAY)

    assert [r.key for r in overdue] == ["dup", "dup"]
    assert len(overdue) == 2
