"""Transport Telegram simulé : aucun réseau, tout est observable.

`FakeTelegramRequest` est branché par `build_application(cfg, conn, request=…)`.
Chaque appel sortant est enregistré dans `sent` sous la forme
`(méthode, paramètres)` ; la méthode est le DERNIER segment de l'URL
(`sendMessage`, `getMe`, `getFile`, …).

Les octets servis par `File.download_as_bytearray()` viennent de `files`
(clé = `file_path` renvoyé par `getFile`, valeur = octets).
"""

from __future__ import annotations

import json
import time
from urllib.parse import urlparse

from telegram.request import BaseRequest

#: Identifiant du bot factice renvoyé par `getMe`.
BOT_USER = {
    "id": 42,
    "is_bot": True,
    "first_name": "PronoteBlocus",
    "username": "pronote_blocus_test_bot",
    "can_join_groups": True,
    "can_read_all_group_messages": False,
    "supports_inline_queries": False,
}

#: Méthodes qui produisent un message sortant observable.
MESSAGE_METHODS = ("sendMessage", "sendPhoto", "sendDocument")


class FakeTelegramRequest(BaseRequest):
    """`BaseRequest` minimal : tout est local, rien ne sort de la machine."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        super().__init__()
        self.files: dict[str, bytes] = dict(files or {})
        #: Tous les appels sortants : [(méthode, paramètres)].
        self.sent: list[tuple[str, dict]] = []
        self._message_id = 0

    # ── cycle de vie ────────────────────────────────────────────────

    @property
    def read_timeout(self) -> float:  # noqa: D102
        return 5.0

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    # ── transport ───────────────────────────────────────────────────

    @staticmethod
    def _method_of(url: str) -> str:
        return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]

    @staticmethod
    def _params(request_data) -> dict:  # noqa: ANN001
        if request_data is None:
            return {}
        return dict(request_data.parameters)

    def _chat_payload(self, chat_id: int) -> dict:
        return {"id": chat_id, "type": "private"}

    def _message_payload(self, params: dict) -> dict:
        self._message_id += 1
        chat_id = params.get("chat_id", 0)
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            chat_id = 0
        return {
            "message_id": self._message_id,
            "date": int(time.time()),
            "chat": self._chat_payload(chat_id),
            "text": params.get("text") or "",
        }

    def _result(self, method: str, params: dict):  # noqa: ANN202
        if method == "getMe":
            return dict(BOT_USER)
        if method in MESSAGE_METHODS:
            return self._message_payload(params)
        if method == "getFile":
            file_id = str(params.get("file_id", ""))
            path = f"photos/{file_id}.png"
            result = {
                "file_id": file_id,
                "file_unique_id": f"u{file_id}",
                "file_path": path,
            }
            if path in self.files:
                result["file_size"] = len(self.files[path])
            return result
        if method == "getUpdates":
            return []
        if method == "deleteMessage":
            return True
        # setMyCommands, answerCallbackQuery, … : acquittement muet.
        return True

    async def do_request(
        self,
        url: str,
        method: str,  # noqa: ARG002
        request_data=None,  # noqa: ANN001
        **kwargs: object,
    ) -> tuple[int, bytes]:
        name = self._method_of(url)
        params = self._params(request_data)
        self.sent.append((name, params))
        body = {"ok": True, "result": self._result(name, params)}
        return 200, json.dumps(body).encode("utf-8")

    async def retrieve(self, url: str, **kwargs: object) -> bytes:  # noqa: ARG002
        path = urlparse(url).path
        for file_path, data in self.files.items():
            if path.endswith(file_path):
                return data
        raise FileNotFoundError(f"fichier inconnu du faux transport : {url}")

    # ── lecture des messages sortants ───────────────────────────────

    @property
    def messages(self) -> list[dict]:
        """Paramètres de tous les `sendMessage` sortants, dans l'ordre."""
        return [params for name, params in self.sent if name in MESSAGE_METHODS]

    def texts(self, chat_id: int | None = None) -> list[str]:
        out: list[str] = []
        for params in self.messages:
            if chat_id is not None:
                try:
                    if int(params.get("chat_id", 0)) != chat_id:
                        continue
                except (TypeError, ValueError):
                    continue
            out.append(str(params.get("text") or ""))
        return out

    def since(self, start: int, chat_id: int | None = None) -> list[str]:
        """Textes envoyés depuis l'indice `start` de `sent`."""
        out: list[str] = []
        for name, params in self.sent[start:]:
            if name not in MESSAGE_METHODS:
                continue
            if chat_id is not None:
                try:
                    if int(params.get("chat_id", 0)) != chat_id:
                        continue
                except (TypeError, ValueError):
                    continue
            out.append(str(params.get("text") or ""))
        return out
