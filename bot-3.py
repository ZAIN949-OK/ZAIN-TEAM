"""
Telegram Admin-Relay Bot — everything in this one file.

SETUP
1. Install dependency:
   pip install "python-telegram-bot[job-queue]"==21.9
2. Run:
   python bot-3.py
   (BOT_TOKEN and OWNER_ID are already filled in below)

HOW ADMINS WORK
- OWNER_ID (fixed, hardcoded below) is the head admin. Only the owner can:
    add/remove admins, open the admin panel (/panel), set the welcome
    message, and manage the daily broadcast.
- Extra admins (added via /addadmin @username, by reply, or via the panel)
  can only: ban/unban users, and receive + reply to relayed user messages.
  They cannot add/remove other admins, use the panel, set the welcome
  message, or manage broadcasts.
- A user must have messaged the bot at least once before you can add
  them as admin by username (the bot needs to have seen their id first).

OWNER-ONLY COMMANDS
- /panel                   open the button-based admin panel
- /addadmin @username | (reply to a relayed message)
- /removeadmin @username | (reply to a relayed message)
- /listadmins              show current extra admins
- /setbroadcast <text>     set the daily broadcast message
- /broadcaston / /broadcastoff

OWNER + ADMIN COMMANDS
- /ban <user_id> | @username | (reply to a relayed message)
- /unban <user_id> | @username | (reply to a relayed message)
- Reply to any forwarded message -> sends your reply back to that user

HOSTING 24/7
Push this file to GitHub and deploy on Railway or Render as a
worker/background service.
"""

import json
import logging
import os
from datetime import time as dtime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---- CONFIG ----
BOT_TOKEN = os.environ["BOT_TOKEN"]  # set this in Railway's "Variables" tab, not here
OWNER_ID = 6731551933  # your Telegram numeric user id — fixed, can't be removed

DATA_FILE = "bot_data.json"

# In-memory: tracks what the owner is being asked for right now
# (e.g. "set_admin", "remove_admin", "set_welcome"). Not persisted — resets on restart.
pending_actions = {}


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {
        "users": [],
        "banned": [],
        "admins": [],           # extra admins (list of user ids), owner not included
        "usernames": {},        # "username" (lowercase, no @) -> user_id
        "message_map": {},      # "chat_id:message_id" -> original user id
        "broadcast_enabled": False,
        "broadcast_text": "",
        "broadcast_hour": 9,
        "broadcast_minute": 0,
        "welcome_message": "Hi! Send me a message and it'll reach the admin.",
        "welcome_photo": None,   # file_id of the welcome image, if any
    }


def save_data(d):
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, indent=2)


data = load_data()
data.setdefault("welcome_message", "Hi! Send me a message and it'll reach the admin.")
data.setdefault("welcome_photo", None)


def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in data["admins"]


def admin_chat_ids():
    return [OWNER_ID] + list(data["admins"])


def remember_username(user):
    if user.username:
        data["usernames"][user.username.lower()] = user.id


def resolve_user_id(arg):
    """arg can be an int-like string or '@username'."""
    if arg.startswith("@"):
        username = arg.lstrip("@").lower()
        return data["usernames"].get(username)
    try:
        return int(arg)
    except ValueError:
        return None


def username_for(uid):
    for uname, stored_uid in data["usernames"].items():
        if stored_uid == uid:
            return uname
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    remember_username(user)
    if user.id not in data["users"]:
        data["users"].append(user.id)
    save_data(data)

    if data.get("welcome_photo"):
        await update.message.reply_photo(
            photo=data["welcome_photo"],
            caption=data.get("welcome_message", ""),
        )
    else:
        await update.message.reply_text(data.get("welcome_message", "Hi!"))


async def relay_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Admins talking to the bot outside of a reply shouldn't be relayed as "users"
    if is_admin(user.id):
        return

    if user.id in data["banned"]:
        return

    remember_username(user)
    if user.id not in data["users"]:
        data["users"].append(user.id)
    save_data(data)

    info = f"From: {user.full_name} (id: {user.id})"
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Ban this user", callback_data=f"ban_{user.id}")]]
    )

    for admin_id in admin_chat_ids():
        try:
            forwarded = await context.bot.forward_message(
                chat_id=admin_id,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )
            data["message_map"][f"{admin_id}:{forwarded.message_id}"] = user.id
            await context.bot.send_message(chat_id=admin_id, text=info, reply_markup=keyboard)
        except Exception as e:
            logger.warning(f"Failed to relay to admin {admin_id}: {e}")

    save_data(data)


# ---- Owner text/photo input for pending panel actions (owner-only) ----
async def owner_pending_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        return
    action = pending_actions.get(user.id)
    if not action:
        return  # nothing pending, let other handlers process normally

    if action == "set_welcome":
        pending_actions.pop(user.id, None)
        if update.message.photo:
            data["welcome_photo"] = update.message.photo[-1].file_id
            data["welcome_message"] = update.message.caption or ""
            save_data(data)
            await update.message.reply_text("Welcome message updated (with image).")
        elif update.message.text:
            data["welcome_photo"] = None
            data["welcome_message"] = update.message.text.strip()
            save_data(data)
            await update.message.reply_text("Welcome message updated (text only).")
        return

    if not update.message.text:
        return
    text = update.message.text.strip()
    pending_actions.pop(user.id, None)

    if action == "set_admin":
        username = text.lstrip("@").lower()
        uid = data["usernames"].get(username)
        if uid is None:
            await update.message.reply_text(
                "Don't know that user's id yet — they must message the bot at least once first."
            )
            return
        if uid == OWNER_ID:
            await update.message.reply_text("That's you — you're already the owner.")
            return
        if uid not in data["admins"]:
            data["admins"].append(uid)
            save_data(data)
        await update.message.reply_text(f"@{username} (id: {uid}) is now an admin.")
        try:
            await context.bot.send_message(chat_id=uid, text="You've been made an admin of this bot.")
        except Exception:
            pass

    elif action == "remove_admin":
        username = text.lstrip("@").lower()
        uid = data["usernames"].get(username)
        if uid is None or uid not in data["admins"]:
            await update.message.reply_text("That user isn't an admin.")
            return
        data["admins"].remove(uid)
        save_data(data)
        await update.message.reply_text(f"@{username} removed from admins.")


async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender_id = update.effective_user.id
    if not is_admin(sender_id):
        return
    if not update.message.reply_to_message:
        return

    key = f"{sender_id}:{update.message.reply_to_message.message_id}"
    target_user_id = data["message_map"].get(key)

    if target_user_id is None:
        await update.message.reply_text("Can't find the original user for this message.")
        return

    try:
        await context.bot.send_message(chat_id=target_user_id, text=update.message.text)
        await update.message.reply_text("Sent.")
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")


# ---- Ban / Unban — available to owner AND regular admins ----
async def ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Only admins can ban.", show_alert=True)
        return
    await query.answer()
    user_id = int(query.data.split("_")[1])
    if user_id not in data["banned"]:
        data["banned"].append(user_id)
        save_data(data)
    await query.edit_message_text(f"User {user_id} banned.")


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    uid = None
    if update.message.reply_to_message:
        key = f"{update.effective_user.id}:{update.message.reply_to_message.message_id}"
        uid = data["message_map"].get(key)
        if uid is None:
            await update.message.reply_text("Can't find the original user for this message.")
            return
    elif context.args:
        uid = resolve_user_id(context.args[0])
        if uid is None:
            await update.message.reply_text(
                "Couldn't resolve that user. Use /ban <user_id>, /ban @username, or reply to their message."
            )
            return
    else:
        await update.message.reply_text("Usage: /ban <user_id> or /ban @username, or reply to their message")
        return

    if uid not in data["banned"]:
        data["banned"].append(uid)
        save_data(data)
    await update.message.reply_text(f"Banned {uid}")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    uid = None
    if update.message.reply_to_message:
        key = f"{update.effective_user.id}:{update.message.reply_to_message.message_id}"
        uid = data["message_map"].get(key)
        if uid is None:
            await update.message.reply_text("Can't find the original user for this message.")
            return
    elif context.args:
        uid = resolve_user_id(context.args[0])
        if uid is None:
            await update.message.reply_text(
                "Couldn't resolve that user. Use /unban <user_id>, /unban @username, or reply to their message."
            )
            return
    else:
        await update.message.reply_text("Usage: /unban <user_id> or /unban @username, or reply to their message")
        return

    if uid in data["banned"]:
        data["banned"].remove(uid)
        save_data(data)
    await update.message.reply_text(f"Unbanned {uid}")


# ---- Admin management — owner only ----
async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    uid = None
    username = None

    if update.message.reply_to_message:
        key = f"{update.effective_user.id}:{update.message.reply_to_message.message_id}"
        uid = data["message_map"].get(key)
        if uid is None:
            await update.message.reply_text("Can't find the original user for this message.")
            return
        username = username_for(uid)
    elif context.args:
        username = context.args[0].lstrip("@").lower()
        uid = data["usernames"].get(username)
        if uid is None:
            await update.message.reply_text(
                "Don't know that user's id yet — they must message the bot at least once first."
            )
            return
    else:
        await update.message.reply_text("Usage: /addadmin @username, or reply to their message")
        return

    if uid == OWNER_ID:
        await update.message.reply_text("That's you — you're already the owner.")
        return
    if uid not in data["admins"]:
        data["admins"].append(uid)
        save_data(data)

    label = f"@{username}" if username else str(uid)
    await update.message.reply_text(f"{label} (id: {uid}) is now an admin.")
    try:
        await context.bot.send_message(chat_id=uid, text="You've been made an admin of this bot.")
    except Exception:
        pass


async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    uid = None
    username = None

    if update.message.reply_to_message:
        key = f"{update.effective_user.id}:{update.message.reply_to_message.message_id}"
        uid = data["message_map"].get(key)
        if uid is None:
            await update.message.reply_text("Can't find the original user for this message.")
            return
        username = username_for(uid)
    elif context.args:
        username = context.args[0].lstrip("@").lower()
        uid = data["usernames"].get(username)
        if uid is None:
            await update.message.reply_text("That user isn't an admin.")
            return
    else:
        await update.message.reply_text("Usage: /removeadmin @username, or reply to their message")
        return

    if uid not in data["admins"]:
        await update.message.reply_text("That user isn't an admin.")
        return

    data["admins"].remove(uid)
    save_data(data)
    label = f"@{username}" if username else str(uid)
    await update.message.reply_text(f"{label} removed from admins.")


async def list_admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not data["admins"]:
        await update.message.reply_text("No extra admins yet. You (owner) are the only admin.")
        return
    lines = [str(uid) for uid in data["admins"]]
    await update.message.reply_text("Extra admins:\n" + "\n".join(lines))


async def set_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    text = update.message.text.partition(" ")[2]
    if not text:
        await update.message.reply_text("Usage: /setbroadcast <message>")
        return
    data["broadcast_text"] = text
    save_data(data)
    await update.message.reply_text("Broadcast message saved.")


async def broadcast_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    data["broadcast_enabled"] = True
    save_data(data)
    await update.message.reply_text("Daily broadcast turned ON.")


async def broadcast_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    data["broadcast_enabled"] = False
    save_data(data)
    await update.message.reply_text("Daily broadcast turned OFF.")


async def send_daily_broadcast(context: ContextTypes.DEFAULT_TYPE):
    if not data.get("broadcast_enabled") or not data.get("broadcast_text"):
        return
    for uid in list(data["users"]):
        if uid in data["banned"]:
            continue
        try:
            await context.bot.send_message(chat_id=uid, text=data["broadcast_text"])
        except Exception as e:
            logger.warning(f"Failed to send to {uid}: {e}")


# ---- Admin panel (buttons) — owner only ----
def panel_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Set Admin", callback_data="panel_setadmin")],
            [InlineKeyboardButton("➖ Remove Admin", callback_data="panel_removeadmin")],
            [InlineKeyboardButton("✏️ Set Welcome Message", callback_data="panel_setwelcome")],
        ]
    )


async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await update.message.reply_text("Admin Panel:", reply_markup=panel_keyboard())


async def panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != OWNER_ID:
        await query.answer("Only the owner can use this.", show_alert=True)
        return
    await query.answer()

    if query.data == "panel_setadmin":
        pending_actions[OWNER_ID] = "set_admin"
        await query.edit_message_text(
            "Send the @username of the user you want to make admin.\n"
            "(They must have messaged the bot at least once.)"
        )
    elif query.data == "panel_removeadmin":
        pending_actions[OWNER_ID] = "remove_admin"
        await query.edit_message_text("Send the @username of the admin you want to remove.")
    elif query.data == "panel_setwelcome":
        pending_actions[OWNER_ID] = "set_welcome"
        await query.edit_message_text(
            "Send the new welcome message:\n"
            "• Send plain text for a text-only welcome, OR\n"
            "• Send a photo with a caption for an image welcome."
        )


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("panel", panel_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("addadmin", add_admin_command))
    app.add_handler(CommandHandler("removeadmin", remove_admin_command))
    app.add_handler(CommandHandler("listadmins", list_admins_command))
    app.add_handler(CommandHandler("setbroadcast", set_broadcast))
    app.add_handler(CommandHandler("broadcaston", broadcast_on))
    app.add_handler(CommandHandler("broadcastoff", broadcast_off))

    app.add_handler(CallbackQueryHandler(ban_callback, pattern=r"^ban_"))
    app.add_handler(CallbackQueryHandler(panel_callback, pattern=r"^panel_"))

    # Owner's pending panel input (text or photo) — checked before other handlers
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (filters.TEXT | filters.PHOTO) & ~filters.COMMAND & filters.User(user_id=OWNER_ID),
            owner_pending_input,
        ),
        group=-1,
    )

    # Any admin (owner or added) replying to a forwarded message -> routes back to that user
    app.add_handler(
        MessageHandler(filters.REPLY & filters.TEXT & filters.ChatType.PRIVATE, admin_reply)
    )

    # Any normal (non-admin) user message -> relay to all admins
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            relay_to_admin,
        )
    )

    job_queue = app.job_queue
    job_queue.run_daily(
        send_daily_broadcast,
        time=dtime(hour=data.get("broadcast_hour", 9), minute=data.get("broadcast_minute", 0)),
    )

    app.run_polling()


if __name__ == "__main__":
    main()
