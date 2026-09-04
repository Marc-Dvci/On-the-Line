"""G.711 mu-law codec and streaming resamplers for the telephony <-> Nova Sonic bridge.

Telephony carries 8 kHz G.711 mu-law. Nova Sonic speaks ``audio/lpcm`` (signed 16-bit
linear PCM, mono) and accepts sample rates of 8000, 16000, 24000 or 48000 on input,
emitting 16000 by default. So the bridge always has to transcode mu-law <-> linear,
and always has to downsample the model's output to 8 kHz for the carrier.

Everything here is stateful where streaming demands it: the resamplers keep their FIR
tail across frames, because filtering each 20 ms frame independently puts an audible
click at every frame boundary.

Implemented against ITU-T G.711 (the reference Sun ``g711.c`` formulation) rather than
:mod:`audioop`, which is deprecated in Python 3.12 and removed in 3.13. The test suite
checks this module bit-for-bit against ``audioop`` where that module is still available.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "MULAW_BIAS",
    "MULAW_CLIP",
    "Framer",
    "Resampler",
    "TELEPHONY_FRAME_MS",
    "TELEPHONY_RATE",
    "mulaw_to_pcm16",
    "pcm16_to_mulaw",
]

MULAW_BIAS = 0x84  # 132, added before segment search
MULAW_CLIP = 8159  # maximum magnitude in the 14-bit domain
TELEPHONY_RATE = 8000
TELEPHONY_FRAME_MS = 20  # carriers deliver 20 ms frames == 160 samples at 8 kHz

# Upper bound of each mu-law segment in the 14-bit domain.
_SEG_END = np.array([0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF], dtype=np.int32)


def _build_decode_table() -> np.ndarray:
    """Precompute all 256 mu-law -> PCM16 values."""
    u = np.arange(256, dtype=np.int32)
    inv = (~u) & 0xFF
    mantissa = inv & 0x0F
    exponent = (inv & 0x70) >> 4
    magnitude = (((mantissa << 3) + MULAW_BIAS) << exponent) - MULAW_BIAS
    return np.where(inv & 0x80, -magnitude, magnitude).astype(np.int16)


_DECODE_TABLE = _build_decode_table()


def mulaw_to_pcm16(payload: bytes) -> np.ndarray:
    """Decode G.711 mu-law bytes to signed 16-bit linear PCM.

    Args:
        payload: mu-law encoded samples, one byte each.

    Returns:
        int16 array of linear samples, same length as ``payload``.
    """
    return _DECODE_TABLE[np.frombuffer(payload, dtype=np.uint8)]


def pcm16_to_mulaw(samples: np.ndarray) -> bytes:
    """Encode signed 16-bit linear PCM to G.711 mu-law.

    Args:
        samples: int16 array (or anything castable to it) of linear samples.

    Returns:
        mu-law encoded bytes, one byte per input sample.
    """
    pcm = np.asarray(samples, dtype=np.int16).astype(np.int32) >> 2  # to 14-bit
    sign_mask = np.where(pcm < 0, 0x7F, 0xFF).astype(np.int32)
    magnitude = np.minimum(np.abs(pcm), MULAW_CLIP) + (MULAW_BIAS >> 2)

    # Segment index = first bucket whose upper bound is >= the magnitude.
    segment = np.searchsorted(_SEG_END, magnitude, side="left").astype(np.int32)

    # Values past the last segment saturate; the shift below would be meaningless.
    saturated = segment >= 8
    segment_safe = np.where(saturated, 0, segment)
    mantissa = (magnitude >> (segment_safe + 1)) & 0x0F
    encoded = np.where(saturated, 0x7F, (segment_safe << 4) | mantissa)

    return ((encoded ^ sign_mask) & 0xFF).astype(np.uint8).tobytes()


def _lowpass_taps(cutoff_hz: float, sample_rate: int, num_taps: int = 63) -> np.ndarray:
    """Windowed-sinc low-pass FIR, Hamming window, unity DC gain."""
    if num_taps % 2 == 0:  # symmetric taps need an odd length for a linear phase centre
        num_taps += 1
    n = np.arange(num_taps) - (num_taps - 1) / 2
    fc = cutoff_hz / sample_rate  # normalised to the sample rate, not Nyquist
    taps = 2 * fc * np.sinc(2 * fc * n) * np.hamming(num_taps)
    return (taps / taps.sum()).astype(np.float64)


class Resampler:
    """Stateful 2:1 / 1:2 resampler between telephony and model sample rates.

    The FIR tail is carried across calls so that consecutive frames filter as one
    continuous signal. Construct one per direction per call, and never share one
    between two audio streams.
    """

    def __init__(self, source_rate: int, target_rate: int, num_taps: int = 63) -> None:
        if {source_rate, target_rate} not in ({8000, 16000}, {8000}, {16000}):
            raise ValueError(
                f"unsupported conversion {source_rate} -> {target_rate}; "
                "this bridge only needs 8k <-> 16k"
            )
        self.source_rate = source_rate
        self.target_rate = target_rate
        self._ratio = target_rate / source_rate

        if source_rate == target_rate:
            self._taps = None
        else:
            # Anti-alias (down) or anti-image (up) at the lower Nyquist, with headroom.
            high_rate = max(source_rate, target_rate)
            self._taps = _lowpass_taps(cutoff_hz=3400.0, sample_rate=high_rate, num_taps=num_taps)
            if target_rate > source_rate:
                self._taps = self._taps * 2.0  # zero-stuffing halves the energy
        self._tail = np.zeros(0 if self._taps is None else len(self._taps) - 1)

    def reset(self) -> None:
        """Drop the filter tail. Call between calls, not between frames."""
        self._tail = np.zeros_like(self._tail)

    def __call__(self, samples: np.ndarray) -> np.ndarray:
        """Resample one frame, returning int16 at the target rate."""
        x = np.asarray(samples, dtype=np.int16).astype(np.float64)
        if self._taps is None:
            return x.astype(np.int16)

        if self._ratio > 1:  # upsample: zero-stuff, then filter at the high rate
            stuffed = np.zeros(len(x) * 2, dtype=np.float64)
            stuffed[::2] = x
            filtered = self._filter(stuffed)
            out = filtered
        else:  # downsample: filter at the high rate, then decimate
            filtered = self._filter(x)
            out = filtered[::2]

        return np.clip(np.round(out), -32768, 32767).astype(np.int16)

    def _filter(self, x: np.ndarray) -> np.ndarray:
        """Convolve with the FIR, consuming and refreshing the overlap tail."""
        assert self._taps is not None
        padded = np.concatenate([self._tail, x])
        self._tail = padded[len(padded) - len(self._taps) + 1 :]
        return np.convolve(padded, self._taps, mode="valid")


class Framer:
    """Rechunk a byte stream into fixed-size frames, buffering the remainder.

    Carriers want exactly one 20 ms frame per packet; Nova Sonic emits whatever size
    it likes. This absorbs the mismatch.
    """

    def __init__(self, frame_bytes: int) -> None:
        if frame_bytes <= 0:
            raise ValueError("frame_bytes must be positive")
        self.frame_bytes = frame_bytes
        self._buffer = bytearray()

    def push(self, payload: bytes) -> list[bytes]:
        """Add bytes, returning every whole frame now available."""
        self._buffer.extend(payload)
        frames = []
        while len(self._buffer) >= self.frame_bytes:
            frames.append(bytes(self._buffer[: self.frame_bytes]))
            del self._buffer[: self.frame_bytes]
        return frames

    def flush(self, pad_byte: int = 0xFF) -> bytes | None:
        """Emit the partial frame at end of call, zero-padded (0xFF == mu-law silence)."""
        if not self._buffer:
            return None
        frame = bytes(self._buffer) + bytes([pad_byte]) * (self.frame_bytes - len(self._buffer))
        self._buffer.clear()
        return frame

    @property
    def pending(self) -> int:
        """Bytes currently held back as a partial frame."""
        return len(self._buffer)
