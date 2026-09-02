from app.services import manylingo_distribution as distribution
from app.services import upload_post


def _item(group_id, order, word, topic="Home", level="A1"):
    return {
        "id": f"{group_id}-{order}",
        "word": word,
        "level": level,
        "topic": topic,
        "group_id": group_id,
        "group_order": order,
        "sentence": f"Example with {word}.",
        "translation": f"Ejemplo con {word}.",
        "search_term": word,
        "times_used": 0,
    }


def test_upload_post_normalizes_twitter_alias():
    assert upload_post.UploadPostService.normalize_platforms(
        ["tiktok", "twitter", "x", "pinterest"]
    ) == ["tiktok", "x", "pinterest"]


def test_vertical_platforms_adds_x_and_pinterest_when_board_exists(monkeypatch):
    service = upload_post.upload_post_service
    monkeypatch.setattr(
        type(service), "platforms", property(lambda self: ["tiktok", "instagram", "youtube"])
    )
    monkeypatch.setattr(
        type(service), "pinterest_board_id", property(lambda self: "board-123")
    )

    platforms, warnings = distribution.vertical_platforms()

    assert platforms == ["tiktok", "instagram", "youtube", "x", "pinterest"]
    assert warnings == []


def test_vertical_platforms_warns_when_pinterest_board_is_missing(monkeypatch):
    service = upload_post.upload_post_service
    monkeypatch.setattr(
        type(service), "platforms", property(lambda self: ["tiktok", "instagram", "youtube"])
    )
    monkeypatch.setattr(
        type(service), "pinterest_board_id", property(lambda self: "")
    )

    platforms, warnings = distribution.vertical_platforms()

    assert "x" in platforms
    assert "pinterest" not in platforms
    assert warnings


def test_horizontal_compilation_keeps_one_topic_and_combines_fixed_groups(monkeypatch):
    vocabulary = []
    for group_number in range(1, 5):
        group_id = f"A1-{group_number:04d}"
        for order in range(1, 6):
            vocabulary.append(
                _item(group_id, order, f"home-{group_number}-{order}", topic="Home")
            )
    for order in range(1, 6):
        vocabulary.append(_item("A1-0100", order, f"food-{order}", topic="Food"))

    monkeypatch.setattr(
        distribution.ml_queue,
        "get_store",
        lambda: {"vocabulary": vocabulary, "jobs": []},
    )

    compilation = distribution.plan_horizontal_compilation(
        level="A1", groups_per_video=4
    )

    assert compilation["content_format"] == "landscape"
    assert compilation["publish_platforms"] == ["youtube"]
    assert compilation["topic"] == "Home"
    assert len(compilation["source_group_ids"]) == 4
    assert len(compilation["items"]) == 20


def test_horizontal_compilation_skips_groups_used_by_previous_landscape_job(monkeypatch):
    vocabulary = []
    for group_number in range(1, 4):
        group_id = f"A1-{group_number:04d}"
        for order in range(1, 6):
            vocabulary.append(
                _item(group_id, order, f"word-{group_number}-{order}", topic="Home")
            )

    store = {
        "vocabulary": vocabulary,
        "jobs": [
            {
                "content_format": "landscape",
                "status": "published",
                "source_group_ids": ["A1-0001"],
            }
        ],
    }
    monkeypatch.setattr(distribution.ml_queue, "get_store", lambda: store)

    compilation = distribution.plan_horizontal_compilation(
        level="A1", groups_per_video=2
    )

    assert "A1-0001" not in compilation["source_group_ids"]
    assert compilation["source_group_ids"] == ["A1-0002", "A1-0003"]
