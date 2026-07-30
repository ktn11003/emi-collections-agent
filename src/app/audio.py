"""Audio plumbing between telephony (8 kHz mu-law) and the AI models (16 kHz PCM).

Telephony and ASR speak different audio. PSTN carries 8 kHz 8-bit G.711 mu-law in
20 ms frames; Saaras wants 16 kHz 16-bit mono linear PCM; Bulbul emits 22.05/24 kHz.
Getting this wrong silently tanks recognition accuracy, so the conversions live in
one tested place.

Python 3.13 removed the ``audioop`` stdlib module, so the G.711 codec and the
resampler are implemented here on numpy instead.
"""

from __future__ import annotations

import io
import struct
import wave

import numpy as np

BIAS = 0x84
CLIP = 32635


# --- G.711 mu-law ------------------------------------------------------------
def pcm16_to_mulaw(pcm: bytes) -> bytes:
    """16-bit linear PCM -> 8-bit mu-law (ITU-T G.711)."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
    sign = np.where(x < 0, 0x80, 0).astype(np.uint8)
    mag = np.minimum(np.abs(x), CLIP) + BIAS
    # exponent = position of the highest set bit above bit 7
    exponent = np.zeros_like(mag)
    for e in range(7, 0, -1):
        exponent = np.where((exponent == 0) & (mag >= (1 << (e + 7))), e, exponent)
    mantissa = (mag >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4).astype(np.uint8) | mantissa.astype(np.uint8))).astype(np.uint8).tobytes()


def mulaw_to_pcm16(mu: bytes) -> bytes:
    """8-bit mu-law -> 16-bit linear PCM (ITU-T G.711)."""
    u = (~np.frombuffer(mu, dtype=np.uint8)).astype(np.int32)
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    mag = ((mantissa << 3) + BIAS) << exponent
    mag -= BIAS
    out = np.where(sign != 0, -mag, mag)
    return np.clip(out, -32768, 32767).astype("<i2").tobytes()


# --- resampling --------------------------------------------------------------
def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolation resampler for mono 16-bit PCM.

    Good enough for speech at telephony rates and dependency-free. Swap in
    ``scipy.signal.resample_poly`` if you need a proper anti-aliasing filter.
    """
    if src_rate == dst_rate or not pcm:
        return pcm
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if x.size == 0:
        return b""
    n_out = max(1, int(round(x.size * dst_rate / src_rate)))
    idx = np.linspace(0, x.size - 1, n_out, dtype=np.float64)
    y = np.interp(idx, np.arange(x.size), x)
    return np.clip(np.round(y), -32768, 32767).astype("<i2").tobytes()


def to_mono(pcm: bytes, channels: int) -> bytes:
    if channels <= 1:
        return pcm
    x = np.frombuffer(pcm, dtype="<i2")
    usable = (x.size // channels) * channels
    return x[:usable].reshape(-1, channels).mean(axis=1).astype("<i2").tobytes()


# --- light front-end conditioning -------------------------------------------
def high_pass(pcm: bytes, sample_rate: int, cutoff_hz: float = 80.0) -> bytes:
    """One-pole high-pass: strips DC/rumble that hurts ASR on noisy lines."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if x.size == 0:
        return pcm
    rc = 1.0 / (2 * np.pi * cutoff_hz)
    alpha = rc / (rc + 1.0 / sample_rate)
    # y[n] = a*(y[n-1] + x[n] - x[n-1])  -- implemented as cumulative filter
    dx = np.diff(x, prepend=x[0])
    y = np.zeros_like(x)
    acc = 0.0
    for i in range(x.size):  # sample loop: frames are 20-100 ms, cost is trivial
        acc = alpha * (acc + dx[i])
        y[i] = acc
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()


def normalise_peak(pcm: bytes, target_dbfs: float = -3.0) -> bytes:
    """Cheap AGC: scale so the loudest sample sits at ``target_dbfs``."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    peak = np.abs(x).max() if x.size else 0.0
    if peak < 1.0:
        return pcm
    gain = (10 ** (target_dbfs / 20.0) * 32767.0) / peak
    return np.clip(x * gain, -32768, 32767).astype("<i2").tobytes()


def rms_dbfs(pcm: bytes) -> float:
    """Frame energy in dBFS, used as the barge-in energy gate."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if x.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(x * x)))
    return -120.0 if rms < 1e-6 else 20.0 * float(np.log10(rms / 32768.0))


# --- WAV container -----------------------------------------------------------
def wav_bytes(pcm: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """Wrap raw PCM in a RIFF/WAVE header (Sarvam's STT accepts wav directly)."""
    byte_rate = sample_rate * channels * 2
    return (
        b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, channels * 2, 16)
        + b"data" + struct.pack("<I", len(pcm)) + pcm
    )


def read_wav(data: bytes) -> tuple[bytes, int, int]:
    """Return ``(pcm, sample_rate, channels)`` from WAV bytes."""
    with wave.open(io.BytesIO(data), "rb") as w:
        return w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()


def wav_to_stt_pcm(data: bytes, target_rate: int = 16000) -> bytes:
    """Any WAV -> mono 16 kHz PCM, ready for Saaras."""
    pcm, sr, ch = read_wav(data)
    return resample_pcm16(to_mono(pcm, ch), sr, target_rate)


# --- the two telephony-facing conversions -----------------------------------
def telephony_to_stt(mulaw_frame: bytes, condition: bool = True) -> bytes:
    """One inbound RTP payload (20 ms of 8 kHz mu-law) -> 16 kHz PCM for Saaras."""
    pcm8 = mulaw_to_pcm16(mulaw_frame)
    if condition:
        pcm8 = normalise_peak(high_pass(pcm8, 8000))
    return resample_pcm16(pcm8, 8000, 16000)


def tts_to_telephony(pcm: bytes, src_rate: int = 24000) -> bytes:
    """Bulbul PCM -> 8 kHz mu-law for the RTP stream back to the caller."""
    return pcm16_to_mulaw(resample_pcm16(pcm, src_rate, 8000))
