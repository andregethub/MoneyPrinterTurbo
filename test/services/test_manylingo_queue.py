from app.services import manylingo_queue as queue


def _use_temp_store(monkeypatch, tmp_path):
    path = tmp_path / "automation.json"
    monkeypatch.setattr(queue, "_store_path", lambda: str(path))
    return path


def test_import_vocabulary_deduplicates_and_keeps_metadata(monkeypatch, tmp_path):
    _use_temp_store(monkeypatch, tmp_path)

    result = queue.import_vocabulary(
        "house | A1 | Home\nKitchen | A1 | Home\nhouse | A1 | Home"
    )

    assert result["added"] == 2
    assert result["total"] == 2
    store = queue.get_store()
    assert [item["word"] for item in store["vocabulary"]] == ["house", "Kitchen"]
    assert all(item["times_used"] == 0 for item in store["vocabulary"])


def test_planner_prefers_same_topic_and_least_used_words(monkeypatch, tmp_path):
    _use_temp_store(monkeypatch, tmp_path)
    queue.import_vocabulary(
        "house | A1 | Home\n"
        "bedroom | A1 | Home\n"
        "kitchen | A1 | Home\n"
        "apple | A1 | Food\n"
        "water | A1 | Food\n"
        "bread | A1 | Food"
    )

    groups = queue.plan_word_groups(level="A1", video_count=2, words_per_video=3)

    assert len(groups) == 2
    assert groups[0]["topic"] in {"Home", "Food"}
    assert len(groups[0]["words"]) == 3
    assert len(set(groups[0]["words"]) & set(groups[1]["words"])) == 0


def test_create_job_marks_vocabulary_as_used(monkeypatch, tmp_path):
    _use_temp_store(monkeypatch, tmp_path)
    queue.import_vocabulary(
        "house | A1 | Home\nbedroom | A1 | Home\nkitchen | A1 | Home"
    )
    group = queue.plan_word_groups(level="A1", video_count=1, words_per_video=3)[0]

    job = queue.create_job(
        task_id="task-1",
        group=group,
        items=[{"word": word} for word in group["words"]],
        subject="English vocabulary: Home (A1)",
        narration="house. bedroom. kitchen.",
    )

    assert job["status"] == "queued"
    stats = queue.vocabulary_stats()
    assert stats["unused"] == 0
    store = queue.get_store()
    assert all(item["times_used"] == 1 for item in store["vocabulary"])


def test_planner_moves_to_less_used_words(monkeypatch, tmp_path):
    _use_temp_store(monkeypatch, tmp_path)
    queue.import_vocabulary(
        "house | A1 | Home\n"
        "bedroom | A1 | Home\n"
        "kitchen | A1 | Home\n"
        "apple | A1 | Food\n"
        "water | A1 | Food\n"
        "bread | A1 | Food"
    )
    first_group = queue.plan_word_groups(level="A1", video_count=1, words_per_video=3)[0]
    queue.create_job(
        task_id="task-1",
        group=first_group,
        items=[{"word": word} for word in first_group["words"]],
        subject="test",
        narration="test",
    )

    second_group = queue.plan_word_groups(level="A1", video_count=1, words_per_video=3)[0]

    assert set(second_group["words"]).isdisjoint(first_group["words"])
