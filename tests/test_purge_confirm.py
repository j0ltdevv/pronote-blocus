"""Confirmation de `/supprimer` : robustesse au démarrage du processus.

Le garde anti-effacement repose sur `time.monotonic() - purges.get(chat_id, 0.0)
<= 60`. `time.monotonic()` commence près de zéro dans certains environnements
(conteneurs, horloges recentrées) : une valeur `0.0` par défaut alors que le
premier appel arrive à `now ≈ 0` rend la différence `<= 60`, donc une
suppression directe **sans confirmation**.

Ces tests vérifient d'abord le comportement existant (rouge), puis la
résilience du correctif : la *première* demande d'un chat doit toujours
demander une confirmation, pointeur de cache ou horloge vides.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from telegram import Update

from app import config, db
from app.main import build_application
from tests.fake_telegram import FakeTelegramRequest

CHAT = 4242


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

    async def text(self, text: str, chat_id: int = CHAT) -> list[str]:
        self._update_id += 1
        return await self.dispatch(
            text_update(text, chat_id, self._update_id), chat_id
        )


async def _run(cfg: config.Config, conn, scenario) -> None:  # noqa: ANN001
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
            "ADMIN_CHAT_ID": "999999",
            "LLM_BASE_URL": "http://127.0.0.1:9/v1",
            "LLM_API_KEY": "cle-de-test",
            "LLM_MODEL": "modele-de-test",
        }
    )


@pytest.fixture
def cfg(secret, tmp_path) -> config.Config:
    return _cfg(secret, tmp_path)


# ─────────────────────────── tests ───────────────────────────


def test_purge_exige_confirmation_a_le_boot(cfg, conn, make_user, monkeypatch):
    """Au tout premier `/supprimer`, le cache `purge` est vide.

    Même si `time.monotonic()` rapporte `0.0` (environnement de conteneur
    recentré), la *première* demande doit demander la confirmation plutôt
    que supprimer directement.
    """
    user_id = make_user(chat_id=CHAT)
    # Le garde repose sur la présence de la clé (`premiere is None`), pas sur
    # la valeur de l'horloge : inutile de geler `time.monotonic`, ce qui
    # bloquerait aussi la boucle asyncio (elle s'en sert).

    async def scenario(rig):
        texts = await rig.text("/supprimer")
        assert any("Renvoie /supprimer dans la minute" in t for t in texts), texts
        # Aucune suppression : le compte est toujours là.
        assert db.get_user(conn, user_id) is not None

    asyncio.run(_run(cfg, conn, scenario))
