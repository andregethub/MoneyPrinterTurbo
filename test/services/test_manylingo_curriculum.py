from app.services import manylingo_curriculum as curriculum
from app.services.manylingo_content_safety import safe_visual_term


def test_parse_cefr_text_preserves_source_level():
    text = """The Oxford 3000 by CEFR level
A1
house n.
kitchen n.
A2
ability n.
travel v., n.
"""
    entries = curriculum.parse_cefr_text(text, source="fixture")
    assert [(item["word"], item["level"]) for item in entries] == [
        ("house", "A1"), ("kitchen", "A1"), ("ability", "A2"), ("travel", "A2")
    ]


def test_deduplicate_headwords_keeps_earliest_official_level():
    entries = [
        {"word": "address", "level": "A1", "pos": "n."},
        {"word": "address", "level": "B2", "pos": "v."},
        {"word": "house", "level": "A1", "pos": "n."},
    ]
    result = curriculum.deduplicate_headwords(entries)
    address = next(item for item in result if item["word"] == "address")
    assert len(result) == 2
    assert address["level"] == "A1"


def test_topic_classifier_groups_obvious_semantic_words():
    assert curriculum.classify_topic("house", "n.") == "Home"
    assert curriculum.classify_topic("bedroom", "n.") == "Home"
    assert curriculum.classify_topic("apple", "n.") == "Food and drink"
    assert curriculum.classify_topic("train", "n.") == "Transport and travel"


def test_fixed_groups_never_mix_level_or_topic():
    entries = [
        {"word": word, "level": "A1", "pos": "n."}
        for word in ["house", "bedroom", "kitchen", "bathroom", "chair", "apple", "banana"]
    ]
    groups = curriculum.build_fixed_groups(entries, words_per_video=5)
    for group in groups:
        assert group["level"] == "A1"
        topics = {
            curriculum.classify_topic(item["word"], item["pos"])
            for item in entries
            if item["word"] in {row["word"] for row in group["items"]}
        }
        assert topics == {group["topic"]}


def test_group_ids_are_stable_and_level_scoped():
    entries = [
        {"word": "house", "level": "A1", "pos": "n."},
        {"word": "kitchen", "level": "A1", "pos": "n."},
        {"word": "ability", "level": "A2", "pos": "n."},
    ]
    first = curriculum.build_fixed_groups(entries, words_per_video=5)
    second = curriculum.build_fixed_groups(entries, words_per_video=5)
    assert [group["video_id"] for group in first] == [group["video_id"] for group in second]
    assert first[0]["video_id"].startswith("A1-")
    assert first[-1]["video_id"].startswith("A2-")


def test_sensitive_vocabulary_does_not_become_stock_search_query():
    assert safe_visual_term("weapon", "weapon close up") == "English vocabulary lesson neutral background"
    assert safe_visual_term("house", "large house exterior") == "large house exterior"
