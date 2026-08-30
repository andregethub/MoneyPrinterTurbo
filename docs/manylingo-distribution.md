# ManyLingo distribution

The ManyLingo acquisition workflow uses one curriculum bank for two output families:

- **Vertical 9:16:** TikTok, Instagram, YouTube, X and Pinterest.
- **Landscape 16:9:** YouTube only, built by combining multiple fixed vocabulary groups from the same CEFR level and semantic topic.

## Upload-Post settings

Keep credentials in the local `config.toml` only.

```toml
[app]
upload_post_enabled = true
upload_post_api_key = "YOUR_KEY"
upload_post_username = "YOUR_PROFILE"
upload_post_platforms = ["tiktok", "instagram", "youtube"]
upload_post_auto_upload = false
upload_post_youtube_privacy_status = "public"

# Required before ManyLingo adds Pinterest to vertical publishing.
upload_post_pinterest_board_id = "YOUR_BOARD_ID"
# Optional. Defaults to https://manylingo.com in the ManyLingo publisher.
upload_post_pinterest_link = "https://manylingo.com"
```

The ManyLingo review publisher automatically adds `x` to the configured vertical destinations. It adds `pinterest` only when `upload_post_pinterest_board_id` is present because Upload-Post requires a board ID for video Pins.

If Instagram is configured to share Reels to Facebook natively, Facebook does not need to be added to this API fan-out.

## Horizontal YouTube compilations

The Streamlit ManyLingo page includes a `YouTube horizontal 16:9` section. The default compilation is four fixed groups. With five words per fixed group, that creates a lesson of about 20 vocabulary items.

The planner:

1. uses only preplanned curriculum rows with saved sentence, translation and visual term;
2. preserves CEFR level;
3. keeps source groups from one semantic topic;
4. remembers source group IDs already used by landscape jobs;
5. emits a 16:9 ManyLingo generation job;
6. publishes that job to `youtube` only after review.

Vertical usage counters are intentionally not incremented by landscape compilations, so the Shorts rotation and the YouTube long-form rotation remain independent.

## Important behavior

The review workflow is the intended path while this feature is being tested. Keep `upload_post_auto_upload = false` so a generated video cannot bypass the ManyLingo-specific routing and manual review step.
