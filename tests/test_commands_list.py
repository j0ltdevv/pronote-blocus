"""Couverture des commandes de consultation : `/retard`, `/aujourdhui`,
`/matiere`, `/cours`, `/resume`.

Aucun réseau : la couche LLM est remplacée, le transport Telegram est le faux
transport de `tests.fake_telegram`. La mécanique d'orchestration (`Rig`,
`run_flow`, `devoir`, `plain`) est réutilisée depuis `tests.test_flow_telegram`
pour ne pas la dupliquer ; seul le patron des fixtures `cfg` / `llm_fake` est
repris, faute de pouvoir l'importer (une fixture définie dans un module de test
n'est visible que dans ce module).
"""

from __future__ import annotations

import re
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import config, db, llm
from app.pronote.normalize import Item
from tests.test_flow_telegram import CHAT, devoir, plain, run_flow

TODAY = config.today()

#: Longueur maximale absolue d'un message Telegram.
LIMITE_TELEGRAM = 4096


# ─────────────────────────── fixtures ───────────────────────────


@pytest.fixture
def cfg(secret, tmp_path) -> config.Config:
    return config.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
            "APP_SECRET": secret,
            "DATA_DIR": str(tmp_path / "data"),
            "WINDOW_START": "2026-10-01",
            "ADMIN_CHAT_ID": "999999",
            "LLM_BASE_URL": "http://127.0.0.1:9/v1",
            "LLM_API_KEY": "cle-de-test",
            "LLM_MODEL": "modele-de-test",
        }
    )


@pytest.fixture
def llm_fake(monkeypatch):
    """Aucun appel LLM : résumés vides, synthèse indisponible, tout déterministe."""
    state = SimpleNamespace(chat_calls=[], synthesis_calls=[])

    def chat_answer(question, data_block, cfg):  # noqa: ANN001
        state.chat_calls.append((question, data_block))
        return "Réponse simulée."

    def weekly_synthesis(rows, cfg):  # noqa: ANN001
        state.synthesis_calls.append(rows)
        return None

    monkeypatch.setattr(llm, "ensure_summaries", lambda conn, items, cfg: {})
    monkeypatch.setattr(llm, "chat_answer", chat_answer)
    monkeypatch.setattr(llm, "weekly_synthesis", weekly_synthesis)
    return state


# ─────────────────────────── données de test ───────────────────────────


def item_long(i: int, *, subject: str = "Mathématiques", due=None) -> Item:
    """Item dont le titre (donc l'action affichée) dépasse la largeur d'un message."""
    long = ("Refaire les exercices du chapitre et réviser la leçon. " * 24).strip()
    return Item(
        key=f"long-{subject}-{i}",
        source="devoir",
        subject=subject,
        title=long,
        body=long,
        due_date=due,
        kind="devoir",
    )


def cours_long(mat: str, j: int) -> Item:
    long = (f"Chapitre {j} : " + "démonstration et applications numériques. " * 20).strip()
    return Item(
        key=f"cours-{mat}-{j}",
        source="cours",
        subject=mat,
        title=long,
        body=long,
        due_date=TODAY - timedelta(days=j + 1),
        kind="cours",
    )


def overdue(n: int, subject: str = "Mathématiques", *, days: int = 1) -> list[Item]:
    return [devoir(subject, f"Exercice {i}.", TODAY - timedelta(days=days)) for i in range(n)]


def numbered_count(joined: str) -> int:
    return len(re.findall(r"(?m)^\d+\. ", joined))


# ─────────────────────────── /retard ───────────────────────────


def test_retard_nominal_entete_et_lignes(cfg, conn, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)
    db.replace_items(
        conn,
        user_id,
        [
            devoir("Mathématiques", "DM dérivées à rendre.", TODAY - timedelta(days=3)),
            devoir("SVT", "Schéma à légender.", TODAY - timedelta(days=1)),
        ],
    )

    async def scenario(rig):
        texts = await rig.text("/retard")
        joined = plain("\n".join(texts))
        assert "En retard" in joined
        assert "— 2 item(s)" in joined
        assert "DM dérivées à rendre" in joined
        assert "Schéma à légender" in joined
        # La liste écrite pour /fait couvre les deux items affichés.
        assert db.resolve_list_ref(conn, user_id, 1) is not None
        assert db.resolve_list_ref(conn, user_id, 2) is not None
        assert db.resolve_list_ref(conn, user_id, 3) is None

    run_flow(cfg, conn, scenario)


def test_retard_pagination_et_bornage_de_page(cfg, conn, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)
    db.replace_items(conn, user_id, overdue(20))

    async def scenario(rig):
        # Page 1 : note « … et 5 autres », pas d'item 16.
        page1 = plain("\n".join(await rig.text("/retard")))
        assert "— 20 item(s)" in page1
        assert re.search(r"(?m)^1\. ", page1)
        assert "autres, /retard 2" in page1
        assert re.search(r"(?m)^16\. ", page1) is None

        # Page hors bornes : ramenée à la dernière page (2), jamais de crash.
        page99 = plain("\n".join(await rig.text("/retard 99")))
        assert re.search(r"(?m)^16\. ", page99)
        assert "Page 2/2" in page99

        # Page 0, négative ou non numérique : ramenée à la page 1.
        for arg in ("0", "-4", "abc", "2.5"):
            joined = plain("\n".join(await rig.text(f"/retard {arg}")))
            assert re.search(r"(?m)^1\. ", joined), arg
            assert re.search(r"(?m)^16\. ", joined) is None, arg

    run_flow(cfg, conn, scenario)


def test_retard_liste_vide(cfg, conn, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)

    async def scenario(rig):
        texts = await rig.text("/retard")
        assert any("Rien en retard 🎉" in t for t in texts)
        # Aucun item : la liste de référence est vidée pour /fait.
        assert db.resolve_list_ref(conn, user_id, 1) is None

    run_flow(cfg, conn, scenario)


# ─────────────────────────── /aujourdhui ───────────────────────────


def test_aujourdhui_nominal_et_arguments_ignores(cfg, conn, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)
    db.replace_items(
        conn,
        user_id,
        [
            devoir("Mathématiques", "DM pour aujourd'hui.", TODAY),
            devoir("Anglais", "Vocabulaire pour demain.", TODAY + timedelta(days=1)),
            devoir("SVT", "Contrôle dans trois jours.", TODAY + timedelta(days=3)),
            devoir("Physique-Chimie", "Ancien devoir.", TODAY - timedelta(days=5)),
        ],
    )

    async def scenario(rig):
        joined = plain("\n".join(await rig.text("/aujourdhui")))
        assert "Aujourd'hui et demain" in joined
        assert "— 2 item(s)" in joined
        assert "DM pour aujourd'hui" in joined
        assert "Vocabulaire pour demain" in joined
        assert "Contrôle dans trois jours" not in joined
        assert "Ancien devoir" not in joined

        # Un argument supplémentaire est ignoré : sortie identique.
        joined_extra = plain("\n".join(await rig.text("/aujourdhui detail")))
        assert "— 2 item(s)" in joined_extra
        assert "DM pour aujourd'hui" in joined_extra

    run_flow(cfg, conn, scenario)


def test_aujourdhui_liste_vide(cfg, conn, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)
    # Des items existent, mais aucun aujourd'hui ni demain.
    db.replace_items(
        conn,
        user_id,
        [
            devoir("SVT", "Contrôle dans trois jours.", TODAY + timedelta(days=3)),
            devoir("Anglais", "Ancien devoir.", TODAY - timedelta(days=2)),
        ],
    )

    async def scenario(rig):
        texts = await rig.text("/aujourdhui")
        assert any("Rien à rendre aujourd'hui ni demain" in t for t in texts)

    run_flow(cfg, conn, scenario)


def test_aujourdhui_pagination(cfg, conn, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)
    db.replace_items(conn, user_id, [devoir("SVT", f"Cours {i}.", TODAY) for i in range(20)])

    async def scenario(rig):
        joined = plain("\n".join(await rig.text("/aujourdhui")))
        assert "— 20 item(s)" in joined
        assert re.search(r"(?m)^1\. ", joined)
        assert re.search(r"(?m)^15\. ", joined)
        assert re.search(r"(?m)^16\. ", joined) is None
        assert "autres, /retard 2" in joined

    run_flow(cfg, conn, scenario)


# ─────────────────────────── /matiere ───────────────────────────


def test_matiere_nominal_insensible_a_la_casse(cfg, conn, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)
    db.replace_items(
        conn,
        user_id,
        [
            devoir("Mathématiques", "DM dérivées.", TODAY - timedelta(days=2)),
            devoir("SVT", "Schéma.", TODAY - timedelta(days=1)),
        ],
    )

    async def scenario(rig):
        joined = plain("\n".join(await rig.text("/matiere MATH")))
        assert "— 1 item(s)" in joined
        assert "DM dérivées" in joined
        assert "Schéma" not in joined

        # Requête multi-mots : « physique chimie » retrouve « Physique-Chimie ».
        db.replace_items(
            conn,
            user_id,
            [devoir("Physique-Chimie", "TP à préparer.", TODAY - timedelta(days=1))],
        )
        joined = plain("\n".join(await rig.text("/matiere physique")))
        assert "TP à préparer" in joined

    run_flow(cfg, conn, scenario)


def test_matiere_vide_filtre_inconnu_et_sans_argument(cfg, conn, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)

    async def scenario(rig):
        # Aucun item du tout.
        assert any("Rien trouvé en « maths »" in t for t in await rig.text("/matiere maths"))

        # Item présent mais filtre sans correspondance.
        db.replace_items(
            conn, user_id, [devoir("Mathématiques", "DM.", TODAY - timedelta(days=1))]
        )
        assert any("Rien trouvé en « inconnue »" in t for t in await rig.text("/matiere inconnue"))

        # Aucun argument : message d'usage.
        assert any(
            "Envoie /matiere suivi du nom" in t for t in await rig.text("/matiere")
        )
        # Espaces seuls : traités comme absence d'argument.
        assert any(
            "Envoie /matiere suivi du nom" in t for t in await rig.text("/matiere   ")
        )

    run_flow(cfg, conn, scenario)


def test_matiere_pagination(cfg, conn, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)
    db.replace_items(conn, user_id, overdue(20, subject="Mathématiques"))

    async def scenario(rig):
        joined = plain("\n".join(await rig.text("/matiere math")))
        assert "— 20 item(s)" in joined
        assert re.search(r"(?m)^15\. ", joined)
        assert re.search(r"(?m)^16\. ", joined) is None
        assert "autres, /retard 2" in joined

    run_flow(cfg, conn, scenario)


# ─────────────────────────── /cours ───────────────────────────


def test_cours_nominal_groupe_par_matiere_recents_d_abord(cfg, conn, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)
    db.replace_items(
        conn,
        user_id,
        [
            devoir("Mathématiques", "Chapitre 3 : dérivées.", TODAY - timedelta(days=5), kind="cours"),
            devoir("Mathématiques", "Chapitre 4 : intégrales.", TODAY - timedelta(days=1), kind="cours"),
            devoir("SVT", "Tectonique des plaques.", TODAY - timedelta(days=2), kind="cours"),
            devoir("Anglais", "Devoir maison de rentrée.", TODAY - timedelta(days=1)),
        ],
    )

    async def scenario(rig):
        joined = plain("\n".join(await rig.text("/cours")))
        assert "Cours à rattraper" in joined
        assert "Chapitre 4" in joined
        assert "Chapitre 3" in joined
        assert "Tectonique des plaques" in joined
        # Un `devoir` n'apparaît jamais dans les contenus de cours.
        assert "Devoir maison de rentrée" not in joined
        # Le cours le plus récent d'une matière vient d'abord.
        assert joined.index("Chapitre 4") < joined.index("Chapitre 3")

        # Filtre par matière (insensible à la casse).
        joined = plain("\n".join(await rig.text("/cours SVT")))
        assert "Tectonique des plaques" in joined
        assert "Chapitre 4" not in joined

    run_flow(cfg, conn, scenario)


def test_cours_vide_et_filtre_inconnu(cfg, conn, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)

    async def scenario(rig):
        # Aucun item : rien à afficher.
        assert any(
            "Aucun contenu de cours pour l'instant" in t for t in await rig.text("/cours")
        )

        # Des devoirs existent mais aucun contenu de cours.
        db.replace_items(
            conn, user_id, [devoir("Mathématiques", "DM.", TODAY - timedelta(days=1))]
        )
        assert any(
            "Aucun contenu de cours pour l'instant" in t for t in await rig.text("/cours")
        )

        # Filtre qui ne correspond à aucune matière.
        db.replace_items(
            conn,
            user_id,
            [devoir("SVT", "Tectonique.", TODAY - timedelta(days=1), kind="cours")],
        )
        assert any(
            "Aucun contenu de cours pour l'instant" in t
            for t in await rig.text("/cours inconnue")
        )

    run_flow(cfg, conn, scenario)


def test_cours_plafonne_a_cinq_par_matiere_avec_pied(cfg, conn, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)
    items = [devoir("SVT", f"Cours {j}.", TODAY - timedelta(days=j + 1), kind="cours") for j in range(7)]
    db.replace_items(conn, user_id, items)

    async def scenario(rig):
        texts = await rig.text("/cours")
        joined = plain("\n".join(texts))
        # 5 cours affichés au maximum, les 2 autres annoncés dans le pied.
        assert "… et 2 autres cours" in joined
        assert "Cours 0" in joined
        assert "Cours 5" not in joined
        assert "Cours 6" not in joined

    run_flow(cfg, conn, scenario)


# ─────────────────────────── /resume ───────────────────────────


def test_resume_nominal_sans_synthese(cfg, conn, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)
    db.replace_items(
        conn,
        user_id,
        [
            devoir("Mathématiques", "DM n°4 à rendre.", TODAY - timedelta(days=3)),
            devoir("SVT", "Schéma à légender.", TODAY - timedelta(days=1)),
        ],
    )

    async def scenario(rig):
        texts = await rig.text("/resume")
        joined = plain("\n".join(texts))
        assert "synthèse indisponible" in joined
        assert "Mathématiques" in joined
        assert "SVT" in joined
        assert "… et d'autres items" in joined
        # La liste affichée est bien celle que /fait N résout.
        key = db.resolve_list_ref(conn, user_id, 1)
        assert key is not None
        assert (await rig.text("/fait 1"))[-1] == "✅"
        assert key in db.done_keys(conn, user_id)
        # La synthèse LLM a bien été tentée une fois, puis rejetée (None).
        assert len(llm_fake.synthesis_calls) == 1

    run_flow(cfg, conn, scenario)


def test_resume_vide(cfg, conn, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)

    async def scenario(rig):
        texts = await rig.text("/resume")
        assert any("Rien à résumer : tu es à jour 🎉" in t for t in texts)
        # Aucun appel de synthèse sur une liste vide.
        assert llm_fake.synthesis_calls == []

    run_flow(cfg, conn, scenario)


def test_resume_limite_a_dix_lignes_et_arguments_ignores(cfg, conn, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)
    db.replace_items(conn, user_id, overdue(12))

    async def scenario(rig):
        texts = await rig.text("/resume")
        joined = plain("\n".join(texts))
        assert "item(s)" not in joined  # /resume ne titre pas par nombre d'items
        assert numbered_count(joined) == 10
        assert re.search(r"(?m)^11\. ", joined) is None
        assert "… et d'autres items" in joined

        # Un argument est ignoré : même sortie.
        joined_extra = plain("\n".join(await rig.text("/resume semaine")))
        assert numbered_count(joined_extra) == 10

    run_flow(cfg, conn, scenario)


# ─────────────────────────── texte très long ───────────────────────────


def test_texte_tres_long_decoupe_sous_la_limite_telegram(cfg, conn, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)
    items: list[Item] = []
    items += [item_long(i, due=TODAY - timedelta(days=1)) for i in range(15)]
    items += [item_long(100 + i, due=TODAY) for i in range(15)]
    mats = ["Mathématiques", "SVT", "Anglais", "Physique-Chimie", "Histoire-Géographie"]
    for mat in mats:
        items += [cours_long(mat, j) for j in range(5)]
    db.replace_items(conn, user_id, items)

    async def scenario(rig):
        for command in ("/retard", "/aujourdhui", "/matiere math", "/cours", "/resume"):
            texts = await rig.text(command)
            assert texts, command
            assert all(t.strip() for t in texts), command
            assert max(len(t) for t in texts) <= LIMITE_TELEGRAM, command
            # Le volume dépasse un seul message : la découpe a bien eu lieu.
            assert len(texts) > 1, command

    run_flow(cfg, conn, scenario)


# ─────────────────────────── base vide ───────────────────────────


def test_toutes_les_commandes_repondent_sur_base_vide(cfg, conn, llm_fake, make_user):
    make_user(chat_id=CHAT)

    async def scenario(rig):
        assert any("Rien en retard 🎉" in t for t in await rig.text("/retard"))
        assert any(
            "Rien à rendre aujourd'hui ni demain" in t for t in await rig.text("/aujourdhui")
        )
        assert any("Rien trouvé en « maths »" in t for t in await rig.text("/matiere maths"))
        assert any(
            "Aucun contenu de cours pour l'instant" in t for t in await rig.text("/cours")
        )
        assert any("Rien à résumer : tu es à jour 🎉" in t for t in await rig.text("/resume"))

    run_flow(cfg, conn, scenario)
