"""Runtime settings: the declared tunables, their JSON override store, and the
live application onto ``config``.

Only keys in ``SETTINGS`` are visible to the API. Overrides persist on the
data volume and are applied at import, so a restart keeps them; the health
poll reads ``config`` on every tick, so a change applies at the next tick.
"""

import json
from enum import Enum
from pathlib import Path
from typing import Any

from medialab_contracts import (
    SettingSource,
    SettingSpec,
    SettingType,
    SettingValue,
    SettingView,
)

from medialab_orchestrator.core.config import AppConfig, config
from medialab_orchestrator.core.logger import app_logger

SERVICE_NAME = "medialab-orchestrator"
_APPLIES_NEXT_TICK = "next health-poll tick"

SETTINGS: tuple[SettingSpec, ...] = (
    SettingSpec(
        key="health_poll_interval_seconds",
        type=SettingType.INT,
        min=0,
        max=3600,
        description="Seconds between health-poll ticks; 0 pauses the poll.",
        applies=_APPLIES_NEXT_TICK,
    ),
    SettingSpec(
        key="auto_resume_max",
        type=SettingType.INT,
        min=0,
        max=10,
        description="How many times a stalled download is resumed before it needs attention.",
        applies=_APPLIES_NEXT_TICK,
    ),
    SettingSpec(
        key="auto_retry_max",
        type=SettingType.INT,
        min=0,
        max=10,
        description="How many times a failed job is retried before it needs attention.",
        applies=_APPLIES_NEXT_TICK,
    ),
)

_SPECS: dict[str, SettingSpec] = {spec.key: spec for spec in SETTINGS}


class UnknownSettingError(KeyError):
    """The key is not a declared setting."""


def _wire(value: Any) -> SettingValue:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _typed(key: str, value: SettingValue) -> Any:
    annotation = AppConfig.model_fields[key].annotation
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    return value


class SettingsStore:
    """Overrides as one JSON document. Missing or unreadable means no overrides."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, SettingValue]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            app_logger.warning("Settings store unreadable, ignoring it: %s", error)
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, overrides: dict[str, SettingValue]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(overrides, indent=2, sort_keys=True), encoding="utf-8")


class RuntimeSettings:
    """The declared settings applied onto a live ``AppConfig``."""

    def __init__(self, cfg: AppConfig, store: SettingsStore) -> None:
        self._config = cfg
        self._store = store
        self._env_values: dict[str, SettingValue] = {
            key: _wire(getattr(cfg, key)) for key in _SPECS
        }
        self._overrides: dict[str, SettingValue] = {}
        for key, raw in store.load().items():
            spec = _SPECS.get(key)
            if spec is None:
                continue
            try:
                self._overrides[key] = spec.coerce(raw)
            except ValueError as error:
                app_logger.warning("Stored setting ignored: %s", error)
        for key, value in self._overrides.items():
            setattr(cfg, key, _typed(key, value))

    def spec(self, key: str) -> SettingSpec:
        try:
            return _SPECS[key]
        except KeyError as error:
            raise UnknownSettingError(key) from error

    def view(self, key: str) -> SettingView:
        spec = self.spec(key)
        default = _wire(AppConfig.model_fields[key].default)
        if key in self._overrides:
            source = SettingSource.OVERRIDE
        elif self._env_values[key] != default:
            source = SettingSource.ENV
        else:
            source = SettingSource.DEFAULT
        return SettingView(
            key=key,
            value=_wire(getattr(self._config, key)),
            default=default,
            source=source,
            type=spec.type,
            description=spec.description,
            applies=spec.applies,
            choices=spec.choices,
            min=spec.min,
            max=spec.max,
        )

    def views(self) -> list[SettingView]:
        return [self.view(spec.key) for spec in SETTINGS]

    def set(self, key: str, raw: Any) -> SettingView:
        """Validate, persist, apply. ``ValueError`` carries the reason."""
        value = self.spec(key).coerce(raw)
        self._overrides[key] = value
        self._store.save(self._overrides)
        setattr(self._config, key, _typed(key, value))
        app_logger.info("Setting %s set to %r.", key, value)
        return self.view(key)

    def reset(self, key: str) -> SettingView:
        """Drop the override; the .env value (or default) applies again."""
        self.spec(key)
        if self._overrides.pop(key, None) is not None:
            self._store.save(self._overrides)
        setattr(self._config, key, _typed(key, self._env_values[key]))
        app_logger.info("Setting %s reset.", key)
        return self.view(key)


runtime_settings = RuntimeSettings(config, SettingsStore(Path(config.settings_path)))
