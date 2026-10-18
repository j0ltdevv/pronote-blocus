"""Chat libre : routage hors commande, quota quotidien et replis sans modèle.

Complète `test_flow_telegram.py` en isolant la couche « question libre »
(`on_text` → `_chat_answer`) : routage du texte non-commande, garde-fou de dix
questions par jour, remise à zéro le lendemain, et les replis quand le LLM est
absent, muet ou en erreur HTTP.
"""

from __future__ import annotations

from datetime import timedelta

import httpx

from app import config, db, llm
from app.main import MSG_CONNEXION, MSG_QUOTA
from app.pronote.normalize import make_item
from tests.test_flow_telegram import (  # noqa: F401  (fixtures réutilisées)
    CHAT,
    TODAY,
    cfg,
    devoir,
    llm_fake,
    plain,
    pronote,
    run_flow,
)

# ─────────────────────────── helpers ───────────────────────────


def _cfg(secret: str, tmp_path, **extra: str) -> config.Config:
    """Config de test avec surcharges ; mêmes clés que la fixture `cfg`."""
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
        "APP_SECRET": secret,
        "DATA_DIR": str(tmp_path / "data"),
        "WINDOW_START": "2026-10-01",
        "ADMIN_CHAT_ID": "999999",
        "LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "LLM_API_KEY": "cle-de-test",
        "LLM_MODEL": "modele-de-test",
    }
    env.update(extra)
    return config.from_env(env)


def _quota(conn, user_id: str) -> tuple[str | None, int]:
    row = conn.execute(
        "SELECT llm_day, llm_count FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    assert row is not None
    return row["llm_day"], row["llm_count"]


def _seed_quota(conn, user_id: str, *, day: str, count: int) -> None:
    with conn:
        conn.execute(
            "UPDATE users SET llm_day = ?, llm_count = ? WHERE id = ?",
            (day, count, user_id),
        )


def _messagerie(subject: str, body: str, due) -> object:
    return make_item(
        kind="devoir",
        source="messagerie",
        subject=subject,
        body=body,
        due_date=due,
    )


# ─────────────── routage : texte hors commande vs commande ───────────────


def test_texte_hors_commande_appelle_le_llm_et_consomme_le_quota(
    cfg, conn, pronote, llm_fake, make_user
):
    """Seul le texte hors commande part au LLM et brûle une question."""
    user_id = make_user(chat_id=CHAT)
    db.replace_items(
        conn,
        user_id,
        [devoir("Mathématiques", "Contrôle sur les dérivées.", TODAY - timedelta(days=2))],
    )

    seen: dict[str, list[str]] = {}

    async def scenario(rig):
        # Une commande ne doit PAS solliciter le LLM ni consommer le quota.
        seen["commande"] = await rig.text("/retard")
        seen["libre"] = await rig.text("tu peux m'aider sur mes devoirs ?")

    run_flow(cfg, conn, scenario)

    assert len(llm_fake.chat_calls) == 1
    assert llm_fake.chat_calls[0][0] == "tu peux m'aider sur mes devoirs ?"
    assert "Réponse simulée." in "\n".join(seen["libre"])
    assert "Réponse simulée." not in "\n".join(seen["commande"])
    assert _quota(conn, user_id) == (TODAY.isoformat(), 1)


# ─────────────── quota : 9e, 10e, 11e question ───────────────


def test_quota_neuvieme_et_dixieme_question_puis_refus(cfg, conn, pronote, llm_fake, make_user):
    """Depuis 8 questions : la 9e et la 10e passent, la 11e est refusée."""
    user_id = make_user(chat_id=CHAT)
    _seed_quota(conn, user_id, day=TODAY.isoformat(), count=8)
    db.replace_items(
        conn,
        user_id,
        [devoir("SVT", "Lire le chapitre 2.", TODAY - timedelta(days=1))],
    )

    seen: dict[str, list[str]] = {}

    async def scenario(rig):
        seen["q9"] = await rig.text("question neuf")
        seen["q10"] = await rig.text("question dix")
        seen["q11"] = await rig.text("question onze")

    run_flow(cfg, conn, scenario)

    assert "Réponse simulée." in "\n".join(seen["q9"])
    assert "Réponse simulée." in "\n".join(seen["q10"])
    assert seen["q11"] == [MSG_QUOTA]
    assert len(llm_fake.chat_calls) == 2
    assert _quota(conn, user_id) == (TODAY.isoformat(), 10)


def test_quota_remis_a_zero_le_lendemain(cfg, conn, pronote, llm_fake, make_user):
    """Un compteur saturé hier ne bloque pas la question du jour."""
    user_id = make_user(chat_id=CHAT)
    hier = (TODAY - timedelta(days=1)).isoformat()
    _seed_quota(conn, user_id, day=hier, count=10)
    db.replace_items(
        conn,
        user_id,
        [devoir("SVT", "Lire le chapitre 2.", TODAY - timedelta(days=1))],
    )

    seen: dict[str, list[str]] = {}

    async def scenario(rig):
        seen["texts"] = await rig.text("bonjour")

    run_flow(cfg, conn, scenario)

    assert "Réponse simulée." in "\n".join(seen["texts"])
    assert len(llm_fake.chat_calls) == 1
    assert _quota(conn, user_id) == (TODAY.isoformat(), 1)


# ─────────────── LLM absent : repli sans consommer de quota ───────────────


def test_llm_absent_repond_la_liste_sans_consommer_le_quota(
    secret, tmp_path, conn, pronote, llm_fake, make_user
):
    """Sans clé/modèle, la question tombe direct sur la liste : zéro quota."""
    local = _cfg(
        secret, tmp_path, LLM_BASE_URL="", LLM_API_KEY="", LLM_MODEL=""
    )
    assert local.llm_enabled is False
    user_id = make_user(chat_id=CHAT)
    db.replace_items(
        conn,
        user_id,
        [devoir("Mathématiques", "Contrôle sur les dérivées.", TODAY - timedelta(days=2))],
    )

    seen: dict[str, list[str]] = {}

    async def scenario(rig):
        seen["texts"] = await rig.text("j'ai quoi comme devoirs ?")

    run_flow(local, conn, scenario)

    assert llm_fake.chat_calls == []
    assert "Je n'ai pas pu réfléchir" in plain("\n".join(seen["texts"]))
    assert _quota(conn, user_id) == (None, 0)


# ─────────────── réponse vide et erreur HTTP du LLM ───────────────


def test_reponse_llm_vide_replie_sur_la_liste(cfg, conn, pronote, llm_fake, make_user):
    """Une réponse vide n'est pas envoyée : repli, mais le quota est brûlé."""
    user_id = make_user(chat_id=CHAT)
    db.replace_items(
        conn,
        user_id,
        [devoir("SVT", "Lire le chapitre 2.", TODAY - timedelta(days=1))],
    )
    llm_fake.answers = [""]

    seen: dict[str, list[str]] = {}

    async def scenario(rig):
        seen["texts"] = await rig.text("j'ai quoi en svt ?")

    run_flow(cfg, conn, scenario)

    assert len(llm_fake.chat_calls) == 1
    joined = plain("\n".join(seen["texts"]))
    assert "Je n'ai pas pu réfléchir" in joined
    assert joined.strip() != ""
    assert _quota(conn, user_id) == (TODAY.isoformat(), 1)


def test_erreur_http_du_llm_replie_sur_la_liste(
    cfg, conn, pronote, make_user, monkeypatch
):
    """Le vrai `llm.chat_answer` avale l'erreur HTTP : le bot replie proprement."""
    user_id = make_user(chat_id=CHAT)
    db.replace_items(
        conn,
        user_id,
        [devoir("Mathématiques", "Contrôle sur les dérivées.", TODAY - timedelta(days=2))],
    )
    monkeypatch.setattr(llm, "ensure_summaries", lambda conn_, items, cfg_: {})

    def _boom(*args, **kwargs):
        raise httpx.ConnectError("LLM injoignable")

    monkeypatch.setattr(llm.httpx, "post", _boom)

    seen: dict[str, list[str]] = {}

    async def scenario(rig):
        seen["texts"] = await rig.text("j'ai quoi comme devoirs ?")

    run_flow(cfg, conn, scenario)

    assert "Je n'ai pas pu réfléchir" in plain("\n".join(seen["texts"]))
    assert _quota(conn, user_id) == (TODAY.isoformat(), 1)


# ─────────────── LLM_MESSAGES ───────────────


def _items_messagerie_et_cours() -> list:
    return [
        _messagerie("Espagnol", "Rendre l'exposé sur Barcelone.", TODAY - timedelta(days=1)),
        make_item(
            kind="cours",
            source="cahier_de_textes",
            subject="SVT",
            body="Chapitre 2 sur la tectonique des plaques.",
            due_date=TODAY,
        ),
        devoir("Mathématiques", "Contrôle sur les dérivées.", TODAY - timedelta(days=2)),
    ]


def test_llm_messages_desactives_retirent_messagerie_et_cours(
    secret, tmp_path, conn, pronote, llm_fake, make_user
):
    """LLM_MESSAGES=0 : pas de message de la messagerie, pas de bloc COURS."""
    local = _cfg(secret, tmp_path, LLM_MESSAGES="0")
    assert local.llm_messages is False
    user_id = make_user(chat_id=CHAT)
    db.replace_items(conn, user_id, _items_messagerie_et_cours())

    async def scenario(rig):
        await rig.text("je peux rattraper les cours ?")

    run_flow(local, conn, scenario)

    _, data_block = llm_fake.chat_calls[0]
    assert "Espagnol" not in data_block
    assert "COURS:" not in data_block
    assert "Mathématiques" in data_block


def test_llm_messages_actives_gardent_messagerie_et_cours(
    cfg, conn, pronote, llm_fake, make_user
):
    """Contrôle du drapeau : activé, la messagerie et le bloc COURS reviennent."""
    assert cfg.llm_messages is True
    user_id = make_user(chat_id=CHAT)
    db.replace_items(conn, user_id, _items_messagerie_et_cours())

    async def scenario(rig):
        await rig.text("je peux rattraper les cours ?")

    run_flow(cfg, conn, scenario)

    _, data_block = llm_fake.chat_calls[0]
    assert "Espagnol" in data_block
    assert "COURS:" in data_block


# ─────────────── question avant toute connexion PRONOTE ───────────────


def test_question_avant_connexion_pronote_demande_start(cfg, conn, pronote, llm_fake):
    """Aucun compte : le texte libre renvoie vers /start, sans toucher au LLM."""
    seen: dict[str, list[str]] = {}

    async def scenario(rig):
        seen["texts"] = await rig.text("j'ai quoi en maths ?")

    run_flow(cfg, conn, scenario)

    assert seen["texts"] == [MSG_CONNEXION]
    assert llm_fake.chat_calls == []
