"""
Upload-Post API integration for cross-posting videos.

Supports the platform values accepted by Upload-Post's video endpoint, including
TikTok, Instagram, YouTube, X and Pinterest. Pinterest requires a board ID.

Docs: https://docs.upload-post.com
"""
import os
from typing import Optional

import requests
from loguru import logger
from app.config import config


class UploadPostService:
    API_BASE = "https://api.upload-post.com"

    @property
    def api_key(self) -> str:
        return config.app.get("upload_post_api_key", "")

    @property
    def username(self) -> str:
        return config.app.get("upload_post_username", "")

    @property
    def enabled(self) -> bool:
        return config.app.get("upload_post_enabled", False)

    @property
    def platforms(self) -> list:
        return config.app.get("upload_post_platforms", ["tiktok", "instagram"])

    @property
    def auto_upload(self) -> bool:
        return config.app.get("upload_post_auto_upload", False)

    @property
    def youtube_privacy_status(self) -> str:
        return config.app.get("upload_post_youtube_privacy_status", "public")

    @property
    def pinterest_board_id(self) -> str:
        return str(config.app.get("upload_post_pinterest_board_id", "") or "").strip()

    @property
    def pinterest_link(self) -> str:
        return str(config.app.get("upload_post_pinterest_link", "") or "").strip()

    def is_configured(self) -> bool:
        return bool(self.api_key and self.username and self.enabled)

    @staticmethod
    def normalize_platforms(platforms: list) -> list[str]:
        """Normalize aliases and remove duplicate platforms while preserving order."""
        normalized = []
        seen = set()
        for platform in platforms or []:
            value = str(platform or "").strip().lower()
            if value == "twitter":
                value = "x"
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    def upload_video(
        self,
        video_path: str,
        title: str,
        platforms: Optional[list] = None,
        privacy_level: str = "PUBLIC_TO_EVERYONE",
        youtube_extra: Optional[dict] = None,
        platform_extra: Optional[dict] = None,
    ) -> dict:
        if not self.is_configured():
            logger.warning("Upload-Post is not configured. Skipping cross-post.")
            return {"success": False, "error": "Upload-Post not configured"}

        platforms = self.normalize_platforms(platforms if platforms is not None else self.platforms)
        platform_extra = dict(platform_extra or {})

        if not platforms:
            return {"success": False, "error": "No Upload-Post platforms configured"}

        if "pinterest" in platforms:
            board_id = str(platform_extra.get("pinterest_board_id") or self.pinterest_board_id).strip()
            if not board_id:
                return {
                    "success": False,
                    "error": (
                        "Pinterest requires upload_post_pinterest_board_id. "
                        "Connect Pinterest in Upload-Post and configure the target board ID."
                    ),
                }

        if not os.path.exists(video_path):
            logger.error(f"Video file not found: {video_path}")
            return {"success": False, "error": f"Video file not found: {video_path}"}

        logger.info(f"Cross-posting video to {', '.join(platforms)} via Upload-Post...")

        try:
            with open(video_path, "rb") as video_file:
                files = {"video": video_file}
                data = [
                    ("user", self.username),
                    ("title", title[:2200]),
                    ("privacy_level", privacy_level),
                ]

                for platform in platforms:
                    data.append(("platform[]", platform))

                if youtube_extra and "youtube" in platforms:
                    if "youtube_title" in youtube_extra:
                        data.append(("youtube_title", youtube_extra["youtube_title"][:100]))
                    if "youtube_description" in youtube_extra:
                        data.append(("youtube_description", youtube_extra["youtube_description"]))
                    for tag in youtube_extra.get("tags", []):
                        data.append(("tags[]", tag))
                    data.append(("privacyStatus", youtube_extra.get("privacyStatus", self.youtube_privacy_status)))
                    data.append(("containsSyntheticMedia", "true"))
                    if youtube_extra.get("defaultLanguage"):
                        data.append(("defaultLanguage", youtube_extra["defaultLanguage"]))
                    if youtube_extra.get("defaultAudioLanguage"):
                        data.append(("defaultAudioLanguage", youtube_extra["defaultAudioLanguage"]))
                    if youtube_extra.get("youtube_playlist_id"):
                        data.append(("youtube_playlist_id", youtube_extra["youtube_playlist_id"]))

                if "x" in platforms:
                    data.append(("made_with_ai", "true"))
                    if platform_extra.get("x_title"):
                        data.append(("x_title", str(platform_extra["x_title"])[:280]))

                if "pinterest" in platforms:
                    board_id = str(platform_extra.get("pinterest_board_id") or self.pinterest_board_id).strip()
                    data.append(("pinterest_board_id", board_id))
                    data.append(("pinterest_title", str(platform_extra.get("pinterest_title") or title)[:100]))
                    description = str(platform_extra.get("pinterest_description") or title).strip()
                    if description:
                        data.append(("pinterest_description", description[:800]))
                    link = str(platform_extra.get("pinterest_link") or self.pinterest_link).strip()
                    if link:
                        data.append(("pinterest_link", link))
                    alt_text = str(platform_extra.get("pinterest_alt_text") or "").strip()
                    if alt_text:
                        data.append(("pinterest_alt_text", alt_text[:500]))

                headers = {"Authorization": f"Apikey {self.api_key}"}
                response = requests.post(
                    f"{self.API_BASE}/api/upload",
                    headers=headers,
                    data=data,
                    files=files,
                    timeout=300,
                )
                response.raise_for_status()
                result = response.json()

                if result.get("success"):
                    logger.info(f"✅ Video cross-posted successfully! Request ID: {result.get('request_id')}")
                else:
                    logger.warning(f"Cross-post failed: {result.get('message', 'Unknown error')}")
                return result

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to cross-post video: {str(e)}")
            return {"success": False, "error": str(e)}

    def check_status(self, request_id: str) -> dict:
        """Check the status of an upload request."""
        try:
            headers = {"Authorization": f"Apikey {self.api_key}"}
            response = requests.get(
                f"{self.API_BASE}/api/uploadposts/status",
                params={"request_id": request_id},
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to check status: {str(e)}")
            return {"success": False, "error": str(e)}


upload_post_service = UploadPostService()


def cross_post_video(
    video_path: str,
    title: str,
    platforms: Optional[list] = None,
    youtube_extra: Optional[dict] = None,
    platform_extra: Optional[dict] = None,
) -> dict:
    return upload_post_service.upload_video(
        video_path,
        title,
        platforms,
        youtube_extra=youtube_extra,
        platform_extra=platform_extra,
    )
