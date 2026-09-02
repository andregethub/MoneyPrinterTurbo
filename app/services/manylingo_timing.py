"""Exact ManyLingo scene timing from TTS boundary events.

Edge TTS exposes word-boundary cues and ElevenLabs exposes character-level alignment.
Both are normalized into the same small task-local sidecar, then applied to ManyLingo
items immediately before rendering. Providers without exact boundaries keep the existing
estimated timing so video generation remains backwards-compatible.
"""

from __future__ import annotations

import glob
import json
import os
import re
from typing import Iterable

from loguru import logger

from app.models.schema import ManyLingoItem, VideoParams

_TIMING_SUFFIX = ".manylingo-word-boundaries.json"


def _tokens(text: str) -> list[str]:
    """Return comparison tokens that are stable across punctuation/case differences."""
    normalized = (
        str(text or "")
        .replace("’", "'")
        .replace("‘", "'")
        .replace("–", "-")
        .replace("—", "-")
        .casefold()
    )
    return re.findall(r"[a-z0-9]+(?:['-][a-z0-9]+)*", normalized)


def _cue_seconds(value) -> float:
    if hasattr(value, "total_seconds"):
        return float(value.total_seconds())
    return float(value or 0.0)


def extract_word_boundaries(sub_maker) -> list[dict]:
    """Extract serializable word boundaries from modern or legacy SubMaker data."""
    cues = list(getattr(sub_maker, "cues", []) or [])
    boundaries: list[dict] = []
    for cue in cues:
        text = str(getattr(cue, "content", "") or "").strip()
        start = _cue_seconds(getattr(cue, "start", 0.0))
        end = _cue_seconds(getattr(cue, "end", start))
        if not text or end <= start:
            continue
        boundaries.append({"text": text, "start": start, "end": end})
    if boundaries:
        return boundaries

    # ElevenLabs timestamped TTS is normalized by manylingo_elevenlabs into the project's
    # historical ``subs`` + ``offset`` structure. Offsets use Edge's 100 ns units.
    subs = list(getattr(sub_maker, "subs", []) or [])
    offsets = list(getattr(sub_maker, "offset", []) or [])
    for text, offset in zip(subs, offsets):
        if not isinstance(offset, (list, tuple)) or len(offset) < 2:
            continue
        try:
            start = float(offset[0]) / 10_000_000
            end = float(offset[1]) / 10_000_000
        except (TypeError, ValueError):
            continue
        text = str(text or "").strip()
        if not text or end <= start:
            continue
        boundaries.append({"text": text, "start": start, "end": end})
    return boundaries


def save_word_boundaries(voice_file: str, text: str, sub_maker) -> str:
    """Persist exact TTS boundaries beside the generated narration audio."""
    boundaries = extract_word_boundaries(sub_maker)
    if not boundaries:
        return ""

    sidecar = f"{voice_file}{_TIMING_SUFFIX}"
    payload = {
        "version": 2,
        "script": str(text or "").strip(),
        "boundaries": boundaries,
    }
    try:
        with open(sidecar, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning(f"ManyLingo could not persist TTS timings: {exc}")
        return ""

    logger.info(
        "ManyLingo exact TTS boundaries saved: "
        f"count={len(boundaries)}, file={sidecar}"
    )
    return sidecar


def apply_word_boundaries_to_items(
    items: Iterable[ManyLingoItem],
    boundaries: Iterable[dict],
    *,
    audio_duration: float | None = None,
) -> bool:
    """Assign exact item start/end times by matching narration tokens to TTS boundaries.

    The narration is deterministic: for each item it speaks ``word`` and then ``sentence``.
    We flatten the expected tokens and observed timing tokens, require an exact ordered
    match, and use the first timestamp of the next item as the scene cut. This leaves the
    current visual on screen during natural punctuation pauses instead of cutting early.
    """
    items = list(items)
    boundaries = [dict(value) for value in boundaries or []]
    if not items or not boundaries:
        return False

    expected_groups: list[list[str]] = []
    expected_flat: list[str] = []
    for item in items:
        group = _tokens(f"{item.word or ''} {item.sentence or ''}")
        if not group:
            return False
        expected_groups.append(group)
        expected_flat.extend(group)

    observed_flat: list[str] = []
    observed_to_boundary: list[int] = []
    usable_boundaries: list[dict] = []
    for boundary in boundaries:
        start = float(boundary.get("start", 0.0) or 0.0)
        end = float(boundary.get("end", start) or start)
        cue_tokens = _tokens(boundary.get("text", ""))
        if not cue_tokens or end <= start:
            continue
        boundary_index = len(usable_boundaries)
        usable_boundaries.append(
            {"text": str(boundary.get("text", "")), "start": start, "end": end}
        )
        for token in cue_tokens:
            observed_flat.append(token)
            observed_to_boundary.append(boundary_index)

    if len(observed_flat) < len(expected_flat):
        logger.warning(
            "ManyLingo exact timing skipped: TTS returned fewer tokens than expected "
            f"({len(observed_flat)} < {len(expected_flat)})"
        )
        return False

    observed_prefix = observed_flat[: len(expected_flat)]
    if observed_prefix != expected_flat:
        mismatch_at = next(
            (
                index
                for index, (expected, observed) in enumerate(
                    zip(expected_flat, observed_prefix)
                )
                if expected != observed
            ),
            0,
        )
        logger.warning(
            "ManyLingo exact timing skipped: narration and TTS timing tokens differ at "
            f"token {mismatch_at + 1}: expected={expected_flat[mismatch_at]!r}, "
            f"observed={observed_prefix[mismatch_at]!r}"
        )
        return False

    starts: list[float] = []
    spoken_ends: list[float] = []
    token_cursor = 0
    for group in expected_groups:
        first_boundary_index = observed_to_boundary[token_cursor]
        last_boundary_index = observed_to_boundary[token_cursor + len(group) - 1]
        starts.append(float(usable_boundaries[first_boundary_index]["start"]))
        spoken_ends.append(float(usable_boundaries[last_boundary_index]["end"]))
        token_cursor += len(group)

    resolved_duration = float(audio_duration or 0.0)
    if resolved_duration <= 0:
        resolved_duration = spoken_ends[-1]
    resolved_duration = max(resolved_duration, spoken_ends[-1])

    starts[0] = 0.0
    for index, item in enumerate(items):
        start = max(0.0, starts[index])
        if index + 1 < len(items):
            end = max(start + 0.001, starts[index + 1])
        else:
            end = max(start + 0.001, resolved_duration)
        item.start = start
        item.end = end

    logger.success(
        "ManyLingo scenes mapped to real TTS timestamps: "
        + ", ".join(
            f"{item.word}={item.start:.3f}-{item.end:.3f}s" for item in items
        )
    )
    return True


def _find_timing_sidecar(output_file: str, script: str) -> dict | None:
    task_dir = os.path.dirname(os.path.realpath(output_file))
    preferred = os.path.join(task_dir, f"audio.mp3{_TIMING_SUFFIX}")
    candidates = [preferred]
    candidates.extend(
        path
        for path in sorted(
            glob.glob(os.path.join(task_dir, f"*{_TIMING_SUFFIX}")),
            key=lambda value: os.path.getmtime(value),
            reverse=True,
        )
        if path != preferred
    )

    normalized_script = str(script or "").strip()
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            logger.warning(f"ManyLingo could not read timing sidecar {path}: {exc}")
            continue
        if str(payload.get("script", "")).strip() != normalized_script:
            continue
        if payload.get("boundaries"):
            return payload
    return None


def prepare_params_with_exact_timing(output_file: str, params: VideoParams) -> bool:
    """Load the task-local timing sidecar and mutate ManyLingo items in-place."""
    if getattr(params, "content_mode", "standard") != "manylingo":
        return False
    items = list(getattr(params, "manylingo_items", []) or [])
    if not items:
        return False

    payload = _find_timing_sidecar(output_file, getattr(params, "video_script", ""))
    if not payload:
        logger.info(
            "ManyLingo exact TTS timing unavailable; keeping estimated scene timing"
        )
        return False

    boundaries = payload.get("boundaries") or []
    last_end = max(float(item.get("end", 0.0) or 0.0) for item in boundaries)
    return apply_word_boundaries_to_items(
        items,
        boundaries,
        audio_duration=last_end,
    )


def install_timing_patch() -> None:
    """Install transparent TTS-sidecar and pre-render timing hooks once per process."""
    from app.services import video as video_service
    from app.services import voice as voice_service

    if not getattr(voice_service, "_manylingo_timing_patch_installed", False):
        original_tts = voice_service.tts

        def tts_with_manylingo_boundaries(*args, **kwargs):
            result = original_tts(*args, **kwargs)
            text = kwargs.get("text")
            voice_file = kwargs.get("voice_file")
            if text is None and len(args) >= 1:
                text = args[0]
            if voice_file is None and len(args) >= 4:
                voice_file = args[3]
            if result is not None and voice_file:
                save_word_boundaries(str(voice_file), str(text or ""), result)
            return result

        voice_service.tts = tts_with_manylingo_boundaries
        voice_service._manylingo_timing_patch_installed = True

    if getattr(video_service, "_manylingo_timing_patch_installed", False):
        return

    original_generate_video = video_service.generate_video

    def generate_video_with_exact_manylingo_timing(*args, **kwargs):
        output_file = kwargs.get("output_file")
        params = kwargs.get("params")
        if output_file is None and len(args) >= 4:
            output_file = args[3]
        if params is None and len(args) >= 5:
            params = args[4]

        if output_file and params is not None:
            prepare_params_with_exact_timing(str(output_file), params)
        return original_generate_video(*args, **kwargs)

    video_service.generate_video = generate_video_with_exact_manylingo_timing
    video_service._manylingo_timing_patch_installed = True
