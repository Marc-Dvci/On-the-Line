"""BidiInput / BidiOutput adapters that put a Strands BidiAgent on a phone line.

A carrier bridge (the production integration uses Twilio bidirectional Media Streams)
hands us 8 kHz G.711 mu-law frames and expects the same back. Nova Sonic wants
base64 linear PCM. These two adapters are the whole translation layer:

    carrier --mu-law 8k--> TelephonyAudioInput  --lpcm--> BidiAgent
    carrier <--mu-law 8k-- TelephonyAudioOutput <--lpcm-- BidiAgent

Both satisfy the runtime-checkable ``BidiInput``/``BidiOutput`` protocols, so they drop
straight into ``BidiAgent.run(inputs=[...], outputs=[...])``.

Input rate note: Nova Sonic accepts an ``input_rate`` of 8000, so the inbound leg can skip
resampling entirely and hand the model native telephony-rate audio. That is the default
here (``model_input_rate=8000``). Set it to 16000 to resample instead -- worth trying if
recognition quality on the 8 kHz path disappoints, since it is one constructor argument.
The outbound leg always resamples, because the model emits 16 kHz and the carrier will
only take 8 kHz.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import TYPE_CHECKING, Literal

import numpy as np
from strands.experimental.bidi.types.events import (
    BidiAudioInputEvent,
    BidiOutputEvent,
)

from .codec import (
    TELEPHONY_FRAME_MS,
    TELEPHONY_RATE,
    Framer,
    Resampler,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
)

if TYPE_CHECKING:
    from strands.experimental.bidi import BidiAgent

logger = logging.getLogger(__name__)

MULAW_FRAME_BYTES = TELEPHONY_RATE * TELEPHONY_FRAME_MS // 1000  # 160 bytes == 20 ms


class TelephonyAudioInput:
    """Feeds carrier mu-law frames into the agent as linear PCM.

    The carrier side calls :meth:`push` from whatever thread or task owns the media
    socket; the agent side awaits ``__call__``. A bounded queue sits between them so a
    stalled agent applies backpressure instead of growing without limit -- on a live call
    the right response to falling behind is to drop old audio, not to buffer a minute of
    it and reply to something the caller said long ago.
    """

    def __init__(
        self,
        model_input_rate: Literal[8000, 16000] = 8000,
        max_queued_frames: int = 50,  # 1 second of 20 ms frames
    ) -> None:
        self.model_input_rate = model_input_rate
        self._resampler = Resampler(TELEPHONY_RATE, model_input_rate)
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=max_queued_frames)
        self._dropped = 0

    async def start(self, agent: BidiAgent) -> None:
        """Reset per-call filter state so a redialled line does not inherit a tail."""
        self._resampler.reset()
        self._dropped = 0

    async def stop(self) -> None:
        if self._dropped:
            logger.warning("dropped %d inbound frames under backpressure", self._dropped)

    def push(self, mulaw_frame: bytes) -> None:
        """Hand one mu-law frame from the carrier to the agent. Never blocks."""
        try:
            self._queue.put_nowait(mulaw_frame)
        except asyncio.QueueFull:
            # Discard the oldest frame: stale audio is worse than a gap.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(mulaw_frame)
            except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover - race only
                pass
            self._dropped += 1

    async def __call__(self) -> BidiAudioInputEvent:
        """Await the next frame and present it to the model as linear PCM."""
        mulaw_frame = await self._queue.get()
        pcm = self._resampler(mulaw_to_pcm16(mulaw_frame))
        return BidiAudioInputEvent(
            audio=base64.b64encode(pcm.tobytes()).decode("utf-8"),
            format="pcm",
            sample_rate=self.model_input_rate,
            channels=1,
        )

    @property
    def dropped_frames(self) -> int:
        """Frames discarded under backpressure. Non-zero means the agent fell behind."""
        return self._dropped


class TelephonyAudioOutput:
    """Turns the agent's PCM into carrier-ready 20 ms mu-law frames.

    Emits through ``sink``, a callable the carrier integration supplies. Transcript
    events are forwarded to ``on_transcript`` when given, which is how the live call view
    and the call record get populated without a second pass over the audio.
    """

    def __init__(
        self,
        sink,
        model_output_rate: Literal[16000, 24000] = 16000,
        on_transcript=None,
        on_interruption=None,
    ) -> None:
        if model_output_rate != 16000:
            raise ValueError(
                "only the 16 kHz model output path is wired; "
                "24 kHz would need a 3:1 decimator"
            )
        self._sink = sink
        self._resampler = Resampler(model_output_rate, TELEPHONY_RATE)
        self._framer = Framer(MULAW_FRAME_BYTES)
        self._on_transcript = on_transcript
        self._on_interruption = on_interruption
        self._frames_sent = 0

    async def start(self, agent: BidiAgent) -> None:
        self._resampler.reset()
        self._frames_sent = 0

    async def stop(self) -> None:
        """Flush the partial frame so the last syllable is not clipped."""
        tail = self._framer.flush()
        if tail is not None:
            await self._emit(tail)

    async def __call__(self, event: BidiOutputEvent) -> None:
        """Route one agent output event to the carrier."""
        kind = event.get("type") if isinstance(event, dict) else None

        if kind == "bidi_audio_stream":
            pcm = np.frombuffer(base64.b64decode(event["audio"]), dtype="<i2")
            mulaw = pcm16_to_mulaw(self._resampler(pcm))
            for frame in self._framer.push(mulaw):
                await self._emit(frame)

        elif kind == "bidi_interruption":
            # The caller spoke over the agent. Drop everything not yet on the wire,
            # otherwise the agent keeps talking for as long as the buffer is deep.
            self._framer = Framer(MULAW_FRAME_BYTES)
            self._resampler.reset()
            if self._on_interruption is not None:
                await _maybe_await(self._on_interruption, event.get("reason"))

        elif kind == "bidi_transcript_stream" and self._on_transcript is not None:
            await _maybe_await(self._on_transcript, event)

    async def _emit(self, frame: bytes) -> None:
        await _maybe_await(self._sink, frame)
        self._frames_sent += 1

    @property
    def frames_sent(self) -> int:
        """Frames handed to the carrier. Useful as a liveness check during the spike."""
        return self._frames_sent


async def _maybe_await(fn, *args):
    """Call ``fn``, awaiting it only if it returns an awaitable."""
    result = fn(*args)
    if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
        return await result
    return result
