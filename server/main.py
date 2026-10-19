"""Point d'entrée FastAPI pour la PWA Pronote Blocus.

Sert l'API (`/api/*`) et les fichiers statiques (`web/`).
Lancement : `uvicorn server.main:app --reload` depuis la racine du projet.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import deps
from .routes import auth, chat, items, status

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


app = FastAPI(title="Pronote Blocus", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=web_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(items.router)
app.include_router(chat.router)
app.include_router(status.router)


@app.get("/health")
def health():
    return {"status": "ok"}


# Static files: serve web/ at root (API routes take precedence since registered first)
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


# Bootstrap au chargement (ouvre la base + la config).
_ = deps.get_cfg()
_ = deps.get_conn()
