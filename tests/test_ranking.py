"""Classement : en retard d'abord, rien de caché."""

from __future__ import annotations

from datetime import date, timedelta

from app.pronote.normalize import Item
from app.pronote.ranking import group_cours, split_sections

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


def _six_items() -> list[Item]:
    return [
        _item("a", "Maths", "controle", TODAY + timedelta(days=2)),
        _item("b", "Histoire", "devoir", TODAY - timedelta(days=9)),
        _item("c", "Anglais", "devoir", None),
        _item("d", "Maths", "devoir", TODAY + timedelta(days=1)),
        _item("e", "SVT", "devoir", TODAY - timedelta(days=3)),
        _item("f", "Maths", "cours", TODAY),
    ]


PRIORITES = {"Maths": 0, "SVT": 1}


def test_split_sections_ordre_exact():
    overdue, upcoming = split_sections(_six_items(), {}, PRIORITES, TODAY)

    assert [r.key for r in overdue] == ["b", "e"], "le plus ancien d'abord"
    assert [r.key for r in upcoming] == ["a", "d", "c"]
    assert overdue[0].item.due_date == TODAY - timedelta(days=9)


def test_split_sections_ne_perd_aucun_item():
    overdue, upcoming = split_sections(_six_items(), {}, PRIORITES, TODAY)

    vus = {r.key for r in overdue} | {r.key for r in upcoming}
    assert len(overdue) + len(upcoming) == 5
    assert vus == {"a", "b", "c", "d", "e"}
    assert "f" not in vus, "les cours n'apparaissent pas dans /retard"


def test_controle_a_venir_passe_devant_un_devoir_plus_proche():
    overdue, upcoming = split_sections(_six_items(), {}, PRIORITES, TODAY)

    assert upcoming[0].key == "a"
    assert upcoming[0].item.kind == "controle"
    assert upcoming[0].item.due_date == TODAY + timedelta(days=2)
    # Le devoir de Maths est à J+1, donc plus proche, mais l'ordre le met après.
    assert upcoming[1].key == "d"


def test_priorite_de_matiere_arbitre_a_anciennete_egale():
    items = [
        _item("svt", "SVT", "devoir", TODAY, body="x" * 40),
        _item("maths", "Maths", "devoir", TODAY, body="x" * 40),
    ]

    overdue, upcoming = split_sections(items, {}, {"Maths": 0, "SVT": 1}, TODAY)

    assert [r.key for r in upcoming] == ["maths", "svt"]
    _, inversees = split_sections(items, {}, {"Maths": 1, "SVT": 0}, TODAY)
    assert [r.key for r in inversees] == ["svt", "maths"]


def test_une_matiere_non_prioritaire_descend_mais_reste_visible():
    overdue, upcoming = split_sections(_six_items(), {}, PRIORITES, TODAY)

    assert "c" in {r.key for r in upcoming}, "Anglais non prioritaire reste listé"
    assert [r.item.subject for r in upcoming] == ["Maths", "Maths", "Anglais"]


def test_done_retire_un_item():
    overdue, upcoming = split_sections(_six_items(), {}, PRIORITES, TODAY, {"b", "a"})

    assert [r.key for r in overdue] == ["e"]
    assert [r.key for r in upcoming] == ["d", "c"]


def test_group_cours_ordre_et_exhaustivite():
    cours = [_item(f"m{i}", "Maths", "cours", TODAY - timedelta(days=i)) for i in range(7)]
    cours.append(_item("svt0", "SVT", "cours", TODAY - timedelta(days=1)))
    cours.append(_item("anglais0", "Anglais", "cours", TODAY))
    cours.append(_item("devoir", "Maths", "devoir", TODAY))

    groups = group_cours(cours, PRIORITES)

    assert [subject for subject, _ in groups] == ["Maths", "SVT", "Anglais"]
    maths = dict(groups)["Maths"]
    assert len(maths) == 7, "aucun cours n'est tronqué"
    assert all(c.kind == "cours" for _, lessons in groups for c in lessons)
    dates = [c.due_date for c in maths]
    assert dates == sorted(dates, reverse=True), "le plus récent d'abord"
    assert len(maths) + len(dict(groups)["SVT"]) + len(dict(groups)["Anglais"]) == 9
