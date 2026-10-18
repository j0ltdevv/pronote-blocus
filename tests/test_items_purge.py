"""`db.replace_items` : une passe remplace, elle n'accumule pas.

La clé d'un item est un hash de son contenu. Sans purge, un devoir corrigé par
le professeur (« Exo 1 » → « Exo 1 corrigé ») crée une seconde ligne, et tout
ce qui sort de la fenêtre PRONOTE reste affiché en retard à vie.
"""

from __future__ import annotations

from datetime import date

from app import db
from app.pronote.normalize import make_item


def _devoir(body: str):
    return make_item(
        kind="devoir",
        source="travail",
        subject="Maths",
        body=body,
        due_date=date(2026, 10, 20),
    )


def _cours(body: str):
    return make_item(
        kind="cours", source="cahier_de_textes", subject="SVT", body=body, due_date=None
    )


def _bodies(conn, user_id: str) -> list[str]:
    return sorted(item.body for item in db.get_items(conn, user_id))


def test_un_devoir_modifie_ne_laisse_pas_de_doublon(conn, make_user):
    user_id = make_user()
    db.replace_items(conn, user_id, [_devoir("Exo 1")])
    db.replace_items(conn, user_id, [_devoir("Exo 1 corrigé")])
    assert _bodies(conn, user_id) == ["Exo 1 corrigé"]


def test_deux_passes_dans_la_meme_seconde_purgent_quand_meme(conn, make_user):
    """`now_iso()` est à la seconde : la purge ne doit pas en dépendre."""
    user_id = make_user()
    db.replace_items(conn, user_id, [_devoir("A"), _devoir("B")])
    db.replace_items(conn, user_id, [_devoir("A")])
    assert _bodies(conn, user_id) == ["A"]


def test_une_passe_vide_efface_les_devoirs(conn, make_user):
    user_id = make_user()
    db.replace_items(conn, user_id, [_devoir("A")])
    db.replace_items(conn, user_id, [])
    assert _bodies(conn, user_id) == []


def test_les_cours_survivent_a_une_passe_sans_cours(conn, make_user):
    """La phase cours ne lit qu'un lot : purger dessus effacerait le reste."""
    user_id = make_user()
    db.replace_items(conn, user_id, [_devoir("A"), _cours("Chapitre 1")])
    db.replace_items(conn, user_id, [_devoir("A")])
    assert _bodies(conn, user_id) == ["A", "Chapitre 1"]


def test_les_devoirs_des_autres_eleves_ne_sont_pas_touches(conn, make_user):
    premier = make_user(chat_id=1001)
    second = make_user(chat_id=1002)
    db.replace_items(conn, premier, [_devoir("A")])
    db.replace_items(conn, second, [_devoir("B")])
    db.replace_items(conn, second, [])
    assert _bodies(conn, premier) == ["A"]
    assert _bodies(conn, second) == []
