"""Exact ManyLingo scene timing from Edge TTS word-boundary events.

MoneyPrinterTurbo already receives WordBoundary events from edge-tts while producing the
narration.  The normal pipeline only keeps them in memory for subtitle generation, so this
module persists a tiny task-local sidecar and applies those real timings to ManyLingo items
immediately before the existing ManyLingo renderer runs.

The implementation is intentionally opt-in: standard videos are unchanged, non-Edge TTS
providers keep the existing estimated ManyLingo timing, and a malformed/mismatched sidecar
is ignored rather than breaking video generation.
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
    """Extract serializable word boundaries from edge-tts 7.x SubMaker cues."""
    cues = list(getattr(sub_maker, "cues", []) or [])
    boundaries: list[dict] = []
    for cue in cues:
        text = str(getattr(cue, "content", "") or "").strip()
        start = _cue_seconds(getattr(cue, "start", 0.0))
        end = _cue_seconds(getattr(cue, "end", start))
        if not text or end <= start:
            continue
        boundaries.append({"text": text, "start": start, "end": end})
    return boundaries


def save_word_boundaries(voice_file: str, text: str, sub_maker) -> str:
    """Persist Edge TTS boundaries beside the generated narration audio."""
    boundaries = extract_word_boundaries(sub_maker)
    if not boundaries:
        return ""

    sidecar = f"{voice_file}{_TIMING_SUFFIX}"
    payload = {
        "version": 1,
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
        "ManyLingo Edge TTS word boundaries saved: "
        f"count={len(boundaries)}, file={sidecar}"
    )
    return sidecar


def apply_word_boundaries_to_items(
    items: Iterable[ManyLingoItem],
    boundaries: Iterable[dict],
    *,
    audio_duration: float | None = None,
) -> bool:
    """Assign exact item start/end times by matching narration tokens to TTS cues.

    The narration is deterministic: for each item it speaks ``word`` and then ``sentence``.
    We therefore flatten the expected tokens and the Edge WordBoundary tokens, require an
    exact ordered match, and use the first cue of the next item as the scene cut.  This keeps
    the current visual on screen during natural punctuation pauses instead of cutting early.
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
            "ManyLingo exact timing skipped: Edge TTS returned fewer tokens than expected "
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
            "ManyLingo exact timing skipped: narration and WordBoundary tokens differ at "
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

    # The first visual should already be present before the first phoneme.  Subsequent cuts
    # happen precisely when Edge says the next vocabulary block begins.  The previous scene
    # naturally stays visible through the pause between blocks.
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
        "ManyLingo scenes mapped to real Edge TTS timestamps: "
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
    """Load the task-local sidecar and mutate ManyLingo items with exact timings."""
    if getattr(params, "content_mode", "standard") != "manylingo":
        return False
    items = list(getattr(params, "manylingo_items", []) or [])
    if not items:
        return False

    payload = _find_timing_sidecar(output_file, getattr(params, "video_script", ""))
    if not payload:
        logger.info(
            "ManyLingo exact Edge timing unavailable; keeping estimated scene timing"
        )
        return False

    # The final rendered clip duration is not known yet, but the last Edge boundary is a
    # better endpoint than text-length estimation. The existing renderer clamps to the real
    # final duration afterwards.
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

    # install_video_patch() runs first. Wrapping that function here lets us populate exact
    # item timings BEFORE its post-render ManyLingo overlay/synchronization step executes.
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
