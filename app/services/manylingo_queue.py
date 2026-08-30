"""Persistent vocabulary memory and review queue for ManyLingo automation.

This module intentionally stays independent from MoneyPrinterTurbo's transient task state.
The normal WebUI task manager may use in-memory state, while ManyLingo needs to remember
which vocabulary was already used across restarts. Data is stored as a small JSON document
under ``storage/manylingo`` and can later be replaced by Supabase without changing the UI
workflow.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from uuid import uuid4

from loguru import logger

from app.models import const
from app.services import state as sm
from app.services import upload_post
from app.services import llm
from app.utils import utils

_LOCK = threading.RLock()
_FILE_NAME = "automation.json"
_ACTIVE_GENERATION = {"queued", "generating"}
_REVIEWABLE = {"review", "approved"}
_FINAL = {"published", "failed"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store_path() -> str:
    directory = utils.storage_dir("manylingo", create=True)
    return os.path.join(directory, _FILE_NAME)


def _empty_store() -> dict:
    return {
        "version": 1,
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
    data.setdefault("version", 1)
    data.setdefault("settings", {})
    data["settings"].setdefault("review_before_publish", True)
    data["settings"].setdefault("translation_language", "Spanish")
    data["settings"].setdefault("social_language", "Spanish")
    data.setdefault("vocabulary", [])
    data.setdefault("jobs", [])
    return data


def _save_unlocked(data: dict) -> None:
    path = _store_path()
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


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

    if "|" in line:
        parts = [part.strip() for part in line.split("|")]
    elif "\t" in line:
        parts = [part.strip() for part in line.split("\t")]
    else:
        parts = [line]

    word = parts[0].strip()
    if not word:
        return None
    level = (parts[1] if len(parts) > 1 else default_level).strip().upper() or "A1"
    topic = (parts[2] if len(parts) > 2 else default_topic).strip() or "General"
    return word, level, topic


def import_vocabulary(
    raw_text: str,
    *,
    default_level: str = "A1",
    default_topic: str = "General",
) -> dict:
    """Import one-time vocabulary memory.

    Accepted rows are either just ``word`` or ``word | level | topic``. Existing words are
    not duplicated; explicit level/topic values update their metadata without resetting usage.
    """
    parsed = []
    for raw_line in str(raw_text or "").splitlines():
        row = _parse_vocab_line(raw_line, default_level, default_topic)
        if row:
            parsed.append(row)

    if not parsed:
        raise ValueError("Adicione pelo menos uma palavra ao banco de vocabulário.")

    with _LOCK:
        data = _load_unlocked()
        vocabulary = data.setdefault("vocabulary", [])
        by_key = {str(item.get("word", "")).casefold(): item for item in vocabulary}
        added = 0
        updated = 0
        for word, level, topic in parsed:
            key = word.casefold()
            existing = by_key.get(key)
            if existing is None:
                item = {
                    "id": str(uuid4()),
                    "word": word,
                    "level": level,
                    "topic": topic,
                    "times_used": 0,
                    "last_used": None,
                    "created_at": _now(),
                }
                vocabulary.append(item)
                by_key[key] = item
                added += 1
            else:
                changed = False
                if level and existing.get("level") != level:
                    existing["level"] = level
                    changed = True
                if topic and existing.get("topic") != topic:
                    existing["topic"] = topic
                    changed = True
                if changed:
                    updated += 1
        _save_unlocked(data)
    return {"added": added, "updated": updated, "total": len(vocabulary)}


def vocabulary_stats() -> dict:
    data = get_store()
    vocabulary = list(data.get("vocabulary") or [])
    levels = {}
    unused = 0
    for item in vocabulary:
        level = str(item.get("level") or "A1")
        levels[level] = levels.get(level, 0) + 1
        if int(item.get("times_used", 0) or 0) == 0:
            unused += 1
    return {"total": len(vocabulary), "unused": unused, "levels": levels}


def available_levels() -> list[str]:
    levels = sorted(vocabulary_stats()["levels"].keys())
    return levels or ["A1"]


def plan_word_groups(
    *,
    level: str,
    video_count: int,
    words_per_video: int,
) -> list[dict]:
    """Choose the least-used vocabulary, preferring same-topic groups.

    The planner does not permanently reserve anything until ``create_job`` is called. This
    means an LLM failure does not incorrectly count vocabulary as used.
    """
    video_count = max(1, min(100, int(video_count)))
    words_per_video = max(1, min(20, int(words_per_video)))
    level_key = str(level or "A1").strip().upper()
    data = get_store()
    vocabulary = [
        dict(item)
        for item in data.get("vocabulary", [])
        if str(item.get("level") or "").upper() == level_key
    ]
    if not vocabulary:
        raise ValueError(f"Não há palavras cadastradas no nível {level_key}.")

    vocabulary.sort(
        key=lambda item: (
            int(item.get("times_used", 0) or 0),
            str(item.get("last_used") or ""),
            str(item.get("topic") or "General").casefold(),
            str(item.get("word") or "").casefold(),
        )
    )

    remaining = list(vocabulary)
    groups = []
    for _ in range(video_count):
        if not remaining:
            break
        anchor = remaining[0]
        topic = str(anchor.get("topic") or "General")
        same_topic = [
            item for item in remaining if str(item.get("topic") or "General") == topic
        ]
        chosen = same_topic[:words_per_video]
        if len(chosen) < words_per_video:
            chosen_ids = {item["id"] for item in chosen}
            chosen.extend(
                item
                for item in remaining
                if item["id"] not in chosen_ids
            )
            chosen = chosen[:words_per_video]

        chosen_ids = {item["id"] for item in chosen}
        remaining = [item for item in remaining if item["id"] not in chosen_ids]
        groups.append(
            {
                "level": level_key,
                "topic": topic,
                "words": [str(item.get("word") or "") for item in chosen],
                "vocabulary_ids": [item["id"] for item in chosen],
            }
        )
    return groups


def create_job(
    *,
    task_id: str,
    group: dict,
    items: list[dict],
    subject: str,
    narration: str,
) -> dict:
    now = _now()
    job = {
        "id": str(uuid4()),
        "task_id": task_id,
        "level": str(group.get("level") or "A1"),
        "topic": str(group.get("topic") or "General"),
        "words": list(group.get("words") or []),
        "vocabulary_ids": list(group.get("vocabulary_ids") or []),
        "items": items,
        "subject": subject,
        "narration": narration,
        "status": "queued",
        "video_paths": [],
        "error": None,
        "publish_results": None,
        "created_at": now,
        "updated_at": now,
    }
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


def list_jobs(*, limit: int = 50) -> list[dict]:
    jobs = list(get_store().get("jobs") or [])
    jobs.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return jobs[: max(1, int(limit))]


def _task_snapshot(task_id: str) -> dict | None:
    try:
        return sm.state.get_task(task_id)
    except Exception as exc:
        logger.warning(f"failed to read ManyLingo task state {task_id}: {exc}")
        return None


def _discover_video_paths(task_id: str) -> list[str]:
    task_dir = utils.task_dir(task_id)
    if not os.path.isdir(task_dir):
        return []
    candidates = []
    for name in os.listdir(task_dir):
        if re.fullmatch(r"final-\d+\.mp4", name):
            candidates.append(os.path.join(task_dir, name))
    return sorted(candidates)


def refresh_jobs() -> list[dict]:
    """Synchronize persistent jobs with MoneyPrinterTurbo's generation task state."""
    with _LOCK:
        data = _load_unlocked()
        changed = False
        for job in data.get("jobs", []):
            if job.get("status") not in _ACTIVE_GENERATION:
                continue
            task_id = str(job.get("task_id") or "")
            task = _task_snapshot(task_id)
            if task:
                state = task.get("state")
                if state == const.TASK_STATE_FAILED:
                    job["status"] = "failed"
                    job["error"] = task.get("error") or "Falha na geração do vídeo."
                    changed = True
                elif state == const.TASK_STATE_COMPLETE:
                    video_paths = list(task.get("videos") or []) or _discover_video_paths(task_id)
                    job["status"] = "review"
                    job["video_paths"] = video_paths
                    job["error"] = None
                    changed = True
                else:
                    if job.get("status") != "generating":
                        job["status"] = "generating"
                        changed = True
            else:
                video_paths = _discover_video_paths(task_id)
                if video_paths:
                    job["status"] = "review"
                    job["video_paths"] = video_paths
                    changed = True
            if changed:
                job["updated_at"] = _now()
        if changed:
            _save_unlocked(data)
        jobs = list(data.get("jobs") or [])
    jobs.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return jobs


def set_job_status(job_id: str, status: str, *, error: str | None = None) -> dict:
    allowed = {
        "queued",
        "generating",
        "review",
        "approved",
        "publishing",
        "published",
        "failed",
    }
    if status not in allowed:
        raise ValueError(f"Status inválido: {status}")
    with _LOCK:
        data = _load_unlocked()
        for job in data.get("jobs", []):
            if job.get("id") == job_id:
                job["status"] = status
                job["error"] = error
                job["updated_at"] = _now()
                _save_unlocked(data)
                return dict(job)
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
                job["status"] = status
                job["publish_results"] = results
                job["error"] = error
                job["updated_at"] = _now()
                _save_unlocked(data)
                return


def publish_job(job_id: str) -> None:
    """Publish an approved/reviewed job through the existing Upload-Post integration."""
    job = _find_job(job_id)
    if job.get("status") not in _REVIEWABLE:
        raise ValueError("O vídeo precisa estar em revisão ou aprovado antes da publicação.")
    if not upload_post.upload_post_service.is_configured():
        raise ValueError("Upload-Post ainda não está configurado.")

    video_paths = list(job.get("video_paths") or []) or _discover_video_paths(job["task_id"])
    if not video_paths:
        raise ValueError("Nenhum vídeo final foi encontrado para publicar.")

    _write_publish_result(job_id, "publishing", None)
    try:
        platforms = list(upload_post.upload_post_service.platforms or [])
        social_language = str(get_settings().get("social_language") or "Spanish")
        has_youtube = any(str(platform).startswith("youtube") for platform in platforms)
        platform = "youtube_shorts" if has_youtube else "tiktok"
        metadata = llm.generate_social_metadata(
            video_subject=str(job.get("subject") or "ManyLingo English vocabulary"),
            video_script=str(job.get("narration") or ""),
            language=social_language,
            platform=platform,
        )
        caption = (
            metadata.get("caption")
            or metadata.get("title")
            or "Aprende inglés con ManyLingo. manylingo.com"
        )
        youtube_extra = None
        if has_youtube:
            youtube_extra = {
                "youtube_title": metadata.get("title") or "ManyLingo English vocabulary",
                "youtube_description": metadata.get("caption") or caption,
                "tags": metadata.get("hashtags") or [],
                "privacyStatus": upload_post.upload_post_service.youtube_privacy_status,
                "containsSyntheticMedia": True,
            }

        results = []
        for path in video_paths:
            results.append(
                upload_post.cross_post_video(
                    video_path=path,
                    title=caption,
                    platforms=platforms,
                    youtube_extra=youtube_extra,
                )
            )
        failures = [result for result in results if not result.get("success")]
        if failures:
            errors = [
                str(result.get("error") or result.get("message") or "erro desconhecido")
                for result in failures
            ]
            _write_publish_result(job_id, "failed", results, "; ".join(errors))
        else:
            _write_publish_result(job_id, "published", results)
    except Exception as exc:
        logger.exception(f"ManyLingo manual publish failed, job_id={job_id}: {exc}")
        _write_publish_result(job_id, "failed", None, str(exc))


def publish_job_async(job_id: str) -> None:
    set_job_status(job_id, "approved")
    thread = threading.Thread(
        target=publish_job,
        args=(job_id,),
        daemon=True,
        name=f"manylingo-publish-{job_id[:8]}",
    )
    thread.start()
