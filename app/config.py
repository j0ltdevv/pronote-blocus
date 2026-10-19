"""Configuration lue dans l'environnement — aucune valeur en dur."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_PRONOTE_URL = "https://0332722e.index-education.net/pronote/eleve.html"
DEFAULT_MOBILE_QUERY = "?fd=1&bydlg=A6ABB224-12DD-4E31-AD3E-8A39A1C2C335&login=true"

#: Telegram identifie les fils par un entier signé 64 bits.
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
#: Plafond de `MAX_CONCURRENT_SYNCS` : au-delà, c'est une faute de frappe.
MAX_SYNCS_CEILING = 64
#: Plafond de `BACKUP_KEEP` : au-delà, c'est une faute de frappe.
MAX_BACKUPS = 365

MOIS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}
JOURS_FR = {
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4,
    "samedi": 5, "dimanche": 6,
}


@dataclass(frozen=True)
class Config:
    telegram_token: str
    app_secret: str
    admin_chat_id: int | None
    window_start: str
    digest_hour: int
    default_pronote_url: str
    mobile_query: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_messages: bool
    max_concurrent_syncs: int
    backup_keep: int
    allow_password_login: bool
    password_ent: str
    db_path: Path
    data_dir: Path

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def locks_dir(self) -> Path:
        return self.data_dir / "locks"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    def window_start_date(self) -> date:
        return date.fromisoformat(self.window_start)


def _int_or_none(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        nombre = int(value.strip())
    except ValueError as exc:
        raise RuntimeError(f"ADMIN_CHAT_ID invalide (entier attendu) : {value!r}") from exc
    if not INT64_MIN <= nombre <= INT64_MAX:
        raise RuntimeError(
            f"ADMIN_CHAT_ID invalide (entier 64 bits attendu) : {value!r}"
        )
    return nombre


def _check_url_port(label: str, url: str) -> None:
    """Refuse un port non numérique ou hors 1..65535.

    Une chaîne sans schéma ni hôte (ex. « pas une url ») reste transmise telle
    quelle : `from_env` ne valide pas les URL complètes, seulement le port
    lorsqu'il y en a un.
    """
    if not url:
        return
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise RuntimeError(f"{label} invalide (URL illisible) : {url!r}") from exc
    if not parts.netloc:
        return
    try:
        port = parts.port
    except ValueError as exc:
        raise RuntimeError(
            f"{label} invalide (port hors 1..65535) : {url!r}"
        ) from exc
    if port is not None and port == 0:
        raise RuntimeError(f"{label} invalide (port hors 1..65535) : {url!r}")


def from_env(env: dict[str, str] | None = None, *, require_token: bool = True) -> Config:
    """Construit la config ; les cas limites échouent tôt ou retombent sur un défaut.

    - variable absente, vide ou blanche et défaut documenté → défaut, sauf
      `LLM_MESSAGES` où une valeur blanche désactive les messages ;
    - `MAX_CONCURRENT_SYNCS` ramené dans 1..64, non entier → 4 ;
    - `BACKUP_KEEP` ramené dans 1..365, non entier → 5 ;
    - `ADMIN_CHAT_ID` hors 64 bits → erreur ;
    - URL dont le port sort de 1..65535 → erreur ;
    - `DATA_DIR` relatif conservé tel quel (résolu depuis le répertoire courant).
    """
    e = dict(os.environ if env is None else env)

    def get(key: str, default: str = "") -> str:
        return (e.get(key) or default).strip()

    token = get("TELEGRAM_BOT_TOKEN")
    if require_token and not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN manquant dans l'environnement")
    secret = get("APP_SECRET")
    if require_token and not secret:
        raise RuntimeError(
            "APP_SECRET manquant. Génère-le avec : python -c "
            '"from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    data_dir_raw = get("DATA_DIR") or "data"
    try:
        data_dir = Path(data_dir_raw).expanduser()
    except RuntimeError as exc:
        raise RuntimeError(
            f"DATA_DIR invalide (utilisateur inconnu) : {data_dir_raw!r}"
        ) from exc
    # Mode web (require_token=False) : on tolère l'absence d'APP_SECRET et
    # on le génère à la volée pour que `uvicorn server.main:app` marche
    # sans rien configurer. La clé est persistée dans DATA_DIR/app_secret
    # pour survivre aux redémarrages (comme scripts/serve.sh).
    if not secret and not require_token:
        key_file = data_dir / "app_secret"
        if key_file.is_file():
            try:
                secret = key_file.read_text(encoding="utf-8").strip()
            except Exception:
                secret = ""
        if not secret:
            try:
                from cryptography.fernet import Fernet

                secret = Fernet.generate_key().decode()
                try:
                    data_dir.mkdir(parents=True, exist_ok=True)
                    key_file.write_text(secret + "\n", encoding="utf-8")
                    try:
                        key_file.chmod(0o600)
                    except Exception:
                        pass
                except Exception:
                    pass
            except Exception:
                secret = ""
    if require_token and secret:
        try:
            from cryptography.fernet import Fernet

            Fernet(secret.encode() if isinstance(secret, str) else secret)
        except Exception as exc:
            raise RuntimeError(f"APP_SECRET invalide (clé Fernet attendue) : {exc}") from exc
    window_start = get("WINDOW_START") or "2026-10-01"
    # Valide tôt : une date mal formée doit casser au démarrage, pas à la
    # première synchro d'un élève.
    try:
        date.fromisoformat(window_start)
    except ValueError as exc:
        raise RuntimeError(f"WINDOW_START invalide (AAAA-MM-JJ attendu) : {window_start!r}") from exc
    # Heure locale à partir de laquelle le résumé quotidien part (0-23).
    # Valide tôt : une valeur hors bornes doit casser au démarrage, sinon la
    # boucle de résumé ne se déclenche jamais (ou à n'importe quelle heure).
    digest_hour = get("DIGEST_HOUR") or "18"
    try:
        digest_hour_value = int(digest_hour)
        if not 0 <= digest_hour_value <= 23:
            raise ValueError
    except ValueError as exc:
        raise RuntimeError(
            f"DIGEST_HOUR invalide (entier 0-23 attendu) : {digest_hour!r}"
        ) from exc
    try:
        concurrency = int(get("MAX_CONCURRENT_SYNCS") or "4")
    except ValueError:
        concurrency = 4
    concurrency = min(MAX_SYNCS_CEILING, max(1, concurrency))
    # 0 supprimerait la sauvegarde qu'on vient d'écrire : plancher à 1.
    try:
        backup_keep = int(get("BACKUP_KEEP") or "5")
    except ValueError:
        backup_keep = 5
    backup_keep = min(MAX_BACKUPS, max(1, backup_keep))
    default_pronote_url = get("DEFAULT_PRONOTE_URL") or DEFAULT_PRONOTE_URL
    _check_url_port("DEFAULT_PRONOTE_URL", default_pronote_url)
    llm_base_url = get("LLM_BASE_URL").rstrip("/")
    _check_url_port("LLM_BASE_URL", llm_base_url)
    return Config(
        telegram_token=token,
        app_secret=secret,
        admin_chat_id=_int_or_none(get("ADMIN_CHAT_ID")),
        window_start=window_start,
        digest_hour=digest_hour_value,
        default_pronote_url=default_pronote_url,
        mobile_query=get("PRONOTE_MOBILE_QUERY") or DEFAULT_MOBILE_QUERY,
        llm_base_url=llm_base_url,
        llm_api_key=get("LLM_API_KEY"),
        llm_model=get("LLM_MODEL"),
        llm_messages=get("LLM_MESSAGES", "1").lower() not in ("0", "false", "no", ""),
        max_concurrent_syncs=concurrency,
        backup_keep=backup_keep,
        allow_password_login=(
            get("ALLOW_PASSWORD_LOGIN", "1").lower() not in ("0", "false", "no")
        ),
        password_ent=get("PASSWORD_ENT") or "lyceeconnecte_aquitaine",
        db_path=data_dir / "app.db",
        data_dir=data_dir,
    )


def today() -> date:
    return datetime.now().date()
