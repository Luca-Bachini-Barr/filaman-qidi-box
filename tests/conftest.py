"""Stand-ins for FilaMan's modules, so the driver can be tested on its own."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
CAPTURES = sorted((ROOT / "captures").glob("*.jsonl"))


class BaseDriver:
    driver_key = ""

    def __init__(self, printer_id: int, config: dict[str, Any], emitter) -> None:
        self.printer_id = printer_id
        self.config = config
        self.emit = emitter
        self._running = False

    def log_debug(self, *_args: Any) -> None:
        pass


class _Column:
    def __eq__(self, _other: object) -> bool:  # type: ignore[override]
        return True

    __hash__ = None  # type: ignore[assignment]

    def is_not(self, _other: object) -> bool:
        return True


class _Model:
    id = slot_id = slot_no = printer_id = spool_id = _Column()


class _Select:
    def __init__(self, *columns: Any) -> None:
        self.columns = columns

    def join(self, *_args: Any) -> _Select:
        return self

    def where(self, *_args: Any) -> _Select:
        return self


def _module(name: str, **attrs: Any) -> None:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    module.__path__ = []  # type: ignore[attr-defined]
    sys.modules[name] = module


class EventBus:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, event: dict) -> None:
        self.events.append(event)


_module("sqlalchemy", select=_Select)
_module("app")
_module("app.core")
_module("app.core.database", async_session_maker=None)
_module("app.core.event_bus", event_bus=EventBus())
_module("app.models")
_module("app.models.printer", PrinterSlot=_Model, PrinterSlotAssignment=_Model)
_module("app.plugins")
_module("app.plugins.base", BaseDriver=BaseDriver)
_module("app.services")
_module("app.services.spool_service", SpoolService=None)
