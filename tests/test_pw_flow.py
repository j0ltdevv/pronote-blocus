"""Parcours mot de passe : résilience après un redémarrage du bot.

`allow_password` est écrit en base par `/mdp` mais, sans le correctif, le
cache mémoire `pw_flow` est perdu au redémarrage : le premier texte de
l'élève (l'identifiant) quitte le écran et tombe dans la question du LLM.

Ces tests démarrent une application fraîche, activent le recours admin, puis
simulent un redémarrage (nouvelle application — cache perdu) et vérifient
que le flag persistant en base relance bien le parcours.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from telegram import Update

from app import config, db, llm
from app.main import build_application
from app.pronote import auth
from tests.fake_telegram import FakeTelegramRequest

CHAT = 4242
ADMIN = 999999
IDENTIFIANT = "mon-identifiant"


# ─────────────────────────── montage ───────────────────────────


def text_update(text: str, chat_id: int, update_id: int = 1) -> dict:
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


class Rig:
    """Application réelle + transport simulé, montée sur demande."""

    def __init__(self, cfg: config.Config, conn) -> None:
        self.fake = FakeTelegramRequest()
        self.app = build_application(cfg, conn, request=self.fake)
        self.errors: list[BaseException] = []
        self._update_id = 0
        self.app.add_error_handler(self._on_error)

    async def _on_error(self, update, context) -> None:  # noqa: ANN001
        self.errors.append(context.error)

    async def dispatch(self, payload: dict, chat_id: int) -> list[str]:
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

    async def text(self, text: str, chat_id: int) -> list[str]:
        self._update_id += 1
        return await self.dispatch(
            text_update(text, chat_id, self._update_id), chat_id
        )


async def _repartir(cfg: config.Config, conn, scenario) -> None:  # noqa: ANN001
    rig = Rig(cfg, conn)
    await rig.app.initialize()
    try:
        await scenario(rig)
    finally:
        await asyncio.sleep(0)
        await rig.app.shutdown()


def _cfg(secret, tmp_path) -> config.Config:
    return config.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
            "APP_SECRET": secret,
            "DATA_DIR": str(tmp_path / "data"),
            "WINDOW_START": "2026-10-01",
            "ADMIN_CHAT_ID": str(ADMIN),
            "LLM_BASE_URL": "http://127.0.0.1:9/v1",
            "LLM_API_KEY": "cle-de-test",
            "LLM_MODEL": "modele-de-test",
        }
    )


@pytest.fixture
def cfg(secret, tmp_path) -> config.Config:
    return _cfg(secret, tmp_path)


# ─────────────────────────── tests ───────────────────────────


def test_pw_flow_survit_au_redemarrage(cfg, conn, make_user, monkeypatch):
    """Après redémarrage, `allow_password` en base relance le parcours.

    Sans le correctif : le cache `pw_flow` est perdu, le flag ne sert qu'à
    `cmd_mdp` → `on_text` ne le consulte pas → l'identifiant de l'élève tombe
    dans la question du LLM au lieu d'être intercepté comme identifiant.
    """
    user_id = make_user(chat_id=CHAT)

    # Le LLM doit rester muet : il ne reçoit JAMAIS l'identifiant du login.
    appels = []

    def chat_answer(question, data_block, cfg):  # noqa: ANN001
        appels.append(question)
        return "Réponse simulée."

    monkeypatch.setattr(llm, "ensure_summaries", lambda c, i, cfg: {})
    monkeypatch.setattr(llm, "chat_answer", chat_answer)
    monkeypatch.setattr(llm, "weekly_synthesis", lambda rows, cfg: None)

    # 1. L'admin autorise le mot de passe : le flag est posé EN BASE.
    async def activer(rig):
        texts = await rig.text(f"/mdp {CHAT}", chat_id=ADMIN)
        assert any(f"C'est fait pour {CHAT}" in t for t in texts)

    asyncio.run(_repartir(cfg, conn, activer))
    assert db.get_user(conn, user_id)["allow_password"] == 1

    # 2. Redémarrage simulé : une application fraîche, le cache `pw_flow` est
    #    vide mais la base conserve `allow_password = 1`.
    login_ok = False

    def refuser_identifiant(*a, **k):  # noqa: ANN001
        nonlocal login_ok
        raise auth.TokenRefused("identifiant invalide dans le test")

    # On ne veut pas de connexion réelle : l'élève n'envoie QUE l'identifiant.
    monkeypatch.setattr(auth, "login_with_password", refuser_identifiant)

    async def envoyer_identifiant(rig):
        texts = await rig.text(IDENTIFIANT, chat_id=CHAT)
        # Le bot doit demander le mot de passe, pas répondre comme à une
        # question de chat libre.
        assert any("Envoie ton mot de passe" in t for t in texts), texts
        # Et surtout : l'identifiant n'a JAMAIS atteint le LLM.
        assert appels == [], appels

    asyncio.run(_repartir(cfg, conn, envoyer_identifiant))
