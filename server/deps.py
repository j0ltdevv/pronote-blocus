"""Dépendances partagées par les routes FastAPI.

Un point d'accès unique à la base et à la config : la connexion sqlite est
ouverte une fois au démarrage et partagée (elle est *thread-safe* via
`LockedConnection`), la config vient de l'environnement comme dans le bot.

Ce module porte aussi l'authentification de l'API : les jetons opaques
(alloués au login PIN) et la dépendance `UserDep` qui traduit l'en-tête
`Authorization: Bearer <jeton>` en identité côté serveur.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import Depends, Request, Response, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import config, db


# ── initialisation unique ──────────────────────────────────────────────

_setup_lock = threading.Lock()
_conn: object | None = None
_cfg: config.Config | None = None


def _bootstrap() -> tuple[config.Config, object]:
    """Ouvre la config et la base une seule fois, de façon thread-safe."""
    global _conn, _cfg
    with _setup_lock:
        if _conn is None or _cfg is None:
            cfg = config.from_env(require_token=False)
            cfg.ensure_dirs()
            conn = db.connect(cfg.db_path)
            db.init_schema(conn)
            db.purge_old_summaries(conn)
            _cfg = cfg
            _conn = conn
    return _cfg, _conn  # type: ignore[return-value]


def get_cfg() -> config.Config:
    cfg, _ = _bootstrap()
    return cfg


def get_conn() -> object:
    _, conn = _bootstrap()
    return conn


# ── dépendances FastAPI ────────────────────────────────────────────────

DBDep = Annotated[object, Depends(get_conn)]
CfgDep = Annotated[config.Config, Depends(get_cfg)]


# ── sessions web : jetons opaques en mémoire ───────────────────────────
#
# Un jeton par connexion PIN, TTL 24 h. Le magasin est volatil (comme
# l'instance qui n'a qu'un processus) : redémarrer le serveur déconnecte
# tout le monde, ce qui est acceptable puisque le compte reste en base.

# 90 jours : l'élève ne refait un QR qu'à l'expiration, pas à chaque
# redémarrage. Le jeton est haché en base, donc une fuite ne donne rien.
TOKEN_TTL_SECONDS = 90 * 24 * 60 * 60
COOKIE_NAME = "blocus_session"

# ── CSRF ───────────────────────────────────────────────────────────────
#
# Chaque écriture doit prouver qu'elle vient de la page de l'élève. Le jeton
# attendu n'est pas stocké : il est dérivé du jeton de session (HMAC avec
# `APP_SECRET`), donc stable entre redémarrages et impossible à deviner sans
# le cookie de session. La copie lisible dans `blocus_csrf` sert aux pages
# qui préfèrent lire le cookie ; la comparaison reste côté serveur.

CSRF_COOKIE_NAME = "blocus_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
CSRF_FIELD_NAME = "csrf_token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Repli si `APP_SECRET` manque (tests unitaires) : les jetons ne survivent
# alors pas à un redémarrage, ce qui n'enlève rien à la protection.
_CSRF_FALLBACK_KEY = secrets.token_bytes(32)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _csrf_key() -> bytes:
    """Clé de dérivation, stable tant que `APP_SECRET` ne change pas."""
    try:
        secret = get_cfg().app_secret or ""
    except Exception:
        secret = ""
    return secret.encode() if secret else _CSRF_FALLBACK_KEY


def csrf_token(session_token: str) -> str:
    """Jeton CSRF lié à une session : un autre jeton de session en donne un autre."""
    return hmac.new(
        _csrf_key(), b"csrf:" + session_token.encode(), hashlib.sha256
    ).hexdigest()


def _cookie_secure(request: Request) -> bool:
    return (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
    )


def set_csrf_cookie(response: Response, request: Request, token: str) -> None:
    """Pose le jeton CSRF dans un cookie `SameSite=Lax`, lisible par le JS."""
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        max_age=TOKEN_TTL_SECONDS,
        httponly=False,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )


def clear_csrf_cookie(response: Response) -> None:
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")


def _bearer_present(request: Request) -> bool:
    """Vrai si l'en-tête `Authorization: Bearer` porte un jeton non vide."""
    entete = request.headers.get("authorization", "")
    prefixe = "bearer "
    return entete.lower().startswith(prefixe) and bool(entete[len(prefixe):].strip())


def presented_token(request: Request) -> str | None:
    """Jeton de session présenté : en-tête prioritaire, sinon cookie httpOnly."""
    entete = request.headers.get("authorization", "")
    prefixe = "bearer "
    if entete.lower().startswith(prefixe):
        jeton = entete[len(prefixe):].strip()
        if jeton:
            return jeton
    return request.cookies.get(COOKIE_NAME)


async def _csrf_from_request(request: Request) -> str:
    """Jeton fourni en en-tête, ou en champ caché d'un formulaire classique.

    Le formulaire est décodé à la main : `request.form()` exigerait
    `python-multipart`, absent des dépendances du projet.
    """
    entete = request.headers.get(CSRF_HEADER_NAME, "").strip()
    if entete:
        return entete
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/x-www-form-urlencoded":
        return ""
    corps = await request.body()
    valeurs = parse_qs(corps.decode("utf-8", "replace"), keep_blank_values=False)
    valeur = valeurs.get(CSRF_FIELD_NAME, [""])[0]
    return valeur.strip()


def _browser_marker(request: Request) -> bool:
    """Un navigateur marque ses requêtes ; un client simple (curl) ne le fait pas."""
    return bool(request.headers.get("origin") or request.headers.get("referer"))


async def require_csrf(request: Request, conn: DBDep) -> None:
    """Refuse (403) toute écriture sans jeton CSRF valide.

    - `Authorization: Bearer` : l'en-tête n'est jamais attaché par le
      navigateur à une requête d'un autre site, donc pas de CSRF possible.
    - Cookie de session vivant : le jeton attendu est `csrf_token(cookie)` ;
      il doit arriver en `X-CSRF-Token` ou dans le champ `csrf_token`.
    - Avant connexion, ou avec un cookie mort (expiré, révoqué, base remise
      à zéro) : un navigateur envoie `Origin` et doit présenter le jeton de
      pré-connexion posé par `/api/auth/csrf`. Sans cette distinction, un
      cookie périmé empêcherait de se reconnecter indéfiniment.
    """
    if request.method.upper() in SAFE_METHODS:
        return
    if _bearer_present(request):
        return

    session = request.cookies.get(COOKIE_NAME)
    if session and resolve_token(session, conn) is not None:
        attendu = csrf_token(session)
        fourni = await _csrf_from_request(request)
        if not fourni:
            raise HTTPException(403, "Jeton CSRF manquant.")
        # `compare_digest` sur des octets : un jeton non ASCII doit être
        # refusé proprement, pas provoquer une TypeError (500).
        if not hmac.compare_digest(attendu.encode(), fourni.encode("utf-8", "replace")):
            raise HTTPException(403, "Jeton CSRF invalide.")
        return

    if not _browser_marker(request):
        return
    fourni = await _csrf_from_request(request)
    if not fourni:
        raise HTTPException(403, "Jeton CSRF manquant.")
    prealable = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not prealable or not hmac.compare_digest(
        prealable.encode("utf-8", "replace"), fourni.encode("utf-8", "replace")
    ):
        raise HTTPException(403, "Jeton CSRF invalide.")


def issue_token(user_id: str, conn=None) -> str:
    """Crée un jeton opaque pour `user_id`, persistant s'il y a une connexion."""
    token = secrets.token_urlsafe(32)
    if conn is not None:
        now = int(time.time())
        db.purge_web_sessions(conn, now)
        db.create_web_session(conn, _hash(token), user_id, now + TOKEN_TTL_SECONDS)
    _MEMORY[token] = (user_id, time.monotonic() + TOKEN_TTL_SECONDS)
    return token


def resolve_token(token: str, conn=None) -> str | None:
    """Renvoie l'identifiant du compte, ou None si le jeton est mort."""
    if conn is not None:
        user_id = db.get_web_session(conn, _hash(token), int(time.time()))
        if user_id is not None:
            return user_id
    entree = _MEMORY.get(token)
    if entree is None:
        return None
    user_id, expire = entree
    if expire <= time.monotonic():
        _MEMORY.pop(token, None)
        return None
    return user_id


def revoke_token(token: str, conn=None) -> None:
    """Oublie un jeton (déconnexion, ou compte supprimé)."""
    _MEMORY.pop(token, None)
    if conn is not None:
        db.delete_web_session(conn, _hash(token))


# ── identité de l'appelant ─────────────────────────────────────────────


@dataclass(frozen=True)
class Identity:
    """Compte authentifié + jeton présenté (pour pouvoir le révoquer)."""

    user_id: str
    token: str


_MEMORY: dict[str, tuple[str, float]] = {}

_bearer = HTTPBearer(auto_error=False, description="Jeton rendu par /api/auth/pin.")


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(401, detail, headers={"WWW-Authenticate": "Bearer"})


def current_identity(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    conn: DBDep,
) -> Identity:
    """Traduit `Authorization: Bearer <jeton>` en compte authentifié.

    401 si l'en-tête manque, si le jeton est inconnu ou expiré, ou si le
    compte a disparu depuis l'émission du jeton. L'identité ne vient jamais
    du client : elle n'existe que par le jeton.
    """
    # Deux porteurs possibles : l'en-tête (clients) et le cookie httpOnly
    # (navigateur), qui survit à la fermeture de l'onglet et au redémarrage
    # du serveur.
    # L'en-tête, s'il est présent, fait foi : un jeton révoqué ne doit pas
    # ressusciter grâce au cookie du navigateur. Le cookie n'est qu'un repli
    # quand aucune credential n'est fournie.
    token = creds.credentials if creds is not None else request.cookies.get(COOKIE_NAME)
    if not token:
        raise _unauthorized("Jeton d'accès requis (Authorization: Bearer).")
    user_id = resolve_token(token, conn)
    if user_id is None:
        raise _unauthorized("Jeton invalide ou expiré.")
    if db.get_user(conn, user_id) is None:
        revoke_token(token, conn)
        raise _unauthorized("Compte supprimé.")
    return Identity(user_id=user_id, token=token)


UserDep = Annotated[Identity, Depends(current_identity)]


# ── store de sessions QR → PIN (mémoire, dev) ──────────────────────────

_SESSIONS: dict[str, dict] = {}
_SESSIONS_LOCK = threading.Lock()


def sessions() -> dict[str, dict]:
    return _SESSIONS


def new_session(qr: dict, pronote_url: str) -> str:
    sid = secrets.token_urlsafe(16)
    with _SESSIONS_LOCK:
        _SESSIONS[sid] = {"qr": qr, "pronote_url": pronote_url, "pin": None}
    return sid


def drop_session(session_id: str) -> None:
    with _SESSIONS_LOCK:
        _SESSIONS.pop(session_id, None)
