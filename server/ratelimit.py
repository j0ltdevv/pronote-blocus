"""Limitation des tentatives de connexion web, par IP.

Cinq échecs sur cinq minutes bloquent l'IP : les tentatives suivantes
reçoivent un 429 avec `Retry-After`. Le compteur vit en mémoire (le serveur
n'a qu'un processus) et se purge tout seul : à chaque appel, les échecs
sortis de la fenêtre sont oubliés, et une IP sans échec récent disparaît de
la table. Un balayage d'adresses ne laisse donc pas de fuite mémoire.

Un succès n'est jamais compté : seuls les refus d'identifiants alimentent
le compteur.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import Callable

from fastapi import HTTPException, Request

#: Échecs tolérés avant blocage, et durée de la fenêtre glissante.
MAX_ECHECS = 5
FENETRE_SECONDS = 300

_LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})


def client_ip(request: Request) -> str:
    """IP de l'appelant.

    Le serveur n'écoute que sur 127.0.0.1, derrière Caddy : dans ce cas la
    vraie IP est la dernière entrée de `X-Forwarded-For` (Caddy l'ajoute à
    la fin). Hors loopback, l'en-tête ne prouve rien et est ignoré.
    """
    hote = request.client.host if request.client else ""
    if hote in _LOOPBACK:
        forwarded = request.headers.get("x-forwarded-for", "")
        dernier = forwarded.split(",")[-1].strip()
        if dernier:
            return dernier
    return hote or "inconnue"


class LoginLimiter:
    """Compteur glissant d'échecs par IP, avec horloge injectable."""

    def __init__(
        self,
        max_echecs: int = MAX_ECHECS,
        fenetre: float = FENETRE_SECONDS,
        horloge: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_echecs = max_echecs
        self.fenetre = fenetre
        self.horloge = horloge
        self._echecs: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _nettoyer(self, maintenant: float) -> None:
        """Oublie les échecs hors fenêtre et les IP devenues vides."""
        for ip, tirs in list(self._echecs.items()):
            while tirs and maintenant - tirs[0] >= self.fenetre:
                tirs.popleft()
            if not tirs:
                del self._echecs[ip]

    def verifier(self, ip: str) -> None:
        """Lève un 429 si l'IP a épuisé ses échecs dans la fenêtre."""
        with self._lock:
            maintenant = self.horloge()
            self._nettoyer(maintenant)
            tirs = self._echecs.get(ip)
            if tirs is None or len(tirs) < self.max_echecs:
                return
            attente = self.fenetre - (maintenant - tirs[0])
            raise HTTPException(
                429,
                "Trop de tentatives de connexion. Réessaie plus tard.",
                headers={"Retry-After": str(max(1, math.ceil(attente)))},
            )

    def echec(self, ip: str) -> None:
        """Enregistre un refus d'identifiants pour cette IP."""
        with self._lock:
            maintenant = self.horloge()
            self._nettoyer(maintenant)
            self._echecs.setdefault(ip, deque()).append(maintenant)


#: Instance partagée par les routes ; les tests la remplacent par une
#: instance à horloge simulée.
LIMITER = LoginLimiter()


def verifier(ip: str) -> None:
    LIMITER.verifier(ip)


def echec(ip: str) -> None:
    LIMITER.echec(ip)
