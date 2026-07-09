"""Shared UI helpers for dashboard pages."""

from __future__ import annotations

import inspect
from contextlib import nullcontext

from nicegui import ui


class _ClientBoundTimer(ui.timer):
    """ui.timer that dies quietly with its client instead of crash-looping.

    The daemon's liveness probe hits '/' every few minutes; each probe is a
    throwaway NiceGUI client that never opens a websocket. When the client is
    pruned, nicegui 3.12's Timer._run_in_loop evaluates `self._get_context()`
    (→ the `parent_slot` Element property) BEFORE its first _should_stop()
    check, and that property raises `RuntimeError: The parent slot of the
    element has been deleted` — one full traceback per pruned client, which
    was 100% of dashboard stderr (316 in one day). Catching inside the
    callback cannot help: the raise happens in the timer machinery before the
    callback ever runs. Intercept at the actual raise point instead.
    """

    def _get_context(self):
        try:
            return super()._get_context()
        except RuntimeError:
            self.cancel()
            return nullcontext()


def guarded_refresh_timer(interval: float, refresh) -> ui.timer:
    """Periodic refresh timer for dashboard pages, safe on pruned clients.

    `refresh` may be sync (refreshable.refresh) or an async callable. The
    in-callback guard below covers slot-deletion raised *during* a refresh
    (element updates racing a disconnect); _ClientBoundTimer covers the
    machinery-level raise before the callback runs.
    """
    timer: ui.timer | None = None

    async def _tick():
        try:
            result = refresh()
            if inspect.isawaitable(result):
                await result
        except RuntimeError as e:
            if "slot" in str(e) and "deleted" in str(e):
                if timer is not None:
                    timer.cancel()
            else:
                raise

    timer = _ClientBoundTimer(interval, _tick)
    return timer
