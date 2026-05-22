"""In-process event bus for real-time UI updates.

Simple pub/sub: components subscribe to event types,
the bus broadcasts when things change. No external dependencies.
"""

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine


@dataclass
class Event:
    """An event on the bus."""
    type: str
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


# Type for async event handlers
Handler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """Lightweight async event bus."""

    def __init__(self):
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._wildcard_handlers: list[Handler] = []

    def on(self, event_type: str, handler: Handler) -> Callable:
        """Subscribe to an event type. Returns unsubscribe function."""
        self._handlers[event_type].append(handler)

        def unsub():
            self._handlers[event_type].remove(handler)
        return unsub

    def on_all(self, handler: Handler) -> Callable:
        """Subscribe to all events."""
        self._wildcard_handlers.append(handler)

        def unsub():
            self._wildcard_handlers.remove(handler)
        return unsub

    async def emit(self, event_type: str, data: dict | None = None):
        """Emit an event to all subscribers."""
        event = Event(type=event_type, data=data or {})
        handlers = self._handlers.get(event_type, []) + self._wildcard_handlers
        for handler in handlers:
            try:
                await handler(event)
            except Exception as e:
                print(f"[EventBus] Handler error for {event_type}: {e}")

    def emit_sync(self, event_type: str, data: dict | None = None):
        """Emit from sync code — schedules on the running loop."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.emit(event_type, data))
        except RuntimeError:
            # No running loop — skip (happens during startup/tests)
            pass


# Singleton
bus = EventBus()
