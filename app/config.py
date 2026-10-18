"""Configuration lue dans l'environnement — aucune valeur en dur."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

DEFAULT_PRONOTE_URL = "https://0332722e.index-education.net/pronote/eleve.html"
DEFAULT_MOBILE_QUERY = "?fd=1&bydlg=A6ABB224-12DD-4E31-AD3E-8A39A1C2C335&login=true"

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
    default_pronote_url: str
    mobile_query: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_messages: bool
    max_concurrent_syncs: int
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

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
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
        return int(value.strip())
    except ValueError as exc:
        raise RuntimeError(f"ADMIN_CHAT_ID invalide (entier attendu) : {value!r}") from exc


def from_env(env: dict[str, str] | None = None, *, require_token: bool = True) -> Config:
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
    data_dir = Path(get("DATA_DIR", "data")).expanduser()
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
    window_start = get("WINDOW_START", "2026-10-01")
    # Valide tôt : une date mal formée doit casser au démarrage, pas à la
    # première synchro d'un élève.
    try:
        date.fromisoformat(window_start)
    except ValueError as exc:
        raise RuntimeError(f"WINDOW_START invalide (AAAA-MM-JJ attendu) : {window_start!r}") from exc
    try:
        concurrency = max(1, int(get("MAX_CONCURRENT_SYNCS", "4")))
    except ValueError:
        concurrency = 4
    return Config(
        telegram_token=token,
        app_secret=secret,
        admin_chat_id=_int_or_none(get("ADMIN_CHAT_ID")),
        window_start=window_start,
        default_pronote_url=get("DEFAULT_PRONOTE_URL", DEFAULT_PRONOTE_URL),
        mobile_query=get("PRONOTE_MOBILE_QUERY", DEFAULT_MOBILE_QUERY),
        llm_base_url=get("LLM_BASE_URL").rstrip("/"),
        llm_api_key=get("LLM_API_KEY"),
        llm_model=get("LLM_MODEL"),
        llm_messages=get("LLM_MESSAGES", "1").lower() not in ("0", "false", "no", ""),
        max_concurrent_syncs=concurrency,
        allow_password_login=get("ALLOW_PASSWORD_LOGIN", "1") not in ("0", "false", "no"),
        password_ent=get("PASSWORD_ENT", "lyceeconnecte_aquitaine"),
        db_path=data_dir / "app.db",
        data_dir=data_dir,
    )


def today() -> date:
    return datetime.now().date()
