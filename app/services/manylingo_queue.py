"""Persistent ManyLingo curriculum, generation memory and review queue."""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from uuid import uuid4

from loguru import logger

from app.models import const
from app.services import llm, state as sm, upload_post
from app.utils import utils

_LOCK = threading.RLock()
_FILE_NAME = "automation.json"
_ACTIVE_GENERATION = {"queued", "generating"}
_REVIEWABLE = {"review", "approved"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store_path() -> str:
    return os.path.join(utils.storage_dir("manylingo", create=True), _FILE_NAME)


def _empty_store() -> dict:
    return {
        "version": 2,
        "settings": {
            "review_before_publish": True,
            "translation_language": "Spanish",
            "social_language": "Spanish",
        },
        "vocabulary": [],
        "jobs": [],
    }


def _load_unlocked() -> dict:
    path = _store_path()
    if not os.path.exists(path):
        return _empty_store()
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error(f"failed to read ManyLingo automation store: {exc}")
        return _empty_store()
    if not isinstance(data, dict):
        return _empty_store()
    data["version"] = max(2, int(data.get("version", 1) or 1))
    data.setdefault("settings", {})
    data["settings"].setdefault("review_before_publish", True)
    data["settings"].setdefault("translation_language", "Spanish")
    data["settings"].setdefault("social_language", "Spanish")
    data.setdefault("vocabulary", [])
    data.setdefault("jobs", [])
    return data


def _save_unlocked(data: dict) -> None:
    path = _store_path()
    temp = f"{path}.tmp"
    with open(temp, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def get_store() -> dict:
    with _LOCK:
        return json.loads(json.dumps(_load_unlocked(), ensure_ascii=False))


def get_settings() -> dict:
    return dict(get_store().get("settings") or {})


def set_settings(**updates) -> dict:
    allowed = {"review_before_publish", "translation_language", "social_language"}
    with _LOCK:
        data = _load_unlocked()
        settings = data.setdefault("settings", {})
        for key, value in updates.items():
            if key in allowed:
                settings[key] = value
        _save_unlocked(data)
        return dict(settings)


def _parse_vocab_line(raw_line: str, default_level: str, default_topic: str):
    line = str(raw_line or "").strip()
    if not line or line.startswith("#"):
        return None
    parts = [part.strip() for part in (line.split("|") if "|" in line else line.split("\t"))]
    word = parts[0] if parts else ""
    if not word:
        return None
    level = (parts[1] if len(parts) > 1 else default_level).upper() or "A1"
    topic = (parts[2] if len(parts) > 2 else default_topic) or "General"
    return word, level, topic


def import_vocabulary(raw_text: str, *, default_level="A1", default_topic="General") -> dict:
    parsed = [row for row in (_parse_vocab_line(line, default_level, default_topic) for line in str(raw_text or "").splitlines()) if row]
    if not parsed:
        raise ValueError("Adicione pelo menos uma palavra ao banco de vocabulário.")
    with _LOCK:
        data = _load_unlocked()
        vocabulary = data.setdefault("vocabulary", [])
        by_key = {str(item.get("word", "")).casefold(): item for item in vocabulary}
        added = updated = 0
        for word, level, topic in parsed:
            existing = by_key.get(word.casefold())
            if existing is None:
                existing = {
                    "id": str(uuid4()), "word": word, "level": level, "topic": topic,
                    "group_id": None, "group_order": None, "sentence": "", "translation": "",
                    "search_term": "", "times_used": 0, "last_used": None, "created_at": _now(),
                }
                vocabulary.append(existing)
                by_key[word.casefold()] = existing
                added += 1
            else:
                changed = existing.get("level") != level or existing.get("topic") != topic
                existing["level"], existing["topic"] = level, topic
                updated += int(changed)
        _save_unlocked(data)
        return {"added": added, "updated": updated, "total": len(vocabulary)}


def import_preplanned_curriculum(raw_text: str) -> dict:
    """Import fixed video groups with no runtime LLM requirement.

    Format: video_id | level | topic | order | word | sentence | translation | search_term
    One row represents one vocabulary item. Five rows with the same video_id form a 5-word video.
    """
    rows = []
    for number, raw in enumerate(str(raw_text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|", 7)]
        if len(parts) != 8:
            raise ValueError(f"Linha {number}: use video_id | nível | tema | ordem | palavra | frase | tradução | termo visual")
        video_id, level, topic, order, word, sentence, translation, search_term = parts
        if not all((video_id, level, topic, word, sentence, translation, search_term)):
            raise ValueError(f"Linha {number}: nenhum campo principal pode ficar vazio.")
        try:
            order_value = int(order)
        except ValueError as exc:
            raise ValueError(f"Linha {number}: ordem precisa ser um número inteiro.") from exc
        rows.append((video_id, level.upper(), topic, order_value, word, sentence, translation, search_term))
    if not rows:
        raise ValueError("Adicione pelo menos uma linha do currículo pré-planejado.")

    with _LOCK:
        data = _load_unlocked()
        vocabulary = data.setdefault("vocabulary", [])
        by_key = {str(item.get("word", "")).casefold(): item for item in vocabulary}
        added = updated = 0
        for video_id, level, topic, order, word, sentence, translation, search_term in rows:
            item = by_key.get(word.casefold())
            if item is None:
                item = {"id": str(uuid4()), "word": word, "times_used": 0, "last_used": None, "created_at": _now()}
                vocabulary.append(item)
                by_key[word.casefold()] = item
                added += 1
            else:
                updated += 1
            item.update({
                "level": level, "topic": topic, "group_id": video_id, "group_order": order,
                "sentence": sentence, "translation": translation, "search_term": search_term,
            })
        _save_unlocked(data)
    groups = len({row[0] for row in rows})
    return {"added": added, "updated": updated, "total": len(vocabulary), "groups": groups, "rows": len(rows)}


def vocabulary_stats() -> dict:
    vocabulary = list(get_store().get("vocabulary") or [])
    levels, unused, preplanned = {}, 0, 0
    group_ids = set()
    for item in vocabulary:
        level = str(item.get("level") or "A1")
        levels[level] = levels.get(level, 0) + 1
        unused += int(int(item.get("times_used", 0) or 0) == 0)
        if item.get("group_id") and item.get("sentence") and item.get("translation"):
            preplanned += 1
            group_ids.add(str(item["group_id"]))
    return {"total": len(vocabulary), "unused": unused, "levels": levels, "preplanned": preplanned, "preplanned_groups": len(group_ids)}


def available_levels() -> list[str]:
    return sorted(vocabulary_stats()["levels"].keys()) or ["A1"]


def plan_word_groups(*, level: str, video_count: int, words_per_video: int) -> list[dict]:
    """Return fixed curriculum groups first; legacy vocabulary falls back to deterministic grouping."""
    video_count = max(1, min(100, int(video_count)))
    words_per_video = max(1, min(20, int(words_per_video)))
    level_key = str(level or "A1").strip().upper()
    vocabulary = [dict(item) for item in get_store().get("vocabulary", []) if str(item.get("level") or "").upper() == level_key]
    if not vocabulary:
        raise ValueError(f"Não há palavras cadastradas no nível {level_key}.")

    fixed = {}
    for item in vocabulary:
        group_id = str(item.get("group_id") or "").strip()
        if group_id and item.get("sentence") and item.get("translation"):
            fixed.setdefault(group_id, []).append(item)
    fixed_groups = []
    for group_id, items in fixed.items():
        items.sort(key=lambda item: (int(item.get("group_order") or 0), str(item.get("word") or "").casefold()))
        fixed_groups.append({
            "group_id": group_id,
            "level": level_key,
            "topic": str(items[0].get("topic") or "General"),
            "words": [str(item.get("word") or "") for item in items],
            "vocabulary_ids": [item["id"] for item in items],
            "items": [{"word": item["word"], "sentence": item["sentence"], "translation": item["translation"], "search_term": item.get("search_term") or item["word"]} for item in items],
            "times_used": max(int(item.get("times_used", 0) or 0) for item in items),
        })
    fixed_groups.sort(key=lambda group: (group["times_used"], group["group_id"]))
    if fixed_groups:
        return fixed_groups[:video_count]

    vocabulary.sort(key=lambda item: (int(item.get("times_used", 0) or 0), str(item.get("topic") or "General").casefold(), str(item.get("word") or "").casefold()))
    remaining, groups = list(vocabulary), []
    while remaining and len(groups) < video_count:
        topic = str(remaining[0].get("topic") or "General")
        same = [item for item in remaining if str(item.get("topic") or "General") == topic]
        chosen = same[:words_per_video]
        if len(chosen) < words_per_video:
            ids = {item["id"] for item in chosen}
            chosen += [item for item in remaining if item["id"] not in ids][: words_per_video - len(chosen)]
        ids = {item["id"] for item in chosen}
        remaining = [item for item in remaining if item["id"] not in ids]
        groups.append({"level": level_key, "topic": topic, "words": [item["word"] for item in chosen], "vocabulary_ids": list(ids), "items": []})
    return groups


def create_job(*, task_id: str, group: dict, items: list[dict], subject: str, narration: str) -> dict:
    now = _now()
    job = {"id": str(uuid4()), "task_id": task_id, "group_id": group.get("group_id"), "level": str(group.get("level") or "A1"), "topic": str(group.get("topic") or "General"), "words": list(group.get("words") or []), "vocabulary_ids": list(group.get("vocabulary_ids") or []), "items": items, "subject": subject, "narration": narration, "status": "queued", "video_paths": [], "error": None, "publish_results": None, "created_at": now, "updated_at": now}
    with _LOCK:
        data = _load_unlocked()
        ids = set(job["vocabulary_ids"])
        for item in data.get("vocabulary", []):
            if item.get("id") in ids:
                item["times_used"] = int(item.get("times_used", 0) or 0) + 1
                item["last_used"] = now
        data.setdefault("jobs", []).append(job)
        _save_unlocked(data)
    return dict(job)


def list_jobs(*, limit=50) -> list[dict]:
    jobs = list(get_store().get("jobs") or [])
    jobs.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return jobs[:max(1, int(limit))]


def _discover_video_paths(task_id: str) -> list[str]:
    directory = utils.task_dir(task_id)
    if not os.path.isdir(directory):
        return []
    return sorted(os.path.join(directory, name) for name in os.listdir(directory) if re.fullmatch(r"final-\d+\.mp4", name))


def refresh_jobs() -> list[dict]:
    with _LOCK:
        data = _load_unlocked()
        changed = False
        for job in data.get("jobs", []):
            if job.get("status") not in _ACTIVE_GENERATION:
                continue
            try:
                task = sm.state.get_task(str(job.get("task_id") or ""))
            except Exception:
                task = None
            if task and task.get("state") == const.TASK_STATE_FAILED:
                job.update(status="failed", error=task.get("error") or "Falha na geração do vídeo.", updated_at=_now()); changed = True
            elif task and task.get("state") == const.TASK_STATE_COMPLETE:
                job.update(status="review", video_paths=list(task.get("videos") or []) or _discover_video_paths(job["task_id"]), error=None, updated_at=_now()); changed = True
            elif task and job.get("status") != "generating":
                job.update(status="generating", updated_at=_now()); changed = True
            elif not task:
                paths = _discover_video_paths(str(job.get("task_id") or ""))
                if paths:
                    job.update(status="review", video_paths=paths, updated_at=_now()); changed = True
        if changed:
            _save_unlocked(data)
        jobs = list(data.get("jobs") or [])
    jobs.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return jobs


def set_job_status(job_id: str, status: str, *, error=None) -> dict:
    allowed = {"queued", "generating", "review", "approved", "publishing", "published", "failed"}
    if status not in allowed:
        raise ValueError(f"Status inválido: {status}")
    with _LOCK:
        data = _load_unlocked()
        for job in data.get("jobs", []):
            if job.get("id") == job_id:
                job.update(status=status, error=error, updated_at=_now()); _save_unlocked(data); return dict(job)
    raise ValueError("Tarefa ManyLingo não encontrada.")


def _find_job(job_id: str) -> dict:
    for job in get_store().get("jobs", []):
        if job.get("id") == job_id:
            return dict(job)
    raise ValueError("Tarefa ManyLingo não encontrada.")


def _write_publish_result(job_id: str, status: str, results, error=None) -> None:
    with _LOCK:
        data = _load_unlocked()
        for job in data.get("jobs", []):
            if job.get("id") == job_id:
                job.update(status=status, publish_results=results, error=error, updated_at=_now()); _save_unlocked(data); return


def publish_job(job_id: str) -> None:
    job = _find_job(job_id)
    if job.get("status") not in _REVIEWABLE:
        raise ValueError("O vídeo precisa estar em revisão ou aprovado antes da publicação.")
    if not upload_post.upload_post_service.is_configured():
        raise ValueError("Upload-Post ainda não está configurado.")
    paths = list(job.get("video_paths") or []) or _discover_video_paths(job["task_id"])
    if not paths:
        raise ValueError("Nenhum vídeo final foi encontrado para publicar.")
    _write_publish_result(job_id, "publishing", None)
    try:
        platforms = list(upload_post.upload_post_service.platforms or [])
        metadata = llm.generate_social_metadata(video_subject=str(job.get("subject") or "ManyLingo English vocabulary"), video_script=str(job.get("narration") or ""), language=str(get_settings().get("social_language") or "Spanish"), platform="youtube_shorts" if any(str(p).startswith("youtube") for p in platforms) else "tiktok")
        caption = metadata.get("caption") or metadata.get("title") or "Aprende inglés con ManyLingo. manylingo.com"
        youtube_extra = None
        if any(str(p).startswith("youtube") for p in platforms):
            youtube_extra = {"youtube_title": metadata.get("title") or "ManyLingo English vocabulary", "youtube_description": metadata.get("caption") or caption, "tags": metadata.get("hashtags") or [], "privacyStatus": upload_post.upload_post_service.youtube_privacy_status, "containsSyntheticMedia": True}
        results = [upload_post.cross_post_video(video_path=path, title=caption, platforms=platforms, youtube_extra=youtube_extra) for path in paths]
        failures = [result for result in results if not result.get("success")]
        if failures:
            _write_publish_result(job_id, "failed", results, "; ".join(str(r.get("error") or r.get("message") or "erro desconhecido") for r in failures))
        else:
            _write_publish_result(job_id, "published", results)
    except Exception as exc:
        logger.exception(f"ManyLingo manual publish failed, job_id={job_id}: {exc}")
        _write_publish_result(job_id, "failed", None, str(exc))


def publish_job_async(job_id: str) -> None:
    set_job_status(job_id, "approved")
    threading.Thread(target=publish_job, args=(job_id,), daemon=True, name=f"manylingo-publish-{job_id[:8]}").start()
