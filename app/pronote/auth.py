"""Connexion PRONOTE — le seul endroit à corriger si PRONOTE change.

Le QR est chiffré AES avec le code à 4 chiffres ; pronotepy fait le travail.
Ce module ne fait que trois choses en plus : décoder l'image, fabriquer l'URL
mobile (obligatoire pour `token_login`) et rendre des credentials prêts à
être rangés.
"""

from __future__ import annotations

import ipaddress
import json
import socket
from io import BytesIO
from urllib.parse import urlsplit, urlunsplit

from ..config import DEFAULT_MOBILE_QUERY

DEVICE_NAME = "pronote-blocus"


class QrError(Exception):
    """QR inexploitable ; `message` est le texte à envoyer à l'élève."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class QrNotReadable(QrError):
    def __init__(self) -> None:
        super().__init__(
            "Image trop floue. Refais la capture en incluant toute la bordure du QR."
        )


class QrNotPronote(QrError):
    def __init__(self) -> None:
        super().__init__(
            "Ce n'est pas un QR PRONOTE. Prends une capture d'écran du QR affiché "
            "par l'application."
        )


class QrExpired(QrError):
    def __init__(self) -> None:
        super().__init__(
            "Ce QR a expiré. Régénère-le dans PRONOTE et renvoie-le tout de suite."
        )


class TokenRefused(Exception):
    """Le jeton d'appareil est révoqué : il faut un nouveau QR.

    pronotepy ne lève RIEN dans ce cas : il rend un client dont
    `logged_in` vaut False. Ignorer ce drapeau, c'est servir des données
    vides à un élève connecté en apparence.
    """


def mobile_url(url: str, query: str = DEFAULT_MOBILE_QUERY) -> str:
    """`.../pronote/eleve.html` → `.../pronote/mobile.eleve.html?fd=1&…`

    `token_login` ne fonctionne QUE sur l'endpoint mobile. On applique cette
    transformation à l'URL au moment de se connecter, jamais avant de la
    stocker : `users.pronote_url` garde la forme publique.
    """
    parts = urlsplit(url)
    segments = parts.path.split("/")
    if segments and not segments[-1].startswith("mobile."):
        segments[-1] = "mobile." + segments[-1]
    q = query[1:] if query.startswith("?") else query
    return urlunsplit((parts.scheme, parts.netloc, "/".join(segments), q, ""))


def _global_literal(host: str) -> bool | None:
    """IP littérale → `is_global` ; `None` si `host` n'est pas une IP."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        # `::ffff:127.0.0.1` doit être jugée comme l'IPv4 qu'elle transporte.
        address = address.ipv4_mapped
    return address.is_global


def _looks_numeric(host: str) -> bool:
    """`2130706433`, `0177.0.0.1`, `0x7f000001` : des IP déguisées, pas des noms."""
    labels = host.lower().split(".")
    return all(label.isdigit() or label.startswith("0x") for label in labels)


def _resolved_addresses(host: str, port: int) -> list[str]:
    """Adresses derrière `host`, telles que le résolveur système les rend."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def validate_pronote_url(url: str) -> str:
    """URL PRONOTE exploitable côté serveur, ou `QrNotPronote`.

    Le QR est une entrée non fiable et c'est `pronotepy` qui requête cette URL
    DEPUIS le serveur : sans filtre, un QR suffit à faire lire les métadonnées
    cloud (169.254.169.254) ou le réseau local. Trois règles :

    1. HTTPS obligatoire (`http://` en clair livrerait le jeton mobile).
    2. L'hôte est un nom DNS ou une IP littérale canonique. Les formes
       déguisées (`2130706433`, `0177.0.0.1`, `0x7f000001`) sont refusées
       AVANT toute résolution : on ne dépend pas du bon vouloir du résolveur.
    3. TOUTES les adresses résolues doivent être `is_global` — loopback
       (127.0.0.2), `0.0.0.0`, lien-local et ULA IPv6 (`fe80::1`, `fc00::1`),
       IPv4-mappées (`::ffff:127.0.0.1`), plages privées et CGNAT comprises.

    Résidu assumé : la résolution faite ici est refaite par `requests` au
    moment de la requête, donc un DNS menteur peut changer d'avis entre les
    deux (rebinding) ; et `pronotepy` crée son propre `requests.Session`
    (`pronoteAPI.py`), qui suit les redirections sans repasser par ici.
    Fermer ces deux trous demande de posséder le transport (session injectée
    ou IP épinglée), ce que pronotepy n'expose pas.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:  # port illisible (`:abc`, `:99999`), IPv6 non fermée
        raise QrNotPronote() from exc
    if parts.scheme != "https" or not parts.netloc or not host:
        raise QrNotPronote()
    if "@" in parts.netloc:
        # `https://index-education.net@127.0.0.1/` : deux hôtes pour un lecteur
        # humain, un seul pour `requests`.
        raise QrNotPronote()
    literal = _global_literal(host)
    if literal is not None:
        if not literal:
            raise QrNotPronote()
        return url
    if _looks_numeric(host):
        raise QrNotPronote()
    try:
        addresses = _resolved_addresses(host, port or 443)
    except OSError as exc:  # nom inconnu, résolveur injoignable
        raise QrNotPronote() from exc
    if not addresses or any(not _global_literal(address) for address in addresses):
        raise QrNotPronote()
    return url


def parse_qr_text(text: str) -> dict:
    """Texte du QR → dict PRONOTE. Lève `QrNotPronote` sinon."""
    try:
        payload = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise QrNotPronote() from exc
    if not isinstance(payload, dict):
        raise QrNotPronote()
    missing = [k for k in ("login", "jeton", "url") if not payload.get(k)]
    if missing:
        raise QrNotPronote()
    # Le QR est une entrée non fiable : l'URL est requêtée depuis le serveur.
    validate_pronote_url(str(payload["url"]))
    return {k: str(payload[k]) for k in ("login", "jeton", "url")}


def decode_qr_image(data: bytes) -> dict:
    """PNG/JPEG du QR → dict PRONOTE."""
    try:
        from PIL import Image
        import zxingcpp

        with Image.open(BytesIO(data)) as image:
            results = zxingcpp.read_barcodes(image.convert("RGB"))
    except Exception as exc:  # image tronquée, format inconnu, zxing absent
        raise QrNotReadable() from exc
    for result in results:
        text = (getattr(result, "text", "") or "").strip()
        if text.startswith("{"):
            try:
                return parse_qr_text(text)
            except QrError:
                continue
    if results:
        raise QrNotPronote()
    raise QrNotReadable()


def login_with_qr(
    qr: dict,
    pin: str,
    app_uuid: str,
    account_pin: str | None = None,
    *,
    skip_2fa: bool = False,
) -> tuple[object, dict]:
    """QR + code à 4 chiffres → (client, credentials exportés).

    `qrcode_login` fait lui-même le `token_login` de conversion : les
    credentials rendus sont déjà un couple jeton/identifiant d'appareil.
    """
    import pronotepy

    client = pronotepy.Client.qrcode_login(
        qr_code=qr,
        pin=pin,
        uuid=app_uuid,
        account_pin=account_pin,
        device_name=DEVICE_NAME,
        client_identifier=None,
        skip_2fa=skip_2fa,
    )
    creds = client.export_credentials()
    creds["uuid"] = app_uuid
    creds["device_name"] = DEVICE_NAME
    if getattr(client, "logged_in", True) is False:
        raise QrExpired()
    return client, creds


def login_with_password(
    pronote_url: str,
    username: str,
    password: str,
    app_uuid: str,
    ent: object | None = None,
) -> tuple[object, dict]:
    """Identifiant + mot de passe (dernier recours, activé par l'admin).

    On stocke les credentials EXPORTÉS, jamais le mot de passe brut ; en mode
    mot de passe PRONOTE ne délivre pas de jeton d'appareil, donc les
    credentials exportés contiennent ce mot de passe — chiffré par Fernet
    comme le reste, jamais lisible en base ni dans un log.
    """
    import pronotepy

    client = pronotepy.Client(
        pronote_url,
        username=username,
        password=password,
        ent=ent,  # type: ignore[arg-type]
        uuid=app_uuid,
        device_name=DEVICE_NAME,
    )
    creds = client.export_credentials()
    creds["uuid"] = app_uuid
    creds["device_name"] = DEVICE_NAME
    if getattr(client, "logged_in", True) is False:
        raise TokenRefused("identifiants PRONOTE refusés")
    return client, creds


def token_login(credentials: dict, *, account_pin: str | None = None) -> object:
    """Credentials rangés → client vivant. URL mobile obligatoire.

    Le nom d'appareil fait partie de l'enregistrement PRONOTE : réutiliser
    celui qui a servi au premier login, sinon le serveur refuse le jeton
    (mesuré : un device_name différent rend `logged_in = False`, sans lever).
    """
    import pronotepy

    client = pronotepy.Client.token_login(
        pronote_url=mobile_url(credentials["pronote_url"]),
        username=credentials["username"],
        password=credentials["password"],
        uuid=credentials["uuid"],
        account_pin=account_pin,
        client_identifier=credentials.get("client_identifier"),
        device_name=credentials.get("device_name") or DEVICE_NAME,
    )
    if getattr(client, "logged_in", True) is False:
        raise TokenRefused("jeton révoqué ou expiré")
    return client
