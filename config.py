"""
config.py — Load config.yaml + .env into a single dict.
"""
import os
import yaml
from dotenv import load_dotenv

load_dotenv()

# Secrets from .env
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")

_config_cache: dict | None = None


_ENV_OVERRIDES = [
    # (env var name, yaml path as tuple, caster)
    ("TELEGRAM_GROUP_CHAT_ID",        ("telegram", "group_chat_id"),        int),
    ("TELEGRAM_OPERATOR_USER_ID",     ("telegram", "operator_user_id"),     int),
    ("TELEGRAM_POLL_INTERVAL_SECONDS", ("telegram", "poll_interval_seconds"), int),
    ("GMAIL_POLL_INTERVAL_SECONDS",   ("gmail", "poll_interval_seconds"),   int),
    ("WORKFLOW_FOLLOWUP_INTERVAL_DAYS", ("workflow", "followup_interval_days"), float),
    ("WORKFLOW_ACCEPTANCE_FOLLOWUP_INTERVAL_HOURS",
     ("workflow", "acceptance_followup_interval_hours"), float),
]


def _apply_env_overrides(config: dict) -> dict:
    """Override selected YAML values with env vars when set. Env wins."""
    for env_name, path, cast in _ENV_OVERRIDES:
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        try:
            value = cast(raw)
        except ValueError as e:
            raise RuntimeError(f"Invalid {env_name}={raw!r}: {e}") from e
        # Walk/create the nested dict, then set the leaf.
        node = config
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value
    return config


def load() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    config_path = os.environ.get("CONFIG_PATH", "./config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        _config_cache = yaml.safe_load(f) or {}

    _config_cache = _apply_env_overrides(_config_cache)
    return _config_cache


def reload() -> dict:
    global _config_cache
    _config_cache = None
    return load()
