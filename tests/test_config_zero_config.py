"""Mode zero-config : `from_env(require_token=False)` génère et persiste APP_SECRET.

1. APP_SECRET absent → clé Fernet générée, écrite dans DATA_DIR/app_secret, mode 0600
2. second appel → même clé relue, pas de régénération
3. APP_SECRET fourni → utilisé tel quel, rien n'est écrit sur disque
4. require_token=True → RuntimeError "APP_SECRET manquant", aucun fichier lu ni écrit
"""

from __future__ import annotations

import os
import stat

import pytest
from cryptography.fernet import Fernet

from app import config


def _zero_env(tmp_path, **extra: str) -> dict[str, str]:
    env = {"DATA_DIR": str(tmp_path / "data")}
    env.update(extra)
    return env


def _posix_only():
    if os.name != "posix" or os.geteuid() == 0:
        pytest.skip("permissions non vérifiables (non-POSIX ou exécuté en root)")


# ── 1. génération + permissions ──────────────────────────────────────────────


def test_zero_config_generates_and_persists_app_secret(tmp_path):
    cfg = config.from_env(_zero_env(tmp_path), require_token=False)

    key_file = tmp_path / "data" / "app_secret"
    assert key_file.is_file(), "le secret doit être persisté dans DATA_DIR/app_secret"
    assert cfg.app_secret == key_file.read_text(encoding="utf-8").strip()
    Fernet(cfg.app_secret.encode())  # la clé générée est réellement utilisable


def test_generated_app_secret_file_has_0600_permissions(tmp_path):
    _posix_only()

    config.from_env(_zero_env(tmp_path), require_token=False)

    mode = stat.S_IMODE((tmp_path / "data" / "app_secret").stat().st_mode)
    assert mode == 0o600, f"attendu 0600, obtenu {oct(mode)}"


def test_zero_config_creates_missing_data_dir(tmp_path):
    cfg = config.from_env(_zero_env(tmp_path), require_token=False)

    assert (tmp_path / "data").is_dir()
    assert cfg.data_dir == tmp_path / "data"


def test_zero_config_generated_secret_is_unique_per_install(tmp_path):
    first = config.from_env(_zero_env(tmp_path), require_token=False).app_secret
    second = config.from_env(
        _zero_env(tmp_path, DATA_DIR=str(tmp_path / "elsewhere")),
        require_token=False,
    ).app_secret

    assert first != second, "deux installations doivent obtenir des clés distinctes"


# ── 2. réutilisation au second appel ─────────────────────────────────────────


def test_zero_config_reuses_persisted_secret_on_second_call(tmp_path):
    env = _zero_env(tmp_path)
    first = config.from_env(env, require_token=False)
    key_file = tmp_path / "data" / "app_secret"
    content = key_file.read_text(encoding="utf-8")
    mtime = key_file.stat().st_mtime_ns

    second = config.from_env(env, require_token=False)

    assert second.app_secret == first.app_secret
    assert key_file.read_text(encoding="utf-8") == content, (
        "le fichier existant ne doit pas être réécrit"
    )
    assert key_file.stat().st_mtime_ns == mtime


def test_zero_config_ignores_unreadable_persisted_secret_file(tmp_path):
    _posix_only()

    # Un fichier illisible ne doit pas planter from_env : on régénère
    # plutôt que de démarrer sans clé.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    key_file = data_dir / "app_secret"
    key_file.write_text("corrompu\n", encoding="utf-8")
    key_file.chmod(0o000)

    cfg = config.from_env(_zero_env(tmp_path), require_token=False)

    assert cfg.app_secret, "un secret doit toujours résulter du mode zero-config"
    Fernet(cfg.app_secret.encode())


# ── 3. priorité d'un APP_SECRET fourni ───────────────────────────────────────


def test_zero_config_env_app_secret_wins_over_generation(tmp_path, secret):
    cfg = config.from_env(_zero_env(tmp_path, APP_SECRET=secret), require_token=False)

    assert cfg.app_secret == secret
    assert not (tmp_path / "data" / "app_secret").is_file(), (
        "APP_SECRET fourni via l'environnement ne doit pas être écrit sur disque"
    )


def test_zero_config_whitespace_only_app_secret_falls_back_to_generation(tmp_path):
    cfg = config.from_env(_zero_env(tmp_path, APP_SECRET="  "), require_token=False)

    assert cfg.app_secret, "un APP_SECRET blanc doit être traité comme absent"
    assert (tmp_path / "data" / "app_secret").is_file()


# ── 4. pas de génération quand require_token=True ────────────────────────────


def test_require_token_true_does_not_generate_app_secret(tmp_path):
    # require_token=True réclame TELEGRAM_BOT_TOKEN et APP_SECRET ; la
    # génération zero-config ne doit jamais se déclencher dans ce mode.
    key_file = tmp_path / "data" / "app_secret"

    with pytest.raises(RuntimeError, match="APP_SECRET manquant"):
        config.from_env(
            _zero_env(tmp_path, TELEGRAM_BOT_TOKEN="x:y"), require_token=True
        )

    assert not key_file.is_file(), (
        "require_token=True ne doit jamais écrire de clé sur disque"
    )


def test_require_token_true_does_not_read_persisted_secret_file(tmp_path):
    persisted = Fernet.generate_key().decode()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "app_secret").write_text(persisted + "\n", encoding="utf-8")

    # Le fichier DATA_DIR/app_secret est une spécificité du mode zero-config :
    # en mode complet il ne doit pas servir de source implicite de APP_SECRET.
    with pytest.raises(RuntimeError, match="APP_SECRET manquant"):
        config.from_env(
            _zero_env(tmp_path, TELEGRAM_BOT_TOKEN="x:y"), require_token=True
        )

    assert (data_dir / "app_secret").read_text(encoding="utf-8").strip() == persisted
