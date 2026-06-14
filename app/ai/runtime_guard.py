from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from threading import RLock, get_ident
from typing import Any, Iterator
from uuid import uuid4


class AIRuntimeBusy(RuntimeError):
    pass


_RUNTIME_LOCK = RLock()
_ACTIVE: dict[str, Any] | None = None
_DEPTH = 0


def current_ai_runtime() -> dict[str, Any] | None:
    if _ACTIVE is None:
        return None
    return {key: value for key, value in _ACTIVE.items() if key != "thread_id"}


@contextmanager
def acquire_ai_runtime(kind: str, *, model: str | None = None, detail: str = "") -> Iterator[dict[str, Any]]:
    global _ACTIVE, _DEPTH

    acquired = _RUNTIME_LOCK.acquire(blocking=False)
    if not acquired:
        active = current_ai_runtime()
        label = active.get("kind") if active else "execucao"
        raise AIRuntimeBusy(f"IA ocupada com {label}. Aguarde finalizar antes de iniciar outra chamada.")

    thread_id = get_ident()
    created = False
    try:
        if _ACTIVE is None:
            _ACTIVE = {
                "run_id": f"ai-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}",
                "kind": kind,
                "model": model,
                "detail": detail,
                "started_at": datetime.utcnow().isoformat(),
                "thread_id": thread_id,
            }
            created = True
        elif _ACTIVE.get("thread_id") != thread_id:
            active = current_ai_runtime()
            label = active.get("kind") if active else "execucao"
            raise AIRuntimeBusy(f"IA ocupada com {label}. Aguarde finalizar antes de iniciar outra chamada.")

        _DEPTH += 1
        yield current_ai_runtime() or {}
    finally:
        _DEPTH = max(0, _DEPTH - 1)
        if created and _DEPTH == 0:
            _ACTIVE = None
        _RUNTIME_LOCK.release()
