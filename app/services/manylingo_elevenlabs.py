"""ElevenLabs TTS integration for ManyLingo with exact timing metadata.

The regular MoneyPrinterTurbo ElevenLabs path only returns audio. ManyLingo needs exact
scene boundaries, so this module swaps that implementation for ElevenLabs' official
``/with-timestamps`` endpoint. The returned character alignment is collapsed into word
boundaries and stored in the same SubMaker-compatible structure used by the rest of the
pipeline.
"""

from __future__ import annotations

import base64
import math
import re
from typing import Union

import requests
from edge_tts import SubMaker
from loguru import logger
from moviepy.audio.io.AudioFileClip import AudioFileClip

from app.config import config

_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*")
_MANYLINGO_ELEVENLABS_SPEED = 0.90


def _alignment_to_legacy_submaker(alignment: dict | None) -> SubMaker | None:
    alignment = dict(alignment or {})
    characters = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    if not characters or len(characters) != len(starts) or len(characters) != len(ends):
        return None

    text = "".join(str(value) for value in characters)
    sub_maker = SubMaker()
    if not hasattr(sub_maker, "subs"):
        sub_maker.subs = []
    if not hasattr(sub_maker, "offset"):
        sub_maker.offset = []
    sub_maker.subs = []
    sub_maker.offset = []

    for match in _WORD_RE.finditer(text):
        start_index = match.start()
        end_index = match.end() - 1
        try:
            start = float(starts[start_index])
            end = float(ends[end_index])
        except (TypeError, ValueError, IndexError):
            continue
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            continue
        sub_maker.subs.append(match.group(0))
        sub_maker.offset.append((int(start * 10_000_000), int(end * 10_000_000)))

    return sub_maker if sub_maker.subs else None


def _audio_duration(path: str) -> float:
    clip = AudioFileClip(path)
    try:
        return float(clip.duration or 0.0)
    finally:
        clip.close()


def install_elevenlabs_timing_patch() -> None:
    """Use ElevenLabs' timestamp endpoint while preserving normal TTS fallback behavior."""
    from app.services import voice as voice_service

    if getattr(voice_service, "_manylingo_elevenlabs_patch_installed", False):
        return

    original_elevenlabs_tts = voice_service.elevenlabs_tts

    def elevenlabs_tts_with_timestamps(
        text: str,
        voice_id: str,
        voice_file: str,
        voice_rate: float = 1.0,
        voice_volume: float = 1.0,
        model_id: str = "",
    ) -> Union[SubMaker, None]:
        text = str(text or "").strip()
        voice_id = str(voice_id or "").strip()
        if not text or not voice_id:
            return original_elevenlabs_tts(
                text,
                voice_id,
                voice_file,
                voice_rate=voice_rate,
                voice_volume=voice_volume,
                model_id=model_id,
            )

        api_key = voice_service.get_elevenlabs_api_key()
        if not api_key:
            return original_elevenlabs_tts(
                text,
                voice_id,
                voice_file,
                voice_rate=voice_rate,
                voice_volume=voice_volume,
                model_id=model_id,
            )

        resolved_model = str(
            model_id
            or config.elevenlabs.get("model_id", "eleven_multilingual_v2")
            or "eleven_multilingual_v2"
        ).strip()
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"
        headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
        payload = {
            "text": text,
            "model_id": resolved_model,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True,
                # ManyLingo is educational content. A slightly slower pace improves
                # intelligibility while the returned alignment remains exact for that audio.
                "speed": _MANYLINGO_ELEVENLABS_SPEED,
            },
        }

        for attempt in range(3):
            try:
                logger.info(
                    "start ElevenLabs timestamped TTS, "
                    f"voice_id: {voice_id}, speed: {_MANYLINGO_ELEVENLABS_SPEED:.2f}, "
                    f"try: {attempt + 1}"
                )
                voice_service.ensure_file_path_exists(voice_file)
                response = requests.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=90,
                )
                if response.status_code != 200:
                    logger.warning(
                        "ElevenLabs timestamped TTS failed with status "
                        f"{response.status_code}: {response.text[:200]}"
                    )
                    if response.status_code in {401, 402, 403, 422}:
                        break
                    continue

                body = response.json()
                audio_base64 = str(body.get("audio_base64") or "").strip()
                if not audio_base64:
                    logger.warning("ElevenLabs timestamped TTS returned no audio")
                    continue
                audio_bytes = base64.b64decode(audio_base64, validate=True)
                if not audio_bytes:
                    logger.warning("ElevenLabs timestamped TTS returned empty audio")
                    continue

                with open(voice_file, "wb") as output:
                    output.write(audio_bytes)

                sub_maker = _alignment_to_legacy_submaker(
                    body.get("alignment") or body.get("normalized_alignment")
                )
                if sub_maker is None:
                    logger.warning(
                        "ElevenLabs returned audio but no usable alignment; "
                        "falling back to proportional subtitle timing"
                    )
                    duration = _audio_duration(voice_file)
                    return voice_service.populate_legacy_submaker_with_full_text(
                        voice_service.ensure_legacy_submaker_fields(SubMaker()),
                        text,
                        duration,
                    )

                duration = _audio_duration(voice_file)
                if duration > 0 and sub_maker.offset:
                    _, last_end = sub_maker.offset[-1]
                    if last_end > int(duration * 10_000_000) + 1_000_000:
                        logger.warning(
                            "ElevenLabs alignment exceeds decoded audio duration; "
                            "keeping alignment because it came from the same API response"
                        )

                logger.success(
                    "ElevenLabs timestamped TTS succeeded: "
                    f"words={len(sub_maker.subs)}, speed={_MANYLINGO_ELEVENLABS_SPEED:.2f}, "
                    f"file={voice_file}"
                )
                return sub_maker
            except Exception as exc:
                logger.warning(f"ElevenLabs timestamped TTS failed: {exc}")

        logger.warning(
            "Falling back to the standard ElevenLabs TTS endpoint without exact timestamps"
        )
        return original_elevenlabs_tts(
            text,
            voice_id,
            voice_file,
            voice_rate=voice_rate,
            voice_volume=voice_volume,
            model_id=model_id,
        )

    voice_service.elevenlabs_tts = elevenlabs_tts_with_timestamps
    voice_service._manylingo_elevenlabs_patch_installed = True
