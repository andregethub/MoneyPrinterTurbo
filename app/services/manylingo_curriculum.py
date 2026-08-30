"""Build a persistent ManyLingo curriculum from user-supplied CEFR word-list text.

The generated curriculum is local data and is intentionally not committed to the
repository. This module does not download or redistribute third-party word lists.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable

CEFR_LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")

TOPIC_WORDS = {
    "Home": "apartment bath bathroom bed bedroom chair closet cupboard desk door floor furniture garage garden home house kitchen room shower sofa table toilet wall window basement carpet cooker corridor blanket".split(),
    "Food and drink": "apple banana bean beef bread breakfast butter cafe cake carrot cheese chicken chocolate coffee cook cooking cream cup dinner dish drink egg food fruit juice lunch meal meat milk onion orange pepper potato restaurant rice salad salt sandwich soup sugar tea tomato vegetable water dairy grocery herb protein spice wheat".split(),
    "Family and people": "adult aunt baby boy brother child cousin dad daughter family father girl grandfather grandmother grandparent husband man mother mum parent partner sister son teenager uncle wife woman sibling ancestor".split(),
    "Transport and travel": "airport bicycle bike boat bus car drive driver flight hotel journey passport plane road station taxi ticket traffic train travel trip vacation visa cruise highway rail transportation".split(),
    "School and learning": "class classroom college course dictionary education exam homework language learn lesson library pencil school science student study teacher test textbook university curriculum scholarship seminar thesis".split(),
    "Work and business": "boss business career colleague company customer employee employer engineer factory job meeting office worker workplace accountant analyst consultant corporation deadline entrepreneur".split(),
    "Technology and media": "app audio blog camera code computer database device digital download email internet phone programming radio television video website browser electronics password".split(),
    "Body and health": "ankle arm back blood body bone brain dentist doctor ear elbow eye face foot hair hand head health healthcare hip hospital leg medication mouth nose nurse tooth wrist".split(),
    "Nature and weather": "air beach climate cloud coast desert earth environment farm field fire flower forest island land mountain rain river sea snow space spring summer sun tree weather winter cave cliff habitat".split(),
    "Animals": "animal bear bird cat cow dog elephant fish horse lion mouse pig sheep snake rat worm".split(),
    "Sports and exercise": "athlete ball baseball basketball coach competition exercise football game gym match player pool sport swim swimming team tennis championship marathon referee tournament workout".split(),
    "Arts and entertainment": "art artist ballet band cinema concert dance dancer dancing film guitar music painting photo photograph piano song theatre animation artwork choir comedy composer opera orchestra".split(),
    "Clothes and appearance": "belt blonde boot clothes clothing coat dress fashion hat jacket jeans shirt shoe skirt sweater trousers t-shirt outfit make-up".split(),
    "Money and shopping": "bank bargain bill buy cash cent cost credit dollar euro market money pay pound price product sell shop shopping supermarket auction deposit mortgage retail revenue".split(),
    "Places and community": "building capital castle centre church city community continent country park place street town village world bridge clinic downtown suburb temple".split(),
    "Time and calendar": "afternoon april august century date day december evening february friday hour january july june march may midnight minute monday month morning night november october saturday second september sunday thursday time today tomorrow tonight tuesday wednesday week weekend year yesterday annually monthly weekly".split(),
    "Communication and language": "answer article ask call chat comment communicate conversation dialogue explain expression letter message phrase question read report say sentence speak spell spelling talk tell text word write writing".split(),
    "Feelings and relationships": "afraid angry bored excited feeling friendly happy hate hope interested love sad sorry anxiety apology depression disappointment fear panic patience pride satisfaction".split(),
    "Government and society": "army charity civilization democracy democratic government governor immigration politics president voting welfare citizenship parliament parliamentary mayor".split(),
    "Law and public safety": "crime criminal detective police policeman prison jail jury theft robbery court attorney".split(),
    "Science and environment": "biology chemistry energy experiment fossil gene genetic greenhouse hypothesis oxygen radiation sustainable technological evolution".split(),
}

POS_TOPIC = {
    "prep.": "Grammar and function words",
    "conj.": "Grammar and function words",
    "pron.": "Grammar and function words",
    "det.": "Grammar and function words",
    "modal": "Grammar and function words",
    "number": "Numbers and quantities",
}


def _normalise_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u00a0", " ")).strip()


def parse_cefr_text(text: str, source: str = "user-supplied") -> list[dict]:
    """Parse CEFR rows while preserving the level printed in the supplied source."""
    entries: list[dict] = []
    level: str | None = None
    seen: set[tuple[str, str, str]] = set()
    for raw in str(text or "").splitlines():
        line = _normalise_space(raw)
        if not line or line.startswith("©") or "Oxford 3000" in line or "Oxford 5000" in line:
            continue
        if line in CEFR_LEVELS:
            level = line
            continue
        if level is None or re.fullmatch(r"\d+\s*/\s*\d+", line):
            continue
        match = re.match(
            r"^(?P<word>.+?)\s+(?P<pos>(?:indefinite article|definite article|infinitive marker|"
            r"modal v\.|auxiliary v\.|n\.|v\.|adj\.|adv\.|prep\.|conj\.|pron\.|det\.|"
            r"exclam\.|number)(?:[^A-Za-z].*)?)$",
            line,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        word = re.sub(r"\d+$", "", match.group("word")).strip(" ,")
        pos = match.group("pos").strip()
        key = (word.casefold(), level, pos.casefold())
        if word and key not in seen:
            seen.add(key)
            entries.append({"word": word, "level": level, "pos": pos, "source": source})
    return entries


def deduplicate_headwords(entries: Iterable[dict]) -> list[dict]:
    """Use each surface headword once, at its earliest source CEFR level.

    The CEFR PDFs can repeat one spelling for different parts of speech/levels.
    Promotional videos teach the headword once, so we retain the earliest official
    level and merge its part-of-speech labels instead of producing duplicate videos.
    """
    rank = {level: index for index, level in enumerate(CEFR_LEVELS)}
    chosen: dict[str, dict] = {}
    for raw in entries:
        item = dict(raw)
        key = str(item.get("word") or "").casefold()
        if not key:
            continue
        existing = chosen.get(key)
        if existing is None or rank.get(item.get("level"), 99) < rank.get(existing.get("level"), 99):
            chosen[key] = item
        elif item.get("level") == existing.get("level") and item.get("pos") not in str(existing.get("pos") or ""):
            existing["pos"] = ", ".join(filter(None, [str(existing.get("pos") or ""), str(item.get("pos") or "")]))
    return list(chosen.values())


def classify_topic(word: str, pos: str = "") -> str:
    token = re.sub(r"[^a-z -]", "", word.casefold()).strip()
    tokens = set(token.replace("-", " ").split())
    for topic, keywords in TOPIC_WORDS.items():
        if token in keywords or tokens.intersection(keywords):
            return topic
    pos_lower = pos.casefold()
    for marker, topic in POS_TOPIC.items():
        if marker in pos_lower:
            return topic
    if "adj." in pos_lower:
        return "Descriptions and qualities"
    if "adv." in pos_lower:
        return "Actions and descriptions"
    if "v." in pos_lower:
        return "Everyday actions"
    return "General vocabulary"


def enrich_topics(entries: Iterable[dict]) -> list[dict]:
    result = []
    for entry in entries:
        item = dict(entry)
        item["topic"] = item.get("topic") or classify_topic(item["word"], item.get("pos", ""))
        result.append(item)
    return result


def build_fixed_groups(entries: Iterable[dict], words_per_video: int = 5) -> list[dict]:
    """Create stable same-level, same-topic groups in deterministic order."""
    words_per_video = max(1, int(words_per_video))
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for entry in enrich_topics(deduplicate_headwords(entries)):
        buckets[(entry["level"], entry["topic"])].append(entry)
    groups = []
    counters = defaultdict(int)
    level_rank = {level: index for index, level in enumerate(CEFR_LEVELS)}
    for (level, topic), items in sorted(
        buckets.items(), key=lambda pair: (level_rank.get(pair[0][0], 99), pair[0][1].casefold())
    ):
        items = sorted(items, key=lambda item: item["word"].casefold())
        for start in range(0, len(items), words_per_video):
            chunk = items[start : start + words_per_video]
            counters[level] += 1
            video_id = f"{level}-{counters[level]:04d}"
            groups.append({
                "video_id": video_id,
                "level": level,
                "topic": topic,
                "status": "pending",
                "items": [
                    {"order": index, "word": item["word"], "pos": item.get("pos", ""),
                     "sentence": "", "translation": "", "search_term": item["word"]}
                    for index, item in enumerate(chunk, start=1)
                ],
            })
    return groups


def curriculum_to_import_text(groups: Iterable[dict]) -> str:
    lines = []
    for group in groups:
        for item in group["items"]:
            lines.append(" | ".join([
                group["video_id"], group["level"], group["topic"], str(item["order"]),
                item["word"], item.get("sentence", ""), item.get("translation", ""),
                item.get("search_term", item["word"]),
            ]))
    return "\n".join(lines)


def save_curriculum(groups: Iterable[dict], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(groups), ensure_ascii=False, indent=2), encoding="utf-8")
    return path
