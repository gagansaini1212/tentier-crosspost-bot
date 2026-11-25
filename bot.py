import json
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from publishers import get_configured_platforms, publish_all

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
ACCOUNTS_PATH = BASE_DIR / "user_accounts.json"
MEDIA_DIR = Path(tempfile.gettempdir()) / "telegram_crosspost_media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class DraftPost:
    user_id: int
    text: str
    image_path: Path | None
    selected: set[str]


DRAFTS: dict[str, DraftPost] = {}
PENDING_CONNECT: dict[int, str] = {}

CONNECT_PROMPTS: dict[str, str] = {
    "telegram": (
        "Telegram Channel:\n"
        "Send your channel @username or numeric ID (e.g. @mychannel).\n"
        "(Make sure this bot is added as Admin in your channel.)"
    ),
    "discord": (
        "Discord:\n"
        "Send your Discord Webhook URL (https://discord.com/api/webhooks/...)."
    ),
    "x": (
        "X (Twitter):\n"
        "Send your 4 X API credentials separated by | :\n"
        "API_KEY | API_SECRET | ACCESS_TOKEN | ACCESS_TOKEN_SECRET"
    ),
    "bluesky": (
        "Bluesky:\n"
        "Send your handle and App Password separated by | :\n"
        "yourhandle.bsky.social | xxxx-xxxx-xxxx-xxxx"
    ),
    "mastodon": (
        "Mastodon:\n"
        "Send your instance URL and access token separated by | :\n"
        "https://mastodon.social | your_access_token"
    ),
    "linkedin": (
        "LinkedIn:\n"
        "Send your Access Token and Author URN separated by | :\n"
        "ACCESS_TOKEN | urn:li:person:xxxx"
    ),
    "facebook": (
        "Facebook Page:\n"
        "Send your Page ID and Meta Access Token separated by | :\n"
        "PAGE_ID | META_ACCESS_TOKEN"
    ),
    "instagram": (
        "Instagram Business:\n"
        "Send 4 values separated by | :\n"
        "IG_USER_ID | META_ACCESS_TOKEN | CLOUDINARY_CLOUD_NAME | CLOUDINARY_UPLOAD_PRESET"
    ),
}

PLATFORM_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "telegram": ("TELEGRAM_CHANNEL_ID",),
    "discord": ("DISCORD_WEBHOOK_URL",),
    "x": ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"),
    "bluesky": ("BLUESKY_HANDLE", "BLUESKY_APP_PASSWORD"),
    "mastodon": ("MASTODON_BASE_URL", "MASTODON_ACCESS_TOKEN"),
    "linkedin": ("LINKEDIN_ACCESS_TOKEN", "LINKEDIN_AUTHOR_URN"),
    "facebook": ("FB_PAGE_ID", "META_ACCESS_TOKEN"),
    "instagram": (
        "IG_USER_ID",
        "META_ACCESS_TOKEN",
        "CLOUDINARY_CLOUD_NAME",
        "CLOUDINARY_UPLOAD_PRESET",
    ),
}


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return env


def load_admin_ids(env: dict[str, str]) -> set[int]:
    raw = env.get("ADMIN_USER_IDS", "")
    return {int(p.strip()) for p in raw.split(",") if p.strip().isdigit()}


def _load_all_accounts() -> dict[str, dict[str, str]]:
    if not ACCOUNTS_PATH.exists():
        return {}
    try:
        data = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_all_accounts(data: dict[str, dict[str, str]]) -> None:
    ACCOUNTS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(ACCOUNTS_PATH, 0o600)
    except OSError:
        pass


def get_user_env(user_id: int) -> dict[str, str]:
    """Return effective credentials for a user (their saved accounts, plus .env defaults for admins)."""
    base_env = load_env()
    admin_ids = load_admin_ids(base_env)
    user_saved = _load_all_accounts().get(str(user_id), {})

    if user_id in admin_ids:
        merged = dict(base_env)
        merged.update(user_saved)
        return merged

    return dict(user_saved)


def save_user_platform(user_id: int, platform: str, raw_input: str) -> tuple[bool, str]:
    """Parse and save credentials for a user's platform."""
    keys = PLATFORM_ENV_KEYS.get(platform)
    if not keys:
        return False, "Unknown platform."

    parts = [p.strip() for p in raw_input.split("|")]
    if len(keys) == 1:
        val = raw_input.strip()
        if not val:
            return False, "Value cannot be empty."
        if platform == "discord" and not val.startswith(("http://", "https://")):
            return False, "Discord webhook must start with https://"
        values = [val]
    else:
        if len(parts) != len(keys) or any(not p for p in parts):
            return (
                False,
                f"Expected {len(keys)} values separated by | .\n\n{CONNECT_PROMPTS[platform]}",
            )
        values = parts

    all_accounts = _load_all_accounts()
    user_cfg = all_accounts.setdefault(str(user_id), {})
    for k, v in zip(keys, values):
        user_cfg[k] = v
    _save_all_accounts(all_accounts)
    return True, f"Connected {platform.title()}! Send /platforms to view or send a post to publish."


def remove_user_platform(user_id: int, platform: str) -> bool:
    keys = PLATFORM_ENV_KEYS.get(platform)
    if not keys:
        return False
    all_accounts = _load_all_accounts()
    user_cfg = all_accounts.get(str(user_id), {})
    removed = False
    for k in keys:
        if k in user_cfg:
            del user_cfg[k]
            removed = True
    if removed:
        _save_all_accounts(all_accounts)
    return removed


def connect_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Telegram Channel", callback_data="cfg:add:telegram"),
                InlineKeyboardButton("Discord", callback_data="cfg:add:discord"),
            ],
            [
                InlineKeyboardButton("X (Twitter)", callback_data="cfg:add:x"),
                InlineKeyboardButton("Bluesky", callback_data="cfg:add:bluesky"),
            ],
            [
                InlineKeyboardButton("Mastodon", callback_data="cfg:add:mastodon"),
                InlineKeyboardButton("LinkedIn", callback_data="cfg:add:linkedin"),
            ],
            [
                InlineKeyboardButton("Facebook Page", callback_data="cfg:add:facebook"),
                InlineKeyboardButton("Instagram", callback_data="cfg:add:instagram"),
            ],
            [InlineKeyboardButton("Cancel", callback_data="cfg:cancel")],
        ]
    )


def build_draft_keyboard(
    draft_id: str, configured: dict[str, str], selected: set[str]
) -> InlineKeyboardMarkup:
    rows = []
    current_row = []
    for key, label in configured.items():
        mark = "✓" if key in selected else " "
        current_row.append(
            InlineKeyboardButton(
                f"[{mark}] {label}",
                callback_data=f"cp:toggle:{draft_id}:{key}",
            )
        )
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)

    rows.append(
        [
            InlineKeyboardButton("Publish Now", callback_data=f"cp:go:{draft_id}"),
            InlineKeyboardButton("Cancel", callback_data=f"cp:cancel:{draft_id}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    user_env = get_user_env(update.effective_user.id)
    configured = get_configured_platforms(user_env)
    plist = ", ".join(configured.values()) if configured else "None yet"
    await update.message.reply_text(
        "Multi-Platform Cross-Poster Bot\n\n"
        "1. Use /connect to add your social media accounts.\n"
        "2. Send any photo + caption (or text) here.\n"
        "3. Select your platforms and tap Publish Now.\n\n"
        f"Your connected platforms: {plist}\n"
        "Commands: /connect, /disconnect, /platforms, /myid"
    )


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.effective_user:
        await update.message.reply_text(
            f"Your Telegram User ID: {update.effective_user.id}"
        )


async def connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat or update.effective_chat.type != "private":
        return
    await update.message.reply_text(
        "Choose a platform to connect to your account:",
        reply_markup=connect_keyboard(),
    )


async def disconnect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    all_accounts = _load_all_accounts()
    user_cfg = all_accounts.get(str(user_id), {})
    configured = get_configured_platforms(user_cfg)
    if not configured:
        await update.message.reply_text("You have no saved platforms to disconnect.")
        return

    rows = [
        [InlineKeyboardButton(f"Disconnect {label}", callback_data=f"cfg:del:{key}")]
        for key, label in configured.items()
    ]
    rows.append([InlineKeyboardButton("Cancel", callback_data="cfg:cancel")])
    await update.message.reply_text(
        "Tap a platform to disconnect:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def platforms_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    user_env = get_user_env(update.effective_user.id)
    configured = get_configured_platforms(user_env)
    if not configured:
        await update.message.reply_text(
            "You haven't connected any platforms yet. Choose one below:",
            reply_markup=connect_keyboard(),
        )
        return
    lines = [f"- {name}" for name in configured.values()]
    await update.message.reply_text(
        "Your connected platforms:\n" + "\n".join(lines) + "\n\nUse /connect to add more."
    )


async def handle_cfg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not update.effective_user:
        return

    user_id = update.effective_user.id
    parts = query.data.split(":")
    action = parts[1]

    if action == "cancel":
        PENDING_CONNECT.pop(user_id, None)
        await query.answer("Cancelled")
        await query.edit_message_text("Cancelled.")
        return

    if action == "add":
        platform = parts[2]
        PENDING_CONNECT[user_id] = platform
        await query.answer()
        await query.edit_message_text(
            CONNECT_PROMPTS[platform] + "\n\n(Send the details now, or /connect to cancel.)"
        )
        return

    if action == "del":
        platform = parts[2]
        remove_user_platform(user_id, platform)
        await query.answer("Disconnected")
        await query.edit_message_text(f"Disconnected {platform.title()}.")
        return


async def handle_post_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat or chat.type != "private":
        return

    # If user is currently answering a /connect prompt
    if user.id in PENDING_CONNECT and msg.text:
        platform = PENDING_CONNECT.pop(user.id)
        _, reply = save_user_platform(user.id, platform, msg.text)
        await msg.reply_text(reply)
        return

    user_env = get_user_env(user.id)
    configured = get_configured_platforms(user_env)
    if not configured:
        await msg.reply_text(
            "You haven't connected any social platforms yet. Tap a platform to connect:",
            reply_markup=connect_keyboard(),
        )
        return

    text = (msg.caption or msg.text or "").strip()
    image_path: Path | None = None
    if msg.photo:
        photo = msg.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        image_path = MEDIA_DIR / f"{uuid.uuid4().hex}.jpg"
        await tg_file.download_to_drive(custom_path=image_path)

    if not text and not image_path:
        return

    draft_id = uuid.uuid4().hex[:8]
    selected = set(configured.keys())
    DRAFTS[draft_id] = DraftPost(
        user_id=user.id,
        text=text,
        image_path=image_path,
        selected=selected,
    )

    preview = text[:120] + ("..." if len(text) > 120 else "")
    kind = "Image + Caption" if image_path else "Text"
    await msg.reply_text(
        f"Ready to cross-post ({kind}):\n\"{preview}\"\n\nSelect platforms:",
        reply_markup=build_draft_keyboard(draft_id, configured, selected),
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    parts = query.data.split(":")
    action = parts[1]
    draft_id = parts[2]
    draft = DRAFTS.get(draft_id)

    if not draft:
        await query.answer("Draft expired.", show_alert=True)
        return

    if not update.effective_user or update.effective_user.id != draft.user_id:
        await query.answer("Not your draft.", show_alert=True)
        return

    user_env = get_user_env(draft.user_id)
    configured = get_configured_platforms(user_env)

    if action == "toggle":
        key = parts[3]
        if key in draft.selected:
            draft.selected.remove(key)
        else:
            draft.selected.add(key)
        await query.answer()
        await query.edit_message_reply_markup(
            reply_markup=build_draft_keyboard(draft_id, configured, draft.selected)
        )
        return

    if action == "cancel":
        DRAFTS.pop(draft_id, None)
        if draft.image_path and draft.image_path.exists():
            draft.image_path.unlink(missing_ok=True)
        await query.answer("Cancelled")
        await query.edit_message_text("Cross-post cancelled.")
        return

    if action == "go":
        if not draft.selected:
            await query.answer("Select at least one platform!", show_alert=True)
            return

        await query.answer("Publishing...")
        await query.edit_message_text("Publishing to selected platforms...")

        results = await publish_all(
            context.bot, user_env, draft.selected, draft.text, draft.image_path
        )
        DRAFTS.pop(draft_id, None)
        if draft.image_path and draft.image_path.exists():
            draft.image_path.unlink(missing_ok=True)

        lines = [
            f"{'OK' if r.ok else 'FAILED'} — {r.platform}: {r.detail}"
            for r in results
        ]
        await query.edit_message_text("Publish Report:\n\n" + "\n".join(lines))


def main() -> None:
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("myid", myid_command))
    app.add_handler(CommandHandler("connect", connect_command))
    app.add_handler(CommandHandler("disconnect", disconnect_command))
    app.add_handler(CommandHandler("platforms", platforms_command))
    app.add_handler(CallbackQueryHandler(handle_cfg_callback, pattern="^cfg:"))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern="^cp:"))
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (filters.PHOTO | (filters.TEXT & ~filters.COMMAND)),
            handle_post_input,
        )
    )

    logger.info("Multi-Platform Cross-Poster bot is running (public mode)...")
    app.run_polling()


if __name__ == "__main__":
    main()
