"""
Telegram Admin-Relay Bot — everything in this one file.

SETUP
1. Install dependency:
   pip install "python-telegram-bot[job-queue]"==21.9
2. Run:
   python bot-3.py
   (BOT_TOKEN and OWNER_ID are already filled in below)

HOW ADMINS WORK
- OWNER_ID (fixed, hardcoded below) is the only person who can:
    ban / unban users, set/toggle the daily broadcast, add/remove admins.
- Extra admins (added via /addadmin @username) receive every user's
  relayed messages and can reply to them — they cannot ban, broadcast,
  or manage other admins.
- A user must have messaged the bot at least once before you can add
  them as admin by username (the bot needs to have seen their id first).

OWNER-ONLY COMMANDS
- /addadmin @username      make someone an admin (relay + reply only)
- /removeadmin @username   remove them
- /listadmins              show current extra admins
- /ban <user_id>           block a user
- /unban <user_id>
- /setbroadcast <text>     set the daily broadcast message
- /broadcaston / /broadcastoff

EVERYONE WHO IS AN ADMIN (owner + added admins)
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
    }


def save_data(d):
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, indent=2)


data = load_data()


def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in data["admins"]


def admin_chat_ids():
    return [OWNER_ID] + list(data["admins"])


def remember_username(user):
    if user.username:
        data["usernames"][user.username.lower()] = user.id


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    remember_username(user)
    if user.id not in data["users"]:
        data["users"].append(user.id)
    save_data(data)
    await update.message.reply_text("Hi! Send me a message and it'll reach the admin.")


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


async def ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != OWNER_ID:
        await query.answer("Only the owner can ban.", show_alert=True)
        return
    await query.answer()
    user_id = int(query.data.split("_")[1])
    if user_id not in data["banned"]:
        data["banned"].append(user_id)
        save_data(data)
    await query.edit_message_text(f"User {user_id} banned.")


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    uid = int(context.args[0])
    if uid not in data["banned"]:
        data["banned"].append(uid)
        save_data(data)
    await update.message.reply_text(f"Banned {uid}")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    uid = int(context.args[0])
    if uid in data["banned"]:
        data["banned"].remove(uid)
        save_data(data)
    await update.message.reply_text(f"Unbanned {uid}")


async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /addadmin @username")
        return
    username = context.args[0].lstrip("@").lower()
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


async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /removeadmin @username")
        return
    username = context.args[0].lstrip("@").lower()
    uid = data["usernames"].get(username)
    if uid is None or uid not in data["admins"]:
        await update.message.reply_text("That user isn't an admin.")
        return
    data["admins"].remove(uid)
    save_data(data)
    await update.message.reply_text(f"@{username} removed from admins.")


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


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("addadmin", add_admin_command))
    app.add_handler(CommandHandler("removeadmin", remove_admin_command))
    app.add_handler(CommandHandler("listadmins", list_admins_command))
    app.add_handler(CommandHandler("setbroadcast", set_broadcast))
    app.add_handler(CommandHandler("broadcaston", broadcast_on))
    app.add_handler(CommandHandler("broadcastoff", broadcast_off))

    app.add_handler(CallbackQueryHandler(ban_callback, pattern=r"^ban_"))

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
