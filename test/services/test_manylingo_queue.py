from app.services import manylingo_queue as queue


def _use_temp_store(monkeypatch, tmp_path):
    path = tmp_path / "automation.json"
    monkeypatch.setattr(queue, "_store_path", lambda: str(path))
    return path


def test_import_vocabulary_deduplicates_and_keeps_metadata(monkeypatch, tmp_path):
    _use_temp_store(monkeypatch, tmp_path)
    result = queue.import_vocabulary("house | A1 | Home\nKitchen | A1 | Home\nhouse | A1 | Home")
    assert result["added"] == 2
    assert result["total"] == 2
    store = queue.get_store()
    assert [item["word"] for item in store["vocabulary"]] == ["house", "Kitchen"]
    assert all(item["times_used"] == 0 for item in store["vocabulary"])


def test_planner_prefers_same_topic_and_least_used_words(monkeypatch, tmp_path):
    _use_temp_store(monkeypatch, tmp_path)
    queue.import_vocabulary("house | A1 | Home\nbedroom | A1 | Home\nkitchen | A1 | Home\napple | A1 | Food\nwater | A1 | Food\nbread | A1 | Food")
    groups = queue.plan_word_groups(level="A1", video_count=2, words_per_video=3)
    assert len(groups) == 2
    assert groups[0]["topic"] in {"Home", "Food"}
    assert len(groups[0]["words"]) == 3
    assert len(set(groups[0]["words"]) & set(groups[1]["words"])) == 0


def test_create_job_marks_vocabulary_as_used(monkeypatch, tmp_path):
    _use_temp_store(monkeypatch, tmp_path)
    queue.import_vocabulary("house | A1 | Home\nbedroom | A1 | Home\nkitchen | A1 | Home")
    group = queue.plan_word_groups(level="A1", video_count=1, words_per_video=3)[0]
    job = queue.create_job(task_id="task-1", group=group, items=[{"word": word} for word in group["words"]], subject="test", narration="test")
    assert job["status"] == "queued"
    assert queue.vocabulary_stats()["unused"] == 0
    assert all(item["times_used"] == 1 for item in queue.get_store()["vocabulary"])


def test_planner_moves_to_less_used_words(monkeypatch, tmp_path):
    _use_temp_store(monkeypatch, tmp_path)
    queue.import_vocabulary("house | A1 | Home\nbedroom | A1 | Home\nkitchen | A1 | Home\napple | A1 | Food\nwater | A1 | Food\nbread | A1 | Food")
    first = queue.plan_word_groups(level="A1", video_count=1, words_per_video=3)[0]
    queue.create_job(task_id="task-1", group=first, items=[], subject="test", narration="test")
    second = queue.plan_word_groups(level="A1", video_count=1, words_per_video=3)[0]
    assert set(second["words"]).isdisjoint(first["words"])


def test_preplanned_curriculum_keeps_exact_video_group_and_content(monkeypatch, tmp_path):
    _use_temp_store(monkeypatch, tmp_path)
    raw = (
        "A1-0001 | A1 | Home | 1 | house | This house is big. | Esta casa es grande. | large house exterior\n"
        "A1-0001 | A1 | Home | 2 | bedroom | My bedroom is upstairs. | Mi dormitorio está arriba. | cozy bedroom interior\n"
        "A1-0002 | A1 | Food | 1 | apple | I eat an apple. | Como una manzana. | person eating apple"
    )
    result = queue.import_preplanned_curriculum(raw)
    assert result["groups"] == 2
    assert queue.vocabulary_stats()["preplanned_groups"] == 2
    groups = queue.plan_word_groups(level="A1", video_count=2, words_per_video=5)
    assert groups[0]["group_id"] == "A1-0001"
    assert groups[0]["words"] == ["house", "bedroom"]
    assert groups[0]["items"][0]["sentence"] == "This house is big."
    assert groups[0]["items"][0]["translation"] == "Esta casa es grande."


def test_preplanned_group_is_reused_only_after_less_used_groups(monkeypatch, tmp_path):
    _use_temp_store(monkeypatch, tmp_path)
    raw = (
        "A1-0001 | A1 | Home | 1 | house | This house is big. | Esta casa es grande. | house exterior\n"
        "A1-0002 | A1 | Food | 1 | apple | I eat an apple. | Como una manzana. | eating apple"
    )
    queue.import_preplanned_curriculum(raw)
    first = queue.plan_word_groups(level="A1", video_count=1, words_per_video=5)[0]
    queue.create_job(task_id="task-1", group=first, items=first["items"], subject="test", narration="test")
    second = queue.plan_word_groups(level="A1", video_count=1, words_per_video=5)[0]
    assert second["group_id"] != first["group_id"]
