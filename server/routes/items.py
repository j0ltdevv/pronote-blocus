"""Routes métiers : liste des devoirs, cocher `fait`, rafraîchir.

L'élève n'est jamais nommé par le client : il vient du jeton (voir `UserDep`).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import db, llm
from app.config import Config, today as _today
from app.pronote import fetch, ranking

from ..deps import CfgDep, DBDep, UserDep

router = APIRouter(prefix="/api/items", tags=["items"])


# ── models ─────────────────────────────────────────────────────────────


class DoneIn(BaseModel):
    item_id: str


# ── helpers ────────────────────────────────────────────────────────────


def _to_api(item, done: set[str]) -> dict[str, Any]:
    return {
        "id": item.key,
        "subject": item.subject,
        "title": item.title,
        "due": item.due_date.isoformat() if item.due_date else None,
        "urgent": bool(
            item.kind == "controle"
            and item.due_date is not None
        ),
        "done": item.key in done,
    }


def _rows_for(conn, cfg: Config, user_id: str):
    """Items + résumés + priorités + cochages, triés par `ranking.split_sections`."""
    items = db.get_items(conn, user_id)
    summaries = llm.ensure_summaries(conn, items, cfg)
    priorities = db.get_priorities(conn, user_id)
    done = db.done_keys(conn, user_id)
    overdue, upcoming = ranking.split_sections(
        items, summaries, priorities, _today(), done
    )
    return list(overdue) + list(upcoming)


# ── GET /api/items ─────────────────────────────────────────────────────


@router.get("", response_model=list[dict[str, Any]])
def list_items(
    user: UserDep,
    conn: DBDep,
    cfg: CfgDep,
) -> list[dict[str, Any]]:
    """Liste des devoirs de l'élève, triés par urgence puis matières."""
    user_id = user.user_id
    done = db.done_keys(conn, user_id)
    rows = _rows_for(conn, cfg, user_id)
    return [_to_api(row.item, done) for row in rows]


# ── POST /api/items/done ───────────────────────────────────────────────


@router.post("/done")
def mark_done(
    body: DoneIn,
    user: UserDep,
    conn: DBDep,
) -> dict[str, Any]:
    """Coche un devoir localement (`done_local`), pour l'appelant uniquement."""
    db.mark_done(conn, user.user_id, body.item_id)
    return {"ok": True}


# ── POST /api/items/refresh ────────────────────────────────────────────


@router.post("/refresh")
def refresh(
    user: UserDep,
    conn: DBDep,
    cfg: CfgDep,
) -> dict[str, Any]:
    """Relance une synchronisation PRONOTE (rotation du jeton)."""
    user_id = user.user_id
    result = fetch.sync_user(conn, user_id, cfg, "items")
    if not result.ok:
        if result.error == fetch.ERR_TOKEN:
            raise HTTPException(
                401, "Connexion PRONOTE expirée — renvoie un QR avec /api/auth/qr."
            )
        raise HTTPException(502, f"PRONOTE indisponible : {result.error}")

    return {"ok": True, "new_count": len(result.items)}
