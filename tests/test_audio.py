"""Tests for audio validation, 16kHz PCM16 normalization, and duration constraints."""

import io
import math
import struct
import wave
import pytest

from services.audio import AudioValidationError, validate_and_process_audio


def make_dummy_wav(
    duration: float = 1.0,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """Generate dummy sine-wave WAV bytes for testing."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        total_frames = int(duration * sample_rate)

        frames = bytearray()
        for i in range(total_frames):
            val = int(10000 * math.sin(2 * math.pi * 440 * i / sample_rate))
            sample_bytes = struct.pack("<h", val)
            for _ in range(channels):
                frames.extend(sample_bytes)

        wf.writeframes(bytes(frames))
    return buf.getvalue()


def test_audio_valid_pcm16_mono_16k():
    """Verify standard valid audio passes without distortion."""
    wav_bytes = make_dummy_wav(duration=2.0, sample_rate=16000, channels=1, sample_width=2)
    processed, duration, rate, channels, sha256 = validate_and_process_audio(wav_bytes)

    assert channels == 1
    assert rate == 16000
    assert 1.95 <= duration <= 2.05
    assert len(sha256) == 64


def test_audio_stereo_downmix_to_mono():
    """Verify stereo audio is correctly downmixed to mono 16kHz."""
    stereo_wav = make_dummy_wav(duration=1.5, sample_rate=16000, channels=2, sample_width=2)
    processed, duration, rate, channels, _ = validate_and_process_audio(stereo_wav)

    assert channels == 1
    assert rate == 16000
    assert 1.45 <= duration <= 1.55


def test_audio_rejects_too_short():
    """Verify audio under min_duration is rejected."""
    short_wav = make_dummy_wav(duration=0.2, sample_rate=16000, channels=1, sample_width=2)
    with pytest.raises(AudioValidationError, match="quá ngắn"):
        validate_and_process_audio(short_wav, min_duration=0.5)


def test_audio_rejects_empty_or_corrupt():
    """Verify non-WAV / empty bytes are rejected cleanly."""
    with pytest.raises(AudioValidationError):
        validate_and_process_audio(b"")

    with pytest.raises(AudioValidationError):
        validate_and_process_audio(b"NOT A WAV FILE CONTENT HEADER AT ALL")

