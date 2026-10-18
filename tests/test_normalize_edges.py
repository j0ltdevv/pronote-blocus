"""Normalisation : ce que le texte brut devient avant d'être comparé.

Ces tests décrivent le comportement RÉEL de `app.pronote.normalize`, y compris
ses angles morts (entités HTML non décodées quand `pronotepy.Util` est absent,
caractères de contrôle conservés, accents non pliés dans la `key`).
Complète `tests/test_ranking.py` (classement) et `tests/test_db.py` (persistance)
sans les dupliquer : ici, seul le passage brut → `Item`.
"""

from __future__ import annotations

import html
import sys
import types
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.pronote.normalize import (
    CONTROLE_RE,
    RawBundle,
    attachment_names,
    build_items,
    clean_text,
    content_key,
    lesson_slot,
    make_item,
    short_title,
    subject_name,
)

AUJOURDHUI = date(2026, 10, 17)  # un samedi


# ─────────────────────────────── clean_text ───────────────────────────────


def test_clean_text_vide_ou_none():
    """Falsy : chaîne vide, `None`, `0`, `False` → chaîne vide."""
    assert clean_text("") == ""
    assert clean_text(None) == ""
    assert clean_text(0) == ""
    assert clean_text(False) == ""
    assert clean_text("   \n\t ") == ""


def test_clean_text_balise_simple_dechets_avec_une_espace():
    """Sans `pronotepy.Util`, le repli remplace chaque balise par une espace."""
    assert clean_text("<b>Maths</b>") == "Maths"
    assert clean_text("<p>un</p><p>deux</p>") == "un deux"


def test_clean_text_entites_html_decodees():
    """Les entités HTML sont décodées grâce à `html.unescape`.

    Le fix remplace l'import fabriqué `pronotepy.Util` par l'appel standard
    `html.unescape`, garantissant que `&amp;`, `&nbsp;`, `&#233;` etc. sont
    décodés avant d'être renvoyés à Telegram.
    """
    assert clean_text("<b>A&amp;B</b>") == "A&B"
    assert clean_text("&nbsp;c&#233;") == "cé"
    assert clean_text("&#x41;&quot;&lt;") == 'A"<'


def test_clean_text_accentue_et_ligature_preserves_tels_quel():
    """CARACTÉRISATION : `clean_text` ne plie ni accents ni ligatures."""
    assert clean_text("Œuvre ŒUVRE à côté") == "Œuvre ŒUVRE à côté"
    assert clean_text("élève") == "élève"


def test_clean_text_espaces_insecables_compactes():
    """U+00A0, U+2007 et U+3000 tombent dans `\\s+` : une seule espace ASCII."""
    assert clean_text("a b c　d") == "a b c d"


def test_clean_text_conserve_les_caracteres_de_controle():
    """CARACTÉRISATION : `\\x00` et `\\x07` ne sont ni supprimés ni échappés.

    Ils traversent `clean_text` tels quels ; seuls les blancs (`\\t`, `\\n`) sont
    transformés en espaces. Un `\x00` finit donc dans le HTML envoyé à Telegram.
    """
    assert clean_text("a\x00b\x07c\td\n") == "a\x00b\x07c d"


def test_clean_text_accepte_un_non_chaine():
    """Un objet non textuel est rendu via `str()`, pas rejeté."""
    assert clean_text(123) == "123"
    assert clean_text([1, 2]) == "[1, 2]"


def test_clean_text_texte_tres_long_reste_fini():
    """5 000 mots sont compactés sans troncature (espace finale coupée)."""
    corps = "phrase " * 5_000
    assert clean_text(corps) == "phrase " * 4_999 + "phrase"
    assert len(clean_text(corps)) == 34_999


# ─────────────────────────────── short_title ───────────────────────────────


def test_short_title_premiere_phrase_seule():
    """La coupure exige un blanc APRÈS la ponctuation."""
    assert short_title("Première phrase. Deuxième phrase.") == "Première phrase."
    assert short_title("Interro ? avec suite") == "Interro ?"
    assert short_title("Interro ?collé") == "Interro ?collé"
    assert short_title("") == ""


def test_short_title_troncature_sur_mot_complet():
    """La coupe retombe au dernier espace : jamais au milieu d'un mot."""
    assert short_title("abcde.", 3) == "abc…"
    assert short_title("a" * 91) == "a" * 90 + "…"
    assert len(short_title("phrase " * 5_000)) == 84  # 83 caractères + « … »


def test_short_title_texte_deja_court_inchange():
    assert short_title("abc", 90) == "abc"


# ─────────────────────────────── content_key ───────────────────────────────


def _key(body: str, **kw) -> str:
    base = {
        "kind": "devoir",
        "subject": "Maths",
        "body": body,
        "due_date": None,
        "attachments": [],
    }
    base.update(kw)
    return content_key(**base)  # type: ignore[arg-type]


def test_content_key_insensible_a_la_casse_et_aux_espaces():
    """Le corps est minusculisé et compacté AVANT le hash."""
    assert _key("DM  numéro  3") == _key("dm   numéro   3")
    assert _key("DM  numéro  3") == _key("DM Numéro 3")
    assert _key("DM numéro 3") != _key("DM numéro 4")


def test_content_key_ne_plie_pas_les_accents_ni_les_ligatures():
    """CARACTÉRISATION : `Évaluation` et `evaluation` donnent deux clés.

    Le pliage d'accents n'existe que dans `app.main._fold`, jamais ici : si
    PRONOTE rend la même ligne différemment d'une session à l'autre, l'élève
    reçoit un nouvel item au lieu d'une mise à jour.
    """
    assert _key("Évaluation") != _key("evaluation")
    assert _key("copier œuvre") != _key("coper oeuvre")
    assert _key("CŒUR") != _key("coeur")


def test_content_key_insensible_a_lordre_des_pieces_jointes():
    """Les noms sont triés : l'ordre d'arrivée ne change pas la clé."""
    assert _key("x", attachments=["a.pdf", "b.pdf"]) == _key("x", attachments=["b.pdf", "a.pdf"])
    assert _key("x", attachments=["a.pdf"]) != _key("x", attachments=[])
    assert _key("x", attachments=["a.pdf"]) != _key("x", attachments=["a.pdf", "b.pdf"])


def test_content_key_champs_structurants_separent_les_items():
    """Kind, matière, date et pièces jointes discriminent."""
    assert _key("x") != _key("x", kind="cours")
    assert _key("x") != _key("x", subject="SVT")
    assert _key("x") != _key("x", due_date=AUJOURDHUI)


# ─────────────────────────────── make_item ─────────────────────────────────


def test_make_item_champs_vides_bouches():
    """Sujet vide ou `None` reçoit son repli ; le titre est dérivé du corps."""
    item = make_item(kind="devoir", source="s", subject="", body="x", due_date=None)
    assert item.subject == "(matière inconnue)"
    assert item.title == "x"

    vide = make_item(
        kind="devoir", source="s", subject=None, body="", due_date=None  # type: ignore[arg-type]
    )
    assert vide.subject == "(matière inconnue)"
    assert vide.title == "(sans titre)"
    assert vide.body == ""


def test_make_item_filtre_les_pieces_jointes_vides():
    """`""` et `None` disparaissent, l'ordre des survivantes est préservé."""
    item = make_item(
        kind="devoir",
        source="s",
        subject="Maths",
        body="x",
        due_date=None,
        attachments=["", None, "a.pdf", "b.png"],  # type: ignore[list-item]
    )
    assert item.attachments == ["a.pdf", "b.png"]
    vide = make_item(
        kind="devoir", source="s", subject="M", body="x", due_date=None, attachments=None
    )
    assert vide.attachments == []


def test_make_item_titre_explicite_gagne_sur_short_title():
    item = make_item(
        kind="cours",
        source="s",
        subject="SVT",
        body="long corps. suite",
        due_date=None,
        title="Titre donné",
    )
    assert item.title == "Titre donné"


# ─────────────────────── subject_name / attachment_names ──────────────────


def test_subject_name_objets_varies():
    """`subject.name` d'abord, puis `subject` brut, puis le repli."""
    assert subject_name(SimpleNamespace(subject=SimpleNamespace(name="  Maths  "))) == "Maths"
    assert subject_name(SimpleNamespace(subject="Histoire-Géographie")) == "Histoire-Géographie"
    assert subject_name(SimpleNamespace(subject=None)) == "(matière inconnue)"
    assert subject_name(object()) == "(matière inconnue)"


def test_subject_name_objet_sans_nom_renvoie_son_repr():
    """CARACTÉRISATION : un `subject` objet dont `.name` est vide est rendu en
    `str()` — la matière affichée devient la représentation Python de l'objet."""
    vide = SimpleNamespace(name="")
    assert subject_name(SimpleNamespace(subject=vide)) == str(vide)


# ─────────────────────────────── lesson_slot ───────────────────────────────


def test_lesson_slot_horodatage_ou_point_d_interrogation():
    """Créneau = « horodatage|matière », sinon `?` pour l'horodatage."""
    cours = SimpleNamespace(
        start=datetime(2026, 10, 14, 8, 0), subject=SimpleNamespace(name="HG")
    )
    assert lesson_slot(cours) == "2026-10-14 08:00|HG"
    assert lesson_slot(SimpleNamespace(start=None, subject=SimpleNamespace(name="HG"))) == "?|HG"
    assert lesson_slot(SimpleNamespace(start=None)) == "?|(matière inconnue)"


# ─────────────────────────────── build_items ───────────────────────────────


def _bundle(**kw) -> RawBundle:
    return RawBundle(today=AUJOURDHUI, **kw)


def _devoir(description: str, **kw):
    base = {
        "description": description,
        "date": None,
        "subject": SimpleNamespace(name="Maths"),
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_build_items_devoir_sans_description_ignore():
    """Corps vide ou blanc : aucun item, pas de `key` fantôme."""
    items = build_items(
        None,
        _bundle(
            homework=[
                _devoir("Exo 1", date=AUJOURDHUI),
                _devoir("", date=AUJOURDHUI),
                _devoir("   ", date=AUJOURDHUI, subject=None),
            ]
        ),
    )
    assert [i.body for i in items] == ["Exo 1"]


def test_build_items_deduplique_les_doublons_stricts():
    """Deux devoirs identiques → un seul item."""
    items = build_items(
        None,
        _bundle(
            homework=[
                _devoir("DM numéro 3", date=date(2026, 10, 20)),
                _devoir("DM numéro 3", date=date(2026, 10, 20)),
            ]
        ),
    )
    assert len(items) == 1
    assert items[0].due_date == date(2026, 10, 20)


def test_build_items_deduplique_meme_si_les_libelles_de_casse_varient():
    """Le corps compacté/minusculisé rend « DF 3 » et « df   3 » identiques."""
    items = build_items(
        None,
        _bundle(
            homework=[
                _devoir("DF 3", date=AUJOURDHUI),
                _devoir("df   3", date=AUJOURDHUI),
            ]
        ),
    )
    assert len(items) == 1


def test_build_items_date_structuree_prime_sur_le_texte():
    """Date PRONOTE > date écrite dans le corps."""
    items = build_items(
        None, _bundle(homework=[_devoir("Exo pour le 21 octobre", date=date(2026, 10, 19))])
    )
    assert items[0].due_date == date(2026, 10, 19)


def test_build_items_formats_de_dates_francaises_reconnus_ou_non():
    """Les formats réellement supportés par `resolve_due`, et les autres.

    Seuls « jour + mois en français » et le nom de jour fonctionnent ; les
    formats numériques (`5/10`, `05-10-2026`) sont ignorés, ce qui laisse
    l'item sans date.
    """
    cas = [
        ("à rendre le 21 octobre", date(2026, 10, 21)),
        ("le 21 octobre 2026", date(2026, 10, 21)),
        ("21 OCTOBRE", date(2026, 10, 21)),
        ("à faire vendredi", date(2026, 10, 23)),
        ("le 5/10", None),
        ("pour le 21/10/2026", None),
        ("05-10-2026", None),
    ]
    items = build_items(None, _bundle(homework=[_devoir(texte) for texte, _ in cas]))
    assert [i.due_date for i in items] == [attendu for _, attendu in cas]


def test_build_items_cours_et_controle_pour_le_meme_contenu():
    """Un cours « Évaluation » produit DEUX items : `cours` et `controle`."""
    lesson = SimpleNamespace(
        start=datetime(2026, 10, 14, 8, 0),
        canceled=False,
        subject=SimpleNamespace(name="Physique"),
    )
    raw = _bundle(
        lessons=[lesson],
        contents={
            "2026-10-14 08:00|Physique": SimpleNamespace(
                title="Évaluation sur le son",
                description="<b>Chapitre 3</b>",
                files=[SimpleNamespace(name="son.pdf")],
            )
        },
    )
    items = build_items(None, raw)
    assert sorted(i.kind for i in items) == ["controle", "cours"]
    cours = next(i for i in items if i.kind == "cours")
    assert cours.body == "Évaluation sur le son Chapitre 3"
    assert cours.title == "Évaluation sur le son"
    assert cours.due_date == date(2026, 10, 14)
    assert cours.attachments == ["son.pdf"]


def test_build_items_ignore_les_cours_annules_et_sans_contenu():
    """Cours annulé : rien. Cours sans contenu : rien. Seul le cours rempli sort."""

    def lesson(**kw):
        return SimpleNamespace(subject=SimpleNamespace(name="HG"), **kw)

    items = build_items(
        None,
        _bundle(
            lessons=[
                lesson(start=datetime(2026, 10, 15, 8, 0), canceled=True),
                lesson(start=datetime(2026, 10, 16, 8, 0), canceled=False),
                lesson(start=datetime(2026, 10, 17, 8, 0), canceled=False),
            ],
            contents={"2026-10-17 08:00|HG": SimpleNamespace(title="Cours 1", description="")},
        ),
    )
    assert len(items) == 1
    assert items[0].due_date == AUJOURDHUI


def test_build_items_controle_reconnait_les_variantes():
    """Le motif de contrôle : accents, sans-accents, abréviations."""
    assert CONTROLE_RE.search("Contrôle de maths")
    assert CONTROLE_RE.search("evaluation demain")
    assert CONTROLE_RE.search("DS de physique")
    assert CONTROLE_RE.search("DM noté à rendre")
    assert not CONTROLE_RE.search("simple correction de Devoirs")


def test_build_items_information_avec_contenu_casse():
    """`content()` qui lève est avalé ; seul le titre reste."""

    def casse() -> str:
        raise RuntimeError("page fermée")

    items = build_items(
        None,
        _bundle(
            infos=[SimpleNamespace(title="Conseil", content=casse, category=None)]
        ),
    )
    assert [(i.subject, i.body) for i in items] == [("Vie du lycée", "Conseil")]


def test_build_items_information_vide_ignore():
    """Titre vide + contenu vide : rien. Catégorie presente ou non, peu importe."""
    items = build_items(
        None,
        _bundle(
            infos=[
                SimpleNamespace(title="", content=lambda: "", category=SimpleNamespace(name="SVT")),
                SimpleNamespace(title="   ", content=lambda: "texte", category=None),
            ]
        ),
    )
    # «   . texte » se réduit à « texte » : seul le second cas produit un item.
    assert [i.body for i in items] == ["texte"]


def test_build_items_information_construit_son_titre_avec_un_point():
    """`title` + `content` sont joints par un point, les points de bord partent."""
    items = build_items(
        None,
        _bundle(
            infos=[
                SimpleNamespace(
                    title="Conseil de classe",
                    content=lambda: "Le <b>16/10</b> à 17h.",
                    category=SimpleNamespace(name="Vie du lycée"),
                    attachments=[SimpleNamespace(name="note.pdf")],
                )
            ]
        ),
    )
    assert items[0].body == "Conseil de classe. Le 16/10 à 17h"
    assert items[0].title == "Conseil de classe"
    assert items[0].attachments == ["note.pdf"]


def test_build_items_categorie_objet_vide_renvoie_son_repr():
    """CARACTÉRISATION : une catégorie `namespace(name='')` finit rendue en `str()`.

    `getattr(category, "name", "") or category` retombe sur l'OBJET, dont la
    représentation Python (« namespace(name='') ») devient la matière.
    """
    items = build_items(
        None,
        _bundle(
            infos=[
                SimpleNamespace(title="Solo", content="texte", category=SimpleNamespace(name=""))
            ]
        ),
    )
    assert items[0].subject == "namespace(name='')"


def test_build_items_messagerie_extrait_avant_de_composer():
    """Extraction LLM présente → item `devoir` (matière extraite) ; sinon `message`."""
    items = build_items(
        None,
        _bundle(
            discussions=[
                ("Mme Martin", lambda: "Merci pour le DM de lundi"),
                ("M. Petit", lambda: "Sujet sans réponse exploitable"),
            ],
            extracted={
                "M. Petit": {"detail": "Fiche 3 pour le 22 octobre", "matiere": "SVT", "due": None}
            },
        ),
    )
    par_cle = {(i.kind, i.subject): i for i in items}
    extrait = par_cle[("devoir", "SVT")]
    assert extrait.due_date == date(2026, 10, 22)
    assert extrait.body == "Fiche 3 pour le 22 octobre"
    message = par_cle[("message", "Mme Martin")]
    assert message.title == "Message — Mme Martin"
    assert message.body == "Merci pour le DM de lundi"


def test_build_items_extraction_vide_retombe_sur_le_message():
    """Extraction sans détail → le message composé est utilisé, sans date du LLM."""
    items = build_items(
        None,
        _bundle(
            discussions=[("M. Petit", lambda: "bonjour")],
            extracted={"M. Petit": {"detail": "", "matiere": "SVT", "due": "2026-10-22"}},
        ),
    )
    assert [(i.kind, i.subject, i.due_date) for i in items] == [("message", "M. Petit", None)]


def test_build_items_messagerie_composee_vide_ignore():
    """Sans détail extrait et sans texte composé : rien."""
    items = build_items(
        None,
        _bundle(
            discussions=[("Mme Martin", lambda: ""), ("M. Petit", lambda: "   ")],
            extracted={"M. Petit": {"detail": None}},
        ),
    )
    assert items == []


def test_build_items_extraction_sans_matiere_retombe_sur_le_sujet():
    """`matiere` absente → le sujet de la discussion fait foi."""
    items = build_items(
        None,
        _bundle(
            discussions=[("M. Petit", lambda: "x")],
            extracted={"M. Petit": {"detail": "à faire", "matiere": "", "due": None}},
        ),
    )
    assert items[0].subject == "M. Petit"


def test_build_items_discussion_a_trois_morceaux_leve():
    """CARACTÉRISATION : le tuple de discussion doit faire EXACTEMENT deux
    éléments. Le commentaire du `RawBundle` en annonce deux (« sujet,
    composer ») ; un trois-tuple fait planter la normalisation."""

    def jamais_appele() -> str:
        raise AssertionError("le compositeur ne doit pas être appelé")

    with pytest.raises(ValueError):
        build_items(
            None, _bundle(discussions=[("Mme Martin", jamais_appele, "extra")])  # type: ignore[misc]
        )


def test_build_items_bundle_vide_renvoie_liste_vide():
    """Aucun champ peuplé : liste vide, pas d'exception."""
    assert build_items(None, RawBundle()) == []


def test_build_items_sans_today_ne_suppose_aucune_date():
    """`today=None` : un devoir sans date structurelle et sans date en clair
    reste sans date — la date réelle du jour ne fabrique rien."""
    items = build_items(None, RawBundle(homework=[_devoir("Exo 1")]))
    assert items[0].due_date is None