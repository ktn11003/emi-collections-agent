"""Audio codec tests.

These matter more than they look: `audioop` was removed from the stdlib in
Python 3.13, so the G.711 codec here is hand-written. If it drifts, STT accuracy
degrades silently rather than failing loudly.
"""

from __future__ import annotations

import math
import struct

import numpy as np
import pytest

from app.audio import (
    mulaw_to_pcm16,
    pcm16_to_mulaw,
    read_wav,
    resample_pcm16,
    rms_dbfs,
    telephony_to_stt,
    to_mono,
    tts_to_telephony,
    wav_bytes,
)


def tone(freq: float, seconds: float, rate: int, amplitude: int = 12000) -> bytes:
    n = int(rate * seconds)
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(n)
    )


class TestMulaw:
    def test_round_trip_preserves_shape(self):
        """mu-law is lossy but must stay close: G.711 is ~13-bit dynamic range."""
        pcm = tone(440, 0.1, 8000)
        back = mulaw_to_pcm16(pcm16_to_mulaw(pcm))

        assert len(back) == len(pcm)
        a = np.frombuffer(pcm, dtype="<i2").astype(float)
        b = np.frombuffer(back, dtype="<i2").astype(float)
        # Correlated to within quantisation noise.
        assert np.corrcoef(a, b)[0, 1] > 0.999
        # Companding error stays proportional, never wild.
        assert np.abs(a - b).max() < 600

    def test_compression_ratio_is_two_to_one(self):
        pcm = tone(300, 0.05, 8000)
        assert len(pcm16_to_mulaw(pcm)) == len(pcm) // 2

    def test_silence_and_extremes(self):
        for sample in (0, 32767, -32768, 1, -1):
            pcm = struct.pack("<h", sample) * 8
            back = np.frombuffer(mulaw_to_pcm16(pcm16_to_mulaw(pcm)), dtype="<i2")
            assert len(back) == 8
            # Sign must survive; magnitude within companding tolerance.
            if sample > 100:
                assert back[0] > 0
            elif sample < -100:
                assert back[0] < 0

    def test_empty_input(self):
        assert pcm16_to_mulaw(b"") == b""
        assert mulaw_to_pcm16(b"") == b""


class TestResample:
    @pytest.mark.parametrize("src,dst", [(8000, 16000), (16000, 8000), (24000, 8000), (22050, 16000)])
    def test_length_scales_with_rate(self, src, dst):
        pcm = tone(200, 0.2, src)
        out = resample_pcm16(pcm, src, dst)
        expected = int(round((len(pcm) // 2) * dst / src))
        assert abs(len(out) // 2 - expected) <= 1

    def test_identity_when_rates_match(self):
        pcm = tone(200, 0.05, 16000)
        assert resample_pcm16(pcm, 16000, 16000) == pcm

    def test_frequency_survives_upsampling(self):
        """A 440 Hz tone must still be 440 Hz after 8k -> 16k."""
        pcm = tone(440, 0.5, 8000)
        out = resample_pcm16(pcm, 8000, 16000)
        x = np.frombuffer(out, dtype="<i2").astype(float)
        spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        peak_hz = np.fft.rfftfreq(len(x), 1 / 16000)[spectrum.argmax()]
        assert abs(peak_hz - 440) < 15


class TestTelephonyPath:
    def test_inbound_frame_becomes_16k_pcm(self):
        """One 20 ms mu-law RTP payload (160 bytes) -> 640 bytes of 16 kHz PCM."""
        frame = pcm16_to_mulaw(tone(300, 0.02, 8000))
        assert len(frame) == 160

        out = telephony_to_stt(frame, condition=False)
        assert len(out) == 640          # 320 samples * 2 bytes

    def test_outbound_pcm_becomes_mulaw(self):
        pcm24k = tone(300, 0.1, 24000)
        mu = tts_to_telephony(pcm24k, src_rate=24000)
        assert len(mu) == pytest.approx(800, abs=2)   # 0.1 s at 8 kHz, 1 byte/sample

    def test_conditioning_does_not_destroy_signal(self):
        frame = pcm16_to_mulaw(tone(500, 0.02, 8000))
        conditioned = telephony_to_stt(frame, condition=True)
        assert rms_dbfs(conditioned) > -40


class TestWav:
    def test_header_round_trip(self):
        pcm = tone(440, 0.1, 16000)
        pcm_out, rate, channels = read_wav(wav_bytes(pcm, 16000, 1))
        assert (rate, channels) == (16000, 1)
        assert pcm_out == pcm

    def test_to_mono_averages_channels(self):
        interleaved = struct.pack("<4h", 1000, 2000, 3000, 5000)
        mono = np.frombuffer(to_mono(interleaved, 2), dtype="<i2")
        assert list(mono) == [1500, 4000]
