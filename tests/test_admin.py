"""Commandes d'administration : `/qui` et `/mdp <chat_id>`, sans réseau.

Ces tests ciblent le contrôle d'accès (réservé à `ADMIN_CHAT_ID`), la
validation de l'argument de `/mdp`, l'activation du parcours identifiant /
mot de passe et la non-divulgation du secret dans les réponses.

Le montage est le même que `test_flow_telegram.py` : application Telegram
réelle branchée sur `FakeTelegramRequest` (aucun octet ne sort), seule la
couche réseau PRONOTE est simulée.
"""

from __future__ import annotations

import asyncio
import re
import time

import pytest
from telegram import Update

from app import config, crypto, db
from app.main import build_application
from app.pronote import auth
from tests.fake_telegram import FakeTelegramRequest

CHAT = 4242
AUTRE_CHAT = 5555
ADMIN = 999999

REFUS = "Commande réservée à l'admin."


# ─────────────────────────── helpers ───────────────────────────


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


def plain(html_text: str) -> str:
    return re.sub(r"<[^>]+>", "", html_text)


class Rig:
    """Application réelle + transport simulé (aucun réseau)."""

    def __init__(self, cfg: config.Config, conn) -> None:
        self.fake = FakeTelegramRequest()
        self.app = build_application(cfg, conn, request=self.fake)
        self.errors: list[BaseException] = []
        self._update_id = 0
        self.app.add_error_handler(self._on_error)

    async def _on_error(self, update, context) -> None:  # noqa: ANN001
        self.errors.append(context.error)

    def _raise_errors(self) -> None:
        if self.errors:
            error = self.errors[0]
            raise AssertionError(
                f"un handler a levé {type(error).__name__}: {error}"
            ) from error

    async def dispatch(self, payload: dict, chat_id: int) -> list[str]:
        before = len(self.fake.sent)
        update = Update.de_json(payload, self.app.bot)
        await self.app.process_update(update)
        await asyncio.sleep(0.05)
        self._raise_errors()
        return self.fake.since(before, chat_id)

    async def text(self, text: str, chat_id: int) -> list[str]:
        self._update_id += 1
        return await self.dispatch(
            text_update(text, chat_id, self._update_id), chat_id
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


def _cfg_with_admin(secret: str, tmp_path, admin: str) -> config.Config:
    return config.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
            "APP_SECRET": secret,
            "DATA_DIR": str(tmp_path / "data"),
            "WINDOW_START": "2026-10-01",
            "ADMIN_CHAT_ID": admin,
            "LLM_BASE_URL": "http://127.0.0.1:9/v1",
            "LLM_API_KEY": "cle-de-test",
            "LLM_MODEL": "modele-de-test",
        }
    )


@pytest.fixture
def cfg(secret, tmp_path) -> config.Config:
    return _cfg_with_admin(secret, tmp_path, str(ADMIN))


# ─────────────────────────── 1. contrôle d'accès ───────────────────────────


def test_qui_et_mdp_refuses_pour_un_chat_non_admin(cfg, conn):
    """Un chat qui n'est pas l'admin n'obtient jamais de réponse exploitable."""

    async def scenario(rig):
        for commande in ("/qui", f"/mdp {CHAT}"):
            texts = await rig.text(commande, chat_id=CHAT)
            assert texts == [REFUS], (commande, texts)

    run_flow(cfg, conn, scenario)


def test_mdp_refuse_ne_modifie_pas_le_compte(cfg, conn, make_user):
    """Le refus d'accès à `/mdp` ne doit rien activer sur la base."""
    user_id = make_user(chat_id=CHAT)

    async def scenario(rig):
        texts = await rig.text(f"/mdp {CHAT}", chat_id=AUTRE_CHAT)
        assert texts == [REFUS]

    run_flow(cfg, conn, scenario)

    assert db.get_user(conn, user_id)["allow_password"] == 0


def test_admin_chat_id_vide_refuse_tout_le_monde(secret, tmp_path, conn):
    """`ADMIN_CHAT_ID` absent : personne n'est admin, même l'ancien id."""
    cfg = _cfg_with_admin(secret, tmp_path, "")
    assert cfg.admin_chat_id is None

    async def scenario(rig):
        for chat in (ADMIN, CHAT, AUTRE_CHAT):
            texts = await rig.text("/qui", chat_id=chat)
            assert texts == [REFUS], (chat, texts)
            texts = await rig.text(f"/mdp {CHAT}", chat_id=chat)
            assert texts == [REFUS], (chat, texts)

    run_flow(cfg, conn, scenario)


# ─────────────────────────── 2. argument de /mdp ───────────────────────────


@pytest.mark.parametrize(
    "commande",
    ["/mdp", "/mdp ", "/mdp abc", "/mdp 12abc", "/mdp 12.5", "/mdp @canal"],
)
def test_mdp_argument_manquant_ou_non_numerique(cfg, conn, commande):
    """Sans entier valide, l'admin reçoit le rappel de syntaxe, rien d'autre."""

    async def scenario(rig):
        texts = await rig.text(commande, chat_id=ADMIN)
        assert texts == ["Envoie /mdp <chat_id>"], (commande, texts)

    run_flow(cfg, conn, scenario)


def test_mdp_chat_id_inconnu(cfg, conn):
    """Un chat_id numérique sans élève associé est signalé, sans effet de bord."""

    async def scenario(rig):
        texts = await rig.text("/mdp 987654", chat_id=ADMIN)
        assert texts == ["Aucun élève avec ce chat_id."]

    run_flow(cfg, conn, scenario)


# ─────────────────────────── 3. /qui ───────────────────────────


def test_qui_compte_inscrits_et_liste_les_erreurs(cfg, conn, make_user):
    """`/qui` résume le nombre d'inscrits et détaille ceux en erreur."""
    make_user(chat_id=CHAT)
    en_erreur_id = make_user(chat_id=AUTRE_CHAT)
    db.set_last_error(conn, en_erreur_id, "Jeton PRONOTE invalide")

    async def scenario(rig):
        texts = await rig.text("/qui", chat_id=ADMIN)
        joined = plain("\n".join(texts))
        assert "2" in joined
        assert "inscrit" in joined
        assert str(AUTRE_CHAT) in joined
        assert "Jeton PRONOTE invalide" in joined
        # Le chat sain (sans erreur) n'apparaît pas dans le détail.
        assert str(CHAT) not in joined

    run_flow(cfg, conn, scenario)


def test_qui_sans_aucun_utilisateur(cfg, conn):
    """Base vide : zéro inscrit, aucun détail, pas de plantage."""

    async def scenario(rig):
        texts = await rig.text("/qui", chat_id=ADMIN)
        joined = plain("\n".join(texts))
        assert "0" in joined
        assert "Aucune erreur" in joined

    run_flow(cfg, conn, scenario)


# ─────────────────────────── 4. activation du parcours mot de passe ───────────────────────────


def test_mdp_active_le_parcours_mot_de_passe(cfg, conn, make_user):
    """`/mdp <chat_id>` autorise le mot de passe et notifie la cible."""
    user_id = make_user(chat_id=CHAT)
    assert db.get_user(conn, user_id)["allow_password"] == 0

    async def scenario(rig):
        texts = await rig.text(f"/mdp {CHAT}", chat_id=ADMIN)
        assert any(f"C'est fait pour {CHAT}" in t for t in texts)

        # La cible reçoit les instructions pour démarrer le parcours.
        recus = rig.fake.texts(CHAT)
        assert any("autorisé la connexion par mot de passe" in t for t in recus)
        assert any("identifiant PRONOTE" in t for t in recus)

        # Le premier message de l'élève est bien routé vers la demande de mot de passe.
        texts = await rig.text("mon-identifiant", chat_id=CHAT)
        assert any("Envoie ton mot de passe" in t for t in texts)

    run_flow(cfg, conn, scenario)

    assert db.get_user(conn, user_id)["allow_password"] == 1


def test_mdp_cible_informe_et_secret_jamais_divulgue(
    cfg, conn, make_user, secret, monkeypatch
):
    """Le mot de passe saisi n'apparaît dans AUCUN message sortant."""
    user_id = make_user(chat_id=CHAT, password="ancien-jeton")
    mot_de_passe = "s3cret-a-ne-pas-trahir"

    def login_ok(url, username, password, app_uuid, ent=None):  # noqa: ANN001
        class _Client:
            def export_credentials(self):  # noqa: ANN202
                return {"password": password, "username": username}

        return _Client(), {
            "password": password,
            "username": username,
            "uuid": app_uuid,
            "client_identifier": "ident-pw",
        }

    monkeypatch.setattr(auth, "login_with_password", login_ok)

    async def scenario(rig):
        await rig.text(f"/mdp {CHAT}", chat_id=ADMIN)
        await rig.text("mon-identifiant", chat_id=CHAT)
        await rig.text(mot_de_passe, chat_id=CHAT)

        # Le message porteur du secret est supprimé…
        assert any(name == "deleteMessage" for name, _ in rig.fake.sent)
        # …et le secret ne fuit dans aucun texte envoyé, dans aucun chat.
        for text in rig.fake.texts():
            assert mot_de_passe not in text
        # Le mot de passe est bien enregistré chiffré, jamais en clair en base.
        row = db.get_user(conn, user_id)
        assert mot_de_passe.encode() not in (row["token_enc"] or b"")
        assert crypto.decrypt(row["token_enc"], secret) == mot_de_passe

    run_flow(cfg, conn, scenario)


def test_mdp_echec_conserve_le_compte_et_tait_le_secret(
    cfg, conn, make_user, monkeypatch
):
    """Mot de passe refusé : le compte reste intact et le secret reste tu."""
    user_id = make_user(chat_id=CHAT, password="ancien-jeton")
    mot_de_passe = "mauvais-s3cret"

    def refuse(*args, **kwargs):  # noqa: ANN002, ANN003
        raise auth.TokenRefused("refusé")

    monkeypatch.setattr(auth, "login_with_password", refuse)

    async def scenario(rig):
        await rig.text(f"/mdp {CHAT}", chat_id=ADMIN)
        await rig.text("mon-identifiant", chat_id=CHAT)
        texts = await rig.text(mot_de_passe, chat_id=CHAT)
        assert any("Identifiant ou mot de passe refusé" in t for t in texts)
        for text in rig.fake.texts():
            assert mot_de_passe not in text

    run_flow(cfg, conn, scenario)

    assert db.get_user(conn, user_id) is not None
