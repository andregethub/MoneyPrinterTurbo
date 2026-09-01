"""Quality improvements for ManyLingo stock queries and educational overlays.

Kept separate from the upstream renderer so the normal MoneyPrinterTurbo mode is not
changed. The patch is installed from app.services bootstrap before the WebUI imports the
ManyLingo helpers.
"""

from __future__ import annotations

import re
from typing import Iterable

from app.models.schema import ManyLingoItem


def _literal_visual_query(item: ManyLingoItem) -> str:
    """Make stock-footage searches literal enough for vocabulary teaching.

    Stock providers sometimes rank a visually attractive but semantically weak result first.
    Repeating the concrete vocabulary concept and adding a few sentence anchors makes the
    provider search less likely to return a generic family/person/outdoor clip.
    """
    word = str(item.word or "").strip()
    original = str(item.search_term or "").strip()
    sentence = str(item.sentence or "").strip().lower()

    special = {
        "house": "house exterior residential home building facade",
        "living room": "living room interior sofa television TV indoors",
        "bedroom": "bedroom interior bed pillows indoors",
        "bathroom": "bathroom interior sink shower indoors",
        "kitchen": "kitchen interior cooking counter indoors",
        "car": "car automobile vehicle",
        "dog": "dog pet animal",
        "cat": "cat pet animal",
    }
    anchors = special.get(word.casefold(), word)

    # Add only concrete sentence clues; avoid generic people/family words dominating search.
    clue_map = {
        "watch": "watching",
        "television": "television TV",
        " tv ": "television TV",
        "bed": "bed",
        "room": "interior indoors",
        "street": "street outdoors",
        "school": "school classroom",
        "office": "office workplace",
        "food": "food meal",
    }
    clues = []
    padded = f" {sentence} "
    for needle, value in clue_map.items():
        if needle in padded and value not in clues:
            clues.append(value)

    query = " ".join(part for part in (anchors, original, *clues) if part)
    tokens = re.findall(r"[A-Za-z0-9'-]+", query)
    deduped = []
    seen = set()
    for token in tokens:
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(token)
    return " ".join(deduped[:14])


def _improve_items(items: Iterable[ManyLingoItem]) -> list[ManyLingoItem]:
    result = []
    for item in items:
        data = item.model_dump()
        data["search_term"] = _literal_visual_query(item)
        result.append(ManyLingoItem(**data))
    return result


def _safe_overlay_factory(base_module):
    def apply_manylingo_overlays(
        video_clip,
        *,
        items: Iterable[ManyLingoItem],
        font_path: str,
        watermark: str = "manylingo.com",
        cta: str = "",
        cta_duration: float = 2.5,
    ):
        """Render text in separated safe zones so CTA and vocabulary never collide."""
        width, height = video_clip.size
        duration = float(video_clip.duration or 0)
        overlays = []
        portrait = height > width

        # Keep the learning block around the middle/lower-middle, but leave generous gaps.
        if portrait:
            word_y, sentence_y, translation_y = 0.46, 0.59, 0.70
            word_scale, sentence_scale, translation_scale = 0.072, 0.047, 0.039
            cta_y = 0.18
        else:
            word_y, sentence_y, translation_y = 0.38, 0.54, 0.68
            word_scale, sentence_scale, translation_scale = 0.055, 0.038, 0.032
            cta_y = 0.14

        for item, start, end in base_module._timed_items(items, duration):
            word = base_module._text_clip(
                text=item.word.upper(), width=width, font_path=font_path,
                font_size=max(54 if portrait else 42, int(width * word_scale)),
                y=int(height * word_y), start=start, end=end,
            )
            sentence = base_module._text_clip(
                text=item.sentence, width=width, font_path=font_path,
                font_size=max(38 if portrait else 30, int(width * sentence_scale)),
                y=int(height * sentence_y), start=start, end=end,
            )
            translation = base_module._text_clip(
                text=item.translation, width=width, font_path=font_path,
                font_size=max(32 if portrait else 26, int(width * translation_scale)),
                y=int(height * translation_y), start=start, end=end, opacity=0.92,
            )
            overlays.extend(x for x in (word, sentence, translation) if x is not None)

        watermark = str(watermark or "").strip()
        if watermark:
            mark = base_module._text_clip(
                text=watermark, width=width, font_path=font_path,
                font_size=max(24, int(width * 0.026)), y=int(height * 0.035),
                start=0, end=duration, opacity=0.72,
            )
            if mark is not None:
                overlays.append(mark)

        cta = str(cta or "").strip()
        cta_duration_value = min(float(cta_duration or 0), duration)
        if cta and cta_duration_value > 0:
            cta_start = max(0.0, duration - cta_duration_value)
            cta_clip = base_module._text_clip(
                text=cta, width=width, font_path=font_path,
                font_size=max(36 if portrait else 28, int(width * 0.042)),
                y=int(height * cta_y), start=cta_start, end=duration,
            )
            if cta_clip is not None:
                overlays.append(cta_clip)

        if not overlays:
            return video_clip
        return base_module.CompositeVideoClip([video_clip, *overlays], size=(width, height))

    return apply_manylingo_overlays


def install_quality_patch() -> None:
    from app.services import manylingo as base

    if getattr(base, "_manylingo_quality_patch_installed", False):
        return

    original_generate = base.generate_manylingo_items

    def generate_manylingo_items(*args, **kwargs):
        return _improve_items(original_generate(*args, **kwargs))

    base.generate_manylingo_items = generate_manylingo_items
    base.apply_manylingo_overlays = _safe_overlay_factory(base)
    base._manylingo_quality_patch_installed = True
