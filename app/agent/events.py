"""Replayable events for an active turn, shared by all connected clients."""

import asyncio
from collections.abc import AsyncIterator

from app.agent.types import AgentEvent


class TurnEvents:
    def __init__(self) -> None:
        self._events: list[AgentEvent] = []
        self._closed = False
        self._changed = asyncio.Event()
        self._reader = self.subscribe()

    def publish(self, event: AgentEvent) -> None:
        self._events.append(event)
        self._wake()

    def close(self) -> None:
        self._closed = True
        self._wake()

    def _wake(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    async def subscribe(self, after: int = 0) -> AsyncIterator[tuple[int, AgentEvent]]:
        index = after
        while True:
            while index < len(self._events):
                index += 1
                yield index, self._events[index - 1]
            if self._closed:
                return
            await self._changed.wait()

    async def get(self) -> AgentEvent | None:
        item = await anext(self._reader, None)
        return item[1] if item else None
