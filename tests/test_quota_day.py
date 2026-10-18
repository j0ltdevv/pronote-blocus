"""Quota LLM : un jour antérieur ne doit jamais rouvrir le quota du jour courant.

`db.bump_llm_count` reçoit la date de l'appelant (`config.today()` côté bot,
`_today()` côté serveur web). Un thread attardé au passage de minuit peut donc
appeler avec la date de la veille *après* que le quota du jour a été consommé.
Sans garde, l'écriture `llm_day = veille` remet `llm_count` à zéro et le retour
au jour courant repart de zéro : le quota est rouvert une seconde fois.

Ce fichier fixe la règle : `day` strictement antérieur à `llm_day` stocké est
refusé sans écriture. Tout reste borné en nombre d'itérations (aucune boucle sur
l'horloge) : la suite tourne en quelques dizaines de millisecondes.
"""

from __future__ import annotations

import threading

from app import db

VEILLE = "2026-10-16"
JOUR = "2026-10-17"
LENDEMAIN = "2026-10-18"
PLAFOND = 10
#: Plus de threads que le plafond : le surplus DOIT être refusé.
WORKERS = 20


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


# ─────────────────────────── jour antérieur ───────────────────────────


def test_jour_anterieur_refuse_sans_toucher_au_compteur(conn, make_user):
    """Un appel daté de la veille renvoie False et laisse `llm_day`/`llm_count`."""
    uid = make_user()
    assert db.bump_llm_count(conn, uid, JOUR, PLAFOND) is True
    assert db.bump_llm_count(conn, uid, JOUR, PLAFOND) is True

    assert db.bump_llm_count(conn, uid, VEILLE, PLAFOND) is False

    row = db.get_user(conn, uid)
    assert row["llm_day"] == JOUR, "la date stockée ne doit jamais reculer"
    assert row["llm_count"] == 2, "le compteur du jour courant est intact"


def test_quota_epuise_reste_ferme_apres_un_appel_attarde(conn, make_user):
    """Le bug d'origine : 3 questions du 17, un appel du 16, puis le quota rouvre."""
    uid = make_user()
    for _ in range(PLAFOND):
        assert db.bump_llm_count(conn, uid, JOUR, PLAFOND) is True
    assert db.bump_llm_count(conn, uid, JOUR, PLAFOND) is False, "quota du 17 épuisé"

    assert db.bump_llm_count(conn, uid, VEILLE, PLAFOND) is False
    assert db.bump_llm_count(conn, uid, JOUR, PLAFOND) is False, (
        "le quota du jour courant doit rester fermé"
    )
    row = db.get_user(conn, uid)
    assert (row["llm_day"], row["llm_count"]) == (JOUR, PLAFOND)


def test_jour_anterieur_ne_rouvre_pas_un_quota_partiellement_consomme(conn, make_user):
    """Même non épuisé, le quota du jour ne recule pas sur un appel daté."""
    uid = make_user()
    for _ in range(PLAFOND - 1):
        assert db.bump_llm_count(conn, uid, JOUR, PLAFOND) is True

    assert db.bump_llm_count(conn, uid, VEILLE, PLAFOND) is False

    row = db.get_user(conn, uid)
    assert (row["llm_day"], row["llm_count"]) == (JOUR, PLAFOND - 1)
    # Une seule question reste due, pas deux.
    assert db.bump_llm_count(conn, uid, JOUR, PLAFOND) is True
    assert db.bump_llm_count(conn, uid, JOUR, PLAFOND) is False


def test_llm_day_vide_accepte_le_premier_appel(conn, make_user):
    """Un compte neuf (`llm_day` NULL) accepte n'importe quelle date du jour."""
    uid = make_user()
    assert db.get_user(conn, uid)["llm_day"] is None

    assert db.bump_llm_count(conn, uid, VEILLE, PLAFOND) is True

    row = db.get_user(conn, uid)
    assert (row["llm_day"], row["llm_count"]) == (VEILLE, 1)


# ─────────────────────────── passage de minuit ───────────────────────────


def test_jour_courant_repete_ne_remet_pas_le_compteur_a_zero(conn, make_user):
    """Appels successifs du même jour : `llm_day` stable, compteur monotone."""
    uid = make_user()
    for attendu in range(1, 5):
        assert db.bump_llm_count(conn, uid, JOUR, PLAFOND) is True
        row = db.get_user(conn, uid)
        assert (row["llm_day"], row["llm_count"]) == (JOUR, attendu)


def test_passage_de_minuit_remet_le_quota_a_zero(conn, make_user):
    """Plafond consommé la veille : le lendemain repart bien de zéro."""
    uid = make_user()
    for _ in range(PLAFOND):
        assert db.bump_llm_count(conn, uid, VEILLE, PLAFOND) is True
    assert db.bump_llm_count(conn, uid, VEILLE, PLAFOND) is False

    assert db.bump_llm_count(conn, uid, JOUR, PLAFOND) is True

    row = db.get_user(conn, uid)
    assert (row["llm_day"], row["llm_count"]) == (JOUR, 1)
    assert db.bump_llm_count(conn, uid, VEILLE, PLAFOND) is False, (
        "revenir en arrière après minuit ne rouvre rien"
    )


# ─────────────────────────── concurrence ───────────────────────────


def test_appels_attardes_et_du_jour_concurrents_respectent_le_plafond(conn, make_user):
    """20 threads : la moitié datés de la veille, l'autre du jour courant."""
    uid = make_user()
    deja = PLAFOND - 1
    for _ in range(deja):
        assert db.bump_llm_count(conn, uid, JOUR, PLAFOND) is True

    def job(i: int) -> bool:
        return db.bump_llm_count(conn, uid, JOUR if i % 2 else VEILLE, PLAFOND)

    resultats = _lance(WORKERS, job)

    attardes = resultats[0::2]
    courants = resultats[1::2]
    assert attardes == [False] * len(attardes), "aucun appel de la veille accepté"
    assert sum(1 for ok in courants if ok) == PLAFOND - deja, (
        f"une seule question restait due : {courants!r}"
    )
    row = db.get_user(conn, uid)
    assert (row["llm_day"], row["llm_count"]) == (JOUR, PLAFOND)


def test_remise_a_zero_concurrente_du_nouveau_jour(conn, make_user):
    """Plafond du jour épuisé, puis 20 questions simultanées du lendemain."""
    uid = make_user()
    for _ in range(PLAFOND):
        assert db.bump_llm_count(conn, uid, JOUR, PLAFOND) is True

    resultats = _lance(WORKERS, lambda _i: db.bump_llm_count(conn, uid, LENDEMAIN, PLAFOND))

    assert sum(1 for ok in resultats if ok) == PLAFOND, (
        "le nouveau jour doit repartir d'un compteur à zéro"
    )
    row = db.get_user(conn, uid)
    assert (row["llm_day"], row["llm_count"]) == (LENDEMAIN, PLAFOND)
