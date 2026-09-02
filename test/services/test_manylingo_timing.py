from datetime import timedelta
from types import SimpleNamespace

from app.models.schema import ManyLingoItem
from app.services.manylingo_timing import (
    apply_word_boundaries_to_items,
    extract_word_boundaries,
)


def _cue(text: str, start: float, end: float):
    return SimpleNamespace(
        content=text,
        start=timedelta(seconds=start),
        end=timedelta(seconds=end),
    )


def test_exact_boundaries_cut_scene_when_next_item_starts():
    items = [
        ManyLingoItem(word="house", sentence="This house is big."),
        ManyLingoItem(
            word="living room",
            sentence="We watch TV in the living room.",
        ),
    ]
    cues = [
        _cue("house", 0.10, 0.55),
        _cue("This", 0.80, 1.05),
        _cue("house", 1.06, 1.40),
        _cue("is", 1.41, 1.55),
        _cue("big", 1.56, 1.95),
        _cue("living", 2.35, 2.70),
        _cue("room", 2.71, 3.05),
        _cue("We", 3.28, 3.48),
        _cue("watch", 3.49, 3.78),
        _cue("TV", 3.79, 4.02),
        _cue("in", 4.03, 4.16),
        _cue("the", 4.17, 4.31),
        _cue("living", 4.32, 4.68),
        _cue("room", 4.69, 5.03),
    ]

    boundaries = extract_word_boundaries(SimpleNamespace(cues=cues))
    assert apply_word_boundaries_to_items(
        items,
        boundaries,
        audio_duration=5.50,
    )

    assert items[0].start == 0.0
    assert items[0].end == 2.35
    assert items[1].start == 2.35
    assert items[1].end == 5.50


def test_mismatched_boundaries_keep_estimated_timing_available():
    items = [ManyLingoItem(word="house", sentence="This house is big.")]
    boundaries = [
        {"text": "car", "start": 0.1, "end": 0.5},
        {"text": "This", "start": 0.7, "end": 1.0},
    ]

    assert not apply_word_boundaries_to_items(items, boundaries, audio_duration=2.0)
    assert items[0].start == 0.0
    assert items[0].end is None
