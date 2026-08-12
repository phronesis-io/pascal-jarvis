"""Stable memorial state values shared across domain modules.

Keeping these constants outside the orchestration facade prevents readers
from importing the 3k-line memorial runtime merely to compare a state value.
"""

ATTENTION_DECISION = "decision"
ATTENTION_NOTICE = "notice"
ATTENTION_ALERT = "alert"
STATUS_LAPSED = "lapsed"
