"""Natural pacing for ManyLingo stock footage.

Exact TTS timestamps should decide scene boundaries, not playback speed. This patch replaces
ManyLingo's old speed-warping behavior with normal-speed trimming and looping so educational
videos stay synchronized without making people or camera motion look unnaturally fast.
"""

from __future__ import annotations

import math
from typing import Iterable

from loguru import logger
from moviepy import concatenate_videoclips

from app.models.schema import ManyLingoItem


def _fit_scene_at_normal_speed(scene, target_duration: float):
    """Fit a visual to a target duration without changing playback speed."""
    target_duration = float(target_duration or 0.0)
    source_duration = float(scene.duration or 0.0)
    if target_duration <= 0 or source_duration <= 0:
        return None

    if source_duration >= target_duration:
        return scene.subclipped(0, target_duration).with_duration(target_duration)

    repeats = max(1, int(math.ceil(target_duration / source_duration)))
    looped = concatenate_videoclips([scene] * repeats, method="compose")
    return looped.subclipped(0, target_duration).with_duration(target_duration)


def retime_manylingo_scenes_natural(
    video_clip,
    *,
    items: Iterable[ManyLingoItem],
    source_scene_duration: float,
):
    """Synchronize scene cuts to TTS timestamps while preserving 1.0x visual speed."""
    from app.services import manylingo as base

    items = list(items)
    duration = float(video_clip.duration or 0.0)
    source_scene_duration = float(source_scene_duration or 0.0)
    if not items or duration <= 0 or source_scene_duration <= 0:
        return video_clip

    target_windows = base._timed_items(items, duration)
    if len(target_windows) != len(items):
        return video_clip

    minimum_source_start = source_scene_duration * (len(items) - 1)
    if minimum_source_start >= duration:
        logger.warning(
            "ManyLingo natural scene sync skipped because the ordered source timeline "
            "does not contain all vocabulary scenes"
        )
        return video_clip

    visual_source = video_clip.without_audio()
    scenes = []
    try:
        for index, (_, target_start, target_end) in enumerate(target_windows):
            target_duration = float(target_end - target_start)
            source_start = index * source_scene_duration
            source_end = min(source_start + source_scene_duration, duration)
            if source_end <= source_start or target_duration <= 0:
                continue

            source_scene = visual_source.subclipped(source_start, source_end)
            fitted = _fit_scene_at_normal_speed(source_scene, target_duration)
            if fitted is None:
                continue
            scenes.append(fitted)

        if len(scenes) != len(items):
            logger.warning(
                "ManyLingo natural scene sync did not produce one scene per item; "
                "falling back to the original ordered video"
            )
            for scene in scenes:
                try:
                    scene.close()
                except Exception:
                    pass
            visual_source.close()
            return video_clip

        synchronized = concatenate_videoclips(scenes, method="compose").with_duration(duration)
        if video_clip.audio is not None:
            synchronized = synchronized.with_audio(video_clip.audio)

        logger.success(
            "ManyLingo scenes synchronized at normal visual speed (1.0x): "
            f"items={len(items)}, duration={duration:.2f}s"
        )
        return synchronized
    except Exception:
        for scene in scenes:
            try:
                scene.close()
            except Exception:
                pass
        try:
            visual_source.close()
        except Exception:
            pass
        raise


def install_pacing_patch() -> None:
    """Replace only the ManyLingo scene-retiming function, leaving standard videos alone."""
    from app.services import manylingo as base

    if getattr(base, "_manylingo_pacing_patch_installed", False):
        return
    base.retime_manylingo_scenes = retime_manylingo_scenes_natural
    base._manylingo_pacing_patch_installed = True
