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
    message, and manage broadcasts (daily + one-time).
- Extra admins (added via /addadmin @username, by reply, or via the panel)
  can only: ban/unban users, and receive + reply to relayed user messages.

PANEL
- /panel opens a persistent button menu (owner only) with:
    Set Admin, Remove Admin, List Admins, Welcome Message,
    Daily Broadcast (set message / set time / toggle on-off),
    One-Time Broadcast (compose -> confirm or cancel before sending)

HOSTING 24/7
Push this file to GitHub and deploy on Railway or Render as a
worker/background service.
IMPORTANT: only run ONE deployment of this bot at a time. Running two
copies with the same BOT_TOKEN causes Telegram to randomly drop updates
between them, making commands behave inconsistently.
"""

import json
import logging
import os
from datetime import time as dtime

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---- CONFIG ----
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = 6731551933

DATA_FILE = "bot_data.json"

# In-memory state (resets on restart)
pending_actions = {}     # owner_id -> action string, e.g. "set_admin"
onetime_draft = {}       # owner_id -> drafted one-time broadcast text awaiting confirm


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {
        "users": [],
        "banned": [],
        "admins": [],
        "usernames": {},
        "message_map": {},
        "broadcast_enabled": False,
        "broadcast_text": "",
        "broadcast_hour": 9,
        "broadcast_minute": 0,
        "welcome_message": "Hi! Send me a message and it'll reach the admin.",
        "welcome_photo": None,
    }


def save_data(d):
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, indent=2)


data = load_data()
data.setdefault("welcome_message", "Hi! Send me a message and it'll reach the admin.")
data.setdefault("welcome_photo", None)
data.setdefault("broadcast_hour", 9)
data.setdefault("broadcast_minute", 0)


def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in data["admins"]


def admin_chat_ids():
    return [OWNER_ID] + list(data["admins"])


def remember_username(user):
    if user.username:
        data["usernames"][user.username.lower()] = user.id


def resolve_user_id(arg):
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


# ---- Keyboards ----
def main_panel_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["👑 Set Admin", "🚫 Remove Admin", "📋 List Admins"],
            ["🎉 Welcome Msg"],
            ["📅 Daily Broadcast", "🕐 One-Time Broadcast"],
            ["❌ Close Panel"],
        ],
        resize_keyboard=True,
    )


def daily_broadcast_keyboard():
    status = "ON 🟢" if data.get("broadcast_enabled") else "OFF 🔴"
    time_str = f"{data.get('broadcast_hour', 9):02d}:{data.get('broadcast_minute', 0):02d}"
    return ReplyKeyboardMarkup(
        [
            ["📝 Set Message", f"⏰ Set Time ({time_str})"],
            [f"📡 Toggle ({status})"],
            ["🔙 Back to Panel"],
        ],
        resize_keyboard=True,
    )


def onetime_broadcast_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📝 Compose Message"],
            ["🔙 Back to Panel"],
        ],
        resize_keyboard=True,
    )


def confirm_cancel_keyboard():
    return ReplyKeyboardMarkup(
        [["✅ Send Now", "❌ Cancel"]],
        resize_keyboard=True,
    )


# ---- Basic user-facing handlers ----
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
    if is_admin(user.id):
        return
    if user.id in data["banned"]:
        return

    remember_username(user)
    if user.id not in data["users"]:
        data["users"].append(user.id)
    save_data(data)

    for admin_id in admin_chat_ids():
        try:
            forwarded = await context.bot.forward_message(
                chat_id=admin_id,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )
            data["message_map"][f"{admin_id}:{forwarded.message_id}"] = user.id
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


# ---- Ban / Unban (owner + admins) ----
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


# ---- Admin management commands (owner only, kept for convenience) ----
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


# ---- Broadcast scheduling ----
async def send_daily_broadcast(context: ContextTypes.DEFAULT_TYPE):
    if not data.get("broadcast_enabled") or not data.get("broadcast_text"):
        return
    for uid in list(data["users"]):
        if uid in data["banned"]:
            continue
        try:
            await context.bot.send_message(chat_id=uid, text=data["broadcast_text"])
        except Exception as e:
            logger.warning(f"Failed to send daily broadcast to {uid}: {e}")


def schedule_daily_job(job_queue):
    for job in job_queue.get_jobs_by_name("daily_broadcast"):
        job.schedule_removal()
    job_queue.run_daily(
        send_daily_broadcast,
        time=dtime(hour=data.get("broadcast_hour", 9), minute=data.get("broadcast_minute", 0)),
        name="daily_broadcast",
    )


async def send_onetime_broadcast(context: ContextTypes.DEFAULT_TYPE, text: str):
    sent, failed = 0, 0
    for uid in list(data["users"]):
        if uid in data["banned"]:
            continue
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Exception as e:
            logger.warning(f"Failed to send one-time broadcast to {uid}: {e}")
            failed += 1
    return sent, failed


# ---- Panel entry point ----
async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    pending_actions.pop(OWNER_ID, None)
    onetime_draft.pop(OWNER_ID, None)
    await update.message.reply_text(
        "🎛️ Admin Control Panel — tap a button below:",
        reply_markup=main_panel_keyboard(),
    )


# ---- Panel router: handles all button presses + pending text/photo input ----
async def owner_panel_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        return

    text = update.message.text.strip() if update.message.text else None

    # --- Navigation & top-level buttons ---
    if text == "❌ Close Panel":
        pending_actions.pop(user.id, None)
        onetime_draft.pop(user.id, None)
        await update.message.reply_text("Panel closed.", reply_markup=ReplyKeyboardRemove())
        return

    if text == "🔙 Back to Panel":
        pending_actions.pop(user.id, None)
        onetime_draft.pop(user.id, None)
        await update.message.reply_text("Main Panel:", reply_markup=main_panel_keyboard())
        return

    if text == "👑 Set Admin":
        pending_actions[user.id] = "set_admin"
        await update.message.reply_text(
            "Send the @username of the user you want to make admin.\n"
            "(They must have messaged the bot at least once.)"
        )
        return

    if text == "🚫 Remove Admin":
        pending_actions[user.id] = "remove_admin"
        await update.message.reply_text("Send the @username of the admin you want to remove.")
        return

    if text == "📋 List Admins":
        if not data["admins"]:
            reply = "No extra admins yet. You (owner) are the only admin."
        else:
            lines = []
            for uid in data["admins"]:
                uname = username_for(uid)
                lines.append(f"@{uname} (id: {uid})" if uname else str(uid))
            reply = "Extra admins:\n" + "\n".join(lines)
        await update.message.reply_text(reply, reply_markup=main_panel_keyboard())
        return

    if text == "🎉 Welcome Msg":
        pending_actions[user.id] = "set_welcome"
        await update.message.reply_text(
            "Send the new welcome message:\n"
            "• Send plain text for a text-only welcome, OR\n"
            "• Send a photo with a caption for an image welcome."
        )
        return

    # --- Daily Broadcast submenu ---
    if text == "📅 Daily Broadcast":
        pending_actions.pop(user.id, None)
        await update.message.reply_text("Daily Broadcast Menu:", reply_markup=daily_broadcast_keyboard())
        return

    if text == "📝 Set Message":
        pending_actions[user.id] = "set_daily_text"
        await update.message.reply_text("Send the message to broadcast daily.")
        return

    if text and text.startswith("⏰ Set Time"):
        pending_actions[user.id] = "set_daily_time"
        await update.message.reply_text("Send the time in 24-hour HH:MM format (e.g. 09:00 or 21:30).")
        return

    if text and text.startswith("📡 Toggle"):
        data["broadcast_enabled"] = not data.get("broadcast_enabled", False)
        save_data(data)
        schedule_daily_job(context.job_queue)
        status = "ON 🟢" if data["broadcast_enabled"] else "OFF 🔴"
        await update.message.reply_text(
            f"Daily broadcast turned {status}.", reply_markup=daily_broadcast_keyboard()
        )
        return

    # --- One-Time Broadcast submenu ---
    if text == "🕐 One-Time Broadcast":
        pending_actions.pop(user.id, None)
        onetime_draft.pop(user.id, None)
        await update.message.reply_text("One-Time Broadcast Menu:", reply_markup=onetime_broadcast_keyboard())
        return

    if text == "📝 Compose Message":
        pending_actions[user.id] = "set_onetime_text"
        await update.message.reply_text("Send the message you want to broadcast once.")
        return

    if text == "✅ Send Now":
        draft = onetime_draft.pop(user.id, None)
        if not draft:
            await update.message.reply_text("Nothing to send.", reply_markup=onetime_broadcast_keyboard())
            return
        await update.message.reply_text("Sending...", reply_markup=onetime_broadcast_keyboard())
        sent, failed = await send_onetime_broadcast(context, draft)
        await update.message.reply_text(
            f"One-time broadcast sent. ✅ Delivered: {sent}  ❌ Failed: {failed}",
            reply_markup=main_panel_keyboard(),
        )
        return

    if text == "❌ Cancel":
        onetime_draft.pop(user.id, None)
        pending_actions.pop(user.id, None)
        await update.message.reply_text("One-time broadcast cancelled.", reply_markup=onetime_broadcast_keyboard())
        return

    # --- Handle whatever pending action is waiting for input ---
    action = pending_actions.get(user.id)
    if not action:
        return  # not part of any panel flow, let other handlers process

    if action == "set_welcome":
        pending_actions.pop(user.id, None)
        if update.message.photo:
            data["welcome_photo"] = update.message.photo[-1].file_id
            data["welcome_message"] = update.message.caption or ""
            save_data(data)
            await update.message.reply_text("Welcome message updated (with image).", reply_markup=main_panel_keyboard())
        elif update.message.text:
            data["welcome_photo"] = None
            data["welcome_message"] = update.message.text.strip()
            save_data(data)
            await update.message.reply_text("Welcome message updated (text only).", reply_markup=main_panel_keyboard())
        return

    if not update.message.text:
        return
    typed = update.message.text.strip()

    if action == "set_admin":
        pending_actions.pop(user.id, None)
        username = typed.lstrip("@").lower()
        uid = data["usernames"].get(username)
        if uid is None:
            await update.message.reply_text(
                "Don't know that user's id yet — they must message the bot at least once first.",
                reply_markup=main_panel_keyboard(),
            )
            return
        if uid == OWNER_ID:
            await update.message.reply_text("That's you — you're already the owner.", reply_markup=main_panel_keyboard())
            return
        if uid not in data["admins"]:
            data["admins"].append(uid)
            save_data(data)
        await update.message.reply_text(f"@{username} (id: {uid}) is now an admin.", reply_markup=main_panel_keyboard())
        try:
            await context.bot.send_message(chat_id=uid, text="You've been made an admin of this bot.")
        except Exception:
            pass
        return

    if action == "remove_admin":
        pending_actions.pop(user.id, None)
        username = typed.lstrip("@").lower()
        uid = data["usernames"].get(username)
        if uid is None or uid not in data["admins"]:
            await update.message.reply_text("That user isn't an admin.", 
