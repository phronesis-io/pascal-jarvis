"""Intent lifecycle boundary.

New production code should import CRUD and state-transition operations from
this module. ``core.intentions`` remains the compatibility implementation and
CLI while its callers are migrated incrementally.
"""

from functools import wraps

from core import intentions as _legacy

__all__ = [
    "cancel_intent",
    "cleanup_expired",
    "create_intent",
    "delete_intent",
    "get_intent",
    "intent_stats",
    "lifecycle_sweep",
    "list_intents",
    "mark_executed",
    "mark_failed",
    "mark_triggered",
    "snapshot_active_intents",
    "update_intent",
]


def _delegate(name):
    @wraps(getattr(_legacy, name))
    def call(*args, **kwargs):
        return getattr(_legacy, name)(*args, **kwargs)
    return call


for _name in __all__:
    globals()[_name] = _delegate(_name)
