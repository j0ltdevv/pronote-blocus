"""Démo hors Telegram, sans réseau : `python -m app.dev_demo`.

Seule la connexion PRONOTE est simulée ; la normalisation, le classement et
l'affichage sont les VRAIS. La date du jour est figée pour que les sorties
soient déterministes.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

from app import dev_demo

AUJOURDHUI = datetime.date(2026, 10, 17)


# ─────────────────────────── données de test ───────────────────────────


def matiere(nom: str) -> SimpleNamespace:
    return SimpleNamespace(name=nom)


def devoir(nom: str, corps: str, pour: datetime.date | None) -> SimpleNamespace:
    return SimpleNamespace(subject=matiere(nom), description=corps, date=pour, files=[])


class Lecon:
    """Cours du cahier de textes ; `contenu` peut être une exception."""

    def __init__(self, nom, start=None, annule=False, contenu="Résumé du cours."):
        self.subject = matiere(nom) if nom is not None else None
        self.start = start or datetime.datetime(2026, 10, 16, 8, 0)
        self.canceled = annule
        self._contenu = contenu

    @property
    def content(self):  # noqa: ANN202
        if isinstance(self._contenu, Exception):
            raise self._contenu
        return SimpleNamespace(title="", description=self._contenu, files=[])


def cours(nom: str, contenu="Résumé du cours.", **kwargs) -> Lecon:
    return Lecon(nom, contenu=contenu, **kwargs)


def info(titre: str, corps: str, categorie: str = "Vie du lycée") -> SimpleNamespace:
    obj = SimpleNamespace(title=titre, category=matiere(categorie), attachments=[])
    obj.content = lambda: corps
    return obj


class FauxClient:
    """Client PRONOTE minimal : une source = une méthode."""

    def __init__(self, *, hw=(), lessons=(), infos=()) -> None:
        self._hw, self._lessons, self._infos = list(hw), list(lessons), list(infos)
        self.appels: list[tuple] = []

    def homework(self, start, end):  # noqa: ANN202
        self.appels.append(("homework", start, end))
        return iter(self._hw)

    def lessons(self, start, end):  # noqa: ANN202
        self.appels.append(("lessons", start, end))
        return iter(self._lessons)

    def information_and_surveys(self, start, end, only_unread=False):  # noqa: ANN202
        self.appels.append(("informations", start, end, only_unread))
        return iter(self._infos)


# ─────────────────────────── montage ───────────────────────────


@pytest.fixture(autouse=True)
def jour_fige(monkeypatch):
    """Fige `date.today()` dans le module démo (le reste du code est réel)."""
    fige = type("Figee", (datetime.date,), {"today": classmethod(lambda cls: AUJOURDHUI)})
    monkeypatch.setattr(dev_demo, "date", fige)
    return AUJOURDHUI


@pytest.fixture
def demo(monkeypatch):
    """Branche la connexion sur un faux client ; expose les arguments d'appel."""
    state = SimpleNamespace(client=None, erreur=None, args=[])

    def login(url, username, password, app_uuid=None, **kwargs):  # noqa: ANN001
        state.args.append((url, username, password, app_uuid))
        if state.erreur is not None:
            raise state.erreur
        return state.client, {}

    monkeypatch.setattr(dev_demo.auth, "login_with_password", login)
    monkeypatch.delenv("WINDOW_START", raising=False)
    return state


def lancer(demo, capsys, **client_kwargs) -> tuple[int, str]:
    """Lance `main()` sur un faux client, rend (code de sortie, sortie stdout)."""
    demo.client = FauxClient(**client_kwargs)
    code = dev_demo.main()
    return code, capsys.readouterr().out




# ─────────────────────────── 1. rendu des sections ───────────────────────────


def test_rendu_des_deux_sections_avec_effectifs(demo, capsys):
    """Un devoir en retard, un devoir à venir, des cours hors sections."""
    code, out = lancer(
        demo,
        capsys,
        hw=[
            devoir("Mathématiques", "Exercice 12 page 42.", datetime.date(2026, 10, 15)),
            devoir("Anglais", "Traduire le texte.", datetime.date(2026, 10, 30)),
        ],
        lessons=[
            cours("Histoire-Géographie", "La Révolution française."),
            cours("Anglais", "Le present perfect."),
        ],
    )
    assert code == 0
    assert "EN RETARD (1)" in out
    assert "À VENIR (1)" in out
    assert "[devoir] Mathématiques — 2026-10-15 · Exercice 12 page 42." in out
    assert "[devoir] Anglais — 2026-10-30 · Traduire le texte." in out
    assert "[cours]" not in out  # les cours alimenteraient /cours, pas ces sections
    assert "Cours lus : 2 · contenus lus : 2" in out
    assert "Items normalisés : 4" in out
    assert "Matières (2) : Anglais, Histoire-Géographie" in out
    assert "⚠️" not in out


def test_controle_ajoute_une_ligne_et_un_avis(demo, capsys):
    """Un cours dont le contenu parle d'évaluation produit un `controle`."""
    code, out = lancer(
        demo,
        capsys,
        lessons=[
            cours("SVT", "Contrôle sur la photosynthèse.", start=datetime.datetime(2026, 11, 2, 8))
        ],
    )
    assert code == 0
    assert "Items normalisés : 2" in out
    assert "À VENIR (1)" in out
    assert "[controle] SVT ⚠️ — 2026-11-02 · Contrôle sur la photosynthèse." in out


def test_numrotation_reinitialisee_par_section(demo, capsys):
    """Chaque section recommence à 1."""
    _, out = lancer(
        demo,
        capsys,
        hw=[
            devoir("Anglais", "A.", datetime.date(2026, 10, 1)),
            devoir("Anglais", "B.", datetime.date(2026, 10, 2)),
            devoir("Anglais", "C.", datetime.date(2026, 11, 1)),
        ],
    )
    assert "EN RETARD (2)" in out and "À VENIR (1)" in out
    assert " 1. [devoir] Anglais — 2026-10-01 · A." in out
    assert " 2. [devoir] Anglais — 2026-10-02 · B." in out
    assert " 1. [devoir] Anglais — 2026-11-01 · C." in out
    assert " 3. " not in out


def test_matiere_inconnue_exclue_du_repertoire(demo, capsys):
    """Un cours sans sujet ne pollue pas la ligne des matières."""
    code, out = lancer(demo, capsys, lessons=[cours(None, "Cours sans sujet.")])
    assert code == 0
    assert "Matières (0) : (aucune)" in out


def test_informations_non_planifiees_n_entrent_pas_en_retard(demo, capsys):
    """Une information sans date tombe dans « à venir », pas « en retard »."""
    code, out = lancer(
        demo,
        capsys,
        hw=[devoir("SVT", "Exo.", datetime.date(2026, 10, 10))],
        infos=[info("Conseil de classe", "Ordre du jour de la semaine.")],
    )
    assert code == 0
    assert "EN RETARD (1)" in out and "À VENIR (1)" in out
    assert "[info] Vie du lycée — sans date · Conseil de classe" in out


# ─────────────────────────── 2. fenêtre temporelle ───────────────────────────


def test_fenetre_par_defaut(demo, capsys, monkeypatch):
    monkeypatch.delenv("WINDOW_START", raising=False)
    _, out = lancer(
        demo, capsys, hw=[devoir("SVT", "Exo.", datetime.date(2026, 10, 10))]
    )
    assert "Fenêtre : 2026-10-01 → 2026-12-01 · aujourd'hui 2026-10-17" in out
    assert demo.client.appels[0] == (
        "homework",
        datetime.date(2026, 10, 1),
        datetime.date(2026, 12, 1),
    )


def test_fenetre_via_window_start(demo, capsys, monkeypatch):
    monkeypatch.setenv("WINDOW_START", "2026-09-15")
    _, out = lancer(
        demo, capsys, hw=[devoir("SVT", "Exo.", datetime.date(2026, 10, 10))]
    )
    assert "Fenêtre : 2026-09-15 → 2026-12-01" in out
    debut = [a for a in demo.client.appels if a[0] == "homework"][0][1]
    assert debut == datetime.date(2026, 9, 15)


def test_fenetre_invalide_remonte_valueerror(demo, capsys, monkeypatch):
    """Caractérisation : `WINDOW_START` non ISO fait planter `main()`."""
    monkeypatch.setenv("WINDOW_START", "hier")
    demo.client = FauxClient()
    with pytest.raises(ValueError):
        dev_demo.main()


def test_cours_et_infos_bornes_a_today(demo, capsys):
    """Cours et informations s'arrêtent à aujourd'hui, pas à +45 jours."""
    lancer(
        demo,
        capsys,
        hw=[devoir("SVT", "Exo.", datetime.date(2026, 10, 10))],
        infos=[info("Réunion", "Foyer des élèves jeudi.")],
    )
    par_source = {a[0]: a[1:] for a in demo.client.appels}
    assert par_source["lessons"] == (datetime.date(2026, 10, 1), AUJOURDHUI)
    assert par_source["informations"] == (datetime.date(2026, 10, 1), AUJOURDHUI, False)


# ─────────────────────────── 3. cas vide ───────────────────────────


def test_cas_vide_affiche_aucun_item_et_sort_en_2(demo, capsys):
    code, out = lancer(demo, capsys)
    assert code == 2
    assert "EN RETARD (0)" in out and "À VENIR (0)" in out
    assert out.count("(rien)") == 2
    assert "Matières (0) : (aucune)" in out
    assert "Aucun item produit" in out


def test_cours_sans_contenu_ne_produit_aucun_item(demo, capsys):
    """Le cours est bien compté lu, mais sans contenu il ne donne aucun item."""
    code, out = lancer(demo, capsys, lessons=[cours("SVT", "")])
    assert code == 2
    assert "Cours lus : 1 · contenus lus : 1" in out
    assert "Items normalisés : 0" in out


# ─────────────────────────── 4. codes de sortie et erreurs ───────────────────────────


def test_code_0_avec_des_items(demo, capsys):
    code, _ = lancer(
        demo, capsys, hw=[devoir("SVT", "Exo.", datetime.date(2026, 10, 10))]
    )
    assert code == 0


def test_echec_de_connexion_sort_en_1(demo, capsys):
    demo.erreur = RuntimeError("pas de réseau")
    assert dev_demo.main() == 1
    out = capsys.readouterr().out
    assert "Connexion à https://demo.index-education.net/pronote/eleve.html (demonstration)" in out
    assert "Échec de connexion au compte de démonstration : pas de réseau" in out
    assert "WINDOW_START" in out  # le script propose la sortie de secours
    assert "EN RETARD" not in out


def test_echec_homework_sort_en_1_sans_rendre_la_main(demo, capsys, monkeypatch):
    """Un devoir illisible est fatal : rien n'est affiché derrière."""
    client = FauxClient()

    def homework(start, end):  # noqa: ANN202
        raise RuntimeError("cahier HS")

    client.homework = homework
    monkeypatch.setattr(dev_demo.auth, "login_with_password", lambda *a, **k: (client, {}))
    demo.client = client

    assert dev_demo.main() == 1
    out = capsys.readouterr().out
    assert "Lecture du travail à faire impossible : cahier HS" in out
    assert "EN RETARD" not in out


def test_echec_lessons_ou_infos_est_tolere(demo, capsys, monkeypatch):
    """Le reste du pipeline tourne même si ces deux sources lèvent."""
    client = FauxClient(hw=[devoir("SVT", "Exo.", datetime.date(2026, 10, 10))])

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("source HS")

    client.lessons = boom
    client.information_and_surveys = boom
    monkeypatch.setattr(dev_demo.auth, "login_with_password", lambda *a, **k: (client, {}))
    demo.client = client

    assert dev_demo.main() == 0
    out = capsys.readouterr().out
    assert "Lecture du cahier de textes impossible : source HS" in out
    assert "Lecture des informations impossible : source HS" in out
    assert "Cours lus : 0 · contenus lus : 0" in out
    assert "Matières (0) : (aucune)" in out
    assert "[devoir] SVT — 2026-10-10 · Exo." in out


# ─────────────────────────── 5. contenus de cours ───────────────────────────


def test_contenus_plafonnes_a_cinq_cours(demo, capsys):
    lessons = [
        cours(f"Matière {i}", f"Contenu {i}.", start=datetime.datetime(2026, 10, 10, 8))
        for i in range(7)
    ]
    _, out = lancer(demo, capsys, lessons=lessons)
    assert "Cours lus : 7 · contenus lus : 5" in out


def test_cours_annule_saute_pour_le_contenu(demo, capsys):
    _, out = lancer(
        demo,
        capsys,
        lessons=[
            cours("SVT", "Cours annulé.", annule=True),
            cours("Anglais", "Le present perfect."),
        ],
    )
    assert "Cours lus : 2 · contenus lus : 1" in out


def test_contenu_illisible_est_signale_sans_echouer(demo, capsys):
    code, out = lancer(
        demo,
        capsys,
        lessons=[
            cours("SVT", RuntimeError("pièce jointe HS")),
            cours("Anglais", "Le present perfect."),
        ],
    )
    assert code == 0
    assert "contenu illisible pour 2026-10-16 08:00|SVT : pièce jointe HS" in out
    assert "Cours lus : 2 · contenus lus : 1" in out


# ─────────────────────────── 6. appel CLI ───────────────────────────


def test_connexion_utilise_le_compte_public_de_demonstration(demo, capsys):
    lancer(demo, capsys, hw=[devoir("SVT", "Exo.", datetime.date(2026, 10, 10))])
    assert demo.args == [
        (dev_demo.DEMO_URL, dev_demo.DEMO_USER, dev_demo.DEMO_PASSWORD, "dev-demo")
    ]
    assert dev_demo.DEMO_USER == "demonstration"


def test_argument_inconnu_est_ignore(demo, capsys, monkeypatch):
    """Caractérisation : `main()` ne lit pas `sys.argv`, un argument passe."""
    monkeypatch.setattr("sys.argv", ["dev_demo.py", "--inconnu", "42"])
    code, out = lancer(
        demo, capsys, hw=[devoir("SVT", "Exo.", datetime.date(2026, 10, 10))]
    )
    assert code == 0
    assert "inconnu" not in out and "42" not in out
