"""Point d'entrée FastAPI pour la PWA Pronote Blocus.

Sert l'API (`/api/*`) et les fichiers statiques (`web/`).
Lancement : `uvicorn server.main:app --reload` depuis la racine du projet.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import backup
from . import deps
from .routes import auth, chat, items, status
from .security import SecurityHeadersMiddleware

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DEFAULT_WEB_ORIGIN = "http://localhost:8000"


def web_origins() -> list[str]:
    """Origines autorisées pour le frontend (`WEB_ORIGIN`, séparées par des virgules).

    Aucun joker : l'API accepte un en-tête `Authorization` et des cookies,
    donc `*` laisserait n'importe quel site lire les devoirs de l'élève.
    """
    raw = os.environ.get("WEB_ORIGIN") or DEFAULT_WEB_ORIGIN
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    return origins or [DEFAULT_WEB_ORIGIN]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Sauvegarde de la base au démarrage, puis toutes les 24 h.

    Le bot fait de même : un `flock` et un contrôle de fraîcheur empêchent
    les deux processus de doubler les fichiers.
    """
    cfg = deps.get_cfg()
    await asyncio.to_thread(
        backup.backup_at_startup, cfg.db_path, cfg.backups_dir, cfg.backup_keep
    )
    task = asyncio.create_task(
        backup.backup_loop(cfg.db_path, cfg.backups_dir, cfg.backup_keep)
    )
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Pronote Blocus", version="0.1.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def _validation_erreur(_request, exc):  # noqa: ANN001
    """422 sans jamais renvoyer l'`input` reçut : un mot de passe envoyé
    dans le corps ne doit pas revenir dans la réponse HTTP."""
    erreurs = [
        {"loc": erreur.get("loc"), "msg": erreur.get("msg"), "type": erreur.get("type")}
        for erreur in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": erreurs})


app.add_middleware(
    CORSMiddleware,
    allow_origins=web_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ajouté en dernier : enveloppe aussi CORS, les erreurs et les fichiers statiques.
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(auth.router)
app.include_router(items.router)
app.include_router(chat.router)
app.include_router(status.router)

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/healthz")
def healthz():
    """Sonde de vivacité : la base répond-elle ? 200, sinon 503.

    La réponse ne contient ni chemin ni message d'erreur : un 503 dit
    seulement que la base est injoignable (fichier disparu, disque plein,
    connexion fermée), pas pourquoi.
    """
    try:
        deps.get_conn().execute("SELECT 1").fetchone()
    except Exception:
        return JSONResponse(
            {"status": "degraded", "db": "unavailable"}, status_code=503
        )
    return {"status": "ok", "db": "ok"}


# Static files: serve web/ at root (API routes take precedence since registered first)
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


# Bootstrap au chargement (ouvre la base + la config).
_ = deps.get_cfg()
_ = deps.get_conn()
