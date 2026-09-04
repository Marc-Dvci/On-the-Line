"""Console text IO for the BidiAgent, with no audio dependency.

Strands ships ``BidiTextIO``, but ``strands.experimental.bidi.io.__init__`` imports
``BidiAudioIO`` -- and therefore ``pyaudio`` -- at package level, so a text-only import
still needs PortAudio present. That is fine on a laptop and a nuisance in a headless
AgentCore container, so the spike carries its own 40-line implementation of the same
protocol. It doubles as the smallest possible worked example of ``BidiInput``/
``BidiOutput`` for the write-up.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

from strands.experimental.bidi.types.events import BidiOutputEvent, BidiTextInputEvent

if TYPE_CHECKING:
    from strands.experimental.bidi import BidiAgent


class ConsoleTextInput:
    """Reads typed lines and sends them to the agent as user turns."""

    def __init__(self, prompt: str = "you > ") -> None:
        self._prompt = prompt

    async def start(self, agent: BidiAgent) -> None:
        return

    async def stop(self) -> None:
        return

    async def __call__(self) -> BidiTextInputEvent:
        # stdin blocks, so keep it off the event loop or the agent cannot speak while
        # you are still typing.
        line = await asyncio.to_thread(input, self._prompt)
        return BidiTextInputEvent(text=line, role="user")


class ConsoleTextOutput:
    """Prints the agent's transcript and announces tool calls as they happen."""

    def __init__(self, show_tools: bool = True) -> None:
        self._show_tools = show_tools
        self._role_open: str | None = None
        self._last_tool_announced: str | None = None

    async def start(self, agent: BidiAgent) -> None:
        return

    async def stop(self) -> None:
        if self._role_open:
            print()

    async def __call__(self, event: BidiOutputEvent) -> None:
        kind = event.get("type") if isinstance(event, dict) else None

        if kind == "bidi_transcript_stream" and event.get("role") == "assistant":
            if self._role_open != "assistant":
                self._newline()
                sys.stdout.write("agent > ")
                self._role_open = "assistant"
            sys.stdout.write(event.get("text", ""))
            sys.stdout.flush()

        elif kind == "tool_use_stream" and self._show_tools:
            name = (event.get("current_tool_use") or {}).get("name")
            if name and name != self._last_tool_announced:
                self._newline()
                print(f"  [tool] {name}")
                self._last_tool_announced = name

        elif kind == "tool_result" and self._show_tools:
            self._newline()
            result = event.get("tool_result") or {}
            body = "".join(block.get("text", "") for block in result.get("content", []))
            print(f"  [tool result] {body[:160]}")
            self._last_tool_announced = None

        elif kind == "bidi_interruption":
            self._newline()
            print(f"  [interrupted: {event.get('reason')}]")

    def _newline(self) -> None:
        if self._role_open:
            print()
            self._role_open = None
