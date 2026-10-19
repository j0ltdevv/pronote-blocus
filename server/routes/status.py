"""Page de statut publique : uptime, synchro, erreurs.

Aucune authentification et aucune donnée d'élève : la page ne lit que des
compteurs et des horodatages globaux. La dernière erreur est réduite à un
libellé connu, jamais au texte brut de la base.
"""

from __future__ import annotations

import html
import time

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.pronote import fetch

from ..deps import DBDep
from ..timing import TIMINGS

router = APIRouter(tags=["status"])

#: Démarrage du processus : `time.monotonic()` ignore les sauts d'horloge.
STARTED_AT = time.monotonic()

#: Seuls ces libellés sortent : tout autre texte stocké est masqué.
PUBLIC_ERRORS = frozenset(
    {
        fetch.ERR_TOKEN,
        fetch.ERR_PIN,
        fetch.ERR_UNREACHABLE,
        fetch.ERR_TIMEOUT,
        fetch.ERR_RATE,
        fetch.ERR_UNKNOWN,
    }
)
MASKED_ERROR = "erreur de synchronisation"


def _public_error(raw: str | None) -> str | None:
    """Erreur stockée → libellé sûr (aucun texte libre ne traverse)."""
    if not raw:
        return None
    return raw if raw in PUBLIC_ERRORS else MASKED_ERROR


def snapshot(conn) -> dict:
    """Valeurs affichées par `/status` et `/status.json`."""
    row = conn.execute(
        "SELECT COUNT(*) AS users, MAX(last_sync_at) AS last_sync_at FROM users"
    ).fetchone()
    erreur = conn.execute(
        "SELECT last_error FROM users"
        " WHERE last_error IS NOT NULL AND last_error <> ''"
        " ORDER BY COALESCE(last_error_at, '') DESC LIMIT 1"
    ).fetchone()
    return {
        "uptime_seconds": int(time.monotonic() - STARTED_AT),
        "last_sync_at": row["last_sync_at"],
        "users": int(row["users"]),
        "last_error": _public_error(erreur["last_error"] if erreur else None),
        # p50/p95 des cinq dernières minutes ; `None` tant qu'aucune requête.
        "latency_ms": TIMINGS.snapshot(),
    }


def _format_uptime(seconds: int) -> str:
    jours, reste = divmod(max(0, seconds), 86400)
    heures, reste = divmod(reste, 3600)
    minutes, secondes = divmod(reste, 60)
    morceaux = []
    if jours:
        morceaux.append(f"{jours} j")
    if heures:
        morceaux.append(f"{heures} h")
    if minutes:
        morceaux.append(f"{minutes} min")
    if not morceaux:
        morceaux.append(f"{secondes} s")
    return " ".join(morceaux)


def _format_stamp(value: str | None) -> str:
    if not value:
        return "jamais"
    return value.replace("T", " ").replace("+00:00", " UTC")


@router.get("/status.json")
def status_json(conn: DBDep) -> dict:
    return snapshot(conn)


@router.get("/status", response_class=HTMLResponse)
def status_page(conn: DBDep) -> HTMLResponse:
    data = snapshot(conn)
    page = f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Statut — Pronote Blocus</title>
<link rel="stylesheet" href="/status.css">
</head>
<body>
<main>
<h1>Pronote Blocus — statut</h1>
<dl>
  <dt>Uptime</dt>
  <dd>{html.escape(_format_uptime(data["uptime_seconds"]))}</dd>
  <dt>Dernière synchro réussie</dt>
  <dd>{html.escape(_format_stamp(data["last_sync_at"]))}</dd>
  <dt>Utilisateurs</dt>
  <dd>{data["users"]}</dd>
  <dt>Dernière erreur</dt>
  <dd>{html.escape(data["last_error"] or "aucune")}</dd>
</dl>
<footer><a href="/status.json">Version JSON</a></footer>
</main>
</body>
</html>
"""
    return HTMLResponse(page)
