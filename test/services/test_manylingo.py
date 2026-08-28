from app.models.schema import ManyLingoItem, VideoParams
from app.services.manylingo import _timed_items, is_manylingo_mode


def test_standard_mode_stays_unchanged():
    params = VideoParams(video_subject="A normal video")
    assert params.content_mode == "standard"
    assert params.manylingo_items == []
    assert params.manylingo_watermark == "manylingo.com"
    assert is_manylingo_mode(params) is False


def test_manylingo_mode_is_opt_in():
    params = VideoParams(
        video_subject="Home vocabulary",
        content_mode="manylingo",
        manylingo_items=[ManyLingoItem(word="house")],
    )
    assert is_manylingo_mode(params) is True


def test_manylingo_item_keeps_visual_data_separate_from_narration():
    item = ManyLingoItem(
        word="house",
        sentence="This house is big.",
        translation="Esta casa es grande.",
        search_term="large house exterior",
    )
    params = VideoParams(
        video_subject="Home vocabulary",
        video_script="House. This house is big.",
        content_mode="manylingo",
        manylingo_items=[item],
    )
    assert params.video_script == "House. This house is big."
    assert params.manylingo_items[0].translation == "Esta casa es grande."
    assert params.manylingo_items[0].search_term == "large house exterior"


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
