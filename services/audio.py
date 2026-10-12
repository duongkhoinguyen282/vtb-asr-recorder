"""Audio validation and normalization for 16kHz mono PCM16 WAV recordings."""

from __future__ import annotations

try:
    import audioop
except ImportError:
    audioop = None
import hashlib
import io
import logging
from typing import Tuple
import wave

logger = logging.getLogger(__name__)


class AudioValidationError(Exception):
    """Raised when recorded audio does not meet validation criteria."""
    pass


def validate_and_process_audio(
    audio_bytes: bytes,
    min_duration: float = 0.5,
    max_duration: float = 120.0,
    target_sample_rate: int = 16000,
) -> Tuple[bytes, float, int, int, str]:
    """Validate and normalize audio to mono PCM16 at target_sample_rate (16kHz).

    Does NOT apply VAD, silence trimming, denoising, volume leveling, or speech normalization.
    Preserves exact acoustic disfluencies, hesitations, repetitions, and pauses.

    Returns:
        (processed_wav_bytes, duration_seconds, sample_rate, channels, sha256_hex)
    """
    if not audio_bytes or len(audio_bytes) < 44:
        raise AudioValidationError("Dữ liệu âm thanh trống hoặc tệp WAV không đầy đủ.")

    try:
        in_io = io.BytesIO(audio_bytes)
        with wave.open(in_io, "rb") as wf:
            nchannels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            nframes = wf.getnframes()
            raw_frames = wf.readframes(nframes)
    except Exception as exc:
        raise AudioValidationError(f"Không thể đọc định dạng WAV: {exc}") from exc

    if nframes == 0 or len(raw_frames) == 0:
        raise AudioValidationError("Tệp ghi âm không có dữ liệu âm thanh.")

    # Convert sample width to 16-bit (2 bytes) if necessary
    if sampwidth != 2:
        try:
            # audioop is available or fallback
            if hasattr(audioop, "lin2lin"):
                raw_frames = audioop.lin2lin(raw_frames, sampwidth, 2)
                sampwidth = 2
            else:
                raise AudioValidationError(f"Định dạng bit-depth {sampwidth * 8}-bit không được hỗ trợ (cần 16-bit PCM).")
        except Exception as exc:
            raise AudioValidationError(f"Lỗi chuyển đổi bit-depth: {exc}") from exc

    # Downmix to mono if stereo or multi-channel
    if nchannels > 1:
        try:
            if hasattr(audioop, "tomono"):
                raw_frames = audioop.tomono(raw_frames, sampwidth, 0.5, 0.5)
                nchannels = 1
            else:
                # Manual fallback: take left channel
                frame_size = nchannels * sampwidth
                mono_bytes = bytearray()
                for i in range(0, len(raw_frames), frame_size):
                    mono_bytes.extend(raw_frames[i : i + sampwidth])
                raw_frames = bytes(mono_bytes)
                nchannels = 1
        except Exception as exc:
            raise AudioValidationError(f"Lỗi chuyển đổi âm thanh sang mono: {exc}") from exc

    # Resample to target_sample_rate if needed
    if framerate != target_sample_rate:
        try:
            if hasattr(audioop, "ratecv"):
                converted, _ = audioop.ratecv(
                    raw_frames, sampwidth, nchannels, framerate, target_sample_rate, None
                )
                raw_frames = converted
                framerate = target_sample_rate
                nframes = len(raw_frames) // (nchannels * sampwidth)
            else:
                raise AudioValidationError(
                    f"Tần số lấy mẫu là {framerate}Hz, yêu cầu chuẩn {target_sample_rate}Hz."
                )
        except Exception as exc:
            raise AudioValidationError(f"Lỗi chuẩn hóa tần số lấy mẫu: {exc}") from exc

    duration = len(raw_frames) / (framerate * sampwidth * nchannels)
    if duration < min_duration:
        raise AudioValidationError(
            f"Thời lượng bản ghi quá ngắn ({duration:.2f}s). Vui lòng đọc đầy đủ câu (tối thiểu {min_duration}s)."
        )
    if duration > max_duration:
        raise AudioValidationError(
            f"Thời lượng bản ghi vượt quá giới hạn cho phép ({duration:.2f}s > {max_duration}s)."
        )

    # Encode standard PCM16 mono WAV
    out_io = io.BytesIO()
    with wave.open(out_io, "wb") as out_wf:
        out_wf.setnchannels(1)
        out_wf.setsampwidth(2)
        out_wf.setframerate(target_sample_rate)
        out_wf.writeframes(raw_frames)

    final_wav_bytes = out_io.getvalue()
    sha256 = hashlib.sha256(final_wav_bytes).hexdigest()

    return final_wav_bytes, duration, target_sample_rate, 1, sha256
