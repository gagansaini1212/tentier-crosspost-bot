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
MEDIA_DIR = Path(tempfile.gettempdir()) / "telegram_crosspost_media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class DraftPost:
    user_id: int
    text: str
    image_path: Path | None
    selected: set[str]


DRAFTS: dict[str, DraftPost] = {}


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


def is_authorized(user_id: int | None, admin_ids: set[int]) -> bool:
    if not user_id:
        return False
    return user_id in admin_ids


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    env = load_env()
    configured = get_configured_platforms(env)
    plist = ", ".join(configured.values()) if configured else "None configured yet"
    await update.message.reply_text(
        "Multi-Platform Cross-Poster Bot\n\n"
        "Send me a photo with a caption (or a text message), choose your target "
        "platforms, and tap Publish Now.\n\n"
        f"Configured platforms: {plist}\n"
        "Commands: /platforms, /myid"
    )


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.effective_user:
        await update.message.reply_text(
            f"Your Telegram User ID: {update.effective_user.id}"
        )


async def platforms_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    env = load_env()
    configured = get_configured_platforms(env)
    if not configured:
        await update.message.reply_text(
            "No platforms configured yet. Add credentials in .env and restart."
        )
        return
    lines = [f"- {name}" for name in configured.values()]
    await update.message.reply_text("Configured platforms:\n" + "\n".join(lines))


async def handle_post_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat or chat.type != "private":
        return

    env = load_env()
    admin_ids = load_admin_ids(env)
    if not is_authorized(user.id, admin_ids):
        await msg.reply_text(
            f"Unauthorized. Add your Telegram User ID ({user.id}) to ADMIN_USER_IDS in .env."
        )
        return

    configured = get_configured_platforms(env)
    if not configured:
        await msg.reply_text(
            "No target platforms are configured in .env yet. Run /platforms for details."
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

    env = load_env()
    configured = get_configured_platforms(env)

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
            context.bot, env, draft.selected, draft.text, draft.image_path
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
    app.add_handler(CommandHandler("platforms", platforms_command))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern="^cp:"))
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (filters.PHOTO | (filters.TEXT & ~filters.COMMAND)),
            handle_post_input,
        )
    )

    logger.info("Multi-Platform Cross-Poster bot is running (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
