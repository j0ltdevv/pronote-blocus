"""Correctifs de `app/main.py` : `/annuler` global, `/fait` fantôme, fuites de secrets.

Chaque test échoue si le correctif correspondant est retiré :

- `/annuler` hors parcours : sans handler global, aucun message ne sortait ;
- `/fait N` : `resolve_list_ref` acceptait une clé absente de `items` ;
- journaux : QR/PIN et mot de passe journalisaient le message d'exception
  (jeton Telegram, URL PRONOTE) ou un `chat_id`.

Hors réseau : application Telegram réelle + transport simulé, base SQLite dans
`tmp_path`, couche PRONOTE simulée.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from datetime import timedelta

import pytest
import segno
from telegram import Update
from telegram.error import InvalidToken

from app import config, db
from app.main import MSG_ANNULER, MSG_INDEX, PROMPT_QR, build_application
from app.pronote import auth
from app.pronote.normalize import make_item
from tests.fake_telegram import FakeTelegramRequest

CHAT = 4242
QR_URL = "https://ETABLISSEMENT.index-education.net/pronote/eleve.html"
TODAY = config.today()


# ─────────────────────────── montage ───────────────────────────


@pytest.fixture
def cfg(secret, tmp_path) -> config.Config:
    return config.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "123456:JETON-SECRET-DE-TEST",
            "APP_SECRET": secret,
            "DATA_DIR": str(tmp_path / "data"),
            "WINDOW_START": "2026-10-01",
            "ADMIN_CHAT_ID": "999999",
            "LLM_BASE_URL": "http://127.0.0.1:9/v1",
            "LLM_API_KEY": "cle-de-test",
            "LLM_MODEL": "modele-de-test",
        }
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


def qr_png() -> bytes:
    """Un VRAI PNG de QR code, lisible par `zxingcpp`."""
    payload = {"login": "00" * 16, "jeton": "11" * 16, "url": QR_URL}
    buffer = io.BytesIO()
    segno.make(json.dumps(payload)).save(buffer, kind="png")
    return buffer.getvalue()


class Rig:
    """Application réelle + transport simulé."""

    def __init__(self, cfg: config.Config, conn, chat_id: int = CHAT) -> None:
        self.fake = FakeTelegramRequest()
        self.app = build_application(cfg, conn, request=self.fake)
        self.chat_id = chat_id
        self.errors: list[BaseException] = []
        self._update_id = 0
        self._file_seq = 0
        self.app.add_error_handler(self._recueille)

    async def _recueille(self, update, context) -> None:  # noqa: ANN001
        self.errors.append(context.error)

    async def _dispatch(self, payload: dict, chat_id: int) -> list[str]:
        before = len(self.fake.sent)
        await self.app.process_update(Update.de_json(payload, self.app.bot))
        await asyncio.sleep(0.05)
        assert not self.errors, f"un handler a levé {self.errors[0]!r}"
        return self.fake.since(before, chat_id)

    async def text(self, text: str, chat_id: int | None = None) -> list[str]:
        self._update_id += 1
        cible = chat_id or self.chat_id
        return await self._dispatch(text_update(text, cible, self._update_id), cible)

    async def photo(self, data: bytes, chat_id: int | None = None) -> list[str]:
        self._update_id += 1
        self._file_seq += 1
        cible = chat_id or self.chat_id
        file_id = f"qr{self._file_seq}"
        self.fake.files[f"photos/{file_id}.png"] = data
        return await self._dispatch(photo_update(file_id, cible, self._update_id), cible)


def run_scenario(cfg: config.Config, conn, scenario) -> None:  # noqa: ANN001
    async def main() -> None:
        rig = Rig(cfg, conn)
        await rig.app.initialize()
        try:
            await scenario(rig)
        finally:
            await asyncio.sleep(0)
            await rig.app.shutdown()

    asyncio.run(main())


# ─────────────────────────── journaux ───────────────────────────


class _Capteur(logging.Handler):
    """Capture tout ce qui est journalisé, traceback comprise."""

    def __init__(self) -> None:
        super().__init__()
        self.textes: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.textes.append(self.format(record))


@pytest.fixture
def journaux():
    capteur = _Capteur()
    capteur.setFormatter(logging.Formatter("%(levelname)s %(name)s : %(message)s"))
    racine = logging.getLogger()
    ancien = racine.level
    racine.addHandler(capteur)
    racine.setLevel(logging.INFO)
    try:
        yield capteur
    finally:
        racine.removeHandler(capteur)
        racine.setLevel(ancien)


def _texte(journaux: _Capteur) -> str:
    return "\n".join(journaux.textes)


# ─────────────────────── 1. /annuler hors parcours ───────────────────────


def test_annuler_hors_parcours_repond(cfg, conn):
    """Sans handler global, `/annuler` hors parcours ne produisait rien."""

    async def scenario(rig):
        assert await rig.text("/annuler") == [MSG_ANNULER]

    run_scenario(cfg, conn, scenario)


def test_annuler_pendant_le_parcours_reste_au_conversation_handler(cfg, conn):
    """Le handler global ne doit pas voler `/annuler` au parcours en cours."""

    async def scenario(rig):
        assert PROMPT_QR in "\n".join(await rig.text("/start"))
        textes = await rig.text("/annuler")
        assert any("Annulé" in t for t in textes)
        assert MSG_ANNULER not in textes

    run_scenario(cfg, conn, scenario)


# ─────────────────────── 2. /fait sur une clé fantôme ───────────────────────


def test_fait_refuse_une_cle_absente_des_items(cfg, conn, make_user):
    """Une référence vers un item disparu est refusée : aucun `done_local` fantôme."""
    user_id = make_user(chat_id=CHAT)
    reel = make_item(
        kind="devoir",
        source="devoir",
        subject="Mathématiques",
        body="DM n°4",
        due_date=TODAY - timedelta(days=1),
    )
    db.replace_items(conn, user_id, [reel])
    db.write_list_ref(conn, user_id, ["devoir:fantome"])

    async def scenario(rig):
        textes = await rig.text("/fait 1")
        assert MSG_INDEX in textes
        assert "✅" not in textes

        # La clé réelle, elle, reste marquable.
        db.write_list_ref(conn, user_id, [reel.key])
        assert (await rig.text("/fait 1"))[-1] == "✅"

    run_scenario(cfg, conn, scenario)
    assert db.done_keys(conn, user_id) == {reel.key}
    restes = conn.execute(
        "SELECT key FROM done_local WHERE user_id = ?", (user_id,)
    ).fetchall()
    assert [row["key"] for row in restes] == [reel.key]


# ─────────────────────── 3. fuites de secrets ───────────────────────


def test_jeton_telegram_jamais_journalise(cfg, conn, journaux):
    """`InvalidToken` porte le jeton : le gestionnaire d'erreurs ne doit pas l'écrire."""
    app = build_application(cfg, conn, request=FakeTelegramRequest())
    erreur = InvalidToken(f"The token `{cfg.telegram_token}` was rejected by the server.")

    asyncio.run(app.process_error(error=erreur, update=None))

    texte = _texte(journaux)
    assert cfg.telegram_token not in texte
    assert "InvalidToken" in texte


def test_echec_connexion_qr_ne_journalise_pas_le_message(cfg, conn, monkeypatch, journaux):
    """Une erreur PRONOTE/HTTP porte l'URL (donc le jeton) : seul le type sort."""
    fuite = f"GET https://api.telegram.org/bot{cfg.telegram_token}/getFile → 500"

    def login_ko(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError(fuite)

    monkeypatch.setattr(auth, "login_with_qr", login_ko)

    async def scenario(rig):
        await rig.text("/start")
        await rig.photo(qr_png())
        await rig.text("1234")

    run_scenario(cfg, conn, scenario)
    texte = _texte(journaux)
    assert cfg.telegram_token not in texte
    assert fuite not in texte
    assert "connexion par QR refusée (RuntimeError)" in texte


def test_echec_connexion_code_pin_ne_journalise_pas_le_message(cfg, conn, monkeypatch, journaux):
    """Même règle pour la branche du code PIN de compte."""
    import pronotepy

    fuite = f"POST https://api.telegram.org/bot{cfg.telegram_token}/sendMessage → 500"
    appels = {"n": 0}

    def login_ko(*args, **kwargs):  # noqa: ANN002, ANN003
        appels["n"] += 1
        if appels["n"] == 1:
            raise pronotepy.MFAError("ce compte demande un code PIN")
        raise RuntimeError(fuite)

    monkeypatch.setattr(auth, "login_with_qr", login_ko)

    async def scenario(rig):
        await rig.text("/start")
        await rig.photo(qr_png())
        # 1er essai : MFA → le bot réclame le code PIN de compte.
        assert appels["n"] == 0
        await rig.text("1234")
        assert appels["n"] == 1
        # 2e essai (état ACCOUNT_PIN) : erreur générique, message qui fuit.
        await rig.text("9876")

    run_scenario(cfg, conn, scenario)
    assert appels["n"] == 2
    texte = _texte(journaux)
    assert cfg.telegram_token not in texte
    assert fuite not in texte
    assert "connexion avec code PIN refusée (RuntimeError)" in texte


def test_echec_mot_de_passe_ne_journalise_pas_le_chat_id(cfg, conn, monkeypatch, journaux):
    """Le refus de connexion par mot de passe ne doit pas écrire de `chat_id`."""
    monkeypatch.setattr(
        auth,
        "login_with_password",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("identifiants refusés")),
    )

    async def scenario(rig):
        rig.app.bot_data["pw_flow"][rig.chat_id] = "pass"
        rig.app.bot_data["pw_user"][rig.chat_id] = "demonstration"
        textes = await rig.text("mot-de-passe")
        assert any("Identifiant ou mot de passe refusé" in t for t in textes)

    run_scenario(cfg, conn, scenario)
    texte = _texte(journaux)
    assert "connexion par mot de passe refusée" in texte
    assert str(CHAT) not in texte


def test_telechargement_de_photo_rate_ne_fuit_pas_le_jeton(cfg, conn, monkeypatch, journaux):
    """L'URL de téléchargement Telegram contient le jeton : jamais dans les logs."""
    vues: list[str] = []

    def requete_ko(url: str, **kwargs: object) -> bytes:
        # Reproduction du transport réel : `HTTPXRequest.retrieve` préfixe par
        # `/file/bot<JETON>` et l'erreur recopie l'URL complète.
        complet = f"https://api.telegram.org/file/bot{cfg.telegram_token}/{url}"
        vues.append(complet)
        raise RuntimeError(f"GET {complet} → 500")

    async def scenario(rig):
        monkeypatch.setattr(rig.fake, "retrieve", requete_ko)
        await rig.text("/start")
        textes = await rig.photo(qr_png())
        assert any("Image trop floue" in t for t in textes)

    run_scenario(cfg, conn, scenario)
    # La sonde a bien transporté le jeton : sans ça le test ne prouverait rien.
    assert vues and cfg.telegram_token in vues[0]
    texte = _texte(journaux)
    assert cfg.telegram_token not in texte
    assert "téléchargement de la photo impossible (RuntimeError)" in texte
