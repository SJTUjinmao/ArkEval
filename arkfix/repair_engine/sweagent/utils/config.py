from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import config as config_file
from sweagent import REPO_ROOT
from sweagent.utils.log import get_logger

logger = get_logger("config")


def convert_path_to_abspath(path: Path | str) -> Path:
    """If path is not absolute, convert it to an absolute path
    using the SWE_AGENT_CONFIG_ROOT environment variable (if set) or
    REPO_ROOT as base.
    """
    path = Path(path)
    root = Path(keys_config.get("SWE_AGENT_CONFIG_ROOT", REPO_ROOT))
    assert root.is_dir()
    if not path.is_absolute():
        path = root / path
    assert path.is_absolute()
    return path.resolve()


def convert_paths_to_abspath(paths: list[Path | str]) -> list[Path]:
    return [convert_path_to_abspath(p) for p in paths]


class Config:
    PROFILED_KEYS = {
        "OPENAI_API_KEY": ("OPENAI_API_KEY", "API_KEY"),
        "OPENAI_API_BASE_URL": ("OPENAI_API_BASE_URL", "OPENAI_BASE_URL", "API_BASE_URL", "BASE_URL"),
        "OPENAI_BASE_URL": ("OPENAI_BASE_URL", "OPENAI_API_BASE_URL", "BASE_URL", "API_BASE_URL"),
        "MODEL": ("MODEL", "OPENAI_MODEL", "MODEL_NAME"),
    }

    def __init__(self, *, keys_cfg_path: Path | None = None):
        """This wrapper class is used to load keys from environment variables or keys.cfg file.
        Whenever both are presents, the environment variable is used.
        """
        if keys_cfg_path is None:
            # Defer import to avoid circular import
            from sweagent import PACKAGE_DIR

            keys_cfg_path = PACKAGE_DIR.parent / "keys.cfg"
        self._keys_cfg = None
        if keys_cfg_path.exists():
            try:
                self._keys_cfg = config_file.Config(str(keys_cfg_path))
            except Exception as e:
                msg = f"Error loading keys.cfg from {keys_cfg_path}. Please check the file."
                raise RuntimeError(msg) from e
        else:
            logger.error(f"keys.cfg not found in {PACKAGE_DIR}")

    @staticmethod
    def _normalize_provider(provider: str | None) -> str | None:
        if not provider:
            return None
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", provider).strip("_").upper()
        return normalized or None

    def _raw_get(self, key: str) -> Any:
        if key in os.environ:
            return os.environ[key]
        if self._keys_cfg is not None and key in self._keys_cfg:
            return self._keys_cfg[key]
        raise KeyError(key)

    def _raw_contains(self, key: str) -> bool:
        return key in os.environ or (self._keys_cfg is not None and key in self._keys_cfg)

    def _active_provider(self) -> str | None:
        for key in ("LLM_PROVIDER", "OPENAI_PROVIDER", "MODEL_PROVIDER"):
            try:
                provider = self._raw_get(key)
            except KeyError:
                continue
            return self._normalize_provider(str(provider))
        return None

    def _provider_key_candidates(self, key: str) -> list[str]:
        provider = self._active_provider()
        if provider is None or key not in self.PROFILED_KEYS:
            return []
        return [f"{provider}_{suffix}" for suffix in self.PROFILED_KEYS[key]]

    def _get_value(self, key: str, default=None, *, missing_ok: bool) -> Any:
        if key in os.environ:
            return os.environ[key]

        provider_keys = self._provider_key_candidates(key)
        if provider_keys:
            for provider_key in provider_keys:
                if provider_key in os.environ:
                    return os.environ[provider_key]
                if self._keys_cfg is not None and provider_key in self._keys_cfg:
                    return self._keys_cfg[provider_key]

        if self._keys_cfg is not None and key in self._keys_cfg:
            return self._keys_cfg[key]
        if missing_ok:
            return default
        msg = f"Key {key} not found in environment variables or keys.cfg (if existing)"
        if provider_keys:
            msg += f"; also checked active-provider keys: {', '.join(provider_keys)}"
        raise KeyError(msg)

    def get(self, key: str, default=None, choices: list[Any] | None = None) -> Any:
        """Get a key from environment variables or keys.cfg.

        Args:
            key: The key to retrieve.
            default: The default value to return if the key is not found.
            choices: If provided, the value must be one of the choices.
        """

        def check_choices(value):
            if choices is not None and value not in choices:
                msg = f"Value {value} for key {key} not in {choices}"
                raise ValueError(msg)
            return value

        return check_choices(self._get_value(key, default, missing_ok=True))

    def __getitem__(self, key: str) -> Any:
        return self._get_value(key, missing_ok=False)

    def __contains__(self, key: str) -> bool:
        return any(
            self._raw_contains(provider_key) for provider_key in self._provider_key_candidates(key)
        ) or self._raw_contains(key)


keys_config = Config()
