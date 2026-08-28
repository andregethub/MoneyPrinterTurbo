from app.models.schema import ManyLingoItem, VideoParams
from app.services.manylingo import _timed_items


def test_manylingo_mode_defaults_do_not_change_standard_mode():
    params = VideoParams(video_subject="test")
    assert params.content_mode == "standard"
    assert params.manylingo_items == []
    assert params.manylingo_watermark == "manylingo.com"


def test_manylingo_items_are_distributed_across_duration():
    items = [
        ManyLingoItem(word="house", sentence="This house is big.", translation="Esta casa es grande."),
        ManyLingoItem(word="kitchen", sentence="The kitchen is clean.", translation="La cocina está limpia."),
    ]
    timed = _timed_items(items, 10.0)
    assert timed[0][1:] == (0.0, 5.0)
    assert timed[1][1:] == (5.0, 10.0)


def test_explicit_manylingo_timing_is_preserved():
    item = ManyLingoItem(word="house", start=1.0, end=3.5)
    timed = _timed_items([item], 10.0)
    assert timed[0][1:] == (1.0, 3.5)
