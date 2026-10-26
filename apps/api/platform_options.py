"""Validate and translate per-platform publish options for the Agent API.

The composer stores provider-facing knobs in ``PlatformPost.platform_extra``.
This module is the single bridge between that storage shape and the typed
``platform_options`` field exposed on REST/MCP create + update requests and on
``PlatformPostSummary`` responses.
"""

from __future__ import annotations

from typing import Any, Literal

from providers import PROVIDER_REGISTRY
from providers.tiktok import VALID_PRIVACY_LEVELS
from providers.types import PostType

# API-facing post_type values per platform (subset of what the publisher accepts).
InstagramPostType = Literal["feed", "reel", "story"]
FacebookPostType = Literal["feed", "reel", "video"]

# Fields the API accepts on ``PlatformOptionsEntry`` (excluding social_account_id).
_API_OPTION_FIELDS = frozenset(
    {
        "post_type",
        "is_aigc",
        "privacy_level",
        "allow_comment",
        "allow_duet",
        "allow_stitch",
        "brand_organic",
        "brand_content",
        "video_cover_timestamp_ms",
    }
)

_PLATFORM_FIELDS: dict[str, frozenset[str]] = {
    "instagram": frozenset({"post_type"}),
    "instagram_login": frozenset({"post_type"}),
    "facebook": frozenset({"post_type"}),
    "tiktok": frozenset(
        {
            "is_aigc",
            "privacy_level",
            "allow_comment",
            "allow_duet",
            "allow_stitch",
            "brand_organic",
            "brand_content",
            "video_cover_timestamp_ms",
        }
    ),
}

_INSTAGRAM_API_POST_TYPES = frozenset({"feed", "reel", "story"})
_FACEBOOK_API_POST_TYPES = frozenset({"feed", "reel", "video"})


def supported_post_type_values(platform: str) -> list[str]:
    """``post_type`` values accepted in ``platform_options`` for ``platform``."""
    if platform in ("instagram", "instagram_login"):
        return sorted(_INSTAGRAM_API_POST_TYPES)
    if platform == "facebook":
        return sorted(_FACEBOOK_API_POST_TYPES)
    return []


def _unsupported_fields(platform: str, provided: dict[str, Any]) -> list[str]:
    allowed = _PLATFORM_FIELDS.get(platform, frozenset())
    return sorted(key for key in provided if key not in allowed)


def _validate_post_type(platform: str, post_type: str) -> str | None:
    if platform in ("instagram", "instagram_login"):
        if post_type not in _INSTAGRAM_API_POST_TYPES:
            return (
                f"post_type must be one of {sorted(_INSTAGRAM_API_POST_TYPES)} for {platform}; got {post_type!r}."
            )
        return None
    if platform == "facebook":
        if post_type not in _FACEBOOK_API_POST_TYPES:
            return f"post_type must be one of {sorted(_FACEBOOK_API_POST_TYPES)} for facebook; got {post_type!r}."
        return None
    return f"post_type is not supported for {platform}."


def validate_platform_options_entry(
    platform: str,
    options: dict[str, Any],
    *,
    media_types: set[str] | None = None,
) -> str | None:
    """Return a human-readable 422 message, or None when valid."""
    if not options:
        return None

    unsupported = _unsupported_fields(platform, options)
    if unsupported:
        return (
            f"Platform {platform} does not support platform option(s): {', '.join(unsupported)}. "
            f"Allowed: {sorted(_PLATFORM_FIELDS.get(platform, ())) or 'none'}."
        )

    post_type = options.get("post_type")
    if post_type is not None:
        err = _validate_post_type(platform, post_type)
        if err:
            return err
        if post_type == "story" and media_types is not None and not media_types:
            return "Instagram Story requires at least one image or video in media_asset_ids."

    privacy = options.get("privacy_level")
    if privacy is not None and privacy not in VALID_PRIVACY_LEVELS:
        return f"privacy_level must be one of {sorted(VALID_PRIVACY_LEVELS)}; got {privacy!r}."

    cover_ms = options.get("video_cover_timestamp_ms")
    if cover_ms is not None:
        try:
            if int(cover_ms) < 0:
                return "video_cover_timestamp_ms must be a non-negative integer."
        except (TypeError, ValueError):
            return "video_cover_timestamp_ms must be a non-negative integer."

    return None


def _storage_post_type_from_api(platform: str, api_post_type: str) -> str | None:
    """Map API post_type to platform_extra value, or None to clear the hint."""
    if api_post_type == "feed":
        return None
    if platform == "facebook" and api_post_type == "video":
        return None
    if api_post_type == "reel":
        return PostType.REEL.value
    if api_post_type == "story":
        return PostType.STORY.value
    return api_post_type


def _api_post_type_from_storage(platform: str, extra: dict[str, Any]) -> str | None:
    stored = extra.get("post_type")
    if not stored:
        return None
    if stored == PostType.REEL.value:
        return "reel"
    if stored == PostType.STORY.value:
        return "story"
    if platform == "facebook" and stored == PostType.VIDEO.value:
        return "video"
    return stored


def api_options_to_platform_extra(
    platform: str,
    options: dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge API ``platform_options`` into a ``platform_extra`` dict."""
    extra = dict(existing or {})
    if not options:
        return extra

    post_type = options.get("post_type")
    if post_type is not None:
        mapped = _storage_post_type_from_api(platform, post_type)
        if mapped is None:
            extra.pop("post_type", None)
        else:
            extra["post_type"] = mapped

    if platform == "tiktok":
        if "privacy_level" in options and options["privacy_level"] is not None:
            extra["privacy_level"] = options["privacy_level"]
        for api_field, storage_field, invert in (
            ("allow_comment", "disable_comment", True),
            ("allow_duet", "disable_duet", True),
            ("allow_stitch", "disable_stitch", True),
            ("brand_organic", "brand_organic_toggle", False),
            ("brand_content", "brand_content_toggle", False),
        ):
            if api_field in options and options[api_field] is not None:
                value = bool(options[api_field])
                extra[storage_field] = (not value) if invert else value
        if "is_aigc" in options and options["is_aigc"] is not None:
            extra["is_aigc"] = bool(options["is_aigc"])
        if "video_cover_timestamp_ms" in options and options["video_cover_timestamp_ms"] is not None:
            extra["video_cover_timestamp_ms"] = int(options["video_cover_timestamp_ms"])

    return extra


def platform_extra_to_api_options(platform: str, extra: dict[str, Any] | None) -> dict[str, Any]:
    """Extract API-shaped ``platform_options`` from stored ``platform_extra``."""
    if not extra:
        return {}
    out: dict[str, Any] = {}
    api_pt = _api_post_type_from_storage(platform, extra)
    if api_pt is not None:
        out["post_type"] = api_pt
    if platform == "tiktok":
        if "privacy_level" in extra:
            out["privacy_level"] = extra["privacy_level"]
        if "disable_comment" in extra:
            out["allow_comment"] = not bool(extra["disable_comment"])
        if "disable_duet" in extra:
            out["allow_duet"] = not bool(extra["disable_duet"])
        if "disable_stitch" in extra:
            out["allow_stitch"] = not bool(extra["disable_stitch"])
        if "brand_organic_toggle" in extra:
            out["brand_organic"] = bool(extra["brand_organic_toggle"])
        if "brand_content_toggle" in extra:
            out["brand_content"] = bool(extra["brand_content_toggle"])
        if "is_aigc" in extra:
            out["is_aigc"] = bool(extra["is_aigc"])
        if "video_cover_timestamp_ms" in extra:
            out["video_cover_timestamp_ms"] = extra["video_cover_timestamp_ms"]
    return out


def build_platform_extra_from_entries(
    *,
    platform: str,
    target_account_id: Any,
    entries: list[Any],
    media_types: set[str],
) -> tuple[dict, str | None]:
    """Validate a list of ``PlatformOptionsEntry`` and return ``(platform_extra, error)``."""
    extra: dict = {}
    for entry in entries:
        if entry.social_account_id != target_account_id:
            return (
                {},
                (
                    f"platform_options[*].social_account_id must match the post's "
                    f"social_account_id ({target_account_id}); got {entry.social_account_id}."
                ),
            )
        options = entry_to_options_dict(entry)
        built, err = validate_and_build_extra_for_account(
            platform,
            options,
            existing=extra,
            media_types=media_types,
        )
        if err:
            return {}, err
        extra = built
    return extra, None


def validate_and_build_extra_for_account(
    platform: str,
    options: dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
    media_types: set[str] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Validate options and return ``(platform_extra, error_message)``."""
    err = validate_platform_options_entry(platform, options, media_types=media_types)
    if err:
        return existing or {}, err
    return api_options_to_platform_extra(platform, options, existing=existing), None


def entry_to_options_dict(entry: Any) -> dict[str, Any]:
    """Collect non-null option fields from a Pydantic ``PlatformOptionsEntry``."""
    raw = entry.model_dump(exclude={"social_account_id"}, exclude_none=True)
    return {k: v for k, v in raw.items() if k in _API_OPTION_FIELDS}


def provider_supported_post_types(platform: str) -> list[str]:
    """Values from the provider's ``supported_post_types`` for capabilities."""
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is None:
        return []
    return sorted(pt.value for pt in provider_cls().supported_post_types)
