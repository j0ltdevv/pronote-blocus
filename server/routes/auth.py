"""Routes d'authentification : QR code → code PIN → connexion PRONOTE.

Le flow PWA reproduit celui du bot Telegram :
  1. le frontend téléverse un QR (image base64) ou fournit l'URL PRONOTE ;
  2. le serveur le décode (`auth.decode_qr_image`), extrait l'URL + le login,
     et crée une session in-memory attendant le code à 4 chiffres ;
  3. le code saisi déclenche `auth.login_with_qr`, puis une première
     collecte (`fetch.collect`) pour récupérer les devoirs et matières ;
  4. le serveur rend un jeton opaque (TTL 24 h) : c'est lui, et lui seul, qui
     identifie l'élève sur les routes `/api` protégées.

`/qr` et `/pin` sont les seules routes ouvertes : elles servent à obtenir un
jeton, elles ne lisent aucune donnée d'élève.
"""

from __future__ import annotations

import base64
import json
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import crypto, db
from app.config import Config
from app.pronote import auth, fetch

from ..deps import (
    COOKIE_NAME,
    TOKEN_TTL_SECONDS,
    CfgDep,
    DBDep,
    UserDep,
    clear_csrf_cookie,
    csrf_token,
    issue_token,
    presented_token,
    require_csrf,
    resolve_token,
    revoke_token,
    sessions,
    set_csrf_cookie,
)


router = APIRouter(
    prefix="/api/auth",
    tags=["auth"],
    dependencies=[Depends(require_csrf)],
)


# ── models ─────────────────────────────────────────────────────────────


class QrIn(BaseModel):
    """Soit une image base64, soit une URL de test (dev)."""

    pronote_url: str | None = Field(None, description="URL PRONOTE de test (dev).")
    image_base64: str | None = Field(
        None, description="Data-URL ou base64 pur du PNG/JPEG du QR."
    )


class PasswordIn(BaseModel):
    """Connexion directe identifiant + mot de passe (repli sans ordinateur).

    `ent` nomme le fournisseur d'identités (`lyceeconnecte_aquitaine`,
    `laclasse_educonnect`…), laissé vide quand PRONOTE authentifie lui-même.
    """

    pronote_url: str | None = None
    username: str
    password: str
    ent: str | None = None


class QrOut(BaseModel):
    session_id: str
    qr_image_base64: str


class PinIn(BaseModel):
    session_id: str
    pin: str
    account_pin: str | None = None


class PinOut(BaseModel):
    """Compte connecté : `token` est le seul laissez-passer des routes `/api`."""

    user_id: str
    token: str
    subjects: list[str]
    csrf_token: str


# ── helpers ────────────────────────────────────────────────────────────


def _decode_image(raw: str) -> bytes:
    """Data-URL `data:image/png;base64,...` ou base64 pur → bytes."""
    if "," in raw[:20]:
        raw = raw.split(",", 1)[1]
    return base64.b64decode(raw)


def _render_qr_png(qr: dict) -> str:
    """Renvoie le QR en base64 PNG — segno si dispo, sinon placeholder."""
    try:
        import io

        import segno

        buf = io.BytesIO()
        segno.make(json.dumps(qr)).save(buf, kind="png")
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return _PLACEHOLDER_PNG


_PLACEHOLDER_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwAE"
    "hQGAhKgYmCAAAB7AAVZ4rGy0wAAAABJRU5ErkJggg=="
)


def _qr_from_url(pronote_url: str) -> dict:
    """Construit un dict QR compatible `parse_qr_text` à partir d'une URL.

    Pour le dev : le frontend fournit l'URL de son PRONOTE et le serveur
    fabrique un QR lisible, sans passer par `pronotepy`.
    """
    # Le serveur requête cette URL depuis son propre réseau : une URL interne
    # (loopback, métadonnées cloud, IP locale) exposerait des ressources à
    # l'extérieur. `validate_pronote_url` bloque ces vecteurs SSRF.
    auth.validate_pronote_url(pronote_url)
    return {
        "login": secrets.token_hex(8),
        "jeton": secrets.token_hex(16),
        "url": pronote_url,
    }


def _web_chat_id(pronote_url: str, login: str) -> int:
    """Identifiant stable simulant le `chat_id` Telegram.

    La PWA n'a pas de Telegram : on dérive un entier stable de
    (url, login) pour réutiliser `get_user_by_chat` telle quelle.
    """
    import hashlib

    h = hashlib.md5(f"{pronote_url}|{login}".encode()).hexdigest()
    return int(h[:8], 16)


def _find_or_create(
    conn,
    cfg: Config,
    pronote_url: str,
    username: str,
    creds: dict,
    class_name: str | None,
    account_pin: str | None,
) -> str:
    """Crée ou remet à jour le compte, en réutilisant le jeton tourné."""
    chat_id = _web_chat_id(pronote_url, username)
    existing = db.get_user_by_chat(conn, chat_id)
    if existing is not None:
        db.reauth_user(
            conn,
            existing["id"],
            creds=creds,
            secret=cfg.app_secret,
            pronote_url=pronote_url,
            username=username,
            class_name=class_name,
            account_pin=account_pin,
        )
        return existing["id"]
    return db.create_user(
        conn,
        chat_id=chat_id,
        pronote_url=pronote_url,
        username=username,
        creds=creds,
        secret=cfg.app_secret,
        class_name=class_name,
        account_pin=account_pin,
    )


# ── 1. QR ──────────────────────────────────────────────────────────────


@router.post("/qr", response_model=QrOut)
def post_qr(body: QrIn) -> QrOut:
    """Décodage du QR PRONOTE → création d'une session en mémoire.

    - `image_base64` : PNG/JPEG téléchargé depuis l'application PRONOTE.
    - `pronote_url`  : pour le dev, génère un QR factice côté serveur.
    """
    if body.image_base64:
        try:
            qr = auth.decode_qr_image(_decode_image(body.image_base64))
        except auth.QrError as exc:
            raise HTTPException(400, exc.message) from exc
    elif body.pronote_url:
        try:
            qr = _qr_from_url(body.pronote_url)
        except auth.QrError as exc:
            raise HTTPException(400, exc.message) from exc
    else:
        raise HTTPException(
            400, "Envoie `image_base64` (capture QR) ou `pronote_url` (dev)."
        )

    session_id = secrets.token_urlsafe(16)
    sessions()[session_id] = {
        "qr": qr,
        "pronote_url": qr.get("url", body.pronote_url or ""),
    }
    return QrOut(session_id=session_id, qr_image_base64=_render_qr_png(qr))


# ── 2 bis. identifiant + mot de passe (repli téléphone) ────────────────


def _ent_function(name: str | None):
    """Résout `pronotepy.ent.<name>` en callable, ou None."""
    if not name:
        return None
    import pronotepy.ent as ent_module

    fonction = getattr(ent_module, name, None)
    if fonction is None or not callable(fonction):
        raise HTTPException(400, f"ENT inconnu : {name}")
    return fonction


@router.get("/options")
def get_options(cfg: CfgDep) -> dict[str, Any]:
    """Ce que le formulaire doit présélectionner : ENT et URL de l'établissement."""
    import pronotepy.ent as ent_module

    # Les handlers sont soit des fonctions, soit des `functools.partial`
    # (module `functools`) : on garde tout ce qui est appelable et public.
    noms = sorted(
        n
        for n in dir(ent_module)
        if not n.startswith("_") and callable(getattr(ent_module, n, None))
    )
    return {
        "ents": noms,
        "default_url": cfg.default_pronote_url,
        "default_ent": cfg.password_ent,
    }


@router.post("/password", response_model=PinOut)
def post_password(
    body: PasswordIn,
    request: Request,
    response: Response,
    conn: DBDep,
    cfg: CfgDep,
) -> PinOut:
    """Connexion par identifiant + mot de passe, pour qui n'a pas d'ordinateur.

    PRONOTE ne délivre aucun jeton dans ce mode : les credentials exportés
    contiennent le mot de passe, qui est donc **conservé chiffré** en base
    (Fernet). Il n'apparaît jamais en clair, ni en base ni dans un journal.
    """
    if not cfg.allow_password_login:
        raise HTTPException(403, "La connexion par mot de passe est désactivée.")

    username = body.username.strip()
    if not username or not body.password:
        raise HTTPException(400, "Identifiant et mot de passe requis.")

    import pronotepy

    ent = _ent_function(body.ent or cfg.password_ent)
    pronote_url = body.pronote_url or cfg.default_pronote_url
    app_uuid = crypto.new_app_uuid()
    try:
        client, creds = auth.login_with_password(
            pronote_url, username, body.password, app_uuid, ent=ent
        )
    except pronotepy.MFAError as exc:
        raise HTTPException(401, "Ce compte demande un code PIN supplémentaire.") from exc
    except Exception as exc:
        # Message utile selon le cas : sans ENT, l'élève doit en choisir un ;
        # avec, ce sont les identifiants qui sont en cause.
        detail = (
            f"Identifiants refusés par « {body.ent or cfg.password_ent} ». "
            "Vérifie ton identifiant et ton mot de passe."
            if ent is not None
            else "Connexion refusée. Choisis ton ENT dans la liste : sans lui, "
            "PRONOTE refuse le mot de passe."
        )
        raise HTTPException(401, detail) from exc

    if getattr(client, "logged_in", True) is False:
        raise HTTPException(401, "Identifiants refusés par PRONOTE.")

    class_name = fetch.class_name_of(client)
    user_id = _find_or_create(
        conn, cfg, pronote_url, username, creds, class_name, None
    )
    fetch.collect(client, conn, user_id, cfg, "items")
    subjects: list[str] = sorted({it.subject for it in db.get_items(conn, user_id)})
    token = issue_token(user_id, conn)
    csrf = csrf_token(token)
    set_csrf_cookie(response, request, csrf)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=TOKEN_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=(
            request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
        ),
        path="/",
    )
    return PinOut(
        user_id=user_id, token=token, subjects=subjects, csrf_token=csrf
    )


# ── 3. PIN ─────────────────────────────────────────────────────────────



@router.post("/pin", response_model=PinOut)
def post_pin(
    body: PinIn,
    request: Request,
    response: Response,
    conn: DBDep,
    cfg: CfgDep,
) -> PinOut:
    """Finalise la connexion avec le code à 4 chiffres, rend un jeton de 90 jours."""
    sess = sessions().pop(body.session_id, None)
    if sess is None:
        raise HTTPException(404, "Session introuvable ou expirée.")

    pin = body.pin.strip()
    if not (pin.isdigit() and len(pin) == 4):
        raise HTTPException(400, "Le code PIN doit être exactement 4 chiffres.")

    qr: dict = sess["qr"]
    pronote_url = sess["pronote_url"]
    app_uuid = crypto.new_app_uuid()

    import pronotepy  # lazy : ne bloque l'import que si réellement absent

    try:
        client, creds = auth.login_with_qr(qr, pin, app_uuid, body.account_pin)
    except pronotepy.MFAError as exc:
        raise HTTPException(401, "Ce compte demande un code PIN supplémentaire.") from exc
    except pronotepy.CryptoError as exc:
        raise HTTPException(401, "QR ou code invalide — régénère un QR dans PRONOTE.") from exc
    except auth.QrExpired as exc:
        raise HTTPException(401, exc.message) from exc
    except Exception as exc:
        raise HTTPException(502, f"Connexion PRONOTE impossible : {exc}") from exc

    if getattr(client, "logged_in", True) is False:
        raise HTTPException(401, "Jeton refusé — renvoie un QR frais.")

    class_name = fetch.class_name_of(client)
    username = qr.get("login", "") or creds.get("username", "")
    user_id = _find_or_create(
        conn, cfg, pronote_url, username, creds, class_name, body.account_pin
    )

    # Première collecte immédiate, comme le bot Telegram.
    result = fetch.collect(client, conn, user_id, cfg, "items")
    if not result.ok:
        # On garde le compte : le client pourra /refresh ensuite.
        pass

    subjects: list[str] = sorted({it.subject for it in db.get_items(conn, user_id)})
    token = issue_token(user_id, conn)
    csrf = csrf_token(token)
    set_csrf_cookie(response, request, csrf)
    # Cookie httpOnly : le navigateur se reconnecte seul pendant 90 jours,
    # y compris après un redémarrage du serveur. `secure` est posé quand la
    # requête arrive en HTTPS (l'instance LAN en HTTP simple ne le permet pas).
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=TOKEN_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=(
            request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
        ),
        path="/",
    )
    return PinOut(
        user_id=user_id, token=token, subjects=subjects, csrf_token=csrf
    )


# ── 2 ter. jeton CSRF ──────────────────────────────────────────────────


@router.get("/csrf")
def get_csrf(request: Request, response: Response, conn: DBDep) -> dict[str, str]:
    """Rend le jeton à joindre aux écritures (`X-CSRF-Token` ou champ caché).

    Avec une session valide, le jeton est lié à celle-ci et stable. Avant
    connexion, il est aléatoire et doublé dans un cookie `SameSite=Lax` :
    un site tiers ne peut ni le lire ni l'attacher à sa propre requête.
    """
    jeton = presented_token(request)
    if jeton and resolve_token(jeton, conn):
        csrf = csrf_token(jeton)
    else:
        csrf = secrets.token_urlsafe(32)
    set_csrf_cookie(response, request, csrf)
    return {"csrf_token": csrf}


# ── 3. déconnexion ─────────────────────────────────────────────────────


@router.post("/logout")
def post_logout(user: UserDep, response: Response, conn: DBDep) -> dict[str, Any]:
    """Révoque le jeton présenté et supprime le compte de l'appelant.

    Le compte visé est toujours celui du jeton : impossible de déconnecter
    (donc d'effacer) l'élève connecté ailleurs.
    """
    revoke_token(user.token, conn)
    response.delete_cookie(COOKIE_NAME, path="/")
    clear_csrf_cookie(response)
    db.delete_user(conn, user.user_id)
    return {"ok": True}
