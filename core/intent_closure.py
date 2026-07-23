"""Intent closure boundary.

All user-facing closure actions, including Memorial buttons and Lark card
callbacks, converge on ``record_closure`` through this module.
"""

from functools import wraps

from core import intentions as _legacy

__all__ = [
    "awaiting_closures",
    "closure_stats",
    "generate_closure_reask_intents",
    "get_closure_due",
    "note_closure_touch",
    "record_closure",
]


def _delegate(name):
    @wraps(getattr(_legacy, name))
    def call(*args, **kwargs):
        return getattr(_legacy, name)(*args, **kwargs)
    return call


for _name in __all__:
    globals()[_name] = _delegate(_name)
