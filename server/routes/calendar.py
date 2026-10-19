"""Export iCalendar (RFC 5545) des devoirs de l'élève connecté.

Un `VEVENT` par devoir daté, `DTSTART` au jour d'échéance (journée entière).
L'`UID` dérive de la clé de contenu de l'item : deux exports successifs
rendent le même identifiant, donc un abonnement calendrier met à jour
l'événement au lieu d'en créer un second.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Response

from app import db

from ..deps import DBDep, UserDep

router = APIRouter(tags=["calendar"])

PRODID = "-//Pronote Blocus//Export iCalendar//FR"

#: RFC 5545 §3.1 : 75 octets par ligne physique, CRLF, pliage compris.
LINE_OCTETS = 75

#: Seuls les devoirs vont dans l'agenda : les `cours` (jour de la leçon) et
#: les items `info`/`message` (dates lues dans le texte) sont exclus.
KINDS_EVENEMENTS = frozenset({"devoir", "controle"})


def escape_text(value: str) -> str:
    """Échappe une valeur TEXT (RFC 5545 §3.3.11) : `\\`, `;`, `,`, retours."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\r", "\\n")
        .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """Plie une ligne à 75 octets, sans couper un caractère UTF-8.

    Les lignes de continuation commencent par une espace, qui compte dans
    les 75 octets : elles portent donc 74 octets de contenu.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= LINE_OCTETS:
        return line
    parts: list[str] = []
    start = 0
    while start < len(encoded):
        budget = LINE_OCTETS - 1 if parts else LINE_OCTETS
        end = min(start + budget, len(encoded))
        # Reculer jusqu'à une frontière de caractère UTF-8.
        while start < end < len(encoded) and (encoded[end] & 0xC0) == 0x80:
            end -= 1
        parts.append(encoded[start:end].decode("utf-8"))
        start = end
    return "\r\n ".join(parts)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _event(key: str, subject: str, title: str, due: date, stamp: str) -> list[str]:
    """Lignes d'un `VEVENT` pour un devoir daté."""
    return [
        "BEGIN:VEVENT",
        f"UID:{key}@pronote-blocus",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{due.strftime('%Y%m%d')}",
        f"SUMMARY:{escape_text(f'{subject} — {title}')}",
        "END:VEVENT",
    ]


def render_calendar(items: list, now: datetime | None = None) -> str:
    """Document iCalendar complet : CRLF partout, chaque ligne pliée."""
    stamp = _stamp(now or datetime.now(timezone.utc))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:Pronote Blocus",
    ]
    dates = [
        item
        for item in items
        if item.due_date is not None and item.kind in KINDS_EVENEMENTS
    ]
    dates.sort(key=lambda item: (item.due_date, item.subject, item.title, item.key))
    for item in dates:
        lines.extend(
            _event(item.key, item.subject, item.title, item.due_date, stamp)
        )
    lines.append("END:VCALENDAR")
    return "".join(fold(line) + "\r\n" for line in lines)


@router.get("/calendar.ics")
def calendar_ics(user: UserDep, conn: DBDep) -> Response:
    """Flux iCalendar des devoirs de l'appelant (jeton Bearer ou cookie)."""
    body = render_calendar(db.get_items(conn, user.user_id))
    return Response(content=body, media_type="text/calendar; charset=utf-8")
