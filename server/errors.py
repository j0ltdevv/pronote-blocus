"""Erreurs internes : JSON pour l'API, page HTML sobre pour le reste.

Le middleware intercepte les exceptions non gérées *avant* le
`ServerErrorMiddleware` de Starlette. Sans cela, celui-ci renvoie bien une
réponse 500 mais relance l'exception : le serveur ASGI journalise alors la
trace brute, où un message d'exception peut contenir un mot de passe ou une
adresse. Ici la trace est masquée, puis journalisée une seule fois ; le
client ne voit qu'un message générique et un identifiant d'incident court.
"""

from __future__ import annotations

import html
import logging
import os
import re
import secrets
import traceback

from starlette.responses import HTMLResponse, JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .security import SECURITY_HEADERS

log = logging.getLogger(__name__)

#: Message unique, volontairement muet sur la cause.
MESSAGE = "Une erreur interne est survenue. Réessaie dans un instant."
TITRE = "Erreur interne"
INCIDENT_HEADER = "X-Incident-Id"

#: Variables d'environnement dont la valeur ne doit jamais finir dans un log.
_SECRET_ENV = ("APP_SECRET", "TELEGRAM_BOT_TOKEN", "LLM_API_KEY")

#: Motifs masqués dans la trace (ordre significatif : le plus spécifique d'abord).
_MASQUES: tuple[tuple[re.Pattern[str], str], ...] = (
    # En-tête d'authentification recopié dans un message d'exception.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+"), r"\1 ***"),
    # Jeton de bot Telegram (`<bot_id>:<secret>`).
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), "***"),
    # Identifiants dans l'URL (`https://user:motdepasse@hôte/...`).
    (re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1***:***@"),
    # Valeurs de query string : un lien PRONOTE peut porter un identifiant.
    (re.compile(r"(https?://[^\s?\"']+)\?[^\s\"']*"), r"\1?***"),
    # Clé Fernet (44 caractères base64 URL-safe, padding compris).
    (re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_=-])"), "***"),
    # Mot de passe, secret, jeton, session ou clé passé en clair.
    (
        re.compile(
            r"(?i)(?<![A-Za-z0-9])(password|passwd|mot[\s_-]?de[\s_-]?passe|secret|token"
            r"|session(?:[_-]?id)?|api[_-]?key)\b"
            r"(\s*[=:]\s*)"
            r"(\"[^\"]*\"|'[^']*'|[^\s,;)}\]]+)"
        ),
        r"\1\2***",
    ),
    # Adresse électronique.
    (re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b"), "***@***"),
    # Identifiant numérique long (chat_id Telegram, téléphone).
    (re.compile(r"\b\d{8,}\b"), "***"),
)


def new_incident() -> str:
    """Identifiant court (8 caractères), recopiable depuis la page d'erreur."""
    return secrets.token_hex(4)


def redact(texte: str) -> str:
    """Masque les secrets connus et les données personnelles évidentes."""
    for nom in _SECRET_ENV:
        valeur = os.environ.get(nom) or ""
        # Une valeur trop courte masquerait du texte innocent.
        if len(valeur) >= 4:
            texte = texte.replace(valeur, "***")
    for motif, remplacement in _MASQUES:
        texte = motif.sub(remplacement, texte)
    return texte


def safe_trace(exc: BaseException) -> str:
    """Trace complète (fichiers, lignes, fonctions), texte de l'erreur masqué."""
    brute = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return redact(brute)


def _page(incident: str) -> str:
    """Page autonome : aucun script, aucune donnée d'élève."""
    reference = html.escape(incident)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="fr">\n'
        "<head>\n"
        '  <meta charset="UTF-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '  <meta name="color-scheme" content="light dark">\n'
        '  <meta name="robots" content="noindex">\n'
        f"  <title>{TITRE} — Pronote Blocus</title>\n"
        '  <link rel="stylesheet" href="/offline.css">\n'
        "</head>\n"
        "<body>\n"
        "  <main>\n"
        f"    <h1>{TITRE}</h1>\n"
        f"    <p>{MESSAGE}</p>\n"
        f"    <p>Référence : <code>{reference}</code></p>\n"
        "  </main>\n"
        "</body>\n"
        "</html>\n"
    )


class ErrorPageMiddleware:
    """Transforme toute exception non gérée en réponse propre et journalisée."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_tracked(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_tracked)
        except Exception as exc:
            incident = new_incident()
            methode = redact(scope.get("method", ""))
            chemin = redact(scope.get("path", ""))  # jamais la query string
            # `log.error`, pas `log.exception` : la trace brute relancerait
            # les secrets que `safe_trace` vient de masquer.
            log.error(
                "incident %s — %s %s\n%s",
                incident,
                methode,
                chemin,
                safe_trace(exc),
            )
            if response_started:
                # Corps déjà entamé (flux) : impossible de le remplacer.
                return
            await self._reponse(scope, receive, send, incident)

    async def _reponse(
        self, scope: Scope, receive: Receive, send: Send, incident: str
    ) -> None:
        chemin = scope.get("path", "")
        entetes = dict(SECURITY_HEADERS)
        entetes[INCIDENT_HEADER] = incident
        # Une erreur n'est jamais mise en cache par un intermédiaire.
        entetes["Cache-Control"] = "no-store"

        if chemin == "/api" or chemin.startswith("/api/"):
            reponse: HTMLResponse | JSONResponse = JSONResponse(
                {"detail": MESSAGE, "incident": incident},
                status_code=500,
                headers=entetes,
            )
        else:
            reponse = HTMLResponse(
                _page(incident), status_code=500, headers=entetes
            )
        await reponse(scope, receive, send)
