from app.models.schema import VideoParams
from app.services.manylingo import (
    ManyLingoItem,
    _timed_items,
    clean_manylingo_subject,
    is_manylingo_mode,
    parse_manylingo_items,
)


def test_standard_mode_stays_unchanged():
    params = VideoParams(video_subject="A normal video")
    assert is_manylingo_mode(params) is False


def test_manylingo_mode_uses_subject_prefix():
    params = VideoParams(video_subject="[ManyLingo] Home vocabulary")
    assert is_manylingo_mode(params) is True
    assert clean_manylingo_subject(params.video_subject) == "Home vocabulary"


def test_manylingo_script_parser():
    items = parse_manylingo_items(
        "house | This house is big. | Esta casa es grande.\n"
        "living room | We watch TV in the living room. | Vemos televisión en la sala."
    )
    assert len(items) == 2
    assert items[0].word == "house"
    assert items[0].sentence == "This house is big."
    assert items[0].translation == "Esta casa es grande."
    assert items[1].word == "living room"


def test_manylingo_items_are_distributed_across_duration():
    items = [
        ManyLingoItem(
            word="house",
            sentence="This house is big.",
            translation="Esta casa es grande.",
        ),
        ManyLingoItem(
            word="kitchen",
            sentence="The kitchen is clean.",
            translation="La cocina está limpia.",
        ),
    ]
    timed = _timed_items(items, 10.0)
    assert timed[0][1:] == (0.0, 5.0)
    assert timed[1][1:] == (5.0, 10.0)


def test_explicit_manylingo_timing_is_preserved():
    item = ManyLingoItem(word="house", start=1.0, end=3.5)
    timed = _timed_items([item], 10.0)
    assert timed[0][1:] == (1.0, 3.5)
