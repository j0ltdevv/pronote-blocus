"""Dépendances partagées par les routes FastAPI.

Un point d'accès unique à la base et à la config : la connexion sqlite est
ouverte une fois au démarrage et partagée (elle est *thread-safe* via
`LockedConnection`), la config vient de l'environnement comme dans le bot.

Ce module porte aussi l'authentification de l'API : les jetons opaques
(alloués au login PIN) et la dépendance `UserDep` qui traduit l'en-tête
`Authorization: Bearer <jeton>` en identité côté serveur.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException
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

TOKEN_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class _Jeton:
    """Compte visé et date d'expiration (horloge monotone, immunisée à l'heure)."""

    user_id: str
    expires_at: float


_tokens: dict[str, _Jeton] = {}
_tokens_lock = threading.RLock()


def issue_token(user_id: str) -> str:
    """Crée un jeton opaque pour `user_id` et le range en mémoire."""
    token = secrets.token_urlsafe(32)
    with _tokens_lock:
        _purge_tokens()
        _tokens[token] = _Jeton(
            user_id=user_id,
            expires_at=time.monotonic() + TOKEN_TTL_SECONDS,
        )
    return token


def resolve_token(token: str) -> str | None:
    """Renvoie l'identifiant du compte, ou None si le jeton est mort."""
    now = time.monotonic()
    with _tokens_lock:
        entree = _tokens.get(token)
        if entree is None:
            return None
        if entree.expires_at <= now:
            del _tokens[token]
            return None
        return entree.user_id


def revoke_token(token: str) -> None:
    """Oublie un jeton (déconnexion, ou compte supprimé)."""
    with _tokens_lock:
        _tokens.pop(token, None)


def _purge_tokens() -> None:
    """Supprime les jetons périmés — appelé sous verrou."""
    now = time.monotonic()
    for token in [t for t, jeton in _tokens.items() if jeton.expires_at <= now]:
        del _tokens[token]


# ── identité de l'appelant ─────────────────────────────────────────────


@dataclass(frozen=True)
class Identity:
    """Compte authentifié + jeton présenté (pour pouvoir le révoquer)."""

    user_id: str
    token: str


_bearer = HTTPBearer(auto_error=False, description="Jeton rendu par /api/auth/pin.")


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(401, detail, headers={"WWW-Authenticate": "Bearer"})


def current_identity(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    conn: DBDep,
) -> Identity:
    """Traduit `Authorization: Bearer <jeton>` en compte authentifié.

    401 si l'en-tête manque, si le jeton est inconnu ou expiré, ou si le
    compte a disparu depuis l'émission du jeton. L'identité ne vient jamais
    du client : elle n'existe que par le jeton.
    """
    if creds is None:
        raise _unauthorized("Jeton d'accès requis (Authorization: Bearer).")
    user_id = resolve_token(creds.credentials)
    if user_id is None:
        raise _unauthorized("Jeton invalide ou expiré.")
    if db.get_user(conn, user_id) is None:
        revoke_token(creds.credentials)
        raise _unauthorized("Compte supprimé.")
    return Identity(user_id=user_id, token=creds.credentials)


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
