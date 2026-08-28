"""Opt-in ManyLingo educational short-video helpers.

ManyLingo data is kept separate from ``video_script`` so TTS only reads the intended
English narration. The normal MoneyPrinterTurbo renderer remains untouched; when
``content_mode == 'manylingo'`` a small wrapper adds the educational visual layer after the
standard render completes.
"""

from __future__ import annotations

import json
import os
import re
from typing import Iterable

from loguru import logger
from moviepy import (
    CompositeVideoClip,
    TextClip,
    VideoFileClip,
    concatenate_videoclips,
    vfx,
)

from app.models.schema import ManyLingoItem, VideoParams
from app.utils import utils

DEFAULT_CTA = (
    "Aprende inglés todos los días\n"
    "manylingo.com\n"
    "Comenta MANYLINGO para recibir el enlace"
)
MAX_AI_WORDS = 20
# A small pause is added to each block because narration speaks the vocabulary word,
# pauses, and then speaks the example sentence. Word count alone would otherwise make
# short items switch scenes too early.
_ITEM_PAUSE_WEIGHT = 1.75


def is_manylingo_mode(params: VideoParams) -> bool:
    return getattr(params, "content_mode", "standard") == "manylingo"


def normalize_words(raw_words: str | Iterable[str]) -> list[str]:
    """Normalize word-only input while preserving the user's order."""
    if isinstance(raw_words, str):
        candidates = re.split(r"[\n,;]+", raw_words)
    else:
        candidates = [str(word) for word in raw_words]

    words = []
    seen = set()
    for candidate in candidates:
        word = str(candidate or "").strip()
        key = word.casefold()
        if not word or key in seen:
            continue
        seen.add(key)
        words.append(word)
        if len(words) >= MAX_AI_WORDS:
            break
    return words


def build_narration(items: Iterable[ManyLingoItem]) -> str:
    """Build English-only TTS narration from structured ManyLingo items."""
    parts = []
    for item in items:
        word = str(item.word or "").strip().rstrip(".?!")
        sentence = str(item.sentence or "").strip().rstrip(".?!")
        if word:
            parts.append(word)
        if sentence:
            parts.append(sentence)
    narration = ". ".join(parts).strip()
    return f"{narration}." if narration else ""


def items_to_editor_text(items: Iterable[ManyLingoItem]) -> str:
    """Serialize items into the editable WebUI row format."""
    rows = []
    for item in items:
        rows.append(
            " | ".join(
                [
                    str(item.word or "").strip(),
                    str(item.sentence or "").strip(),
                    str(item.translation or "").strip(),
                    str(item.search_term or item.word or "").strip(),
                ]
            )
        )
    return "\n".join(rows)


def _extract_json_array(response: str):
    value = str(response or "").strip()
    if value.startswith("Error:"):
        raise ValueError(value.removeprefix("Error:").strip())

    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)

    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", value, re.DOTALL)
        if not match:
            raise ValueError("The LLM did not return a JSON array.")
        data = json.loads(match.group())

    if not isinstance(data, list):
        raise ValueError("The LLM response is not a JSON array.")
    return data


def generate_manylingo_items(
    raw_words: str | Iterable[str],
    *,
    translation_language: str = "Spanish",
    app_config=None,
) -> list[ManyLingoItem]:
    """Create sentence, translation, and stock-footage search term from word-only input."""
    words = normalize_words(raw_words)
    if not words:
        raise ValueError("Add at least one vocabulary word.")

    from app.services import llm

    words_json = json.dumps(words, ensure_ascii=False)
    prompt = f"""
# Role: ManyLingo Vocabulary Content Generator

Create beginner-friendly educational content for the exact English vocabulary words below.

## Rules
1. Return ONLY one valid JSON array. No markdown and no commentary.
2. Return exactly one object for each input word and preserve the exact input order.
3. Every object must contain exactly: "word", "sentence", "translation", "search_term".
4. "word" must exactly match the corresponding input word.
5. "sentence" must be a natural beginner-level English example using that exact word or phrase and should contain about 5-8 words so each vocabulary scene has a consistent speaking duration.
6. "translation" must translate the full English sentence into {translation_language}.
7. "search_term" must be a concise English stock-video search query that visually represents the vocabulary item and sentence. Prefer concrete scenes and objects.
8. Do not add explanations, pronunciation guides, emojis, hashtags, or extra keys.

## Input words
{words_json}

## Output shape
[{{"word":"house","sentence":"This house is very big and beautiful.","translation":"Esta casa es muy grande y hermosa.","search_term":"large house exterior"}}]
""".strip()

    if app_config is None:
        response = llm._generate_response(prompt)
    else:
        response = llm._generate_response(prompt, app_config=app_config)

    raw_items = _extract_json_array(response)
    if len(raw_items) != len(words):
        raise ValueError(
            f"The LLM returned {len(raw_items)} items for {len(words)} input words."
        )

    result = []
    for index, (expected_word, raw_item) in enumerate(zip(words, raw_items), start=1):
        if not isinstance(raw_item, dict):
            raise ValueError(f"Item {index} is not a JSON object.")

        returned_word = str(raw_item.get("word", "") or "").strip()
        if returned_word.casefold() != expected_word.casefold():
            raise ValueError(
                f"Item {index} changed the input word '{expected_word}' to '{returned_word}'."
            )

        sentence = str(raw_item.get("sentence", "") or "").strip()
        translation = str(raw_item.get("translation", "") or "").strip()
        search_term = str(raw_item.get("search_term", "") or "").strip()
        if not sentence or not translation:
            raise ValueError(f"Item {index} is missing sentence or translation.")

        result.append(
            ManyLingoItem(
                word=expected_word,
                sentence=sentence,
                translation=translation,
                search_term=search_term or expected_word,
            )
        )
    return result


def _speech_weight(item: ManyLingoItem) -> float:
    """Estimate how much of the narration duration belongs to one vocabulary item."""
    spoken = f"{item.word or ''} {item.sentence or ''}".strip()
    token_count = len(re.findall(r"[\w'-]+", spoken, flags=re.UNICODE))
    return max(1.0, float(token_count) + _ITEM_PAUSE_WEIGHT)


def _timed_items(
    items: Iterable[ManyLingoItem], duration: float
) -> list[tuple[ManyLingoItem, float, float]]:
    """Resolve explicit timings or distribute real video duration by speech length.

    A previous implementation split the video into equal windows. That works for equally
    sized sentences but causes a long phrase to switch visuals before its narration ends.
    Here each automatic block receives a proportional share of the actual final duration.
    Explicit ``end`` timings still take precedence for callers that provide real timestamps.
    """
    items = list(items)
    if not items or duration <= 0:
        return []

    if all(item.end is not None for item in items):
        resolved = []
        for item in items:
            start = min(max(0.0, float(item.start or 0.0)), duration)
            end = min(max(start, float(item.end or start)), duration)
            if end > start:
                resolved.append((item, start, end))
        return resolved

    weights = [_speech_weight(item) for item in items]
    total_weight = sum(weights)
    cursor = 0.0
    resolved = []
    for index, (item, weight) in enumerate(zip(items, weights)):
        start = cursor
        if index == len(items) - 1:
            end = duration
        else:
            end = min(duration, start + duration * (weight / total_weight))
        if end > start:
            resolved.append((item, start, end))
        cursor = end
    return resolved


def retime_manylingo_scenes(
    video_clip,
    *,
    items: Iterable[ManyLingoItem],
    source_scene_duration: float,
):
    """Map one ordered source scene to each vocabulary block and retime it to narration.

    MoneyPrinterTurbo's ordered material mode may append another round of stock clips when
    narration lasts longer than one pass through the vocabulary list. For a teaching video
    that can cause the timeline to return to the first word near the end. This function uses
    only the first scene for each vocabulary item, then changes each scene's playback speed
    so its boundary matches the estimated spoken block duration. The original narration audio
    is attached unchanged after the visual retiming.
    """
    items = list(items)
    duration = float(video_clip.duration or 0.0)
    source_scene_duration = float(source_scene_duration or 0.0)
    if not items or duration <= 0 or source_scene_duration <= 0:
        return video_clip

    target_windows = _timed_items(items, duration)
    if len(target_windows) != len(items):
        return video_clip

    # Every vocabulary item needs at least some footage in the original ordered timeline.
    minimum_source_start = source_scene_duration * (len(items) - 1)
    if minimum_source_start >= duration:
        logger.warning(
            "ManyLingo scene retiming skipped because the ordered source timeline does "
            f"not contain all vocabulary scenes: items={len(items)}, "
            f"scene_duration={source_scene_duration:.2f}s, video_duration={duration:.2f}s"
        )
        return video_clip

    visual_source = video_clip.without_audio()
    scenes = []
    try:
        for index, (_, target_start, target_end) in enumerate(target_windows):
            target_duration = target_end - target_start
            source_start = index * source_scene_duration
            source_end = min(source_start + source_scene_duration, duration)
            source_duration = source_end - source_start
            if source_duration <= 0 or target_duration <= 0:
                continue

            scene = visual_source.subclipped(source_start, source_end)
            speed_factor = source_duration / target_duration
            if abs(speed_factor - 1.0) > 0.01:
                scene = scene.with_effects([vfx.MultiplySpeed(factor=speed_factor)])
            scene = scene.with_duration(target_duration)
            scenes.append(scene)

        if len(scenes) != len(items):
            logger.warning(
                "ManyLingo scene retiming did not produce one scene per vocabulary item; "
                "fall back to the original ordered video"
            )
            for scene in scenes:
                scene.close()
            visual_source.close()
            return video_clip

        retimed = concatenate_videoclips(scenes, method="compose").with_duration(duration)
        if video_clip.audio is not None:
            retimed = retimed.with_audio(video_clip.audio)
        logger.info(
            "ManyLingo scenes synchronized to narration blocks: "
            f"items={len(items)}, duration={duration:.2f}s"
        )
        return retimed
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


def _text_clip(
    *,
    text: str,
    width: int,
    font_path: str,
    font_size: int,
    y,
    start: float,
    end: float,
    opacity: float = 1.0,
):
    if not text or end <= start:
        return None
    clip = TextClip(
        text=text,
        font=font_path,
        font_size=font_size,
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


def apply_manylingo_overlays(
    video_clip,
    *,
    items: Iterable[ManyLingoItem],
    font_path: str,
    watermark: str = "manylingo.com",
    cta: str = "",
    cta_duration: float = 2.5,
):
    """Overlay vocabulary, example, translation, watermark and optional final CTA."""
    width, height = video_clip.size
    duration = float(video_clip.duration or 0)
    overlays = []

    for item, start, end in _timed_items(items, duration):
        word = _text_clip(
            text=item.word.upper(),
            width=width,
            font_path=font_path,
            font_size=max(72, int(width * 0.085)),
            y=int(height * 0.54),
            start=start,
            end=end,
        )
        sentence = _text_clip(
            text=item.sentence,
            width=width,
            font_path=font_path,
            font_size=max(46, int(width * 0.052)),
            y=int(height * 0.66),
            start=start,
            end=end,
        )
        translation = _text_clip(
            text=item.translation,
            width=width,
            font_path=font_path,
            font_size=max(38, int(width * 0.043)),
            y=int(height * 0.75),
            start=start,
            end=end,
            opacity=0.92,
        )
        overlays.extend(x for x in (word, sentence, translation) if x is not None)

    watermark = str(watermark or "").strip()
    if watermark:
        mark = _text_clip(
            text=watermark,
            width=width,
            font_path=font_path,
            font_size=max(28, int(width * 0.03)),
            y=int(height * 0.035),
            start=0,
            end=duration,
            opacity=0.72,
        )
        if mark is not None:
            overlays.append(mark)

    cta = str(cta or "").strip()
    cta_duration = min(float(cta_duration or 0), duration)
    if cta and cta_duration > 0:
        cta_start = max(0.0, duration - cta_duration)
        cta_clip = _text_clip(
            text=cta,
            width=width,
            font_path=font_path,
            font_size=max(46, int(width * 0.052)),
            y="center",
            start=cta_start,
            end=duration,
        )
        if cta_clip is not None:
            overlays.append(cta_clip)

    if not overlays:
        return video_clip
    return CompositeVideoClip([video_clip, *overlays], size=(width, height))


def render_manylingo_output(output_file: str, params: VideoParams) -> None:
    """Synchronize ordered scenes and apply the ManyLingo educational visual layer."""
    if not is_manylingo_mode(params):
        return

    items = list(getattr(params, "manylingo_items", []) or [])
    if not items:
        raise ValueError("ManyLingo mode requires at least one manylingo_items entry.")

    font_name = getattr(params, "font_name", "") or "STHeitiMedium.ttc"
    font_path = os.path.join(utils.font_dir(), font_name)
    if os.name == "nt":
        font_path = font_path.replace("\\", "/")

    temp_file = f"{output_file}.manylingo.mp4"
    source = VideoFileClip(output_file)
    synchronized = source
    composed = None
    try:
        synchronized = retime_manylingo_scenes(
            source,
            items=items,
            source_scene_duration=float(getattr(params, "video_clip_duration", 2) or 2),
        )
        composed = apply_manylingo_overlays(
            synchronized,
            items=items,
            font_path=font_path,
            watermark=getattr(params, "manylingo_watermark", "manylingo.com"),
            cta=getattr(params, "manylingo_cta", ""),
            cta_duration=getattr(params, "manylingo_cta_duration", 2.5),
        )
        composed.write_videofile(
            temp_file,
            codec="libx264",
            audio_codec="aac",
            fps=int(getattr(source, "fps", 0) or 30),
            logger=None,
        )
    finally:
        if composed is not None and composed is not synchronized:
            composed.close()
        if synchronized is not source:
            synchronized.close()
        source.close()

    os.replace(temp_file, output_file)
    logger.success(f"ManyLingo synchronized overlay rendered: {output_file}")


def install_video_patch() -> None:
    """Wrap the existing renderer without editing the large upstream video.py file."""
    from app.services import video as video_service

    if getattr(video_service, "_manylingo_patch_installed", False):
        return

    original_generate_video = video_service.generate_video

    def generate_video_with_manylingo(*args, **kwargs):
        result = original_generate_video(*args, **kwargs)

        output_file = kwargs.get("output_file")
        params = kwargs.get("params")
        if output_file is None and len(args) >= 4:
            output_file = args[3]
        if params is None and len(args) >= 5:
            params = args[4]

        if output_file and params is not None and is_manylingo_mode(params):
            render_manylingo_output(output_file, params)
        return result

    video_service.generate_video = generate_video_with_manylingo
    video_service._manylingo_patch_installed = True
