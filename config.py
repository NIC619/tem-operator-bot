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


def load() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    config_path = os.environ.get("CONFIG_PATH", "./config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        _config_cache = yaml.safe_load(f)

    return _config_cache


def reload() -> dict:
    global _config_cache
    _config_cache = None
    return load()
