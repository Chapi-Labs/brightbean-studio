"""Per-platform publish options on POST/PATCH /posts (platform_extra bridge)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.composer.models import PlatformPost, Post
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.composer.models import PostMedia
from apps.media_library.models import MediaAsset
from apps.publisher.engine import PublishEngine
from providers.types import PostType


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="popt@example.com",
        password="testpass123",
        name="PlatformOpts",
        tos_accepted_at=timezone.now(),
    )


@pytest.fixture
def organization(db):
    from apps.organizations.models import Organization

    return Organization.objects.create(name="PO Org")


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="PO WS", organization=organization)


@pytest.fixture
def owner_memberships(db, user, organization, workspace):
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    return WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )


@pytest.fixture
def issued_key(db, user, owner_memberships, workspace, social_accounts):
    return services.issue_api_key(
        workspace=workspace,
        social_accounts=social_accounts,
        issued_by=user,
        name="popt",
        permissions=list(PERMISSION_KEYS),
    )


@pytest.fixture
def client_with_token(issued_key):
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {issued_key.plaintext_token}")


@pytest.fixture
def social_accounts(db, workspace):
    from apps.social_accounts.models import SocialAccount

    ig = SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram_login",
        account_platform_id="ig-1",
        account_name="IG",
        connection_status="connected",
    )
    tt = SocialAccount.objects.create(
        workspace=workspace,
        platform="tiktok",
        account_platform_id="tt-1",
        account_name="TT",
        connection_status="connected",
    )
    return [ig, tt]


def _post(client, body: dict):
    return client.post("/api/v1/posts/", data=json.dumps(body), content_type="application/json")


def _patch(client, post_id, body: dict):
    return client.patch(
        f"/api/v1/posts/{post_id}",
        data=json.dumps(body),
        content_type="application/json",
    )


@pytest.mark.django_db
class TestInstagramStoryPlatformOptions:
    @pytest.fixture
    def ig_account(self, social_accounts):
        return social_accounts[0]

    @pytest.fixture
    def image_asset(self, db, workspace, user, ig_account):
        asset = MediaAsset.objects.create(
            organization=workspace.organization,
            workspace=workspace,
            uploaded_by=user,
            file="test/story.jpg",
            filename="story.jpg",
            media_type=MediaAsset.MediaType.IMAGE,
            mime_type="image/jpeg",
        )
        return asset

    def test_story_persists_and_round_trips(self, client_with_token, ig_account, image_asset):
        r = _post(
            client_with_token,
            {
                "social_account_id": str(ig_account.id),
                "caption": "story time",
                "media_asset_ids": [str(image_asset.id)],
                "platform_options": [
                    {"social_account_id": str(ig_account.id), "post_type": "story"},
                ],
                "action": "draft",
            },
        )
        assert r.status_code == 201, r.content
        pp = PlatformPost.objects.get()
        assert pp.platform_extra == {"post_type": "story"}
        body = r.json()
        assert body["platform_posts"][0]["platform_options"] == {"post_type": "story"}

    def test_story_resolves_to_post_type_in_engine(self):
        assert (
            PublishEngine._resolve_post_type(
                platform="instagram_login",
                platform_extra={"post_type": "story"},
                media_count=1,
                first_media_type="image",
            )
            == PostType.STORY
        )

    def test_linkedin_post_type_rejected(self, client_with_token, db, workspace, user):
        from apps.social_accounts.models import SocialAccount

        li = SocialAccount.objects.create(
            workspace=workspace,
            platform="linkedin_personal",
            account_platform_id="li-1",
            account_name="LI",
            connection_status="connected",
        )
        key = services.issue_api_key(
            workspace=workspace,
            social_accounts=[li],
            issued_by=user,
            name="li-only",
            permissions=list(PERMISSION_KEYS),
        )
        client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")
        r = _post(
            client,
            {
                "social_account_id": str(li.id),
                "caption": "nope",
                "platform_options": [{"social_account_id": str(li.id), "post_type": "story"}],
                "action": "draft",
            },
        )
        assert r.status_code == 422
        assert "post_type" in r.json()["detail"]


@pytest.mark.django_db
class TestTikTokPlatformOptions:
    @pytest.fixture
    def tt_account(self, social_accounts):
        return social_accounts[1]

    @pytest.fixture
    def video_asset(self, db, workspace, user):
        return MediaAsset.objects.create(
            organization=workspace.organization,
            workspace=workspace,
            uploaded_by=user,
            file="test/clip.mp4",
            filename="clip.mp4",
            media_type=MediaAsset.MediaType.VIDEO,
            mime_type="video/mp4",
        )

    def test_is_aigc_maps_to_platform_extra(self, client_with_token, tt_account, video_asset):
        r = _post(
            client_with_token,
            {
                "social_account_id": str(tt_account.id),
                "caption": "ai clip",
                "media_asset_ids": [str(video_asset.id)],
                "platform_options": [
                    {
                        "social_account_id": str(tt_account.id),
                        "is_aigc": True,
                        "privacy_level": "SELF_ONLY",
                        "allow_comment": True,
                    }
                ],
                "action": "draft",
            },
        )
        assert r.status_code == 201, r.content
        pp = PlatformPost.objects.get(social_account=tt_account)
        assert pp.platform_extra["is_aigc"] is True
        assert pp.platform_extra["privacy_level"] == "SELF_ONLY"
        assert pp.platform_extra["disable_comment"] is False

    def test_patch_merges_tiktok_options(self, client_with_token, tt_account, video_asset):
        r = _post(
            client_with_token,
            {
                "social_account_id": str(tt_account.id),
                "caption": "patch me",
                "media_asset_ids": [str(video_asset.id)],
                "action": "draft",
            },
        )
        post_id = r.json()["id"]
        r2 = _patch(
            client_with_token,
            post_id,
            {
                "platform_options": [
                    {"social_account_id": str(tt_account.id), "is_aigc": True},
                ],
            },
        )
        assert r2.status_code == 200, r2.content
        pp = PlatformPost.objects.get(social_account=tt_account)
        assert pp.platform_extra["is_aigc"] is True
        assert r2.json()["platform_posts"][0]["platform_options"]["is_aigc"] is True

    def test_tiktok_publish_passes_is_aigc_to_provider(self, tt_account, video_asset, workspace, user):
        from apps.composer.models import Post

        post = Post.objects.create(workspace=workspace, author=user, caption="ai")
        pp = PlatformPost.objects.create(
            post=post,
            social_account=tt_account,
            platform_extra={"is_aigc": True, "privacy_level": "SELF_ONLY"},
        )
        PostMedia.objects.create(post=post, media_asset=video_asset, position=0)

        mock_provider = MagicMock()
        mock_provider.publish_post.return_value = MagicMock(platform_post_id="x", extra={})
        mock_provider.supported_post_types = [PostType.VIDEO]

        with patch("apps.publisher.engine.get_provider", return_value=mock_provider):
            with patch("apps.publisher.engine.download_to_path"):
                engine = PublishEngine()
                engine._dispatch_to_provider(pp)

        _token, content = mock_provider.publish_post.call_args.args
        assert content.extra.get("is_aigc") is True

    def test_post_mode_inbox_round_trips(self, client_with_token, tt_account, video_asset):
        r = _post(
            client_with_token,
            {
                "social_account_id": str(tt_account.id),
                "caption": "inbox clip",
                "media_asset_ids": [str(video_asset.id)],
                "platform_options": [
                    {"social_account_id": str(tt_account.id), "post_mode": "inbox"},
                ],
                "action": "draft",
            },
        )
        assert r.status_code == 201, r.content
        pp = PlatformPost.objects.get(social_account=tt_account)
        assert pp.platform_extra["post_mode"] == "inbox"
        assert r.json()["platform_posts"][0]["platform_options"]["post_mode"] == "inbox"

    def test_post_mode_rejects_unknown_value(self, client_with_token, tt_account, video_asset):
        r = _post(
            client_with_token,
            {
                "social_account_id": str(tt_account.id),
                "caption": "bad mode",
                "media_asset_ids": [str(video_asset.id)],
                "platform_options": [
                    {"social_account_id": str(tt_account.id), "post_mode": "drafts"},
                ],
                "action": "draft",
            },
        )
        assert r.status_code in (400, 422), r.content
        assert not PlatformPost.objects.filter(social_account=tt_account).exists()

    def test_tiktok_publish_passes_post_mode_to_provider(self, tt_account, video_asset, workspace, user):
        post = Post.objects.create(workspace=workspace, author=user, caption="inbox")
        pp = PlatformPost.objects.create(
            post=post,
            social_account=tt_account,
            platform_extra={"post_mode": "inbox"},
        )
        PostMedia.objects.create(post=post, media_asset=video_asset, position=0)

        mock_provider = MagicMock()
        mock_provider.publish_post.return_value = MagicMock(platform_post_id="x", extra={})
        mock_provider.supported_post_types = [PostType.VIDEO]

        with (
            patch("apps.publisher.engine.get_provider", return_value=mock_provider),
            patch("apps.publisher.engine.download_to_path"),
        ):
            PublishEngine()._dispatch_to_provider(pp)

        _token, content = mock_provider.publish_post.call_args.args
        assert content.extra.get("post_mode") == "inbox"
