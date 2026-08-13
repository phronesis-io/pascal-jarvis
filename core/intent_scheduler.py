"""Intent scheduling and execution-envelope boundary."""

from functools import wraps

from core import intentions as _legacy

__all__ = [
    "clear_breaches",
    "clear_inflight",
    "defer_inflight_infrastructure",
    "generate_calendar_intents",
    "get_due_intents",
    "mark_breaches_shown",
    "peek_breaches",
    "read_inflight",
    "read_inflight_breaches",
    "reconcile_inflight",
    "snap_to_golden",
    "store_breach_entry",
    "validate_envelope",
    "write_inflight",
]


def _delegate(name):
    @wraps(getattr(_legacy, name))
    def call(*args, **kwargs):
        return getattr(_legacy, name)(*args, **kwargs)
    return call


for _name in __all__:
    globals()[_name] = _delegate(_name)
