"""Quota LLM et `done_local` sous écritures simultanées réelles.

`tests/test_concurrency.py` couvre la rotation de jeton et la réécriture des
items ; ici on vise les deux autres compteurs que plusieurs threads peuvent
faire avancer en même temps :

- `db.bump_llm_count`, consommé par l'event loop à chaque question et
  aujourd'hui appelé par un seul thread — la propriété à garantir est qu'un
  appel concurrent ne peut ni dépasser le plafond du jour, ni perdre un
  incrément (read-modify-write non atomique) ;
- `db.mark_done`, qui doit rester idempotent quel que soit le nombre de
  threads qui cochent le même devoir, et survivre à `db.replace_items`.

Tout est borné en nombre d'itérations (aucune boucle sur l'horloge) : ces
tests tournent en quelques dizaines de millisecondes.
"""

from __future__ import annotations

import threading

from app import db
from app.pronote.normalize import Item

JOUR = "2026-10-17"
VEILLE = "2026-10-16"
LENDEMAIN = "2026-10-18"
PLAFOND = 10
#: Plus de threads que le plafond : le surplus DOIT être refusé.
WORKERS = 2 * PLAFOND


def _lance(workers: int, job) -> list:
    """Lance `workers` threads sur `job(i)`, tous libérés en même temps.

    Rend la liste des résultats, ou échoue avec les exceptions remontées par
    les threads (aucune ne doit remonter).
    """
    barriere = threading.Barrier(workers)
    erreurs: list[BaseException] = []
    resultats: list = [None] * workers

    def runner(i: int) -> None:
        try:
            barriere.wait(timeout=10)
            resultats[i] = job(i)
        except BaseException as exc:  # noqa: BLE001 - on veut TOUT remonter
            erreurs.append(exc)

    threads = [
        threading.Thread(target=runner, args=(i,), daemon=True) for i in range(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive(), "un thread d'écriture est resté bloqué"

    assert not erreurs, f"exceptions concurrentes : {erreurs!r}"
    return resultats


# ─────────────────────────── quota LLM ───────────────────────────


def test_increments_simultanes_ne_depassent_jamais_le_plafond(conn, make_user):
    """20 questions lancées d'un coup, plafond 10 : exactement 10 acceptées."""
    uid = make_user()

    autorisees = _lance(WORKERS, lambda _i: db.bump_llm_count(conn, uid, JOUR, PLAFOND))

    assert sum(1 for ok in autorisees if ok) == PLAFOND, (
        f"le plafond doit être atteint exactement une fois : {autorisees!r}"
    )
    assert sum(1 for ok in autorisees if ok is False) == WORKERS - PLAFOND
    row = db.get_user(conn, uid)
    assert row["llm_day"] == JOUR
    assert row["llm_count"] == PLAFOND


def test_increments_simultanes_sous_le_plafond_ne_perdent_rien(conn, make_user):
    """Aucun `lost update` : 32 threads sous plafond lointain font tous +1."""
    uid = make_user()

    autorisees = _lance(WORKERS, lambda _i: db.bump_llm_count(conn, uid, JOUR, 10_000))

    assert autorisees == [True] * WORKERS, "aucune question ne doit être perdue"
    row = db.get_user(conn, uid)
    assert row["llm_count"] == WORKERS, "le compteur doit valoir le nombre d'appels"
    assert row["llm_day"] == JOUR


def test_remise_a_zero_journaliere_resiste_aux_appels_simultanes(conn, make_user):
    """Plafond consommé la veille, puis 10 questions simultanées du lendemain."""
    uid = make_user()
    for _ in range(PLAFOND):
        assert db.bump_llm_count(conn, uid, VEILLE, PLAFOND) is True
    assert db.bump_llm_count(conn, uid, VEILLE, PLAFOND) is False

    autorisees = _lance(
        PLAFOND, lambda _i: db.bump_llm_count(conn, uid, LENDEMAIN, PLAFOND)
    )

    assert autorisees == [True] * PLAFOND, (
        "le nouveau jour doit repartir d'un compteur à zéro"
    )
    row = db.get_user(conn, uid)
    assert row["llm_day"] == LENDEMAIN
    assert row["llm_count"] == PLAFOND


def test_plafonds_de_deux_comptes_restent_independants(conn, make_user):
    """Le quota d'un élève ne doit pas consommer celui d'un autre."""
    premier = make_user(chat_id=1)
    second = make_user(chat_id=2)
    plafond_partage = 3
    par_compte = 2 * plafond_partage
    cibles = [premier] * par_compte + [second] * par_compte

    def job(i: int) -> bool:
        return db.bump_llm_count(conn, cibles[i], JOUR, plafond_partage)

    autorisees = _lance(len(cibles), job)

    premieres = autorisees[:par_compte]
    secondes = autorisees[par_compte:]
    assert sum(1 for ok in premieres if ok) == plafond_partage
    assert sum(1 for ok in secondes if ok) == plafond_partage
    assert db.get_user(conn, premier)["llm_count"] == plafond_partage
    assert db.get_user(conn, second)["llm_count"] == plafond_partage


def test_compte_inconnu_refuse_sans_rien_ecrire(conn):
    """Un `user_id` absent renvoie False au lieu de lever (appelé par l'event loop)."""

    assert db.bump_llm_count(conn, "inconnu", JOUR, PLAFOND) is False
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_jour_anterieur_ne_rouvre_pas_le_quota_du_jour(conn, make_user):
    """Un appel daté de la veille (thread attardé à minuit) ne rouvre pas le quota.

    Le détail des cas est dans `tests/test_quota_day.py`.
    """
    uid = make_user()
    for _ in range(3):
        assert db.bump_llm_count(conn, uid, JOUR, 3) is True
    assert db.bump_llm_count(conn, uid, JOUR, 3) is False, "quota du 17 épuisé"

    assert db.bump_llm_count(conn, uid, VEILLE, 3) is False
    assert db.bump_llm_count(conn, uid, JOUR, 3) is False, "quota du 17 toujours épuisé"
    row = db.get_user(conn, uid)
    assert (row["llm_day"], row["llm_count"]) == (JOUR, 3), "ni date ni compteur ne reculent"


# ─────────────────────────── done_local ───────────────────────────


def test_marquer_le_meme_devoir_est_idempotent_sous_concurrence(conn, make_user):
    """16 threads cochent le même devoir : une seule ligne, aucune exception."""
    uid = make_user()

    _lance(16, lambda _i: db.mark_done(conn, uid, "maths-devoir-1"))

    assert db.done_keys(conn, uid) == {"maths-devoir-1"}
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM done_local WHERE user_id = ?", (uid,)
        ).fetchone()[0]
        == 1
    )


def test_marquages_simultanes_de_cles_distinctes_sont_tous_conserves(conn, make_user):
    """6 threads × 5 devoirs différents : les 30 clés doivent être là, sans doublon."""
    uid = make_user()
    par_thread = 5
    workers = 6
    attendues = {
        f"devoir-{i}-{j}" for i in range(workers) for j in range(par_thread)
    }

    def job(i: int) -> None:
        for j in range(par_thread):
            db.mark_done(conn, uid, f"devoir-{i}-{j}")

    _lance(workers, job)

    assert db.done_keys(conn, uid) == attendues
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM done_local WHERE user_id = ?", (uid,)
        ).fetchone()[0]
        == len(attendues)
    )


def test_done_local_survit_a_une_reecriture_concurrente_des_items(conn, make_user):
    """Une synchro qui réécrit les items ne doit pas effacer un devoir coché."""
    uid = make_user()
    deja_faits = [f"fait-{i}" for i in range(4)]
    for key in deja_faits:
        db.mark_done(conn, uid, key)
    items = [
        Item(
            key="en-cours",
            source="travail_a_faire",
            subject="Maths",
            title="Exercices",
            body="page 12",
            due_date=None,
            kind="devoir",
        )
    ]
    rounds = 40

    def job(i: int) -> None:
        if i % 2:
            for _ in range(rounds):
                db.replace_items(conn, uid, items)
        else:
            for _ in range(rounds):
                for key in deja_faits:
                    db.mark_done(conn, uid, key)

    _lance(4, job)

    assert db.done_keys(conn, uid) == set(deja_faits), "les devoirs cochés ont survécu"
    assert {item.key for item in db.get_items(conn, uid)} == {"en-cours"}
