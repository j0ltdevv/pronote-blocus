"""Démo hors Telegram : pipeline complet sur le compte public PRONOTE.

    python -m app.dev_demo

Se connecte au lycée de démonstration (`demo.index-education.net`), collecte
les cinq sources accessibles, normalise, classe et imprime les deux sections
(retard / à venir) plus la liste des matières. Aucun token Telegram, aucune
base, aucun LLM : ce script sert à voir tourner le pipeline en vrai.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

from .pronote import auth, normalize, ranking

DEMO_URL = "https://demo.index-education.net/pronote/eleve.html"
DEMO_USER = "demonstration"
DEMO_PASSWORD = "pronotevs"


def _window_start() -> date:
    return date.fromisoformat(os.environ.get("WINDOW_START", "2026-10-01"))


def main() -> int:
    today = date.today()
    start = _window_start()
    end = today + timedelta(days=45)
    print(f"Connexion à {DEMO_URL} ({DEMO_USER})…")
    try:
        client = auth.login_with_password(
            DEMO_URL, DEMO_USER, DEMO_PASSWORD, app_uuid="dev-demo"
        )[0]
    except Exception as exc:  # réseau, ent, identifiants de démo retirés
        print(f"Échec de connexion au compte de démonstration : {exc}")
        print(
            "L'URL de démo ou le compte public a peut-être changé. "
            "Utilise WINDOW_START=… avec un compte réel si besoin."
        )
        return 1

    try:
        homework = list(client.homework(start, end))
    except Exception as exc:
        print(f"Lecture du travail à faire impossible : {exc}")
        return 1

    try:
        lessons = list(client.lessons(start, today))
    except Exception as exc:
        print(f"Lecture du cahier de textes impossible : {exc}")
        lessons = []

    try:
        infos = list(client.information_and_surveys(start, today, only_unread=False))
    except Exception as exc:
        print(f"Lecture des informations impossible : {exc}")
        infos = []

    # Contenus de cours : une requête réseau par cours, on plafonne pour la démo.
    contents = {}
    for lesson in lessons[:5]:
        if getattr(lesson, "canceled", False):
            continue
        try:
            contents[normalize.lesson_slot(lesson)] = lesson.content
        except Exception as exc:
            print(f"  (contenu illisible pour {normalize.lesson_slot(lesson)} : {exc})")

    raw = normalize.RawBundle(
        homework=homework,
        lessons=lessons,
        contents=contents,
        infos=infos,
        discussions=[],
        today=today,
    )
    items = normalize.build_items(client, raw)

    subjects = sorted(
        {
            normalize.subject_name(lesson)
            for lesson in lessons
            if normalize.subject_name(lesson) != "(matière inconnue)"
        }
    )

    overdue, upcoming = ranking.split_sections(items, {}, {}, today)

    print()
    print(f"Fenêtre : {start} → {end} · aujourd'hui {today}")
    print(f"Cours lus : {len(lessons)} · contenus lus : {len(contents)}")
    print(f"Items normalisés : {len(items)}")
    print(f"Matières ({len(subjects)}) : {', '.join(subjects) or '(aucune)'}")
    print()

    for titre, rows in (("EN RETARD", overdue), ("À VENIR", upcoming)):
        print(f"── {titre} ({len(rows)}) " + "─" * 20)
        if not rows:
            print("  (rien)")
        for idx, row in enumerate(rows, start=1):
            due = row.item.due_date.isoformat() if row.item.due_date else "sans date"
            flag = " ⚠️" if row.item.kind == "controle" else ""
            print(
                f"  {idx:2d}. [{row.item.kind}] {row.item.subject}{flag} — "
                f"{due} · {row.item.title}"
            )
        print()

    if not items:
        print(
            "Aucun item produit : le compte de démonstration est peut-être vide "
            "ou l'URL de démo a été retirée."
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
