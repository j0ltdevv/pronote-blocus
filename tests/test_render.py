"""Rendu Telegram et normalisation : bornes, échappement, numérotation.

Tout ce qui part vers Telegram passe par `esc` puis `chunk`. Un décalage ici
rend `/fait N` faux ou fait rejeter le message par l'API (entité coupée,
balise non refermée), donc les bornes et l'équilibre des balises sont testés
au même endroit que le format des lignes.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from app.pronote.normalize import (
    Item,
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
from app.pronote.ranking import Row, group_cours, split_sections
from app.render import (
    MESSAGE_LIMIT,
    SUITE,
    _cours_label,
    chunk,
    compose,
    due_label,
    esc,
    format_cours,
    format_row,
    numbered_lines,
    paginate,
)

TODAY = date(2026, 10, 17)
ENTITY_RE = re.compile(r"&(?:#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);")
TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>")


# ─────────────────────────── utilitaires ───────────────────────────


def _item(**overrides) -> Item:
    base = dict(
        key="cle",
        source="travail_a_faire",
        subject="Mathématiques",
        title="titre",
        body="corps du devoir",
        due_date=TODAY,
        kind="devoir",
        attachments=[],
    )
    base.update(overrides)
    return Item(**base)


def _row(item: Item | None = None, **overrides) -> Row:
    base = dict(summary="résumé", action="Refaire les exercices", effort="moyen")
    base.update(overrides)
    return Row(item=item or _item(), **base)


class _Balance(HTMLParser):
    """Vérifie qu'un morceau ne laisse aucune balise ouverte."""

    VOID = {"br", "hr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.mismatch: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.mismatch = f"</{tag}> inattendu (pile={self.stack})"
            return
        self.stack.pop()


def _assert_balanced(part: str) -> None:
    parser = _Balance()
    parser.feed(part)
    parser.close()
    assert parser.mismatch is None, parser.mismatch
    assert parser.stack == [], f"balises non refermées : {parser.stack}"


def _assert_intact_entities(part: str) -> None:
    for match in re.finditer(r"&", part):
        assert ENTITY_RE.match(part, match.start()), (
            f"entité HTML coupée dans {part[match.start():match.start() + 12]!r}"
        )


def _assert_parts(parts: list[str], limit: int) -> None:
    assert parts, "aucun morceau rendu"
    for part in parts:
        assert len(part) <= limit, f"morceau de {len(part)} > {limit}"
        _assert_balanced(part)
        _assert_intact_entities(part)
    for part in parts[:-1]:
        assert part.endswith(SUITE), "marque de suite absente"
    assert not parts[-1].endswith(SUITE), "marque de suite sur le dernier morceau"


# ─────────────────────────── esc ───────────────────────────


def test_esc_echappe_les_trois_caracteres_html():
    assert esc("<b>") == "&lt;b&gt;"
    assert esc("a & b") == "a &amp; b"
    assert esc("1 < 2 > 0") == "1 &lt; 2 &gt; 0"


def test_esc_ne_touche_pas_les_guillemets():
    """parse_mode HTML de Telegram n'exige pas d'échapper les guillemets."""
    assert esc('il a dit "oui"') == 'il a dit "oui"'
    assert esc("l'élève") == "l'élève"


def test_esc_accepte_un_objet_quelconque():
    assert esc(42) == "42"
    assert esc(None) == "None"


# ─────────────────────────── chunk ───────────────────────────


def test_chunk_vide_rend_une_liste_vide():
    assert chunk("") == []
    assert chunk("   \n  ") == []
    assert chunk(None) == []


def test_chunk_texte_court_reste_intact():
    assert chunk("bonjour") == ["bonjour"]
    assert chunk("deux\nlignes") == ["deux\nlignes"]


def test_chunk_respecte_la_limite_et_marque_la_suite():
    text = "\n".join(f"Ligne {i:03d} " + "x" * 60 for i in range(100))

    parts = chunk(text, 300)

    _assert_parts(parts, 300)
    assert len(parts) > 1
    rebuilt = "\n".join(
        part[: -len(SUITE)] if part.endswith(SUITE) else part for part in parts
    )
    assert rebuilt == text, "le texte doit se reconstituer exactement"


def test_chunk_coupe_sur_les_sauts_de_ligne_quand_c_est_possible():
    lines = [f"Ligne {i:03d} " + "x" * 40 for i in range(30)]

    parts = chunk("\n".join(lines), 200)

    _assert_parts(parts, 200)
    seen = "\n".join(parts)
    for line in lines:
        assert line in seen, f"ligne coupée en deux : {line!r}"


def test_chunk_ne_coupe_jamais_une_entite_html():
    """Défaut réel : la coupe dure tombait au milieu de `&amp;`."""
    width = MESSAGE_LIMIT - len(SUITE)
    text = "a" * (width - 3) + "&amp;" + "b" * 100

    parts = chunk(text)

    _assert_parts(parts, MESSAGE_LIMIT)
    assert sum(part.count("&amp;") for part in parts) == 1
    assert "&am\n" not in parts[0]
    assert parts[0].endswith("a" + SUITE)


def test_chunk_referme_et_reouvre_les_balises():
    """Une ligne `<b>…</b>` plus longue que la limite doit rester valide."""
    line = "<b>" + "a" * 5000 + "</b>"

    parts = chunk(line, 300)

    _assert_parts(parts, 300)
    assert parts[0].startswith("<b>")
    assert parts[0].endswith("</b>" + SUITE)
    assert parts[1].startswith("<b>"), "la balise doit être réouverte"
    assert parts[-1].endswith("</b>")
    visible = "".join(re.sub(r"<[^>]+>", "", p.removesuffix(SUITE)) for p in parts)
    assert visible == "a" * 5000


def test_chunk_equilibre_les_balises_imbriquees():
    line = "<b><i>" + "z" * 900 + "</i></b>"

    parts = chunk(line, 120)

    _assert_parts(parts, 120)
    for part in parts[:-1]:
        assert part.endswith("</i></b>" + SUITE)
    for part in parts[1:]:
        assert part.startswith("<b><i>")


def test_chunk_entite_atomique_meme_avec_une_petite_limite():
    parts = chunk("a" * 30 + "&eacute;" + "b" * 30, 60)

    _assert_parts(parts, 60)
    assert sum(part.count("&eacute;") for part in parts) == 1


def test_chunk_limite_minuscule_termine_et_borne():
    parts = chunk("abcdefghij", 1)

    assert all(len(part) == 1 for part in parts)
    assert "".join(parts) == "abcdefghij"


def test_chunk_omet_la_marque_de_suite_quand_la_limite_est_trop_petite():
    parts = chunk("x" * 200, 40)

    assert all(len(part) <= 40 for part in parts)
    assert all(not part.endswith(SUITE) for part in parts)


def test_chunk_texte_echappe_reste_visible_apres_coupe():
    """Bout en bout : esc puis chunk, le texte affiché est intact."""
    text = esc("Contrôle <urgent> & devoir " * 400)

    parts = chunk(text)

    _assert_parts(parts, MESSAGE_LIMIT)
    visible = "".join(
        part.removesuffix(SUITE).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        for part in parts
    )
    assert visible.replace("\n", "").rstrip() == ("Contrôle <urgent> & devoir " * 400).rstrip()


# ─────────────────────────── dates et lignes ───────────────────────────


@pytest.mark.parametrize(
    ("due", "attendu"),
    [
        (TODAY - timedelta(days=9), "9 j de retard"),
        (TODAY - timedelta(days=1), "1 j de retard"),
        (TODAY, "aujourd'hui"),
        (TODAY + timedelta(days=1), "demain"),
        (TODAY + timedelta(days=5), "dans 5 j"),
        (None, ""),
    ],
)
def test_due_label(due, attendu):
    assert due_label(due, TODAY) == attendu


def test_format_row_devoir_simple():
    row = _row()

    assert format_row(3, row, TODAY) == (
        "3. <b>Mathématiques</b> — Refaire les exercices · aujourd'hui"
    )


def test_format_row_echappe_matiere_et_action():
    row = _row(item=_item(subject="SVT & co"), action="lire <le> chapitre")

    line = format_row(1, row, TODAY)

    assert "SVT &amp; co" in line
    assert "lire &lt;le&gt; chapitre" in line
    assert "<le>" not in line


def test_format_row_controle_et_pieces_jointes():
    row = _row(
        item=_item(kind="controle", attachments=["dm.pdf", "corrige.pdf"]),
        action="Réviser",
    )

    line = format_row(1, row, TODAY)

    assert "⚠️" in line
    assert "📎2" in line
    assert "aujourd'hui" in line


def test_format_row_sans_date_ni_controle_ni_piece_jointes():
    row = _row(item=_item(due_date=None), action="Lire")

    assert format_row(1, row, TODAY) == "1. <b>Mathématiques</b> — Lire"
    assert "⚠️" not in format_row(1, row, TODAY)


def test_format_row_retard():
    row = _row(item=_item(due_date=TODAY - timedelta(days=3)))

    assert "3 j de retard" in format_row(1, row, TODAY)


def test_numbered_lines_numerote_depuis_start():
    rows = [_row(item=_item(key=f"k{i}", subject=f"Matière {i}")) for i in range(3)]

    lines = numbered_lines(rows, TODAY, start=16)

    assert [line.split(".")[0] for line in lines] == ["16", "17", "18"]


def test_numbered_lines_liste_vide():
    assert numbered_lines([], TODAY) == []


# ─────────────────────────── pagination ───────────────────────────


def _rows(n: int) -> list[Row]:
    return [_row(item=_item(key=f"k{i}", subject=f"Matière {i}")) for i in range(n)]


def test_paginate_premiere_page_annonce_le_reste():
    visible, page, note = paginate(_rows(40), 1, per_page=15)

    assert len(visible) == 15
    assert page == 1
    assert note == "… et 25 autres, /retard 2"


def test_paginate_page_intermediaire():
    visible, page, note = paginate(_rows(40), 2, per_page=15)

    assert [row.key for row in visible] == [f"k{i}" for i in range(15, 30)]
    assert page == 2
    assert note == "… et 10 autres, /retard 3"


def test_paginate_derniere_page():
    visible, page, note = paginate(_rows(40), 3, per_page=15)

    assert len(visible) == 10
    assert page == 3
    assert note == "Page 3/3"


def test_paginate_borne_la_page_demandee():
    visible, page, note = paginate(_rows(40), 99, per_page=15)

    assert [row.key for row in visible] == [f"k{i}" for i in range(30, 40)]
    assert page == 3
    assert note == "Page 3/3"


@pytest.mark.parametrize("page", [0, -4, None])
def test_paginate_page_invalide_revient_a_un(page):
    visible, bornee, note = paginate(_rows(40), page, per_page=15)

    assert bornee == 1
    assert visible[0].key == "k0"
    assert note == "… et 25 autres, /retard 2"


def test_paginate_liste_vide():
    assert paginate([], 1, per_page=15) == ([], 1, "")


def test_paginate_per_page_zero_est_borne_a_un():
    visible, page, note = paginate(_rows(3), 1, per_page=0)

    assert len(visible) == 1
    assert page == 1
    assert note == "… et 2 autres, /retard 2"


def test_paginate_une_seule_page_sans_note():
    visible, page, note = paginate(_rows(3), 1, per_page=15)

    assert len(visible) == 3
    assert note == ""


def test_pagination_et_numerotation_sont_coherentes():
    """/fait N doit viser la ligne N de l'ordre global, pages comprises."""
    rows = _rows(40)
    visible, page, _ = paginate(rows, 2, per_page=15)
    start = (page - 1) * 15 + 1

    lines = numbered_lines(visible, TODAY, start=start)

    assert lines[0].startswith("16. ")
    assert lines[-1].startswith("30. ")
    assert [row.key for row in visible] == [f"k{i}" for i in range(15, 30)]


# ─────────────────────────── composition ───────────────────────────


def test_compose_assemble_entete_lignes_pied():
    parts = compose("<b>Titre</b>", ["1. a", "2. b"], "pied")

    assert parts == ["<b>Titre</b>\n1. a\n2. b\npied"]


def test_compose_ignore_les_lignes_vides():
    assert compose("", ["", "1. a", "", "2. b"], None) == ["1. a\n2. b"]


def test_compose_sans_rien_rend_une_liste_vide():
    assert compose("", [], None) == []
    assert compose("", [""], "") == []


def test_compose_n_ajoute_aucun_saut_superflu():
    assert compose("entête", [], "pied") == ["entête\npied"]
    assert compose("", ["ligne"], None) == ["ligne"]


def test_compose_decoupe_un_gros_message_sans_perdre_de_ligne():
    rows = _rows(300)
    lines = numbered_lines(rows, TODAY)

    parts = compose("<b>En retard</b>", lines, "/retard pour tout voir", 4096)

    _assert_parts(parts, 4096)
    assert len(parts) > 1
    for line in lines:
        assert any(line in part for part in parts), f"ligne perdue : {line!r}"
    assert parts[0].startswith("<b>En retard</b>")


def test_compose_texte_tres_long_est_borne_par_la_limite_telegram():
    lines = [f"{i}. <b>Maths</b> — " + "réviser " * 200 for i in range(1, 21)]

    parts = compose("<b>Résumé</b>", lines, "fin", limit=4096)

    _assert_parts(parts, 4096)
    assert sum(part.count("<b>Maths</b>") for part in parts) == 20


# ─────────────────────────── cours ───────────────────────────


def test_cours_label_court_reste_intact():
    assert _cours_label(_item(title="Chapitre 3")) == "Chapitre 3"


def test_cours_label_tronque_a_cent_caracteres():
    label = _cours_label(_item(title="x" * 150))

    assert len(label) <= 101
    assert label.endswith("…")
    assert label.startswith("x" * 100)


def test_cours_label_retombe_sur_le_corps_sans_titre():
    assert _cours_label(_item(title="", body="Corps du cours")) == "Corps du cours"


def _cours(key: str, subject: str, due: date | None, title: str = "cours") -> Item:
    return _item(key=key, source="cahier_de_textes", subject=subject, kind="cours", due_date=due, title=title)


def test_format_cours_plafonne_a_cinq_per_matiere():
    groups = [("Maths", [_cours(f"m{i}", "Maths", TODAY - timedelta(days=i)) for i in range(7)])]

    lines, hidden = format_cours(groups)

    assert len(lines) == 5
    assert hidden == 2


def test_format_cours_trie_du_plus_recent_au_plus_ancien():
    groups = [(
        "Maths",
        [
            _cours("vieux", "Maths", TODAY - timedelta(days=10)),
            _cours("recent", "Maths", TODAY),
            _cours("moyen", "Maths", TODAY - timedelta(days=3)),
        ],
    )]

    lines, hidden = format_cours(groups)

    assert hidden == 0
    assert "2026-10-17" in lines[0]
    assert "2026-10-14" in lines[1]
    assert "2026-10-07" in lines[2]


def test_format_cours_sans_date():
    lines, _ = format_cours([("Anglais", [_cours("a", "Anglais", None)])])

    assert "sans date" in lines[0]
    assert lines[0].endswith("sans date")


def test_format_cours_echappe_matiere_et_titre():
    item = _cours("x", "SVT & co", TODAY, title="<script> & suite")

    lines, _ = format_cours([("SVT & co", [item])])

    assert "<script>" not in lines[0]
    assert "&lt;script&gt;" in lines[0]
    assert "SVT &amp; co" in lines[0]


def test_format_cours_liste_vide():
    assert format_cours([]) == ([], 0)


def test_format_cours_suit_l_ordre_des_matieres_prioritaires():
    items = [
        _cours("h", "Histoire", TODAY),
        _cours("m", "Maths", TODAY),
        _item(key="d", subject="Maths", kind="devoir", due_date=TODAY),
    ]

    groups = group_cours(items, {"Maths": 0, "Histoire": 1})
    lines, hidden = format_cours(groups)

    assert [subject for subject, _ in groups] == ["Maths", "Histoire"]
    assert lines[0].startswith("<b>Maths</b>")
    assert lines[1].startswith("<b>Histoire</b>")
    assert hidden == 0


# ─────────────────────────── tri et numérotation ───────────────────────────


def test_retard_avant_a_venir_et_matieres_prioritaires_en_tete():
    items = [
        _item(key="up-loin", subject="Anglais", due_date=TODAY + timedelta(days=5)),
        _item(key="retard-vieux", subject="Anglais", due_date=TODAY - timedelta(days=9)),
        _item(key="up-prio", subject="Maths", due_date=TODAY + timedelta(days=5)),
        _item(key="retard-prio", subject="Maths", due_date=TODAY - timedelta(days=2)),
        _item(key="retard-urgent", subject="SVT", due_date=TODAY - timedelta(days=9)),
    ]

    overdue, upcoming = split_sections(items, {}, {"Maths": 0, "SVT": 1}, TODAY)
    rows = list(overdue) + list(upcoming)
    lines = numbered_lines(rows, TODAY)

    assert [row.key for row in overdue] == ["retard-urgent", "retard-vieux", "retard-prio"]
    assert [row.key for row in upcoming] == ["up-prio", "up-loin"]
    assert [line.split(".")[0] for line in lines] == ["1", "2", "3", "4", "5"]
    assert "retard" in lines[0] and "9 j" in lines[0]
    assert "retard" in lines[1]
    assert "/fait" not in "\n".join(lines), "le rendu ne doit pas inventer de commande"


# ─────────────────────────── normalisation ───────────────────────────


def test_clean_text_retire_les_balises_et_decode_les_entites():
    assert clean_text("<b>Contrôle</b> &amp; DM") == "Contrôle & DM"
    assert clean_text("caf&eacute; &#233;t&eacute;") == "café été"


def test_clean_text_compacte_les_espaces_et_preserve_les_accents():
    assert clean_text("  Réviser\n\n  la leçon\t3  ") == "Réviser la leçon 3"
    assert clean_text("a&nbsp;&nbsp;b") == "a b"


def test_clean_text_decode_les_chevrons_echappes():
    assert clean_text("1 &lt; 2 et 3 &gt; 2") == "1 < 2 et 3 > 2"


def test_clean_text_entree_vide():
    assert clean_text("") == ""
    assert clean_text(None) == ""


def test_short_title_premiere_phrase():
    assert short_title("Relire le chapitre. Puis les exercices.") == "Relire le chapitre."


def test_short_title_tronque_sur_un_mot():
    title = short_title("mot " * 40, limit=20)

    assert title.endswith("…")
    assert len(title) <= 21
    assert "  " not in title


def test_short_title_long_mot_sans_espace():
    title = short_title("x" * 200, limit=20)

    assert title == "x" * 20 + "…"


def test_short_title_ne_coupe_pas_une_phrase_assez_courte():
    assert short_title("Court", limit=90) == "Court"
    assert short_title("", limit=90) == ""


def test_content_key_est_stable_et_sensible_aux_champs():
    base = dict(kind="devoir", subject="Maths", body="Relire", due_date=TODAY, attachments=[])

    assert content_key(**base) == content_key(**base)
    assert len(content_key(**base)) == 64
    assert content_key(**base) != content_key(**{**base, "kind": "controle"})
    assert content_key(**base) != content_key(**{**base, "subject": "SVT"})
    assert content_key(**base) != content_key(**{**base, "body": "Autre"})
    assert content_key(**base) != content_key(**{**base, "due_date": None})
    assert content_key(**base) != content_key(**{**base, "attachments": ["dm.pdf"]})


def test_content_key_ignore_casse_espaces_et_ordre_des_pieces_jointes():
    first = content_key(
        kind="devoir",
        subject="Maths",
        body="  RELIRE   le cours ",
        due_date=TODAY,
        attachments=["b.pdf", "a.pdf"],
    )
    second = content_key(
        kind="devoir",
        subject="Maths",
        body="relire le cours",
        due_date=TODAY,
        attachments=["a.pdf", "b.pdf"],
    )

    assert first == second


def test_make_item_applique_les_fallbacks():
    item = make_item(
        kind="info", source="informations", subject="", body="", due_date=None
    )

    assert item.subject == "(matière inconnue)"
    assert item.title == "(sans titre)"
    assert item.key == content_key(
        kind="info", subject="(matière inconnue)", body="", due_date=None, attachments=[]
    )


def test_make_item_filtre_les_pieces_jointes_vides_et_respecte_le_titre():
    item = make_item(
        kind="devoir",
        source="travail_a_faire",
        subject="Maths",
        body="Relire le chapitre 3",
        due_date=TODAY,
        attachments=["", "dm.pdf", None],
        title="Titre imposé",
    )

    assert item.attachments == ["dm.pdf"]
    assert item.title == "Titre imposé"


def test_subject_name_gere_objet_chaine_et_absence():
    assert subject_name(SimpleNamespace(subject=SimpleNamespace(name="Maths"))) == "Maths"
    assert subject_name(SimpleNamespace(subject="Histoire")) == "Histoire"
    assert subject_name(SimpleNamespace(subject=None)) == "(matière inconnue)"
    assert subject_name(SimpleNamespace()) == "(matière inconnue)"


def test_attachment_names_ignore_les_sans_nom():
    files = [SimpleNamespace(name="dm.pdf"), SimpleNamespace(name=""), SimpleNamespace()]

    assert attachment_names(files) == ["dm.pdf"]
    assert attachment_names(None) == []


def test_lesson_slot_formate_horodatage_et_matiere():
    lesson = SimpleNamespace(
        start=datetime(2026, 10, 17, 8, 30), subject=SimpleNamespace(name="Maths")
    )

    assert lesson_slot(lesson) == "2026-10-17 08:30|Maths"
    assert lesson_slot(SimpleNamespace(subject=SimpleNamespace(name="SVT"))) == "?|SVT"


# ─────────────────────────── build_items ───────────────────────────


def _homework(subject: str, description: str, due: date | None = None, files=None):
    return SimpleNamespace(
        subject=SimpleNamespace(name=subject),
        description=description,
        date=due,
        files=files or [],
    )


def _lesson(subject: str, start: datetime, canceled: bool = False):
    return SimpleNamespace(
        subject=SimpleNamespace(name=subject), start=start, canceled=canceled
    )


def _content(title: str = "", description: str = "", files=None):
    return SimpleNamespace(title=title, description=description, files=files or [])


def test_build_items_devoirs_normalises_et_dedoublonnes():
    raw = RawBundle(
        homework=[
            _homework("Maths", "<b>Relire</b> le chapitre &amp; les exercices", TODAY),
            _homework("Maths", "Relire le chapitre & les exercices", TODAY),
            _homework("SVT", "", TODAY),
        ],
        today=TODAY,
    )

    items = build_items(None, raw)

    assert len(items) == 1, "même contenu = même clé, doublon ignoré"
    assert items[0].body == "Relire le chapitre & les exercices"
    assert items[0].subject == "Maths"
    assert items[0].source == "travail_a_faire"


def test_build_items_cours_et_controle_depuis_le_cahier_de_textes():
    lesson = _lesson("Maths", datetime(2026, 10, 17, 8, 30))
    content = _content(
        title="DS n°3",
        description="Contrôle sur les suites",
        files=[SimpleNamespace(name="sujet.pdf")],
    )
    raw = RawBundle(
        lessons=[lesson], contents={lesson_slot(lesson): content}, today=TODAY
    )

    items = build_items(None, raw)
    kinds = sorted(item.kind for item in items)

    assert kinds == ["controle", "cours"]
    controle = next(item for item in items if item.kind == "controle")
    assert controle.due_date == TODAY, "la date du cours sert de date au contrôle"
    assert controle.attachments == ["sujet.pdf"]
    assert controle.title == "DS n°3"


def test_build_items_ignore_cours_annule_ou_sans_contenu():
    annule = _lesson("Maths", datetime(2026, 10, 17, 8, 30), canceled=True)
    vide = _lesson("SVT", datetime(2026, 10, 17, 9, 30))
    raw = RawBundle(
        lessons=[annule, vide],
        contents={lesson_slot(annule): _content(title="", description="Annulé")},
        today=TODAY,
    )

    assert build_items(None, raw) == []


def test_build_items_informations_avec_categorie_et_date():
    info = SimpleNamespace(
        title="Sortie",
        content=lambda: "<p>Autorisation à rendre</p>",
        category=SimpleNamespace(name="Vie du lycée"),
        attachments=[SimpleNamespace(name="autorisation.pdf")],
    )
    raw = RawBundle(infos=[info], today=TODAY)

    items = build_items(None, raw)

    assert len(items) == 1
    assert items[0].kind == "info"
    assert items[0].subject == "Vie du lycée"
    assert items[0].body == "Sortie. Autorisation à rendre"
    assert items[0].attachments == ["autorisation.pdf"]


def test_build_items_information_sans_categorie():
    info = SimpleNamespace(
        title="Photo de classe", content=lambda: "Mardi matin", category=None, attachments=[]
    )

    items = build_items(None, RawBundle(infos=[info], today=TODAY))

    assert items[0].subject == "Vie du lycée"


def test_build_items_messagerie_utilise_l_extraction():
    raw = RawBundle(
        discussions=[("Sortie", lambda: "corps du message")],
        extracted={"Sortie": {"detail": "Rendre l'autorisation", "matiere": "Vie scolaire"}},
        today=TODAY,
    )

    items = build_items(None, raw)

    assert len(items) == 1
    assert items[0].kind == "devoir"
    assert items[0].subject == "Vie scolaire"
    assert items[0].body == "Rendre l'autorisation"


def test_build_items_messagerie_sans_extraction_garde_le_message():
    raw = RawBundle(discussions=[("Sortie", lambda: "corps du message")], today=TODAY)

    items = build_items(None, raw)

    assert len(items) == 1
    assert items[0].kind == "message"
    assert items[0].title == "Message — Sortie"
    assert items[0].body == "corps du message"


def test_build_items_messagerie_vide_est_ignoree():
    raw = RawBundle(
        discussions=[("Sortie", lambda: "   "), ("Autre", lambda: "")], today=TODAY
    )

    assert build_items(None, raw) == []
