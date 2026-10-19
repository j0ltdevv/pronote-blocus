"""Durée des requêtes HTTP : une ligne de log par requête, p50/p95 récents.

La mesure ne garde que `(instant, ms)` en mémoire, sur cinq minutes. Rien du
corps, des en-têtes ni de la requête (ni jeton, ni mot de passe) n'est lu :
seuls la méthode, le chemin de la route, le statut et la durée sortent.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from typing import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

#: Fenêtre des percentiles exposés : les vieilles requêtes sont oubliées.
WINDOW_SECONDS = 300.0

#: Plafond dur : borne la mémoire si le trafic explose dans la fenêtre.
MAX_SAMPLES = 20_000


def percentile(sorted_values: list[float], rank: float) -> float | None:
    """Percentile par rang le plus proche, arrondi au dixième de ms.

    Sur de petits échantillons, interposer n'apporte rien : le p95 d'une
    liste de trois valeurs est la plus lente, pas une moyenne inventée.
    """
    if not sorted_values:
        return None
    index = max(0, math.ceil(rank / 100 * len(sorted_values)) - 1)
    return round(sorted_values[index], 1)


class Timings:
    """Durées récentes (ms) et percentiles associés."""

    def __init__(
        self,
        *,
        window: float = WINDOW_SECONDS,
        maxlen: int = MAX_SAMPLES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window
        self._clock = clock
        self._samples: deque[tuple[float, float]] = deque(maxlen=maxlen)

    def record(self, ms: float) -> None:
        self._samples.append((self._clock(), ms))

    def clear(self) -> None:
        """Vide l'historique (isolation des tests)."""
        self._samples.clear()

    def _recent(self) -> list[float]:
        seuil = self._clock() - self._window
        # Les plus anciennes sont en tête : une seule purge suffit.
        while self._samples and self._samples[0][0] < seuil:
            self._samples.popleft()
        return sorted(ms for _, ms in self._samples)

    def snapshot(self) -> dict:
        """Valeurs publiées par `/status.json` ; vides tant qu'aucune requête."""
        durees = self._recent()
        return {
            "window_seconds": int(self._window),
            "count": len(durees),
            "p50": percentile(durees, 50),
            "p95": percentile(durees, 95),
        }


#: Instance unique du processus : middleware et route de statut la partagent.
TIMINGS = Timings()


def _path(scope: Scope) -> str:
    """Chemin de la route (`/api/items/{item_id}`), sinon le chemin brut.

    La requête entière (query string comprise) n'est jamais journalisée : un
    jeton passé en paramètre ne doit pas finir dans les logs.
    """
    route = scope.get("route")
    modele = getattr(route, "path", None)
    return modele or scope.get("path") or "-"


class RequestTimingMiddleware:
    """Mesure chaque requête HTTP et journalise `method path status ms`."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        debut = time.perf_counter()
        statut = 500

        async def send_avec_statut(message: Message) -> None:
            nonlocal statut
            if message["type"] == "http.response.start":
                statut = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_avec_statut)
        finally:
            ms = (time.perf_counter() - debut) * 1000
            TIMINGS.record(ms)
            # Une seule ligne, clés fixes : `journalctl -o cat` reste greppable.
            log.info(
                "requête method=%s path=%s status=%d ms=%.1f",
                scope.get("method", "-"),
                _path(scope),
                statut,
                ms,
            )
