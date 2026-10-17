"""Parcours réels dans le bot, sans réseau.

Tout ce qui sort est observé via `FakeTelegramRequest` ; seule la couche
réseau de PRONOTE et les appels LLM sont simulés. Le décodage du QR est le
VRAI (`auth.decode_qr_image` + zxingcpp), le déchiffrement du code à quatre
chiffres est le VRAI `pronotepy` (il échoue localement, sans réseau).
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
import segno
from telegram import Update

from app import config, crypto, db, llm
from app.main import MSG_CODE, MSG_CRYPTO, MSG_INDEX, MSG_QUOTA, build_application
from app.pronote import auth, fetch
from app.pronote.normalize import Item, make_item
from tests.fake_telegram import FakeTelegramRequest

CHAT = 4242
QR_URL = "https://0332722e.index-education.net/pronote/eleve.html"
SUBJECTS = [
    "Mathématiques",
    "Histoire-Géographie",
    "SVT",
    "Physique-Chimie",
    "Anglais",
]

TODAY = config.today()


# ─────────────────────────── données de test ───────────────────────────


def qr_png(login: str = "00" * 16, jeton: str = "11" * 16) -> bytes:
    """Un VRAI PNG de QR code, lisible par zxingcpp."""
    payload = {"login": login, "jeton": jeton, "url": QR_URL}
    buffer = io.BytesIO()
    segno.make(json.dumps(payload)).save(buffer, kind="png")
    return buffer.getvalue()


def devoir(subject: str, body: str, due, kind: str = "devoir") -> Item:
    return make_item(
        kind=kind, source=kind, subject=subject, body=body, due_date=due
    )


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


def photo_update(file_id: str, chat_id: int = CHAT, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": False, "first_name": "Élève"},
            "photo": [
                {
                    "file_id": file_id,
                    "file_unique_id": f"u{file_id}",
                    "width": 300,
                    "height": 300,
                }
            ],
        },
    }


# ─────────────────────────── montage ───────────────────────────


class Rig:
    """Application réelle + transport simulé + helpers d'envoi."""

    def __init__(self, cfg: config.Config, conn, chat_id: int = CHAT) -> None:
        self.fake = FakeTelegramRequest()
        self.app = build_application(cfg, conn, request=self.fake)
        self.chat_id = chat_id
        self.errors: list[BaseException] = []
        self._update_id = 0
        self._file_seq = 0
        self.app.add_error_handler(self._on_error)

    async def _on_error(self, update, context) -> None:  # noqa: ANN001
        self.errors.append(context.error)

    def _raise_errors(self) -> None:
        if self.errors:
            error = self.errors[0]
            raise AssertionError(
                f"un handler a levé {type(error).__name__}: {error}"
            ) from error

    async def _dispatch(self, payload: dict, chat_id: int) -> list[str]:
        before = len(self.fake.sent)
        update = Update.de_json(payload, self.app.bot)
        await self.app.process_update(update)
        # Laisse tourner les tâches de fond (`_cours_after_sync`, digest…).
        await asyncio.sleep(0.05)
        self._raise_errors()
        return self.fake.since(before, chat_id)

    async def text(self, text: str, chat_id: int | None = None) -> list[str]:
        self._update_id += 1
        return await self._dispatch(
            text_update(text, chat_id or self.chat_id, self._update_id),
            chat_id or self.chat_id,
        )

    async def photo(self, data: bytes, chat_id: int | None = None) -> list[str]:
        self._update_id += 1
        self._file_seq += 1
        file_id = f"qr{self._file_seq}"
        self.fake.files[f"photos/{file_id}.png"] = data
        return await self._dispatch(
            photo_update(file_id, chat_id or self.chat_id, self._update_id),
            chat_id or self.chat_id,
        )


def run_flow(cfg: config.Config, conn, scenario) -> None:  # noqa: ANN001
    async def main() -> None:
        rig = Rig(cfg, conn)
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
        }
    )


@pytest.fixture
def pronote(monkeypatch):
    """Simule UNIQUEMENT la couche réseau PRONOTE."""
    state = SimpleNamespace(
        items=[],
        subjects=list(SUBJECTS),
        class_name="3A",
        collect_calls=[],
        sync_calls=[],
    )

    def class_name_of(client):  # noqa: ANN001
        return state.class_name

    def subjects_of(client, cfg):  # noqa: ANN001
        return list(state.subjects)

    def collect(client, conn, user_id, cfg, phase="items"):  # noqa: ANN001
        state.collect_calls.append((user_id, phase))
        db.replace_items(conn, user_id, list(state.items))
        return fetch.SyncResult(
            True, None, list(state.items), state.class_name, phase, 0
        )

    def sync_user(conn, user_id, cfg, phase="items"):  # noqa: ANN001
        state.sync_calls.append((user_id, phase))
        return fetch.SyncResult(True, None, [], state.class_name, phase, 0)

    monkeypatch.setattr(fetch, "class_name_of", class_name_of)
    monkeypatch.setattr(fetch, "subjects_of", subjects_of)
    monkeypatch.setattr(fetch, "collect", collect)
    monkeypatch.setattr(fetch, "sync_user", sync_user)
    return state


@pytest.fixture
def llm_fake(monkeypatch):
    """Aucun appel LLM : tout est déterministe et les entrées sont observables."""
    state = SimpleNamespace(
        chat_calls=[],
        extract_calls=[],
        answers=[],
        default_answer="Réponse simulée.",
    )

    def chat_answer(question, data_block, cfg):  # noqa: ANN001
        state.chat_calls.append((question, data_block))
        if state.answers:
            return state.answers.pop(0)
        return state.default_answer

    def extract_devoir(subject, content, cfg):  # noqa: ANN001
        state.extract_calls.append((subject, content))
        return None

    monkeypatch.setattr(llm, "chat_answer", chat_answer)
    monkeypatch.setattr(llm, "ensure_summaries", lambda conn, items, cfg: {})
    monkeypatch.setattr(llm, "extract_devoir", extract_devoir)
    monkeypatch.setattr(llm, "weekly_synthesis", lambda rows, cfg: None)
    return state


def patch_login(monkeypatch, *, password="tok", username="u", uuid="uuid-nouveau"):
    """Remplace le seul appel réseau restant : la connexion QR."""
    state = SimpleNamespace(qr=None, pin=None, uuid=None)

    class _Client:
        def export_credentials(self):  # noqa: ANN202
            return {"password": password, "username": username, "uuid": uuid}

    def login_with_qr(qr, pin, app_uuid, account_pin=None, **kwargs):  # noqa: ANN001
        state.qr, state.pin, state.uuid = qr, pin, app_uuid
        return _Client(), {
            "password": password,
            "username": username,
            "uuid": uuid,
            "client_identifier": "ident-2",
        }

    monkeypatch.setattr(auth, "login_with_qr", login_with_qr)
    return state


def plain(html_text: str) -> str:
    return re.sub(r"<[^>]+>", "", html_text)


def user_id_of(conn, chat_id: int = CHAT) -> str:
    row = db.get_user_by_chat(conn, chat_id)
    assert row is not None
    return row["id"]


# ─────────────────────────── 1. connexion complète ───────────────────────────


def test_parcours_connexion_complet(cfg, conn, pronote, llm_fake, monkeypatch):
    seen = {}

    async def scenario(rig):
        start_texts = await rig.text("/start")
        assert any("QR code" in t and "capture" in t for t in start_texts)

        texts = await rig.photo(qr_png())
        assert any("QR lu" in t for t in texts)

        # Mauvais code : le VRAI pronotepy échoue localement (aucun réseau).
        texts = await rig.text("1234")
        assert MSG_CRYPTO in texts

        # QR neuf, puis code trop court.
        texts = await rig.photo(qr_png(login="22" * 16, jeton="33" * 16))
        assert any("QR lu" in t for t in texts)
        texts = await rig.text("12")
        assert MSG_CODE in texts

        # Succès simulé (seule la connexion est remplacée).
        patch_login(monkeypatch)
        texts = await rig.text("1234")
        joined = plain("\n".join(texts))
        assert "Connecté, 3A" in joined
        for idx, subject in enumerate(SUBJECTS, start=1):
            assert f"{idx}. {subject}" in joined
        seen["user"] = db.get_user_by_chat(conn, CHAT)

    run_flow(cfg, conn, scenario)

    user = seen["user"]
    assert user is not None
    assert user["class_name"] == "3A"


# ─────────────────────────── 2. priorités + première liste ───────────────────────────


def test_priorites_et_premiere_liste(cfg, conn, pronote, llm_fake, monkeypatch):
    maths = devoir(
        "Mathématiques", "Contrôle sur les dérivées. Réviser le chapitre 3.", TODAY - timedelta(days=3)
    )
    svt = devoir("SVT", "Lire le chapitre 2 sur la tectonique.", TODAY + timedelta(days=2))
    anglais = devoir("Anglais", "Exercice 5 page 42 du workbook.", TODAY - timedelta(days=1))
    pronote.items = [maths, svt, anglais]
    patch_login(monkeypatch)

    async def scenario(rig):
        await rig.text("/start")
        await rig.photo(qr_png())
        await rig.text("1234")
        texts = await rig.text("3 1 5")
        seen["texts"] = texts

    seen = {}
    run_flow(cfg, conn, scenario)

    user_id = user_id_of(conn)
    assert list(
        subject
        for subject, _ in sorted(
            db.get_priorities(conn, user_id).items(), key=lambda kv: kv[1]
        )
    ) == ["SVT", "Mathématiques", "Anglais"]

    joined = plain("\n".join(seen["texts"]))
    assert "— 2 item(s)" in joined
    assert "Contrôle sur les dérivées" in joined
    assert joined.index("Contrôle sur les dérivées") < joined.index("Exercice 5 page 42")

    assert db.resolve_list_ref(conn, user_id, 1) == maths.key
    assert db.resolve_list_ref(conn, user_id, 2) == anglais.key


# ─────────────────────────── 3. /fait ───────────────────────────


def test_fait_coche_et_retire_de_la_liste(cfg, conn, pronote, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)
    items = [
        devoir("Mathématiques", "DM n°4 : suite arithmétique.", TODAY - timedelta(days=4)),
        devoir("SVT", "Schéma de la cellule à légender.", TODAY - timedelta(days=2)),
        devoir("Anglais", "Apprendre le vocabulaire de la leçon 3.", TODAY - timedelta(days=1)),
    ]
    db.replace_items(conn, user_id, items)

    async def scenario(rig):
        texts = await rig.text("/retard")
        joined = plain("\n".join(texts))
        match = re.search(r"(?m)^(\d+)\. ", joined)
        assert match, "aucune ligne numérotée dans /retard"
        idx = int(match.group(1))
        tracked = db.resolve_list_ref(conn, user_id, idx)
        assert tracked is not None
        tracked_item = next(item for item in db.get_items(conn, user_id) if item.key == tracked)

        texts = await rig.text(f"/fait {idx}")
        assert texts[-1] == "✅"
        assert tracked in db.done_keys(conn, user_id)

        texts = await rig.text("/retard")
        joined = plain("\n".join(texts))
        assert "— 2 item(s)" in joined
        assert tracked_item.title not in joined

        texts = await rig.text("/fait 999")
        assert MSG_INDEX in texts

    run_flow(cfg, conn, scenario)


# ─────────────────────────── 4. découpage ───────────────────────────


def test_decoupage_200_items(cfg, conn, pronote, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)
    long_tail = "réviser les chapitres du trimestre et refaire les exercices corrigés "
    items = [
        Item(
            key=f"item-{i:03d}",
            source="devoir",
            subject="Mathématiques",
            title=f"Exercice {i} — " + long_tail * 4,
            body=f"Exercice {i}. " + long_tail * 4,
            due_date=TODAY - timedelta(days=(i % 9) + 1),
            kind="devoir",
        )
        for i in range(200)
    ]
    db.replace_items(conn, user_id, items)

    async def scenario(rig):
        seen["texts"] = await rig.text("/retard")

    seen = {}
    run_flow(cfg, conn, scenario)

    texts = seen["texts"]
    assert len(texts) > 1, "les 15 lignes de la page 1 doivent être découpées"
    assert all(len(text) < 4096 for text in texts)
    assert "— 200 item(s)" in texts[0]

    numbers = [
        int(n)
        for text in texts
        for n in re.findall(r"(?m)^(\d+)\. ", text)
    ]
    assert numbers[:15] == list(range(1, 16))
    assert db.resolve_list_ref(conn, user_id, 15) is not None


# ─────────────────────────── 5. chat libre ancré ───────────────────────────


def test_chat_libre_renvoie_la_reponse_tronquee(cfg, conn, pronote, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)
    db.replace_items(
        conn,
        user_id,
        [
            devoir("Mathématiques", "Contrôle sur les dérivées.", TODAY - timedelta(days=2)),
            devoir("SVT", "Lire le chapitre 2 sur la tectonique.", TODAY - timedelta(days=1)),
        ],
    )
    llm_fake.answers = ["A" * 900]

    async def scenario(rig):
        seen["texts"] = await rig.text("j'ai quoi en maths ?")

    seen = {}
    run_flow(cfg, conn, scenario)

    assert len(llm_fake.chat_calls) == 1
    question, data_block = llm_fake.chat_calls[0]
    assert question == "j'ai quoi en maths ?"
    assert "Mathématiques" in data_block
    assert seen["texts"][-1] == "A" * 700


def test_chat_libre_contexte_filtre_sur_la_question(cfg, conn, pronote, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)
    db.replace_items(
        conn,
        user_id,
        [
            devoir("Mathématiques", "Contrôle sur les dérivées.", TODAY - timedelta(days=2)),
            devoir("SVT", "Lire le chapitre 2 sur la tectonique.", TODAY - timedelta(days=1)),
        ],
    )

    async def scenario(rig):
        await rig.text("j'ai quoi en maths ?")

    run_flow(cfg, conn, scenario)

    _, data_block = llm_fake.chat_calls[0]
    assert "Mathématiques" in data_block
    assert "SVT" not in data_block


# ─────────────────────────── 6. garde-fou de volume ───────────────────────────


def test_garde_fou_volume_10_questions(cfg, conn, pronote, llm_fake, make_user):
    make_user(chat_id=CHAT)

    async def scenario(rig):
        for i in range(10):
            texts = await rig.text(f"question {i}")
            assert texts, f"question {i} sans réponse"
        assert len(llm_fake.chat_calls) == 10
        texts = await rig.text("question 11")
        assert texts == [MSG_QUOTA]

    run_flow(cfg, conn, scenario)

    assert len(llm_fake.chat_calls) == 10


# ─────────────────────────── 7. recovery ───────────────────────────


def test_start_avec_jeton_invalide_redemande_le_qr(cfg, conn, pronote, llm_fake, make_user):
    user_id = make_user(chat_id=CHAT)
    db.set_last_error(conn, user_id, fetch.ERR_TOKEN)

    async def scenario(rig):
        seen["texts"] = await rig.text("/start")

    seen = {}
    run_flow(cfg, conn, scenario)

    joined = plain("\n".join(seen["texts"]))
    assert "QR code" in joined
    assert "expiré" in joined
    assert "En retard" not in joined


def test_reauth_compte_existant_ne_cree_pas_de_second_compte(
    cfg, conn, pronote, llm_fake, make_user, secret, monkeypatch
):
    user_id = make_user(chat_id=CHAT, password="ancien-jeton")
    db.set_priorities(conn, user_id, ["SVT"])
    db.set_last_error(conn, user_id, fetch.ERR_TOKEN)
    patch_login(monkeypatch)

    async def scenario(rig):
        await rig.text("/start")
        await rig.photo(qr_png())
        seen["texts"] = await rig.text("1234")

    seen = {}
    run_flow(cfg, conn, scenario)

    joined = plain("\n".join(seen["texts"]))
    assert "Connecté, 3A" in joined

    rows = conn.execute(
        "SELECT * FROM users WHERE chat_id = ?", (CHAT,)
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == user_id
    assert crypto.decrypt(row["token_enc"], secret) == "tok"
    assert row["last_error"] is None
    assert row["app_uuid"] == "uuid-nouveau"
    assert db.get_priorities(conn, user_id) == {"SVT": 0}


# ─────────────────────────── bug observé : double traitement ───────────────────────────


def test_pas_de_double_traitement_pendant_la_connexion(
    cfg, conn, pronote, llm_fake, monkeypatch
):
    patch_login(monkeypatch)

    async def scenario(rig):
        await rig.text("/start")
        await rig.photo(qr_png())
        await rig.text("1234")

    run_flow(cfg, conn, scenario)

    assert llm_fake.chat_calls == []
