"""Commandes qui écrivent de l'état : /fait, /refresh, /priorites, /supprimer, /annuler.

Hors réseau : application Telegram réelle + transport simulé
(`FakeTelegramRequest`), base SQLite dans `tmp_path`, couche PRONOTE et LLM
simulées. Aucun numéro de ligne n'est deviné : chaque test part d'une liste
envoyée par le bot et relit ce que la base contient réellement.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone

import pytest
from telegram import Update

from app import config, db, llm
from app.main import (
    MSG_ANNULER,
    MSG_CONNEXION,
    MSG_INDEX,
    PROMPT_MATIERES,
    PROMPT_QR,
    build_application,
)
from app.pronote import fetch
from app.pronote.normalize import Item, make_item
from tests.fake_telegram import FakeTelegramRequest

CHAT = 4242
AUTRE_CHAT = 7777
TODAY = config.today()


# ─────────────────────────── données de test ───────────────────────────


def devoir(subject: str, body: str, due, kind: str = "devoir") -> Item:
    return make_item(kind=kind, source=kind, subject=subject, body=body, due_date=due)


def text_update(text: str, chat_id: int = CHAT, update_id: int = 1) -> dict:
    message = {
        "message_id": update_id,
        "date": int(time.time()),
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": chat_id, "is_bot": False, "first_name": "Élève"},
        "text": text,
    }
    if text.startswith("/"):
        message["entities"] = [
            {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
        ]
    return {"update_id": update_id, "message": message}


def plain(html_text: str) -> str:
    return re.sub(r"<[^>]+>", "", html_text)


def premier_numero(textes: list[str]) -> int:
    """Numéro de la première ligne numérotée d'une liste envoyée par le bot."""
    match = re.search(r"(?m)^(\d+)\. ", plain("\n".join(textes)))
    assert match, "aucune ligne numérotée dans la réponse"
    return int(match.group(1))


def compter(conn, table: str, user_id: str) -> int:
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM {table} WHERE user_id = ?", (user_id,)
    ).fetchone()["n"]


def poser_last_sync(conn, user_id: str, minutes: float) -> None:
    """Recule `last_sync_at` de `minutes` par rapport à maintenant."""
    stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    with conn:
        conn.execute(
            "UPDATE users SET last_sync_at = ? WHERE id = ?",
            (stamp.isoformat(timespec="seconds"), user_id),
        )


# ─────────────────────────── montage ───────────────────────────


class Rig:
    """Application réelle + transport simulé ; rien ne sort de la machine."""

    def __init__(self, cfg: config.Config, conn, chat_id: int = CHAT) -> None:
        self.fake = FakeTelegramRequest()
        self.app = build_application(cfg, conn, request=self.fake)
        self.chat_id = chat_id
        self.errors: list[BaseException] = []
        self._update_id = 0
        self.app.add_error_handler(self._on_error)

    async def _on_error(self, update, context) -> None:  # noqa: ANN001
        self.errors.append(context.error)

    async def _dispatch(self, payload: dict, chat_id: int) -> list[str]:
        before = len(self.fake.sent)
        update = Update.de_json(payload, self.app.bot)
        await self.app.process_update(update)
        await asyncio.sleep(0.05)
        if self.errors:
            error = self.errors[0]
            raise AssertionError(
                f"un handler a levé {type(error).__name__}: {error}"
            ) from error
        return self.fake.since(before, chat_id)

    async def text(self, text: str, chat_id: int | None = None) -> list[str]:
        self._update_id += 1
        cible = chat_id or self.chat_id
        return await self._dispatch(
            text_update(text, cible, self._update_id), cible
        )


def run_flow(cfg: config.Config, conn, scenario, chat_id: int = CHAT) -> None:  # noqa: ANN001
    async def main() -> None:
        rig = Rig(cfg, conn, chat_id)
        await rig.app.initialize()
        try:
            await scenario(rig)
        finally:
            await asyncio.sleep(0)
            await rig.app.shutdown()

    asyncio.run(main())


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
    """Aucun appel LLM : résumés vides et réponses constantes."""
    state = {"chat_calls": []}

    def chat_answer(question, data_block, cfg):  # noqa: ANN001
        state["chat_calls"].append(question)
        return "Réponse simulée."

    monkeypatch.setattr(llm, "ensure_summaries", lambda conn, items, cfg: {})
    monkeypatch.setattr(llm, "chat_answer", chat_answer)
    monkeypatch.setattr(llm, "weekly_synthesis", lambda rows, cfg: None)
    return state


TROIS_DEVOIRS = [
    ("Mathématiques", "DM n°4 : suite arithmétique.", 4),
    ("SVT", "Schéma de la cellule à légender.", 2),
    ("Anglais", "Apprendre le vocabulaire de la leçon 3.", 1),
]


def trois_devoirs() -> list[Item]:
    return [devoir(sujet, corps, TODAY - timedelta(days=jours)) for sujet, corps, jours in TROIS_DEVOIRS]


# ─────────────────────────── /fait ───────────────────────────


def test_fait_indice_hors_bornes(cfg, conn, llm_fake, make_user):
    """`/fait 0`, `/fait 3` (liste de 2) et `/fait -1` ne cochent rien."""
    user_id = make_user(chat_id=CHAT)
    db.replace_items(conn, user_id, trois_devoirs()[:2])

    async def scenario(rig):
        await rig.text("/retard")
        assert db.resolve_list_ref(conn, user_id, 2) is not None

        # `999` est déjà couvert par `test_flow_telegram`; ici les bornes 0 et
        # haut-de-liste, plus la syntaxe signée.
        for entree in ("/fait 0", "/fait 3"):
            textes = await rig.text(entree)
            assert MSG_INDEX in textes, entree

        # Un signe n'est pas un numéro : c'est la mauvaise syntaxe qui est
        # signalée, pas l'indice.
        textes = await rig.text("/fait -1")
        assert any("Envoie /fait suivi d'un numéro" in t for t in textes)
        assert db.done_keys(conn, user_id) == set()

    run_flow(cfg, conn, scenario)


def test_fait_sans_liste_envoyee_refuse_l_indice(cfg, conn, llm_fake, make_user):
    """Sans `/retard` préalable, `list_ref` est vide : aucun numéro ne résout."""
    user_id = make_user(chat_id=CHAT)
    db.replace_items(conn, user_id, trois_devoirs())

    async def scenario(rig):
        textes = await rig.text("/fait 1")
        assert MSG_INDEX in textes

    run_flow(cfg, conn, scenario)
    assert db.done_keys(conn, user_id) == set()


def test_fait_refuse_une_cle_absente_des_items(cfg, conn, llm_fake, make_user):
    """Une référence vers un item disparu est refusée, pas cochée.

    Avant le correctif, `/fait` écrivait un ✅ pour une clé absente de `items` :
    un `done_local` fantôme, qu'aucune synchro ne viendrait jamais purger.
    """
    user_id = make_user(chat_id=CHAT)
    reel = trois_devoirs()[0]
    db.replace_items(conn, user_id, [reel])
    db.write_list_ref(conn, user_id, ["devoir:fantome", reel.key])

    async def scenario(rig):
        textes = await rig.text("/fait 1")
        assert MSG_INDEX in textes
        # La clé voisine, elle, reste marquable : la liste n'est pas cassée.
        assert (await rig.text("/fait 2"))[-1] == "✅"
        textes = await rig.text("/retard")
        assert any("Rien en retard" in t for t in textes)

    run_flow(cfg, conn, scenario)
    assert db.done_keys(conn, user_id) == {reel.key}


def test_fait_double_marquage_est_idempotent(cfg, conn, llm_fake, make_user):
    """Deux `/fait 1` d'affilée : deux ✅, une seule ligne en base."""
    user_id = make_user(chat_id=CHAT)
    db.replace_items(conn, user_id, trois_devoirs())

    async def scenario(rig):
        await rig.text("/retard")
        key = db.resolve_list_ref(conn, user_id, 1)
        assert key is not None

        assert (await rig.text("/fait 1"))[-1] == "✅"
        assert (await rig.text("/fait 1"))[-1] == "✅"
        assert db.done_keys(conn, user_id) == {key}

        textes = await rig.text("/retard")
        assert "— 2 item(s)" in plain("\n".join(textes))

    run_flow(cfg, conn, scenario)


def test_fait_marque_survit_a_une_reecriture_des_items(cfg, conn, llm_fake, make_user):
    """`replace_items` (une synchro) ne ressuscite pas un devoir coché."""
    user_id = make_user(chat_id=CHAT)
    items = trois_devoirs()
    db.replace_items(conn, user_id, items)

    async def scenario(rig):
        textes = await rig.text("/retard")
        idx = premier_numero(textes)
        key = db.resolve_list_ref(conn, user_id, idx)
        assert key is not None
        assert (await rig.text(f"/fait {idx}"))[-1] == "✅"

        # Nouvelle synchro : mêmes items, tout est réécrit.
        db.replace_items(conn, user_id, items)
        assert key in db.done_keys(conn, user_id)

        textes = await rig.text("/retard")
        joined = plain("\n".join(textes))
        assert "— 2 item(s)" in joined
        assert "DM n°4" not in joined

    run_flow(cfg, conn, scenario)


# ─────────────────────────── /refresh ───────────────────────────


def test_refresh_respecte_la_fenetre_de_10_minutes(
    cfg, conn, llm_fake, make_user, monkeypatch
):
    """Synchro si `last_sync_at` est vieux de plus de 10 min, sinon liste seule."""
    user_id = make_user(chat_id=CHAT)
    maths = trois_devoirs()[0]
    appels = []

    def sync_ok(conn_, user_id_, cfg_, phase="items"):  # noqa: ANN001
        appels.append((user_id_, phase))
        db.replace_items(conn_, user_id_, [maths])
        db.mark_synced(conn_, user_id_)
        return fetch.SyncResult(True, None, [maths], "3A", phase, 0)

    monkeypatch.setattr(fetch, "sync_user", sync_ok)

    async def scenario(rig):
        # Jamais synchronisé : la fenêtre est ouverte.
        textes = await rig.text("/refresh")
        assert any("Je vais chercher tes devoirs" in t for t in textes)
        assert "DM n°4" in plain("\n".join(textes))
        assert len(appels) == 1

        # 9 minutes : trop tôt, aucune synchro, l'horodatage ne bouge pas.
        poser_last_sync(conn, user_id, 9)
        avant = db.get_user(conn, user_id)["last_sync_at"]
        textes = await rig.text("/refresh")
        assert not any("Je vais chercher tes devoirs" in t for t in textes)
        assert "DM n°4" in plain("\n".join(textes))
        assert len(appels) == 1
        assert db.get_user(conn, user_id)["last_sync_at"] == avant

        # 11 minutes : la fenêtre est rouverte.
        poser_last_sync(conn, user_id, 11)
        textes = await rig.text("/refresh")
        assert any("Je vais chercher tes devoirs" in t for t in textes)
        assert len(appels) == 2

    run_flow(cfg, conn, scenario)


# ─────────────────────────── /priorites ───────────────────────────


def test_priorites_relance_le_choix(cfg, conn, llm_fake, make_user):
    """Une relance de `/priorites` rouvre le choix ; une saisie hors liste le garde."""
    user_id = make_user(chat_id=CHAT)

    async def scenario(rig):
        textes = await rig.text("/priorites")
        assert any("Je ne connais pas encore tes matières" in t for t in textes)

        db.replace_items(
            conn,
            user_id,
            [
                devoir("Mathématiques", "DM dérivées.", TODAY - timedelta(days=2)),
                devoir("SVT", "Schéma à légender.", TODAY - timedelta(days=1)),
            ],
        )

        textes = await rig.text("/priorites")
        joined = plain("\n".join(textes))
        assert "Tes matières" in joined
        assert "1. Mathématiques" in joined
        assert "2. SVT" in joined
        assert PROMPT_MATIERES in joined

        # Relance : la question est reposée, toujours en attente d'une réponse.
        textes = await rig.text("/priorites")
        joined = plain("\n".join(textes))
        assert "Tes matières" in joined
        assert PROMPT_MATIERES in joined

        # Réponse hors liste : le choix reste ouvert.
        textes = await rig.text("9")
        assert any("Réponds avec les numéros de la liste" in t for t in textes)
        assert db.get_priorities(conn, user_id) == {}

        textes = await rig.text("2 1")
        assert any("C'est noté" in t for t in textes)
        assert list(
            sujet
            for sujet, _ in sorted(
                db.get_priorities(conn, user_id).items(), key=lambda kv: kv[1]
            )
        ) == ["SVT", "Mathématiques"]

        # Le choix est consommé : le texte libre repart au chat.
        textes = await rig.text("quelle est la date du contrôle ?")
        assert textes[-1] == "Réponse simulée."
        assert llm_fake["chat_calls"] == ["quelle est la date du contrôle ?"]

        # Une nouvelle relance repart de zéro et peut inverser l'ordre.
        textes = await rig.text("/priorites")
        assert "1. Mathématiques" in plain("\n".join(textes))
        textes = await rig.text("1 2")
        assert any("C'est noté" in t for t in textes)

    run_flow(cfg, conn, scenario)
    assert list(
        sujet
        for sujet, _ in sorted(
            db.get_priorities(conn, user_id).items(), key=lambda kv: kv[1]
        )
    ) == ["Mathématiques", "SVT"]


# ─────────────────────────── /supprimer ───────────────────────────


def test_supprimer_efface_toutes_les_donnees(cfg, conn, llm_fake, make_user):
    """Deux `/supprimer` d'affilée vident toutes les tables de l'élève."""
    user_id = make_user(chat_id=CHAT)
    db.replace_items(conn, user_id, trois_devoirs())
    db.set_priorities(conn, user_id, ["SVT", "Mathématiques"])
    with conn:
        conn.execute(
            "INSERT INTO lesson_seen (user_id, slot, status, updated_at)"
            " VALUES (?,?,?,?)",
            (user_id, "2026-10-17-maths", "lu", "2026-10-17T08:00:00+00:00"),
        )

    async def scenario(rig):
        await rig.text("/retard")
        await rig.text("/fait 1")
        assert db.done_keys(conn, user_id)

        textes = await rig.text("/supprimer")
        assert any("Renvoie /supprimer dans la minute" in t for t in textes)
        assert db.get_user_by_chat(conn, CHAT) is not None

        textes = await rig.text("/supprimer")
        assert any("compte et toutes tes données sont supprimés" in t for t in textes)

        textes = await rig.text("/retard")
        assert MSG_CONNEXION in textes

    run_flow(cfg, conn, scenario)

    assert db.get_user(conn, user_id) is None
    for table in ("priorities", "items", "done_local", "list_ref", "lesson_seen"):
        assert compter(conn, table, user_id) == 0, table


def test_supprimer_confirmation_expiree_ne_supprime_pas(cfg, conn, llm_fake, make_user):
    """Passé la minute, la confirmation est oubliée : il faut la redemander."""
    user_id = make_user(chat_id=CHAT)

    async def scenario(rig):
        textes = await rig.text("/supprimer")
        assert any("Renvoie /supprimer dans la minute" in t for t in textes)

        # La confirmation a expiré : le second appel redemande au lieu d'effacer.
        rig.app.bot_data["purge"][CHAT] = time.monotonic() - 61.0
        textes = await rig.text("/supprimer")
        assert any("Renvoie /supprimer dans la minute" in t for t in textes)
        assert db.get_user_by_chat(conn, CHAT) is not None

        # Le nouvel appel a réarmé la confirmation ; celui-ci efface.
        textes = await rig.text("/supprimer")
        assert any("compte et toutes tes données sont supprimés" in t for t in textes)

    run_flow(cfg, conn, scenario)
    assert db.get_user(conn, user_id) is None


# ─────────────────────────── /annuler ───────────────────────────


def test_annuler_hors_parcours_repond_sans_rien_toucher(cfg, conn, llm_fake, make_user):
    """`/annuler` sans connexion en cours répond et ne touche à rien."""
    user_id = make_user(chat_id=CHAT)
    db.replace_items(conn, user_id, trois_devoirs())

    async def scenario(rig):
        textes = await rig.text("/annuler")
        assert textes == [MSG_ANNULER]
        assert db.get_user(conn, user_id) is not None

        # Le bot reste utilisable juste après.
        textes = await rig.text("/retard")
        assert "— 3 item(s)" in plain("\n".join(textes))

    run_flow(cfg, conn, scenario)
    assert db.get_user(conn, user_id) is not None


def test_annuler_pendant_la_connexion_remet_le_parcours_a_zero(
    cfg, conn, llm_fake, make_user
):
    """`/annuler` en pleine connexion termine le parcours, sans compte créé."""
    make_user(chat_id=CHAT)  # un autre élève, pour que la base ne soit pas vide

    async def scenario(rig):
        textes = await rig.text("/start", chat_id=AUTRE_CHAT)
        assert PROMPT_QR in plain("\n".join(textes))

        textes = await rig.text("/annuler", chat_id=AUTRE_CHAT)
        assert any("Annulé" in t for t in textes)

        # Le parcours est bien terminé : le texte libre retombe sur /start.
        textes = await rig.text("bonjour", chat_id=AUTRE_CHAT)
        assert MSG_CONNEXION in textes

    run_flow(cfg, conn, scenario)
    assert db.get_user_by_chat(conn, AUTRE_CHAT) is None
