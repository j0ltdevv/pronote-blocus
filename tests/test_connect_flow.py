"""Parcours de connexion, de `/start` à la première liste, sans réseau.

Le fil complet est rejoué ici : `/start` → photo du QR → code à quatre
chiffres → liste numérotée des matières → `3 1 5` → première liste. Les cas
d'erreur du même fil suivent : QR illisible, texte au lieu d'une photo, PIN
refusé, `/annuler` à chaque étape, reprise après expiration.

Seules la couche réseau PRONOTE et la connexion QR sont simulées. Le
transport Telegram est le faux local (`tests.fake_telegram`), le décodage du
QR est le VRAI `auth.decode_qr_image` (zxingcpp) : un PNG de QR fabriqué par
`segno` est donc vraiment lu.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from datetime import date, timedelta
from types import SimpleNamespace

import pronotepy
import pytest
import segno
from telegram import Update

from app import config, crypto, db, llm
from app.main import (
    MSG_CODE,
    MSG_CONNEXION,
    MSG_CRYPTO,
    MSG_EXPIRE,
    MSG_MFA,
    build_application,
)
from app.pronote import auth, fetch
from app.pronote.normalize import Item, make_item
from tests.fake_telegram import FakeTelegramRequest

CHAT = 4242
QR_URL = "https://ETABLISSEMENT.index-education.net/pronote/eleve.html"
SUBJECTS = [
    "Mathématiques",
    "Histoire-Géographie",
    "SVT",
    "Physique-Chimie",
    "Anglais",
]

TODAY = config.today()

#: Caches du `ConversationHandler` remplis pendant la connexion.
CACHES = ("qr_cache", "pending_uuid", "mfa_tries", "subjects", "pin_code")

PROMPT_QR = "QR code"  # fragment stable du message d'accueil
PROMPT_PIN = "QR lu"
PROMPT_MATIERES = "3 1 5"


# ─────────────────────────── données de test ───────────────────────────


def qr_png(login: str = "00" * 16, jeton: str = "11" * 16) -> bytes:
    """Un VRAI PNG de QR PRONOTE, lisible par zxingcpp."""
    payload = {"login": login, "jeton": jeton, "url": QR_URL}
    return png_of(qr_text(payload))


def qr_text(payload: dict) -> str:
    return json.dumps(payload)


def png_of(text: str) -> bytes:
    """PNG d'un QR portant exactement `text`."""
    buffer = io.BytesIO()
    segno.make(text).save(buffer, kind="png")
    return buffer.getvalue()


def png_blanc() -> bytes:
    """PNG valide mais sans aucun QR : le décodeur ne trouve rien."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (96, 96), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def devoir(subject: str, body: str, due: date) -> Item:
    return make_item(kind="devoir", source="devoir", subject=subject, body=body, due_date=due)


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


def plain(html_text: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", html_text)


# ─────────────────────────── montage ───────────────────────────


class Rig:
    """Application réelle, transport simulé, helpers d'envoi."""

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

    async def _dispatch(self, payload: dict, chat_id: int) -> list[str]:
        before = len(self.fake.sent)
        update = Update.de_json(payload, self.app.bot)
        await self.app.process_update(update)
        # Laisse tourner les tâches de fond (phase `cours`, digest…).
        await asyncio.sleep(0.05)
        if self.errors:
            error = self.errors[0]
            raise AssertionError(
                f"un handler a levé {type(error).__name__}: {error}"
            ) from error
        return self.fake.since(before, chat_id)

    async def text(self, text: str) -> list[str]:
        self._update_id += 1
        return await self._dispatch(
            text_update(text, self.chat_id, self._update_id), self.chat_id
        )

    async def photo(self, data: bytes) -> list[str]:
        self._update_id += 1
        self._file_seq += 1
        file_id = f"qr{self._file_seq}"
        self.fake.files[f"photos/{file_id}.png"] = data
        return await self._dispatch(
            photo_update(file_id, self.chat_id, self._update_id), self.chat_id
        )

    # ── inspection de l'état interne du `ConversationHandler` ──

    def cache(self, name: str):  # noqa: ANN201
        return self.app.bot_data[name]

    def assert_caches_vides(self) -> None:
        for name in CACHES:
            assert self.cache(name).get(self.chat_id) is None, (
                f"la clé {name!r} est restée après la sortie de la conversation"
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


def joined(texts: list[str]) -> str:
    return plain("\n".join(texts))


def user_id_of(conn, chat_id: int = CHAT) -> str:
    row = db.get_user_by_chat(conn, chat_id)
    assert row is not None
    return row["id"]


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
        return fetch.SyncResult(True, None, list(state.items), state.class_name, phase, 0)

    def sync_user(conn, user_id, cfg, phase="items"):  # noqa: ANN001
        state.sync_calls.append((user_id, phase))
        return fetch.SyncResult(True, None, [], state.class_name, phase, 0)

    monkeypatch.setattr(fetch, "class_name_of", class_name_of)
    monkeypatch.setattr(fetch, "subjects_of", subjects_of)
    monkeypatch.setattr(fetch, "collect", collect)
    monkeypatch.setattr(fetch, "sync_user", sync_user)
    return state


@pytest.fixture
def llm_stub(monkeypatch):
    """Aucun appel LLM : la liste de la première connexion reste déterministe."""
    monkeypatch.setattr(llm, "chat_answer", lambda question, data, cfg: "Réponse simulée.")
    monkeypatch.setattr(llm, "ensure_summaries", lambda conn, items, cfg: {})
    monkeypatch.setattr(llm, "extract_devoir", lambda subject, content, cfg: None)
    monkeypatch.setattr(llm, "weekly_synthesis", lambda rows, cfg: None)


class LoginSim:
    """Remplace le seul appel réseau restant : `auth.login_with_qr`.

    `raise_first` est la liste des pannes à lever, une par appel, avant de
    rendre une connexion réussie (`pronotepy` n'est jamais appelé).
    """

    def __init__(
        self,
        monkeypatch,
        *,
        raise_first: list[BaseException] | None = None,
        password: str = "jeton-initial",
        client_identifier: str = "ident-2",
    ) -> None:
        self.calls: list[SimpleNamespace] = []
        self.pannes = list(raise_first or [])
        self.password = password
        self.client_identifier = client_identifier
        monkeypatch.setattr(auth, "login_with_qr", self._login)

    def _login(self, qr, pin, app_uuid, account_pin=None, **kwargs):  # noqa: ANN001
        self.calls.append(
            SimpleNamespace(qr=qr, pin=pin, app_uuid=app_uuid, account_pin=account_pin)
        )
        if self.pannes:
            raise self.pannes.pop(0)
        client = SimpleNamespace(name="client")
        return client, {
            "password": self.password,
            "username": "demonstration",
            "uuid": app_uuid,
            "client_identifier": self.client_identifier,
            "account_pin": account_pin,
        }


@pytest.fixture
def login(monkeypatch) -> LoginSim:
    return LoginSim(monkeypatch)


# ═════════════════ 1. le fil nominal, de /start à la première liste ═════════════════


def test_parcours_complet_jusqu_a_la_premiere_liste(
    cfg, conn, pronote, llm_stub, login, secret
):
    """`/start` → photo → PIN → matières → `3 1 5` → liste, et rien d'autre."""
    # Anglais est le plus en retard (urgence), puis à égalité de retard
    # l'ordre des priorités décide : SVT (1er), Mathématiques (2e), et
    # Histoire-Géographie, non prioritaire, descend mais reste visible.
    anglais = devoir("Anglais", "Exercice 5 page 42 du workbook.", TODAY - timedelta(days=5))
    svt = devoir("SVT", "Lire le chapitre 2 sur la tectonique.", TODAY - timedelta(days=2))
    maths = devoir(
        "Mathématiques",
        "Contrôle sur les dérivées. Réviser le chapitre 3.",
        TODAY - timedelta(days=2),
    )
    histoire = devoir(
        "Histoire-Géographie", "Fiche de révision sur la Guerre froide.", TODAY - timedelta(days=2)
    )
    pronote.items = [maths, svt, anglais, histoire]

    async def scenario(rig):
        # 1. `/start` : rien n'est encore connu, on demande le QR.
        texts = await rig.text("/start")
        assert PROMPT_QR in joined(texts)
        assert "QR expire vite" in joined(texts)

        # 2. Photo du QR : lu pour de vrai, on demande le code à 4 chiffres.
        texts = await rig.photo(qr_png())
        assert PROMPT_PIN in joined(texts)

        # 3. Code PIN : connexion simulée, classe annoncée, matières numérotées.
        texts = await rig.text("1234")
        assert "Je me connecte à PRONOTE…" in texts
        body = joined(texts)
        assert "Connecté, 3A" in body
        for index, subject in enumerate(SUBJECTS, start=1):
            assert f"{index}. {subject}" in body
        assert PROMPT_MATIERES in body
        # La liste des matières précède la demande de priorités.
        assert body.index("1. Mathématiques") < body.index(PROMPT_MATIERES)

        # 4. `3 1 5` : priorités rangées, puis première liste tout de suite.
        texts = await rig.text("3 1 5")
        first = joined(texts)
        assert "C'est noté" in first
        assert "— 4 item(s)" in first
        ranking = [
            "Exercice 5 page 42",
            "Lire le chapitre 2",
            "Contrôle sur les dérivées",
            "Fiche de révision sur la Guerre froide",
        ]
        for item in ranking:
            assert item in first, item
        # Urgence d'abord, puis priorités, puis le reste : jamais masqué.
        positions = [first.index(item) for item in ranking]
        assert positions == sorted(positions)

    run_flow(cfg, conn, scenario)

    # ── ce qui a été rangé en base ──
    user = db.get_user_by_chat(conn, CHAT)
    assert user is not None
    assert user["username"] == "00" * 16  # le login du QR fait foi
    assert user["pronote_url"] == QR_URL  # forme publique, jamais l'URL mobile
    assert user["class_name"] == "3A"
    assert user["client_identifier"] == "ident-2"
    assert user["account_pin_enc"] is None  # pas de MFA sur ce compte
    assert user["last_error"] is None
    assert crypto.decrypt(user["token_enc"], secret) == "jeton-initial"

    # L'`app_uuid` envoyé à PRONOTE est celui rangé, et ce n'est pas le login.
    sent = login.calls[0]
    assert sent.qr == {"login": "00" * 16, "jeton": "11" * 16, "url": QR_URL}
    assert sent.pin == "1234"
    assert sent.account_pin is None
    assert user["app_uuid"] == sent.app_uuid
    assert user["app_uuid"] != user["username"]

    # La collecte immédiate a bien eu lieu pendant la connexion.
    assert len(db.get_items(conn, user["id"])) == 4
    assert pronote.collect_calls == [(user["id"], "items")]

    # Priorités dans l'ordre donné, et numérotation de la liste resolvable.
    assert list(
        subject
        for subject, _ in sorted(
            db.get_priorities(conn, user["id"]).items(), key=lambda kv: kv[1]
        )
    ) == ["SVT", "Mathématiques", "Anglais"]
    assert db.resolve_list_ref(conn, user["id"], 1) == anglais.key
    assert db.resolve_list_ref(conn, user["id"], 2) == svt.key
    assert db.resolve_list_ref(conn, user["id"], 3) == maths.key
    assert db.resolve_list_ref(conn, user["id"], 4) == histoire.key


def test_apres_la_premiere_liste_la_conversation_est_fermee(cfg, conn, pronote, llm_stub, login):
    """Une fois `3 1 5` donné, un texte libre retombe sur le chat, pas sur le fil."""
    async def scenario(rig):
        await rig.text("/start")
        await rig.photo(qr_png())
        await rig.text("1234")
        await rig.text("3 1 5")
        rig.assert_caches_vides()

        # `/start` ne rouvre plus la connexion : l'élève est connu.
        texts = await rig.text("/start")
        assert "QR" not in joined(texts)

    run_flow(cfg, conn, scenario)

    assert db.get_user_by_chat(conn, CHAT) is not None


# ═════════════════ 2. texte à la place d'une photo ═════════════════


def test_texte_quelconque_a_l_etape_qr_ne_perd_pas_l_etape(cfg, conn, pronote, llm_stub, login):
    """Un message texte au lieu de la capture : refus explicite, puis ça marche."""
    async def scenario(rig):
        await rig.text("/start")

        texts = await rig.text("bonjour, voici mon QR")
        assert auth.QrNotPronote().message in texts

        # Le fil est toujours à l'étape QR : la vraie photo passe.
        texts = await rig.photo(qr_png())
        assert PROMPT_PIN in joined(texts)

        texts = await rig.text("1234")
        assert "Connecté, 3A" in joined(texts)

    run_flow(cfg, conn, scenario)

    assert db.get_user_by_chat(conn, CHAT) is not None


def test_qr_colle_en_texte_remplace_la_photo(cfg, conn, pronote, llm_stub, login):
    """Le contenu du QR collé en texte est accepté sans passer par la photo."""
    async def scenario(rig):
        await rig.text("/start")
        texts = await rig.text(qr_text({"login": "aa" * 16, "jeton": "bb" * 16, "url": QR_URL}))
        assert PROMPT_PIN in joined(texts)

        texts = await rig.text("4321")
        assert "Connecté, 3A" in joined(texts)

    run_flow(cfg, conn, scenario)

    assert login.calls[0].pin == "4321"
    assert login.calls[0].qr["login"] == "aa" * 16


def test_photo_envoyee_a_l_etape_pin_est_ignoree(cfg, conn, pronote, llm_stub, login):
    """Caractérisation : une photo pendant la saisie du PIN ne déclenche rien."""
    async def scenario(rig):
        await rig.text("/start")
        await rig.photo(qr_png())

        texts = await rig.photo(qr_png(login="44" * 16, jeton="55" * 16))
        assert texts == []  # aucun handler : l'étape PIN n'accepte que du texte

        # L'étape est intacte, le QR mémorisé est toujours le premier.
        texts = await rig.text("1234")
        assert "Connecté, 3A" in joined(texts)

    run_flow(cfg, conn, scenario)

    # Le second QR n'a jamais remplacé le premier.
    assert login.calls[0].qr["login"] == "00" * 16


# ═════════════════ 3. QR illisible ═════════════════


def test_qr_illisible_ou_etranger_reste_a_l_etape_qr(cfg, conn, pronote, llm_stub, login):
    """Photo floue, PNG sans QR, QR non PRONOTE : trois refus, un seul état."""
    async def scenario(rig):
        await rig.text("/start")

        texts = await rig.photo(b"\x00\x01pas une image")
        assert auth.QrNotReadable().message in texts

        texts = await rig.photo(png_blanc())
        assert auth.QrNotReadable().message in texts

        texts = await rig.photo(png_of("bonjour"))
        assert auth.QrNotPronote().message in texts

        # Le QR utile arrive enfin : même étape, il est pris.
        texts = await rig.photo(qr_png())
        assert PROMPT_PIN in joined(texts)

    run_flow(cfg, conn, scenario)

    assert db.get_user_by_chat(conn, CHAT) is None  # rien n'a été créé entre-temps


def test_pin_reclame_apres_un_qr_perdu_en_memoire(cfg, conn, pronote, llm_stub, login):
    """Sans QR en cache, un code à 4 chiffres renvoie à l'étape QR (et non au PIN)."""
    async def scenario(rig):
        await rig.text("/start")
        await rig.photo(qr_png())
        # Le cache est vidé comme le ferait un redémarrage du bot.
        rig.cache("qr_cache").clear()

        texts = await rig.text("1234")
        assert "Je n'ai plus le QR en mémoire. Renvoie-le, puis le code." in texts
        assert login.calls == []

        texts = await rig.photo(qr_png(login="cc" * 16, jeton="dd" * 16))
        assert PROMPT_PIN in joined(texts)
        texts = await rig.text("1234")
        assert "Connecté, 3A" in joined(texts)

    run_flow(cfg, conn, scenario)


# ═════════════════ 4. PIN refusé ═════════════════


def test_pin_ou_code_a_quatre_chiffres_strict(cfg, conn, pronote, llm_stub, login):
    """Seuls 4 chiffres passent ; espaces autour tolérés, lettres refusées."""
    async def scenario(rig):
        await rig.text("/start")
        await rig.photo(qr_png())

        for mauvais in ("12", "12345", "12a4", "12 34", "0000 1111"):
            texts = await rig.text(mauvais)
            assert MSG_CODE in texts, mauvais
            assert login.calls == [], mauvais

        # Les espaces autour sont rognés, le code reste valide.
        texts = await rig.text(" 1234 ")
        assert "Connecté, 3A" in joined(texts)

    run_flow(cfg, conn, scenario)

    assert login.calls[0].pin == "1234"


def test_pin_chiffres_non_ascii_passe_le_filtre(cfg, conn, pronote, llm_stub, login):
    """Caractérisation : `\\d` accepte les chiffres Unicode, pas seulement 0-9.

    Le code part donc tel quel vers PRONOTE au lieu d'être refusé par
    « Ça doit être exactement 4 chiffres. ».
    """
    async def scenario(rig):
        await rig.text("/start")
        await rig.photo(qr_png())

        texts = await rig.text("١٢٣٤")  # quatre chiffres arabo-indiens
        assert MSG_CODE not in texts
        assert "Connecté, 3A" in joined(texts)

    run_flow(cfg, conn, scenario)

    assert login.calls[0].pin == "١٢٣٤"


def test_pin_refuse_par_pronote_redemande_un_qr(cfg, conn, pronote, llm_stub, monkeypatch):
    """`CryptoError` : message dédié, retour à l'étape QR, puis reprise réussie."""
    sim = LoginSim(
        monkeypatch, raise_first=[pronotepy.CryptoError("code à 4 chiffres refusé")]
    )

    async def scenario(rig):
        await rig.text("/start")
        await rig.photo(qr_png())
        texts = await rig.text("1234")
        assert MSG_CRYPTO in texts
        assert "Connecté" not in joined(texts)

        # Étape QR : un nouveau code à 4 chiffres est traité comme du texte de QR.
        texts = await rig.text("1234")
        assert auth.QrNotPronote().message in texts

        texts = await rig.photo(qr_png(login="ee" * 16, jeton="ff" * 16))
        assert PROMPT_PIN in joined(texts)
        texts = await rig.text("9876")
        assert "Connecté, 3A" in joined(texts)

    run_flow(cfg, conn, scenario)

    assert [call.pin for call in sim.calls] == ["1234", "9876"]
    assert [call.qr["login"] for call in sim.calls] == ["00" * 16, "ee" * 16]
    assert db.get_user_by_chat(conn, CHAT)["username"] == "ee" * 16


@pytest.mark.parametrize(
    ("panne", "attendu"),
    [
        (auth.QrExpired(), auth.QrExpired().message),
        (RuntimeError("socket fermée par PRONOTE"), MSG_EXPIRE),
    ],
    ids=["qr-expire", "panne-inattendue"],
)
def test_connexion_impossible_redemande_le_qr(
    cfg, conn, pronote, llm_stub, monkeypatch, panne, attendu
):
    """QR expiré ou panne inattendue : message, étape QR, et reprise au QR suivant."""
    sim = LoginSim(monkeypatch, raise_first=[panne])

    async def scenario(rig):
        await rig.text("/start")
        await rig.photo(qr_png())
        texts = await rig.text("1234")
        assert attendu in texts
        assert "Connecté" not in joined(texts)
        # Le QR reste mémorisé : l'élève n'a qu'un nouveau code à envoyer.
        assert rig.cache("qr_cache").get(CHAT)["login"] == "00" * 16

        # Reprise : nouveau QR, nouveau code.
        await rig.photo(qr_png(login="12" * 16, jeton="34" * 16))
        texts = await rig.text("1111")
        assert "Connecté, 3A" in joined(texts)

    run_flow(cfg, conn, scenario)

    assert len(sim.calls) == 2
    assert db.get_user_by_chat(conn, CHAT)["username"] == "12" * 16


def test_echecs_repetes_ne_bloquent_pas_l_eleve(cfg, conn, pronote, llm_stub, monkeypatch):
    """Caractérisation : aucun verrou après plusieurs échecs — chaque échec rend la main."""
    sim = LoginSim(
        monkeypatch,
        raise_first=[
            RuntimeError("premier code refusé"),
            RuntimeError("second code refusé"),
        ],
    )

    async def scenario(rig):
        await rig.text("/start")
        await rig.photo(qr_png())
        texts = await rig.text("1234")
        assert MSG_EXPIRE in texts

        # Un texte non-QR pendant l'étape QR ne consomme aucun essai.
        texts = await rig.text("1234")
        assert auth.QrNotPronote().message in texts
        assert len(sim.calls) == 1

        # Nouveau QR, nouveau code : réessai autorisé, et de nouveau refusé.
        await rig.photo(qr_png(login="77" * 16, jeton="88" * 16))
        texts = await rig.text("2222")
        assert MSG_EXPIRE in texts

    run_flow(cfg, conn, scenario)

    assert len(sim.calls) == 2
    assert db.get_user_by_chat(conn, CHAT) is None


def test_mfa_puis_code_pin_refuse_ferme_la_conversation(
    cfg, conn, pronote, llm_stub, monkeypatch
):
    """Le compte exige un code PIN ; s'il est faux, on sort proprement."""
    sim = LoginSim(
        monkeypatch,
        raise_first=[
            pronotepy.MFAError("code PIN demandé"),
            RuntimeError("code PIN refusé"),
        ],
    )

    async def scenario(rig):
        await rig.text("/start")
        await rig.photo(qr_png())

        texts = await rig.text("1234")
        assert MSG_MFA in texts

        texts = await rig.text("9876")
        assert "Le code PIN n'a pas marché" in joined(texts)
        rig.assert_caches_vides()

        # Conversation fermée : le texte suivant repart de la case départ.
        texts = await rig.text("bonjour")
        assert MSG_CONNEXION in texts

    run_flow(cfg, conn, scenario)

    assert [(call.pin, call.account_pin) for call in sim.calls] == [
        ("1234", None),
        ("1234", "9876"),
    ]
    assert db.get_user_by_chat(conn, CHAT) is None


def test_mfa_reussi_range_le_code_pin_du_compte(cfg, conn, pronote, llm_stub, monkeypatch, secret):
    """Le code PIN du compte est mémorisé chiffré : il resservira au prochain login."""
    LoginSim(
        monkeypatch,
        raise_first=[pronotepy.MFAError("code PIN demandé")],
    )

    async def scenario(rig):
        await rig.text("/start")
        await rig.photo(qr_png())
        await rig.text("1234")
        texts = await rig.text("9876")
        assert "Connecté, 3A" in joined(texts)

    run_flow(cfg, conn, scenario)

    row = db.get_user_by_chat(conn, CHAT)
    assert crypto.decrypt(row["account_pin_enc"], secret) == "9876"


# ═════════════════ 5. /annuler à chaque étape ═════════════════


def test_annuler_a_l_etape_qr_oublie_le_qr_et_ferme(cfg, conn, pronote, llm_stub, login):
    async def scenario(rig):
        await rig.text("/start")
        texts = await rig.text("/annuler")
        assert "Annulé" in joined(texts)
        rig.assert_caches_vides()

        # Plus de conversation : le texte libre retombe sur la case départ.
        texts = await rig.text("1234")
        assert MSG_CONNEXION in texts

        # `/start` rouvre le fil proprement.
        texts = await rig.text("/start")
        assert PROMPT_QR in joined(texts)
        texts = await rig.photo(qr_png())
        assert PROMPT_PIN in joined(texts)

    run_flow(cfg, conn, scenario)


def test_annuler_a_l_etape_pin_oublie_le_qr(cfg, conn, pronote, llm_stub, login):
    async def scenario(rig):
        await rig.text("/start")
        await rig.photo(qr_png())
        assert rig.cache("qr_cache").get(CHAT) is not None

        texts = await rig.text("/annuler")
        assert "Annulé" in joined(texts)
        rig.assert_caches_vides()

        # Le QR oublié ne peut pas resservir : aucun appel de connexion.
        texts = await rig.text("1234")
        assert MSG_CONNEXION in texts
        assert login.calls == []

        # Reprise complète après annulation.
        await rig.text("/start")
        await rig.photo(qr_png())
        texts = await rig.text("1234")
        assert "Connecté, 3A" in joined(texts)

    run_flow(cfg, conn, scenario)

    assert db.get_user_by_chat(conn, CHAT) is not None


def test_annuler_a_l_etape_matieres_ne_range_aucune_priorite(
    cfg, conn, pronote, llm_stub, login
):
    pronote.items = [devoir("SVT", "Lire le chapitre 2.", TODAY - timedelta(days=1))]

    async def scenario(rig):
        await rig.text("/start")
        await rig.photo(qr_png())
        await rig.text("1234")
        assert rig.cache("subjects").get(CHAT) == SUBJECTS

        texts = await rig.text("/annuler")
        assert "Annulé" in joined(texts)
        rig.assert_caches_vides()

        # `3 1 5` n'est plus lu comme des priorités : la conversation est
        # fermée, le texte part au chat libre (le compte, lui, existe).
        texts = await rig.text("3 1 5")
        body = joined(texts)
        assert "C'est noté" not in body
        assert "Réponds avec les numéros" not in body

    run_flow(cfg, conn, scenario)

    user_id = user_id_of(conn)  # le compte existe : la connexion avait abouti
    assert db.get_priorities(conn, user_id) == {}


def test_numeros_hors_liste_ou_en_double(cfg, conn, pronote, llm_stub, login):
    """Hors liste : on redemande. Doublon : une seule priorité rangée."""
    async def scenario(rig):
        await rig.text("/start")
        await rig.photo(qr_png())
        await rig.text("1234")

        texts = await rig.text("3 3 9")
        assert "Réponds avec les numéros de la liste ci-dessus" in joined(texts)
        assert rig.cache("subjects").get(CHAT) == SUBJECTS

        texts = await rig.text("0")
        assert "Réponds avec les numéros de la liste ci-dessus" in joined(texts)

        texts = await rig.text("3 3 5")
        assert "C'est noté" in joined(texts)

    run_flow(cfg, conn, scenario)

    user_id = user_id_of(conn)
    assert list(
        subject
        for subject, _ in sorted(
            db.get_priorities(conn, user_id).items(), key=lambda kv: kv[1]
        )
    ) == ["SVT", "Anglais"]


def test_start_avec_un_compte_en_erreur_rouvre_le_parcours(
    cfg, conn, pronote, llm_stub, login, make_user
):
    """Jeton révoqué puis `/start` : le QR est redemandé, la connexion refaite."""
    user_id = make_user(chat_id=CHAT, password="ancien-jeton")
    db.set_last_error(conn, user_id, fetch.ERR_TOKEN)

    async def scenario(rig):
        texts = await rig.text("/start")
        body = joined(texts)
        assert "a expiré" in body
        assert PROMPT_QR in body

        await rig.photo(qr_png())
        texts = await rig.text("1234")
        assert "Connecté, 3A" in joined(texts)

    run_flow(cfg, conn, scenario)

    rows = conn.execute("SELECT * FROM users WHERE chat_id = ?", (CHAT,)).fetchall()
    assert len(rows) == 1  # ré-appairage, pas de second compte
    assert rows[0]["id"] == user_id
    assert rows[0]["last_error"] is None
