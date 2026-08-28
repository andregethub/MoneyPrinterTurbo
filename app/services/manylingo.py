"""Rendering helpers for the opt-in ManyLingo educational short-video mode."""

from __future__ import annotations

from typing import Iterable

from moviepy import CompositeVideoClip, TextClip

from app.models.schema import ManyLingoItem, VideoParams


def _timed_items(items: Iterable[ManyLingoItem], duration: float) -> list[tuple[ManyLingoItem, float, float]]:
    """Resolve explicit or evenly distributed timings without exceeding video duration."""
    items = list(items)
    if not items or duration <= 0:
        return []
    default_span = duration / len(items)
    resolved = []
    for index, item in enumerate(items):
        start = min(float(item.start or index * default_span), duration)
        end = float(item.end) if item.end is not None else min(duration, start + default_span)
        if end > start:
            resolved.append((item, start, min(end, duration)))
    return resolved


def apply_manylingo_overlays(video_clip, params: VideoParams, font_path: str):
    """Overlay word, example, translation, watermark and optional CTA on a rendered clip.

    This function is deliberately isolated from the standard subtitle renderer. Calling it is
    opt-in (`content_mode == 'manylingo'`), which keeps existing MoneyPrinterTurbo videos
    byte-for-byte on the old rendering path.
    """
    if getattr(params, "content_mode", "standard") != "manylingo":
        return video_clip

    width, height = video_clip.size
    duration = float(video_clip.duration or 0)
    overlays = []

    def text_clip(text: str, size: int, y, start: float, end: float, opacity: float = 1.0):
        if not text or end <= start:
            return None
        clip = TextClip(
            text=text,
            font=font_path,
            font_size=size,
            color="#FFFFFF",
            stroke_color="#000000",
            stroke_width=2,
            size=(int(width * 0.88), None),
            text_align="center",
            method="caption",
        )
        return (
            clip.with_start(start)
            .with_end(end)
            .with_duration(end - start)
            .with_position(("center", y))
            .with_opacity(opacity)
        )

    for item, start, end in _timed_items(params.manylingo_items, duration):
        word = text_clip(item.word.upper(), max(72, int(width * 0.085)), int(height * 0.54), start, end)
        sentence = text_clip(item.sentence, max(46, int(width * 0.052)), int(height * 0.66), start, end)
        translation = text_clip(item.translation, max(38, int(width * 0.043)), int(height * 0.75), start, end, 0.92)
        overlays.extend(x for x in (word, sentence, translation) if x is not None)

    watermark = str(getattr(params, "manylingo_watermark", "") or "").strip()
    if watermark:
        mark = text_clip(watermark, max(28, int(width * 0.03)), int(height * 0.035), 0, duration, 0.72)
        if mark is not None:
            overlays.append(mark)

    cta = str(getattr(params, "manylingo_cta", "") or "").strip()
    cta_duration = min(float(getattr(params, "manylingo_cta_duration", 2.5) or 0), duration)
    if cta and cta_duration > 0:
        cta_start = max(0.0, duration - cta_duration)
        cta_clip = text_clip(cta, max(46, int(width * 0.052)), "center", cta_start, duration)
        if cta_clip is not None:
            overlays.append(cta_clip)

    if not overlays:
        return video_clip
    return CompositeVideoClip([video_clip, *overlays], size=(width, height))
