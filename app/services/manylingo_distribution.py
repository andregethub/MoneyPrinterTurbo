"""ManyLingo distribution helpers for vertical social posts and 16:9 YouTube compilations."""
from __future__ import annotations

import threading
from collections import defaultdict

from loguru import logger

from app.services import llm, manylingo_queue as ml_queue, upload_post


def vertical_platforms() -> tuple[list[str], list[str]]:
    """Return ManyLingo vertical destinations and configuration warnings.

    X is added automatically to the configured Upload-Post destinations. Pinterest
    is added when a board ID is configured because Upload-Post requires it.
    """
    service = upload_post.upload_post_service
    platforms = service.normalize_platforms(list(service.platforms or []))
    warnings = []

    if "x" not in platforms:
        platforms.append("x")

    if service.pinterest_board_id:
        if "pinterest" not in platforms:
            platforms.append("pinterest")
    else:
        warnings.append(
            "Pinterest ainda não entra na publicação: configure upload_post_pinterest_board_id."
        )

    return platforms, warnings


def _fixed_groups(level: str) -> list[dict]:
    level_key = str(level or "A1").strip().upper()
    vocabulary = [
        dict(item)
        for item in ml_queue.get_store().get("vocabulary", [])
        if str(item.get("level") or "").upper() == level_key
        and item.get("group_id")
        and item.get("sentence")
        and item.get("translation")
    ]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in vocabulary:
        grouped[str(item["group_id"])].append(item)

    groups = []
    for group_id, items in grouped.items():
        items.sort(
            key=lambda item: (
                int(item.get("group_order") or 0),
                str(item.get("word") or "").casefold(),
            )
        )
        groups.append(
            {
                "group_id": group_id,
                "level": level_key,
                "topic": str(items[0].get("topic") or "General"),
                "words": [str(item.get("word") or "") for item in items],
                "items": [
                    {
                        "word": item["word"],
                        "sentence": item["sentence"],
                        "translation": item["translation"],
                        "search_term": item.get("search_term") or item["word"],
                    }
                    for item in items
                ],
            }
        )
    groups.sort(key=lambda group: group["group_id"])
    return groups


def _used_landscape_group_ids() -> set[str]:
    used = set()
    for job in ml_queue.get_store().get("jobs", []):
        if job.get("content_format") != "landscape":
            continue
        if job.get("status") == "failed" and not job.get("video_paths"):
            continue
        used.update(str(value) for value in job.get("source_group_ids") or [])
    return used


def plan_horizontal_compilation(*, level: str, groups_per_video: int = 4) -> dict:
    """Combine fixed 5-word groups into one same-topic 16:9 YouTube lesson."""
    groups_per_video = max(2, min(12, int(groups_per_video)))
    available = [
        group
        for group in _fixed_groups(level)
        if group["group_id"] not in _used_landscape_group_ids()
    ]
    if not available:
        raise ValueError(
            f"Não há grupos pré-planejados disponíveis para um vídeo horizontal em {level}."
        )

    by_topic: dict[str, list[dict]] = defaultdict(list)
    for group in available:
        by_topic[group["topic"]].append(group)

    # Prefer a topic that can fill the requested compilation. Otherwise use the
    # topic with the largest remaining set so semantic coherence is preserved.
    candidates = sorted(
        by_topic.items(),
        key=lambda pair: (
            len(pair[1]) < groups_per_video,
            -len(pair[1]),
            pair[0].casefold(),
        ),
    )
    topic, selected = candidates[0]
    selected = selected[:groups_per_video]

    items = []
    words = []
    source_group_ids = []
    for group in selected:
        source_group_ids.append(group["group_id"])
        words.extend(group["words"])
        items.extend(group["items"])

    return {
        "group_id": f"YT-{selected[0]['level']}-{source_group_ids[0]}-{source_group_ids[-1]}",
        "level": selected[0]["level"],
        "topic": topic,
        "words": words,
        "items": items,
        "vocabulary_ids": [],
        "source_group_ids": source_group_ids,
        "content_format": "landscape",
        "publish_platforms": ["youtube"],
    }


def mark_horizontal_job(job_id: str, group: dict) -> None:
    """Attach long-form metadata to a job created by the persistent queue."""
    with ml_queue._LOCK:
        data = ml_queue._load_unlocked()
        for job in data.get("jobs", []):
            if job.get("id") != job_id:
                continue
            job["content_format"] = "landscape"
            job["publish_platforms"] = ["youtube"]
            job["source_group_ids"] = list(group.get("source_group_ids") or [])
            ml_queue._save_unlocked(data)
            return
    raise ValueError("Tarefa horizontal ManyLingo não encontrada.")


def _publish(job_id: str) -> None:
    job = ml_queue._find_job(job_id)
    if job.get("status") not in {"review", "approved"}:
        raise ValueError("O vídeo precisa estar em revisão ou aprovado antes da publicação.")
    service = upload_post.upload_post_service
    if not service.is_configured():
        raise ValueError("Upload-Post ainda não está configurado.")

    paths = list(job.get("video_paths") or []) or ml_queue._discover_video_paths(job["task_id"])
    if not paths:
        raise ValueError("Nenhum vídeo final foi encontrado para publicar.")

    is_landscape = job.get("content_format") == "landscape"
    platforms = ["youtube"] if is_landscape else vertical_platforms()[0]
    metadata_platform = "youtube" if is_landscape else "youtube_shorts"

    ml_queue._write_publish_result(job_id, "publishing", None)
    try:
        metadata = llm.generate_social_metadata(
            video_subject=str(job.get("subject") or "ManyLingo English vocabulary"),
            video_script=str(job.get("narration") or ""),
            language=str(ml_queue.get_settings().get("social_language") or "Spanish"),
            platform=metadata_platform,
        )
        caption = (
            metadata.get("caption")
            or metadata.get("title")
            or "Aprende inglés con ManyLingo. manylingo.com"
        )
        youtube_extra = None
        if "youtube" in platforms:
            youtube_extra = {
                "youtube_title": metadata.get("title") or "ManyLingo English vocabulary",
                "youtube_description": metadata.get("caption") or caption,
                "tags": metadata.get("hashtags") or [],
                "privacyStatus": service.youtube_privacy_status,
                "defaultLanguage": "es",
                "defaultAudioLanguage": "en-US",
            }

        platform_extra = {
            "x_title": caption,
            "pinterest_title": metadata.get("title") or "Aprende inglés con ManyLingo",
            "pinterest_description": caption,
            "pinterest_link": service.pinterest_link or "https://manylingo.com",
            "pinterest_alt_text": "Lección breve de vocabulario en inglés de ManyLingo",
        }

        results = [
            upload_post.cross_post_video(
                video_path=path,
                title=caption,
                platforms=platforms,
                youtube_extra=youtube_extra,
                platform_extra=platform_extra,
            )
            for path in paths
        ]
        failures = [result for result in results if not result.get("success")]
        if failures:
            ml_queue._write_publish_result(
                job_id,
                "failed",
                results,
                "; ".join(
                    str(result.get("error") or result.get("message") or "erro desconhecido")
                    for result in failures
                ),
            )
        else:
            ml_queue._write_publish_result(job_id, "published", results)
    except Exception as exc:
        logger.exception(f"ManyLingo distribution failed, job_id={job_id}: {exc}")
        ml_queue._write_publish_result(job_id, "failed", None, str(exc))


def publish_job_async(job_id: str) -> None:
    ml_queue.set_job_status(job_id, "approved")
    threading.Thread(
        target=_publish,
        args=(job_id,),
        daemon=True,
        name=f"manylingo-distribute-{job_id[:8]}",
    ).start()
