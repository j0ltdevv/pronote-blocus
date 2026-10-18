#!/usr/bin/env python3
"""Smoke bout en bout du pipeline PRONOTE public.

    /home/jolt/dev/pronote-blocus/.venv/bin/python scripts/smoke_pronote.py
    /home/jolt/dev/pronote-blocus/.venv/bin/python scripts/smoke_pronote.py --json

Le script rejoue la séquence exacte de `python -m app.dev_demo` sur le compte
de démonstration (`demo.index-education.net`) : connexion par mot de passe,
travail à faire, cahier de textes, informations, lecture des contenus des cinq
premiers cours non annulés, normalisation puis classement. `app/` reste
intact : S1 corrige `dev_demo`, ce smoke se contente de constater.

Invariants vérifiés (tous doivent passer) :
  * connexion au compte de démonstration ;
  * au moins un cours lu ;
  * au moins un item normalisé ;
  * sections « en retard » et « à venir » triées par date croissante ;
  * matières non vides et identifiables ;
  * toutes les dates des items dans la fenêtre [WINDOW_START, aujourd'hui + 45 j].

Codes de sortie :
  0  succès (une anomalie connue peut être signalée sans échouer) ;
  1  invariant violé, ou erreur de pipeline / réponse PRONOTE inexploitable ;
  2  erreur réseau (connexion, DNS, TLS, timeout, HTTP).

L'anomalie « contenus lus : 0 » est RAPPORTÉE, jamais corrigée : sur le compte
public, les 52 cours de la fenêtre sont annulés, donc `dev_demo` saute les cinq
premiers (`continue`) et n'appelle jamais `.content` (voir S1).

Ce n'est pas un test pytest : c'est un smoke réseau, hors suite de tests.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import socket
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import dev_demo  # noqa: E402
from app.pronote import auth, normalize, ranking  # noqa: E402

EXIT_OK = 0
EXIT_INVARIANT = 1
EXIT_NETWORK = 2

#: Même plafond que `app/dev_demo.py` : une requête réseau par cours lu.
CONTENT_LESSON_LIMIT = 5
#: Même horizon que `app/dev_demo.py`.
FORWARD_DAYS = 45
UNKNOWN_SUBJECT = "(matière inconnue)"


@dataclass
class Run:
    """Tout ce que le pipeline a ramené, sans jamais imprimer."""

    today: date
    start: date
    end: date
    connected: bool = False
    homework: list[Any] = field(default_factory=list)
    lessons: list[Any] = field(default_factory=list)
    infos: list[Any] = field(default_factory=list)
    contents: dict[str, Any] = field(default_factory=dict)
    content_errors: list[dict[str, Any]] = field(default_factory=list)
    read_errors: list[dict[str, Any]] = field(default_factory=list)
    items: list[Any] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    overdue: list[Any] = field(default_factory=list)
    upcoming: list[Any] = field(default_factory=list)
    fatal: dict[str, Any] | None = None


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


# ─────────────────────────── erreurs ───────────────────────────


def _is_network_error(exc: BaseException) -> bool:
    """Vrai si l'échec est au niveau du transport, pas de la donnée.

    Une `PronoteAPIError` (réponse inattendue du serveur) reste une erreur de
    pipeline : le réseau a répondu. Seuls les exceptions `requests` et les
    erreurs de connexion/socket donnent le code 2.
    """
    try:
        import requests
    except Exception:  # pragma: no cover - requests est une dépendance dure
        requests = None  # type: ignore[assignment]
    if requests is not None and isinstance(exc, requests.RequestException):
        return True
    return isinstance(exc, (TimeoutError, ConnectionError, socket.gaierror))


def _error_payload(stage: str, exc: BaseException) -> dict[str, Any]:
    message = str(exc).strip() or type(exc).__name__
    return {
        "stage": stage,
        "type": type(exc).__name__,
        "error": message,
        "network": _is_network_error(exc),
    }


# ─────────────────────────── pipeline ───────────────────────────


def run_pipeline() -> Run:
    """Rejoue `app/dev_demo.py`, sans rien imprimer. Lève sur erreur fatale."""
    today = date.today()
    run = Run(
        today=today,
        start=dev_demo._window_start(),
        end=today + timedelta(days=FORWARD_DAYS),
    )

    try:
        client = auth.login_with_password(
            dev_demo.DEMO_URL,
            dev_demo.DEMO_USER,
            dev_demo.DEMO_PASSWORD,
            app_uuid="smoke-pronote",
        )[0]
    except Exception as exc:  # réseau, identifiants de démo retirés
        run.fatal = _error_payload("connexion", exc)
        return run

    run.connected = getattr(client, "logged_in", True) is not False
    if not run.connected:
        run.fatal = {
            "stage": "connexion",
            "type": "LoginRefused",
            "error": "client.logged_in = False",
            "network": False,
        }
        return run

    try:
        run.homework = list(client.homework(run.start, run.end))
    except Exception as exc:
        # Même choix que dev_demo : sans travail à faire, le pipeline s'arrête.
        run.fatal = _error_payload("travail_a_faire", exc)
        return run

    try:
        run.lessons = list(client.lessons(run.start, run.today))
    except Exception as exc:
        run.read_errors.append(_error_payload("cahier_de_textes", exc))

    try:
        run.infos = list(
            client.information_and_surveys(run.start, run.today, only_unread=False)
        )
    except Exception as exc:
        run.read_errors.append(_error_payload("informations", exc))

    # Contenus : une requête réseau par cours, plafonnée comme dev_demo.
    for lesson in run.lessons[:CONTENT_LESSON_LIMIT]:
        if getattr(lesson, "canceled", False):
            continue
        slot = normalize.lesson_slot(lesson)
        try:
            run.contents[slot] = lesson.content
        except Exception as exc:
            run.content_errors.append({**_error_payload("contenu", exc), "slot": slot})

    try:
        raw = normalize.RawBundle(
            homework=run.homework,
            lessons=run.lessons,
            contents=run.contents,
            infos=run.infos,
            discussions=[],
            today=run.today,
        )
        run.items = normalize.build_items(client, raw)
        run.subjects = sorted(
            {
                normalize.subject_name(lesson)
                for lesson in run.lessons
                if normalize.subject_name(lesson) != UNKNOWN_SUBJECT
            }
        )
        run.overdue, run.upcoming = ranking.split_sections(
            run.items, {}, {}, run.today
        )
    except Exception as exc:
        run.fatal = _error_payload("normalisation", exc)

    return run


def _execute() -> Run:
    """`run_pipeline` ne doit jamais laisser échapper d'exception inattendue."""
    try:
        return run_pipeline()
    except Exception as exc:
        today = date.today()
        run = Run(today=today, start=today, end=today)
        run.fatal = _error_payload("pipeline", exc)
        return run


# ─────────────────────────── invariants ───────────────────────────


def _cancelled_count(lessons: list[Any]) -> int:
    return sum(1 for lesson in lessons if getattr(lesson, "canceled", False))


def _fatal_at(run: Run, stage: str) -> bool:
    return run.fatal is not None and run.fatal["stage"] == stage


def _connection_detail(run: Run) -> str:
    if run.fatal is not None and run.fatal["stage"] in ("connexion", "pipeline"):
        return f"échec : {run.fatal['error']}"
    if not run.connected:
        return "client non connecté"
    return "client connecté"


def _lesson_detail(run: Run, cancelled: int) -> str:
    total = len(run.lessons)
    if total and cancelled == total:
        return f"{total} cours lu(s), tous annulés"
    if cancelled:
        return f"{total} cours lu(s) ({cancelled} annulé(s))"
    return f"{total} cours lu(s)"


def _sort_problem(rows: list[Any]) -> str | None:
    """None si les dates montent ; sinon la première inversion lisible."""
    previous: date | None = None
    for index, row in enumerate(rows, start=1):
        due = row.item.due_date
        current = due or date.max
        if previous is not None and current < previous:
            label = due.isoformat() if due else "sans date"
            return f"ligne {index} ({label}) après une date plus récente"
        previous = current
    return None


def _sort_check(run: Run) -> Check:
    problems = []
    for label, rows in (("retard", run.overdue), ("à venir", run.upcoming)):
        problem = _sort_problem(rows)
        if problem:
            problems.append(f"{label} : {problem}")
    if problems:
        return Check("tri_dates_croissantes", False, "; ".join(problems))
    rows = run.overdue + run.upcoming
    undated = sum(1 for row in rows if row.item.due_date is None)
    detail = f"{len(rows)} ligne(s) triée(s) par date croissante"
    if undated:
        detail += f" ({undated} sans date, en fin de section)"
    return Check("tri_dates_croissantes", True, detail)


def _subjects_check(run: Run) -> Check:
    blank = [subject for subject in run.subjects if not subject.strip()]
    unknown = [item for item in run.items if item.subject == UNKNOWN_SUBJECT]
    blank_items = [item for item in run.items if not item.subject.strip()]
    problems = []
    if not run.subjects:
        problems.append("aucune matière")
    if blank:
        problems.append(f"{len(blank)} matière(s) vide(s)")
    if blank_items:
        problems.append(f"{len(blank_items)} item(s) sans matière")
    if unknown:
        problems.append(f"{len(unknown)} item(s) de matière inconnue")
    if problems:
        return Check("matieres_non_vides", False, "; ".join(problems))
    return Check(
        "matieres_non_vides",
        True,
        f"{len(run.subjects)} matière(s), aucune vide",
    )


def _dates_check(run: Run) -> Check:
    dated = [item for item in run.items if item.due_date is not None]
    undated = len(run.items) - len(dated)
    outside = [
        item for item in dated if not (run.start <= item.due_date <= run.end)
    ]
    if outside:
        sample = ", ".join(
            f"{item.due_date.isoformat()} ({item.subject})" for item in outside[:3]
        )
        if len(outside) > 3:
            sample += f" (+{len(outside) - 3})"
        return Check(
            "dates_dans_fenetre",
            False,
            f"{len(outside)} date(s) hors fenêtre : {sample}",
        )
    detail = (
        f"{len(dated)} date(s) dans "
        f"[{run.start.isoformat()}, {run.end.isoformat()}]"
    )
    if undated:
        detail += f", {undated} sans date"
    return Check("dates_dans_fenetre", True, detail)


def build_checks(run: Run) -> list[Check]:
    cancelled = _cancelled_count(run.lessons)
    return [
        Check(
            "connexion",
            run.connected and not _fatal_at(run, "connexion"),
            _connection_detail(run),
        ),
        Check(
            "cours_lus",
            len(run.lessons) > 0,
            _lesson_detail(run, cancelled),
        ),
        Check(
            "items_normalises",
            len(run.items) > 0,
            f"{len(run.items)} item(s) normalisé(s)",
        ),
        _sort_check(run),
        _subjects_check(run),
        _dates_check(run),
    ]


# ─────────────────────────── anomalies ───────────────────────────


def detect_anomalies(run: Run) -> list[dict[str, Any]]:
    """Signale l'anomalie connue sans la corriger (S1 s'en charge)."""
    anomalies: list[dict[str, Any]] = []
    if not run.lessons or run.contents:
        return anomalies

    window = run.lessons[:CONTENT_LESSON_LIMIT]
    window_cancelled = _cancelled_count(window)
    all_cancelled = _cancelled_count(run.lessons)
    if window and window_cancelled == len(window):
        explanation = (
            f"les {len(window)} premiers cours de la fenêtre sont annulés ; "
            "dev_demo saute les cours annulés avant de lire `.content`, donc "
            "aucune requête de contenu n'est faite."
        )
    elif run.content_errors:
        explanation = (
            "les lectures de `.content` ont toutes échoué (voir la liste des "
            "erreurs)."
        )
    else:
        explanation = "aucun contenu lu pour les cours non annulés de la fenêtre."

    anomalies.append(
        {
            "code": "contenus_zero",
            "owner": "S1",
            "message": (
                f"contenus lus : 0 alors que {len(run.lessons)} cours sont lus"
            ),
            "contents_read": len(run.contents),
            "lessons": len(run.lessons),
            "cancelled_lessons": all_cancelled,
            "content_window": len(window),
            "content_window_cancelled": window_cancelled,
            "content_errors": len(run.content_errors),
            "course_items": sum(1 for item in run.items if item.kind == "cours"),
            "explanation": explanation,
        }
    )
    return anomalies


# ─────────────────────────── sortie ───────────────────────────


def _all_errors(run: Run) -> list[dict[str, Any]]:
    errors = []
    if run.fatal is not None:
        errors.append(run.fatal)
    errors.extend(run.read_errors)
    errors.extend(run.content_errors)
    return errors


def _exit_code(errors: list[dict[str, Any]], checks: list[Check]) -> int:
    if any(error["network"] for error in errors):
        return EXIT_NETWORK
    if errors:
        return EXIT_INVARIANT
    if not all(check.ok for check in checks):
        return EXIT_INVARIANT
    return EXIT_OK


def _item_payload(item: Any, overdue: set[str], upcoming: set[str]) -> dict[str, Any]:
    if item.key in overdue:
        section = "overdue"
    elif item.key in upcoming:
        section = "upcoming"
    else:
        section = None
    return {
        "kind": item.kind,
        "source": item.source,
        "subject": item.subject,
        "title": item.title,
        "due_date": item.due_date.isoformat() if item.due_date else None,
        "attachments": len(item.attachments),
        "section": section,
    }


def build_payload(
    run: Run,
    checks: list[Check],
    anomalies: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    code: int,
) -> dict[str, Any]:
    cancelled = _cancelled_count(run.lessons)
    kinds = Counter(item.kind for item in run.items)
    overdue = {row.key for row in run.overdue}
    upcoming = {row.key for row in run.upcoming}
    return {
        "ok": code == EXIT_OK,
        "exit_code": code,
        "url": dev_demo.DEMO_URL,
        "account": dev_demo.DEMO_USER,
        "window": {
            "start": run.start.isoformat(),
            "end": run.end.isoformat(),
            "today": run.today.isoformat(),
        },
        "counts": {
            "lessons": len(run.lessons),
            "cancelled_lessons": cancelled,
            "readable_lessons": len(run.lessons) - cancelled,
            "homework": len(run.homework),
            "infos": len(run.infos),
            "contents_read": len(run.contents),
            "items": len(run.items),
            "items_by_kind": dict(sorted(kinds.items())),
            "subjects": len(run.subjects),
            "overdue": len(run.overdue),
            "upcoming": len(run.upcoming),
        },
        "checks": [
            {"name": check.name, "ok": check.ok, "detail": check.detail}
            for check in checks
        ],
        "anomalies": anomalies,
        "errors": errors,
        "subjects": run.subjects,
        "items": [
            _item_payload(item, overdue, upcoming) for item in run.items
        ],
    }


def render_text(
    run: Run,
    checks: list[Check],
    anomalies: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    code: int,
) -> str:
    cancelled = _cancelled_count(run.lessons)
    kinds = Counter(item.kind for item in run.items)
    kind_text = ", ".join(f"{kind}={count}" for kind, count in sorted(kinds.items()))
    lines = [
        f"Smoke PRONOTE — {dev_demo.DEMO_URL}",
        (
            f"Compte : {dev_demo.DEMO_USER} · fenêtre {run.start.isoformat()} → "
            f"{run.end.isoformat()} · aujourd'hui {run.today.isoformat()}"
        ),
        "",
        f"Connexion : {'OK' if run.connected else 'ÉCHEC'}",
        (
            f"Cours lus : {len(run.lessons)} ({cancelled} annulé(s)) · "
            f"contenus lus : {len(run.contents)}"
        ),
        (
            f"Items normalisés : {len(run.items)}"
            f"{f' ({kind_text})' if kind_text else ''} · "
            f"matières : {len(run.subjects)}"
        ),
        f"Sections : retard {len(run.overdue)} · à venir {len(run.upcoming)}",
        "",
        "Invariants",
    ]
    for check in checks:
        status = "OK  " if check.ok else "FAIL"
        lines.append(f"  {status} {check.name:<24} {check.detail}")

    if anomalies:
        lines += ["", "Anomalies"]
        for anomaly in anomalies:
            lines.append(
                f"  {anomaly['code']} ({anomaly['owner']}) : {anomaly['message']}"
            )
            lines.append(f"    {anomaly['explanation']}")
            lines.append(
                f"    {anomaly['cancelled_lessons']}/{anomaly['lessons']} cours "
                f"annulés · {anomaly['content_errors']} erreur(s) de contenu · "
                f"{anomaly['course_items']} item(s) « cours »"
            )

    if errors:
        lines += ["", "Erreurs"]
        for error in errors:
            label = "réseau" if error["network"] else "pipeline"
            lines.append(f"  [{label}] {error['stage']} : {error['error']}")

    lines += ["", f"Résultat : {'OK' if code == EXIT_OK else 'ÉCHEC'} (code {code})"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Smoke bout en bout du pipeline PRONOTE sur le compte de "
            "démonstration (réseau réel, hors pytest)."
        )
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="sortie JSON unique sur stdout (les bruits éventuels vont sur stderr)",
    )
    args = parser.parse_args(argv)

    if args.json:
        # Garantit un stdout strictement JSON même si une dépendance imprime.
        with contextlib.redirect_stdout(sys.stderr):
            run = _execute()
    else:
        run = _execute()

    checks = build_checks(run)
    anomalies = detect_anomalies(run)
    errors = _all_errors(run)
    code = _exit_code(errors, checks)

    if args.json:
        payload = build_payload(run, checks, anomalies, errors, code)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(run, checks, anomalies, errors, code))
    return code


if __name__ == "__main__":
    sys.exit(main())
