import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


@dataclass
class PublishResult:
    platform: str
    ok: bool
    detail: str


def get_configured_platforms(env: dict[str, str]) -> dict[str, str]:
    """Return {platform_key: display_name} for platforms that have credentials set."""
    configured: dict[str, str] = {}
    if env.get("TELEGRAM_CHANNEL_ID"):
        configured["telegram"] = "Telegram Channel"
    if env.get("DISCORD_WEBHOOK_URL"):
        configured["discord"] = "Discord"
    if all(
        env.get(k)
        for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
    ):
        configured["x"] = "X (Twitter)"
    if env.get("BLUESKY_HANDLE") and env.get("BLUESKY_APP_PASSWORD"):
        configured["bluesky"] = "Bluesky"
    if env.get("MASTODON_BASE_URL") and env.get("MASTODON_ACCESS_TOKEN"):
        configured["mastodon"] = "Mastodon"
    if env.get("LINKEDIN_ACCESS_TOKEN") and env.get("LINKEDIN_AUTHOR_URN"):
        configured["linkedin"] = "LinkedIn"
    if env.get("FB_PAGE_ID") and env.get("META_ACCESS_TOKEN"):
        configured["facebook"] = "Facebook Page"
    if env.get("IG_USER_ID") and env.get("META_ACCESS_TOKEN"):
        configured["instagram"] = "Instagram"
    return configured


async def publish_to_telegram(
    bot, channel_id: str, text: str, image_path: Path | None
) -> PublishResult:
    try:
        if image_path:
            with image_path.open("rb") as f:
                await bot.send_photo(chat_id=channel_id, photo=f, caption=text or None)
        else:
            await bot.send_message(chat_id=channel_id, text=text)
        return PublishResult("Telegram Channel", True, f"Posted to {channel_id}")
    except Exception as exc:
        return PublishResult("Telegram Channel", False, str(exc))


async def publish_to_discord(
    webhook_url: str, text: str, image_path: Path | None
) -> PublishResult:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            if image_path:
                with image_path.open("rb") as f:
                    files = {"file": (image_path.name, f, "image/jpeg")}
                    data = {"content": text[:2000]} if text else {}
                    resp = await client.post(webhook_url, data=data, files=files)
            else:
                resp = await client.post(webhook_url, json={"content": text[:2000]})
            resp.raise_for_status()
        return PublishResult("Discord", True, "Posted via webhook")
    except Exception as exc:
        return PublishResult("Discord", False, str(exc))


async def publish_to_x(
    env: dict[str, str], text: str, image_path: Path | None
) -> PublishResult:
    try:
        import asyncio
        import tweepy

        def _post():
            auth = tweepy.OAuth1UserHandler(
                env["X_API_KEY"],
                env["X_API_SECRET"],
                env["X_ACCESS_TOKEN"],
                env["X_ACCESS_TOKEN_SECRET"],
            )
            api_v1 = tweepy.API(auth)
            client_v2 = tweepy.Client(
                consumer_key=env["X_API_KEY"],
                consumer_secret=env["X_API_SECRET"],
                access_token=env["X_ACCESS_TOKEN"],
                access_token_secret=env["X_ACCESS_TOKEN_SECRET"],
            )
            media_ids = None
            if image_path:
                media = api_v1.media_upload(filename=str(image_path))
                media_ids = [media.media_id]
            tweet_text = (text or "")[:280]
            client_v2.create_tweet(text=tweet_text or None, media_ids=media_ids)

        await asyncio.to_thread(_post)
        return PublishResult("X (Twitter)", True, "Tweet published")
    except Exception as exc:
        return PublishResult("X (Twitter)", False, str(exc))


async def publish_to_bluesky(
    env: dict[str, str], text: str, image_path: Path | None
) -> PublishResult:
    try:
        import asyncio
        from atproto import Client

        def _post():
            client = Client()
            client.login(env["BLUESKY_HANDLE"], env["BLUESKY_APP_PASSWORD"])
            post_text = (text or "")[:300]
            if image_path:
                client.send_image(
                    text=post_text,
                    image=image_path.read_bytes(),
                    image_alt=post_text[:100] or "Image",
                )
            else:
                client.send_post(text=post_text)

        await asyncio.to_thread(_post)
        return PublishResult("Bluesky", True, "Post published")
    except Exception as exc:
        return PublishResult("Bluesky", False, str(exc))


async def publish_to_mastodon(
    env: dict[str, str], text: str, image_path: Path | None
) -> PublishResult:
    try:
        base = env["MASTODON_BASE_URL"].rstrip("/")
        headers = {"Authorization": f"Bearer {env['MASTODON_ACCESS_TOKEN']}"}
        async with httpx.AsyncClient(timeout=30) as client:
            media_ids = []
            if image_path:
                with image_path.open("rb") as f:
                    m_resp = await client.post(
                        f"{base}/api/v2/media",
                        headers=headers,
                        files={"file": (image_path.name, f, "image/jpeg")},
                    )
                m_resp.raise_for_status()
                media_ids.append(m_resp.json()["id"])

            payload: dict = {"status": (text or "")[:500]}
            if media_ids:
                payload["media_ids"] = media_ids
            resp = await client.post(
                f"{base}/api/v1/statuses", headers=headers, json=payload
            )
            resp.raise_for_status()
        return PublishResult("Mastodon", True, "Status published")
    except Exception as exc:
        return PublishResult("Mastodon", False, str(exc))


async def publish_to_linkedin(
    env: dict[str, str], text: str, image_path: Path | None
) -> PublishResult:
    try:
        token = env["LINKEDIN_ACCESS_TOKEN"]
        author = env["LINKEDIN_AUTHOR_URN"]
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Restli-Protocol-Version": "2.0.0",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            asset_urn = None
            if image_path:
                reg_payload = {
                    "registerUploadRequest": {
                        "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                        "owner": author,
                        "serviceRelationships": [
                            {
                                "relationshipType": "OWNER",
                                "identifier": "urn:li:userGeneratedContent",
                            }
                        ],
                    }
                }
                reg_resp = await client.post(
                    "https://api.linkedin.com/v2/assets?action=registerUpload",
                    headers=headers,
                    json=reg_payload,
                )
                reg_resp.raise_for_status()
                reg_data = reg_resp.json()["value"]
                upload_url = reg_data["uploadMechanism"][
                    "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"
                ]["uploadUrl"]
                asset_urn = reg_data["asset"]
                up_resp = await client.put(
                    upload_url,
                    headers={"Authorization": f"Bearer {token}"},
                    content=image_path.read_bytes(),
                )
                up_resp.raise_for_status()

            share_content: dict = {
                "shareCommentary": {"text": text or ""},
                "shareMediaCategory": "IMAGE" if asset_urn else "NONE",
            }
            if asset_urn:
                share_content["media"] = [{"status": "READY", "media": asset_urn}]

            post_payload = {
                "author": author,
                "lifecycleState": "PUBLISHED",
                "specificContent": {"com.linkedin.ugc.ShareContent": share_content},
                "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
            }
            resp = await client.post(
                "https://api.linkedin.com/v2/ugcPosts",
                headers=headers,
                json=post_payload,
            )
            resp.raise_for_status()
        return PublishResult("LinkedIn", True, "Post published")
    except Exception as exc:
        return PublishResult("LinkedIn", False, str(exc))


async def publish_to_facebook(
    env: dict[str, str], text: str, image_path: Path | None
) -> PublishResult:
    try:
        page_id = env["FB_PAGE_ID"]
        token = env["META_ACCESS_TOKEN"]
        async with httpx.AsyncClient(timeout=30) as client:
            if image_path:
                with image_path.open("rb") as f:
                    resp = await client.post(
                        f"https://graph.facebook.com/v21.0/{page_id}/photos",
                        data={"caption": text or "", "access_token": token},
                        files={"source": (image_path.name, f, "image/jpeg")},
                    )
            else:
                resp = await client.post(
                    f"https://graph.facebook.com/v21.0/{page_id}/feed",
                    data={"message": text, "access_token": token},
                )
            resp.raise_for_status()
        return PublishResult("Facebook Page", True, "Post published")
    except Exception as exc:
        return PublishResult("Facebook Page", False, str(exc))


async def publish_to_instagram(
    env: dict[str, str], text: str, image_path: Path | None
) -> PublishResult:
    if not image_path:
        return PublishResult("Instagram", False, "Instagram requires an image")
    cloud = env.get("CLOUDINARY_CLOUD_NAME")
    preset = env.get("CLOUDINARY_UPLOAD_PRESET")
    if not cloud or not preset:
        return PublishResult(
            "Instagram",
            False,
            "Set CLOUDINARY_CLOUD_NAME and CLOUDINARY_UPLOAD_PRESET for public image hosting",
        )
    try:
        ig_id = env["IG_USER_ID"]
        token = env["META_ACCESS_TOKEN"]
        async with httpx.AsyncClient(timeout=45) as client:
            with image_path.open("rb") as f:
                c_resp = await client.post(
                    f"https://api.cloudinary.com/v1_1/{cloud}/image/upload",
                    data={"upload_preset": preset},
                    files={"file": (image_path.name, f, "image/jpeg")},
                )
            c_resp.raise_for_status()
            image_url = c_resp.json()["secure_url"]

            create_resp = await client.post(
                f"https://graph.facebook.com/v21.0/{ig_id}/media",
                data={
                    "image_url": image_url,
                    "caption": text or "",
                    "access_token": token,
                },
            )
            create_resp.raise_for_status()
            creation_id = create_resp.json()["id"]

            pub_resp = await client.post(
                f"https://graph.facebook.com/v21.0/{ig_id}/media_publish",
                data={"creation_id": creation_id, "access_token": token},
            )
            pub_resp.raise_for_status()
        return PublishResult("Instagram", True, "Post published")
    except Exception as exc:
        return PublishResult("Instagram", False, str(exc))


async def publish_all(
    bot,
    env: dict[str, str],
    selected: set[str],
    text: str,
    image_path: Path | None,
) -> list[PublishResult]:
    import asyncio

    tasks = []
    if "telegram" in selected:
        tasks.append(publish_to_telegram(bot, env["TELEGRAM_CHANNEL_ID"], text, image_path))
    if "discord" in selected:
        tasks.append(publish_to_discord(env["DISCORD_WEBHOOK_URL"], text, image_path))
    if "x" in selected:
        tasks.append(publish_to_x(env, text, image_path))
    if "bluesky" in selected:
        tasks.append(publish_to_bluesky(env, text, image_path))
    if "mastodon" in selected:
        tasks.append(publish_to_mastodon(env, text, image_path))
    if "linkedin" in selected:
        tasks.append(publish_to_linkedin(env, text, image_path))
    if "facebook" in selected:
        tasks.append(publish_to_facebook(env, text, image_path))
    if "instagram" in selected:
        tasks.append(publish_to_instagram(env, text, image_path))

    if not tasks:
        return []
    return await asyncio.gather(*tasks)
