"""Connexion PRONOTE : QR, PIN, chiffrement des jetons, chemins de secours.

Tout est hors réseau : `pronotepy` est remplacé par des doubles, et les QR
sont générés localement avec `segno` puis relus par le vrai décodeur.

1. génération / lecture du QR (PNG, JPEG, illisible, non PRONOTE)
2. URL mobile (endpoint obligatoire pour `token_login`)
3. code PIN à 4 chiffres transmis tel quel
4. chiffrement Fernet des jetons et échec explicite (clé changée, jeton corrompu)
5. session expirée / jeton révoqué
6. parcours identifiant + mot de passe, sans mot de passe dans les logs
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from app import crypto, db
from app.config import DEFAULT_MOBILE_QUERY, DEFAULT_PRONOTE_URL
from app.pronote import auth

QR_PAYLOAD = {
    "login": "a1b2c3d4e5f6",
    "jeton": "0123456789abcdef0123456789abcdef",
    "url": DEFAULT_PRONOTE_URL,
}


# ─────────────────────────── outillage ───────────────────────────


def _qr_png(text: str) -> bytes:
    """Vrai PNG de QR, généré hors ligne."""
    import segno

    buffer = io.BytesIO()
    segno.make(text).save(buffer, kind="png", scale=6, border=4)
    return buffer.getvalue()


def _qr_jpeg(text: str) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(_qr_png(text))) as image:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _blank_png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (120, 120), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeClient:
    """Double minimal : seul ce que `auth` consomme est implémenté."""

    def __init__(self, creds: dict | None = None, *, logged_in: bool = True) -> None:
        self._creds = dict(
            creds
            if creds is not None
            else {
                "username": "demonstration",
                "password": "jeton-exporte",
                "client_identifier": "ident-1",
                "uuid": "uuid-serveur",
                "device_name": "pronote-blocus",
            }
        )
        self.logged_in = logged_in

    def export_credentials(self) -> dict:
        return dict(self._creds)


def _install_qr_login(monkeypatch, *, logged_in: bool = True) -> dict:
    import pronotepy

    calls: dict = {}

    def fake_qrcode_login(
        *, qr_code, pin, uuid, account_pin=None, client_identifier=None,
        device_name=None, skip_2fa=False,
    ):
        calls.update(
            qr_code=qr_code,
            pin=pin,
            uuid=uuid,
            account_pin=account_pin,
            client_identifier=client_identifier,
            device_name=device_name,
            skip_2fa=skip_2fa,
        )
        return _FakeClient(logged_in=logged_in)

    monkeypatch.setattr(pronotepy.Client, "qrcode_login", fake_qrcode_login)
    return calls


def _install_token_login(monkeypatch, *, logged_in: bool = True) -> dict:
    import pronotepy

    calls: dict = {}

    def fake_token_login(
        *, pronote_url, username, password, uuid, account_pin=None,
        client_identifier=None, device_name=None,
    ):
        calls.update(
            pronote_url=pronote_url,
            username=username,
            password=password,
            uuid=uuid,
            account_pin=account_pin,
            client_identifier=client_identifier,
            device_name=device_name,
        )
        return _FakeClient(logged_in=logged_in)

    monkeypatch.setattr(pronotepy.Client, "token_login", fake_token_login)
    return calls


def _install_password_login(monkeypatch, *, logged_in: bool = True) -> dict:
    import pronotepy

    calls: dict = {}

    class FakeClient:
        def __init__(
            self, pronote_url, username="", password="", ent=None, mode="normal",
            uuid="", account_pin=None, client_identifier=None, device_name=None,
        ):
            calls.update(
                pronote_url=pronote_url,
                username=username,
                password=password,
                ent=ent,
                mode=mode,
                uuid=uuid,
                account_pin=account_pin,
                client_identifier=client_identifier,
                device_name=device_name,
            )
            self.logged_in = logged_in
            self._creds = {
                "username": username,
                "password": password,
                "client_identifier": client_identifier,
            }

        def export_credentials(self) -> dict:
            return dict(self._creds)

    monkeypatch.setattr(pronotepy, "Client", FakeClient)
    return calls


# ─────────────────────── 1. lecture du QR ───────────────────────


def test_decode_qr_image_lit_un_png_valide():
    qr = auth.decode_qr_image(_qr_png(json.dumps(QR_PAYLOAD)))

    assert qr == QR_PAYLOAD


def test_decode_qr_image_lit_un_jpeg_valide():
    qr = auth.decode_qr_image(_qr_jpeg(json.dumps(QR_PAYLOAD)))

    assert qr["login"] == QR_PAYLOAD["login"]
    assert qr["url"] == QR_PAYLOAD["url"]


def test_decode_qr_image_bytes_non_image_leve_qr_not_readable():
    with pytest.raises(auth.QrNotReadable):
        auth.decode_qr_image(b"ceci n'est pas une image")


def test_decode_qr_image_sans_code_leve_qr_not_readable():
    with pytest.raises(auth.QrNotReadable):
        auth.decode_qr_image(_blank_png())


def test_decode_qr_image_qr_quelconque_leve_qr_not_pronote():
    with pytest.raises(auth.QrNotPronote):
        auth.decode_qr_image(_qr_png("https://example.com/ quelconque"))


def test_decode_qr_image_qr_json_incomplet_leve_qr_not_pronote():
    with pytest.raises(auth.QrNotPronote):
        auth.decode_qr_image(_qr_png(json.dumps({"login": "x", "url": DEFAULT_PRONOTE_URL})))


def test_decode_qr_image_url_locale_refusee():
    payload = dict(QR_PAYLOAD, url="http://localhost/pronote/eleve.html")

    with pytest.raises(auth.QrNotPronote):
        auth.decode_qr_image(_qr_png(json.dumps(payload)))


@pytest.mark.parametrize(
    ("error", "attendu"),
    [
        (auth.QrNotReadable(), "bordure"),
        (auth.QrNotPronote(), "QR PRONOTE"),
        (auth.QrExpired(), "expiré"),
    ],
)
def test_message_utilisateur_actionnable(error, attendu):
    """`main` renvoie `exc.message` tel quel à l'élève."""
    assert isinstance(error, auth.QrError)
    assert attendu in error.message
    assert str(error) == error.message


# ─────────────────── 2. URL mobile / endpoint ───────────────────


def test_mobile_url_ajoute_l_endpoint_mobile_sur_l_url_par_defaut():
    assert auth.mobile_url(DEFAULT_PRONOTE_URL) == (
        "https://ETABLISSEMENT.index-education.net/pronote/mobile.eleve.html"
        + DEFAULT_MOBILE_QUERY
    )


def test_mobile_url_conserve_un_endpoint_deja_mobile():
    deja = "https://ETABLISSEMENT.index-education.net/pronote/mobile.eleve.html"

    assert auth.mobile_url(deja) == deja + DEFAULT_MOBILE_QUERY


def test_mobile_url_requete_personnalisee_sans_point_interrogation():
    url = auth.mobile_url(DEFAULT_PRONOTE_URL, "?fd=1&x=2")

    assert url.endswith("/pronote/mobile.eleve.html?fd=1&x=2")
    assert "??" not in url


def test_mobile_url_perd_le_fragment():
    url = auth.mobile_url(DEFAULT_PRONOTE_URL + "#ancre")

    assert "#" not in url


def test_mobile_url_conserve_le_port_et_le_sous_domaine():
    url = auth.mobile_url("https://ETABLISSEMENT.index-education.net:8443/pronote/parent.html")

    assert url.startswith("https://ETABLISSEMENT.index-education.net:8443/pronote/mobile.parent.html")


# ─────────────────────── 3. texte du QR ───────────────────────


def test_parse_qr_text_valide():
    assert auth.parse_qr_text(json.dumps(QR_PAYLOAD)) == QR_PAYLOAD


def test_parse_qr_text_convertit_les_valeurs_en_chaines():
    payload = {"login": 123456, "jeton": 987654321, "url": DEFAULT_PRONOTE_URL}

    parsed = auth.parse_qr_text(json.dumps(payload))

    assert parsed == {
        "login": "123456",
        "jeton": "987654321",
        "url": DEFAULT_PRONOTE_URL,
    }


@pytest.mark.parametrize(
    "text",
    ["", "pas du json", "[]", '"une chaine"', "123", "null"],
)
def test_parse_qr_text_json_invalide_leve_qr_not_pronote(text):
    with pytest.raises(auth.QrNotPronote):
        auth.parse_qr_text(text)


@pytest.mark.parametrize("cle", ["login", "jeton", "url"])
def test_parse_qr_text_champ_manquant_leve_qr_not_pronote(cle):
    payload = dict(QR_PAYLOAD)
    payload.pop(cle)

    with pytest.raises(auth.QrNotPronote):
        auth.parse_qr_text(json.dumps(payload))


@pytest.mark.parametrize("cle", ["login", "jeton", "url"])
def test_parse_qr_text_champ_vide_leve_qr_not_pronote(cle):
    with pytest.raises(auth.QrNotPronote):
        auth.parse_qr_text(json.dumps(dict(QR_PAYLOAD, **{cle: ""})))


@pytest.mark.parametrize(
    "url",
    [
        "ftp://ETABLISSEMENT.index-education.net/pronote/eleve.html",
        "file:///pronote/eleve.html",
        "javascript:alert(1)",
        "https:///pronote/eleve.html",
        "pronote/eleve.html",
    ],
)
def test_parse_qr_text_url_sans_endpoint_exploitable(url):
    """Sans schéma http(s) ni hôte, `mobile_url` produirait une URL inutilisable."""
    with pytest.raises(auth.QrNotPronote):
        auth.parse_qr_text(json.dumps(dict(QR_PAYLOAD, url=url)))


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/pronote/eleve.html",
        "http://127.0.0.1/pronote/eleve.html",
        "http://[::1]/pronote/eleve.html",
        "http://192.168.1.10/pronote/eleve.html",
        "http://10.0.0.5/pronote/eleve.html",
        "http://172.16.0.5/pronote/eleve.html",
    ],
)
def test_parse_qr_text_ssrf_refuse(url):
    with pytest.raises(auth.QrNotPronote):
        auth.parse_qr_text(json.dumps(dict(QR_PAYLOAD, url=url)))


# ─────────────────── 4. login QR et code PIN ───────────────────


def test_login_with_qr_transmet_le_pin_et_l_uuid(monkeypatch):
    calls = _install_qr_login(monkeypatch)
    qr = dict(QR_PAYLOAD)

    client, creds = auth.login_with_qr(qr, "1234", "uuid-app")

    assert isinstance(client, _FakeClient)
    assert calls["pin"] == "1234"
    assert len(calls["pin"]) == 4 and calls["pin"].isdigit()
    assert calls["uuid"] == "uuid-app"
    assert calls["qr_code"] is qr
    assert calls["device_name"] == auth.DEVICE_NAME
    assert calls["client_identifier"] is None
    assert calls["skip_2fa"] is False


def test_login_with_qr_enrichit_les_credentials(monkeypatch):
    _install_qr_login(monkeypatch)

    _, creds = auth.login_with_qr(dict(QR_PAYLOAD), "1234", "uuid-app")

    assert creds["uuid"] == "uuid-app"
    assert creds["device_name"] == auth.DEVICE_NAME
    assert creds["password"] == "jeton-exporte"


def test_login_with_qr_transmet_account_pin_et_skip_2fa(monkeypatch):
    calls = _install_qr_login(monkeypatch)

    auth.login_with_qr(
        dict(QR_PAYLOAD), "1234", "uuid-app", "9999", skip_2fa=True
    )

    assert calls["account_pin"] == "9999"
    assert calls["skip_2fa"] is True


def test_login_with_qr_expire_leve_qr_expired(monkeypatch):
    _install_qr_login(monkeypatch, logged_in=False)

    with pytest.raises(auth.QrExpired) as excinfo:
        auth.login_with_qr(dict(QR_PAYLOAD), "1234", "uuid-app")

    assert "expiré" in excinfo.value.message


# ───────────── 5. chiffrement Fernet des jetons rangés ─────────────


def test_credentials_rangees_chiffrees_puis_relues(conn, secret):
    creds = {
        "username": "demonstration",
        "password": "jeton-exporte",
        "client_identifier": "ident-1",
        "uuid": "uuid-app",
    }

    uid = db.create_user(
        conn,
        chat_id=42,
        pronote_url=DEFAULT_PRONOTE_URL,
        username=creds["username"],
        creds=creds,
        secret=secret,
    )

    row = conn.execute("SELECT token_enc FROM users WHERE id = ?", (uid,)).fetchone()
    assert b"jeton-exporte" not in bytes(row["token_enc"])

    relus = db.credentials_for(conn, uid, secret)
    assert relus["password"] == "jeton-exporte"
    assert relus["pronote_url"] == DEFAULT_PRONOTE_URL
    assert relus["uuid"] == "uuid-app"


def test_credentials_avec_autre_cle_app_secret_echoue(conn, secret, other_secret):
    creds = {"username": "demonstration", "password": "jeton-exporte", "uuid": "u"}
    uid = db.create_user(
        conn,
        chat_id=43,
        pronote_url=DEFAULT_PRONOTE_URL,
        username="demonstration",
        creds=creds,
        secret=secret,
    )

    with pytest.raises(crypto.SecretError, match="APP_SECRET"):
        db.credentials_for(conn, uid, other_secret)


def test_jeton_corrompu_echoue(conn, secret):
    creds = {"username": "demonstration", "password": "jeton-exporte", "uuid": "u"}
    uid = db.create_user(
        conn,
        chat_id=44,
        pronote_url=DEFAULT_PRONOTE_URL,
        username="demonstration",
        creds=creds,
        secret=secret,
    )
    with conn:
        conn.execute(
            "UPDATE users SET token_enc = ? WHERE id = ?",
            (b"jeton-en-clair-corrompu", uid),
        )

    with pytest.raises(crypto.SecretError, match="corrompue"):
        db.credentials_for(conn, uid, secret)


def test_crypto_aller_retour_et_secret_vide(secret):
    assert crypto.decrypt(crypto.encrypt("jeton", secret), secret) == "jeton"

    with pytest.raises(crypto.SecretError, match="vide"):
        crypto.encrypt("jeton", "")


# ───────────────── 6. session expirée / jeton révoqué ─────────────────


def test_token_login_rejoue_le_jeton_sur_l_endpoint_mobile(monkeypatch):
    calls = _install_token_login(monkeypatch)
    credentials = {
        "pronote_url": DEFAULT_PRONOTE_URL,
        "username": "demonstration",
        "password": "jeton-exporte",
        "uuid": "uuid-app",
        "client_identifier": "ident-1",
        "device_name": "pronote-blocus",
    }

    auth.token_login(credentials)

    assert calls["pronote_url"] == (
        "https://ETABLISSEMENT.index-education.net/pronote/mobile.eleve.html"
        + DEFAULT_MOBILE_QUERY
    )
    assert calls["password"] == "jeton-exporte"
    assert calls["uuid"] == "uuid-app"
    assert calls["client_identifier"] == "ident-1"
    assert calls["device_name"] == "pronote-blocus"


def test_token_login_ne_modifie_pas_l_url_stockee(monkeypatch):
    _install_token_login(monkeypatch)
    credentials = {
        "pronote_url": DEFAULT_PRONOTE_URL,
        "username": "demonstration",
        "password": "jeton-exporte",
        "uuid": "uuid-app",
    }

    auth.token_login(credentials)

    assert credentials["pronote_url"] == DEFAULT_PRONOTE_URL


def test_token_login_nom_d_appareil_par_defaut(monkeypatch):
    calls = _install_token_login(monkeypatch)

    auth.token_login(
        {
            "pronote_url": DEFAULT_PRONOTE_URL,
            "username": "demonstration",
            "password": "jeton-exporte",
            "uuid": "uuid-app",
        }
    )

    assert calls["device_name"] == auth.DEVICE_NAME
    assert calls["client_identifier"] is None


def test_token_login_transmet_account_pin(monkeypatch):
    calls = _install_token_login(monkeypatch)

    auth.token_login(
        {
            "pronote_url": DEFAULT_PRONOTE_URL,
            "username": "demonstration",
            "password": "jeton-exporte",
            "uuid": "uuid-app",
        },
        account_pin="9876",
    )

    assert calls["account_pin"] == "9876"


def test_token_login_revoque_leve_token_refused(monkeypatch):
    _install_token_login(monkeypatch, logged_in=False)

    with pytest.raises(auth.TokenRefused, match="révoqué"):
        auth.token_login(
            {
                "pronote_url": DEFAULT_PRONOTE_URL,
                "username": "demonstration",
                "password": "jeton-exporte",
                "uuid": "uuid-app",
            }
        )


# ───────────── 7. secours identifiant + mot de passe ─────────────


def test_login_with_password_utilise_l_url_par_defaut(monkeypatch):
    calls = _install_password_login(monkeypatch)

    client, creds = auth.login_with_password(
        DEFAULT_PRONOTE_URL, "demonstration", "mot-de-passe", "uuid-app"
    )

    assert calls["pronote_url"] == DEFAULT_PRONOTE_URL, (
        "l'URL publique est stockée telle quelle ; mobile_url s'applique au login"
    )
    assert calls["username"] == "demonstration"
    assert calls["password"] == "mot-de-passe"
    assert calls["uuid"] == "uuid-app"
    assert calls["device_name"] == auth.DEVICE_NAME
    assert creds["uuid"] == "uuid-app"
    assert creds["device_name"] == auth.DEVICE_NAME
    assert client.logged_in is True


def test_login_with_password_transmet_l_ent(monkeypatch):
    calls = _install_password_login(monkeypatch)
    ent = object()

    auth.login_with_password(DEFAULT_PRONOTE_URL, "demonstration", "mdp", "uuid-app", ent)

    assert calls["ent"] is ent


def test_login_with_password_refuse_leve_token_refused(monkeypatch):
    _install_password_login(monkeypatch, logged_in=False)

    with pytest.raises(auth.TokenRefused, match="refusés"):
        auth.login_with_password(DEFAULT_PRONOTE_URL, "demonstration", "mdp", "uuid-app")


def test_login_with_password_ne_journalise_jamais_le_mot_de_passe(monkeypatch, caplog):
    _install_password_login(monkeypatch)
    secret_password = "MotDePasse-Tres-Secret-42"

    with caplog.at_level(logging.DEBUG):
        auth.login_with_password(
            DEFAULT_PRONOTE_URL, "demonstration", secret_password, "uuid-app"
        )

    assert secret_password not in caplog.text
    assert secret_password not in repr(caplog.records)


def test_login_with_password_echec_ne_journalise_pas_le_mot_de_passe(monkeypatch, caplog):
    import pronotepy

    secret_password = "MotDePasse-Tres-Secret-43"

    class Refus:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("identifiants refusés")

    monkeypatch.setattr(pronotepy, "Client", Refus)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RuntimeError, match="identifiants refusés"):
            auth.login_with_password(
                DEFAULT_PRONOTE_URL, "demonstration", secret_password, "uuid-app"
            )

    assert secret_password not in caplog.text
