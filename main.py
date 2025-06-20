import os
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    filters, ChatJoinRequestHandler
)
import phonenumbers
from phonenumbers import NumberParseException
import sqlite3
import signal
import sys
import asyncio

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
PORT = int(os.getenv('PORT', '8000'))

# --- Rate Limiter for spam protection ---
class RateLimiter:
    def __init__(self):
        self.verification_attempts = defaultdict(list)
        self.join_attempts = defaultdict(list)
        self.spam_messages = defaultdict(list)
        self.verification_limit = 3  # per 24h
        self.join_limit = 5  # per 24h
        self.message_limit = 20  # per minute

    def _cleanup(self, user_id, key, window):
        now = datetime.now()
        attempts = getattr(self, key)[user_id]
        setattr(self, key, {
            **getattr(self, key),
            user_id: [t for t in attempts if now - t < window]
        })

    def can_verify(self, user_id):
        self._cleanup(user_id, 'verification_attempts', timedelta(days=1))
        return len(self.verification_attempts[user_id]) < self.verification_limit

    def record_verification(self, user_id):
        self.verification_attempts[user_id].append(datetime.now())

    def can_join(self, user_id):
        self._cleanup(user_id, 'join_attempts', timedelta(days=1))
        return len(self.join_attempts[user_id]) < self.join_limit

    def record_join(self, user_id):
        self.join_attempts[user_id].append(datetime.now())

    def can_message(self, user_id):
        self._cleanup(user_id, 'spam_messages', timedelta(minutes=1))
        return len(self.spam_messages[user_id]) < self.message_limit

    def record_message(self, user_id):
        self.spam_messages[user_id].append(datetime.now())

# --- Database Manager ---
class DatabaseManager:
    def __init__(self, db_path: str = "filipino_bot.db"):
        self.db_path = db_path
        self.init_database()

    def init_database(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS verified_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                phone_number TEXT,
                verified_date TIMESTAMP,
                is_banned BOOLEAN DEFAULT FALSE
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS join_requests (
                user_id INTEGER,
                chat_id INTEGER,
                request_date TIMESTAMP,
                status TEXT DEFAULT 'pending',
                PRIMARY KEY (user_id, chat_id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS managed_groups (
                chat_id INTEGER PRIMARY KEY,
                chat_title TEXT,
                chat_type TEXT,
                added_date TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS spam_tracking (
                user_id INTEGER,
                incident_type TEXT,
                incident_time TIMESTAMP,
                details TEXT,
                PRIMARY KEY (user_id, incident_time)
            )
        ''')
        conn.commit()
        conn.close()

    def add_verified_user(self, user_id: int, username: str, first_name: str, phone_number: str):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO verified_users 
            (user_id, username, first_name, phone_number, verified_date, is_banned)
            VALUES (?, ?, ?, ?, ?, FALSE)
        ''', (user_id, username or "", first_name or "", phone_number, datetime.now().isoformat()))
        conn.commit()
        conn.close()

    def is_verified(self, user_id: int) -> bool:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM verified_users WHERE user_id = ? AND is_banned = FALSE', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return result is not None

    def get_user_phone(self, user_id: int) -> str:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('SELECT phone_number FROM verified_users WHERE user_id = ? AND is_banned = FALSE', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else None

    def ban_user(self, user_id: int):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('UPDATE verified_users SET is_banned = TRUE WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()

    def add_join_request(self, user_id: int, chat_id: int):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO join_requests 
            (user_id, chat_id, request_date, status)
            VALUES (?, ?, ?, 'pending')
        ''', (user_id, chat_id, datetime.now().isoformat()))
        conn.commit()
        conn.close()

    def update_join_request_status(self, user_id: int, chat_id: int, status: str):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE join_requests 
            SET status = ? 
            WHERE user_id = ? AND chat_id = ?
        ''', (status, user_id, chat_id))
        conn.commit()
        conn.close()

    def add_managed_group(self, chat_id: int, chat_title: str, chat_type: str):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO managed_groups 
            (chat_id, chat_title, chat_type, added_date, is_active)
            VALUES (?, ?, ?, ?, TRUE)
        ''', (chat_id, chat_title, chat_type, datetime.now().isoformat()))
        conn.commit()
        conn.close()

    def get_managed_groups(self) -> list:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT chat_id, chat_title, chat_type 
            FROM managed_groups 
            WHERE is_active = TRUE
        ''')
        results = cursor.fetchall()
        conn.close()
        return results

    def log_spam_incident(self, user_id: int, incident_type: str, details: str):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO spam_tracking (user_id, incident_type, incident_time, details)
            VALUES (?, ?, ?, ?)
        ''', (user_id, incident_type, datetime.now().isoformat(), details))
        conn.commit()
        conn.close()

# --- Phone Number Verification ---
class PhoneVerifier:
    @staticmethod
    def verify_phone_number(phone_number: str) -> dict:
        try:
            cleaned_number = phone_number.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
            if cleaned_number.startswith("09"):
                cleaned_number = "+63" + cleaned_number[1:]
            elif cleaned_number.startswith("9") and len(cleaned_number) == 10:
                cleaned_number = "+63" + cleaned_number
            elif cleaned_number.startswith("63") and not cleaned_number.startswith("+63"):
                cleaned_number = "+" + cleaned_number
            elif not cleaned_number.startswith("+") and len(cleaned_number) == 11 and cleaned_number.startswith("0"):
                cleaned_number = "+63" + cleaned_number[1:]
            elif not cleaned_number.startswith("+") and len(cleaned_number) == 10:
                cleaned_number = "+63" + cleaned_number
            parsed = phonenumbers.parse(cleaned_number)
            region = phonenumbers.region_code_for_number(parsed)
            is_valid = phonenumbers.is_valid_number(parsed)
            is_ph = region == 'PH' and parsed.country_code == 63 and is_valid
            return {
                'is_filipino': is_ph,
                'formatted_number': phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL),
                'is_valid': is_valid,
                'region': region,
                'country_code': parsed.country_code if is_valid else None
            }
        except NumberParseException as e:
            logger.error(f"Phone parsing error: {e}")
            return {
                'is_filipino': False,
                'formatted_number': phone_number,
                'is_valid': False,
                'region': None,
                'country_code': None
            }

# --- FilipinoBotManager with Anti-Spam ---
class FilipinoBotManager:
    def __init__(self):
        if not BOT_TOKEN:
            raise ValueError("BOT_TOKEN environment variable is required!")
        if not ADMIN_ID:
            raise ValueError("ADMIN_ID environment variable is required!")
        self.db = DatabaseManager()
        self.verifier = PhoneVerifier()
        self.rate_limiter = RateLimiter()
        self.spam_words = set(['spam', 'viagra', 'crypto', 'earn money', 'bit.ly', 't.me/', 'porn', 'xxx', 'loan', 'http://', 'https://'])
        self.blocked_users = set()

    async def generate_invite_links(self, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
        managed_groups = self.db.get_managed_groups()
        invite_messages = []
        if not managed_groups:
            return "❌ No managed groups found. Add the bot to groups first and make it admin."
        for chat_id, chat_title, chat_type in managed_groups:
            try:
                invite_link = await context.bot.create_chat_invite_link(
                    chat_id=chat_id,
                    member_limit=1,
                    name=f"Filipino-{user_id}",
                    creates_join_request=False
                )
                group_type = "🔒 Private" if chat_type == "private" else "👥 Group" if chat_type == "group" else "📢 Channel"
                invite_messages.append(f"{group_type} **{chat_title}**\n🔗 {invite_link.invite_link}")
            except Exception as e:
                logger.error(f"Failed to create invite link for {chat_title}: {e}")
                invite_messages.append(f"❌ **{chat_title}** - Failed to create invite link")
        return "\n\n".join(invite_messages)

    def is_spam_message(self, message: str) -> bool:
        message_lower = message.lower()
        if any(word in message_lower for word in self.spam_words):
            return True
        if any(char * 5 in message for char in message):
            return True
        if len(message) > 10:
            uppercase_ratio = sum(1 for c in message if c.isupper()) / len(message)
            if uppercase_ratio > 0.7:
                return True
        return False

    async def block_user(self, user_id: int, reason: str, context=None):
        self.blocked_users.add(user_id)
        self.db.ban_user(user_id)
        self.db.log_spam_incident(user_id, "block", reason)
        try:
            if context:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"🚫 **User Blocked**\n\nUser ID: `{user_id}`\nReason: {reason}",
                    parse_mode=ParseMode.MARKDOWN
                )
        except Exception as e:
            logger.warning(f"Failed to notify admin of blocked user: {e}")

    async def handle_new_chat_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.message.new_chat_members:
            return
        bot_user = await context.bot.get_me()
        for member in update.message.new_chat_members:
            if member.id == bot_user.id:
                chat = update.effective_chat
                try:
                    bot_member = await context.bot.get_chat_member(chat.id, bot_user.id)
                    if bot_member.status in ['administrator', 'creator']:
                        self.db.add_managed_group(chat.id, chat.title, chat.type)
                        await context.bot.send_message(
                            ADMIN_ID,
                            f"🆕 **Bot Added to New Group**\n\n**Group:** {chat.title}\n**Type:** {chat.type}\n**ID:** `{chat.id}`\n**Status:** Added to managed groups ✅",
                            parse_mode=ParseMode.MARKDOWN
                        )
                    else:
                        await context.bot.send_message(
                            ADMIN_ID,
                            f"⚠️ **Bot Added but Not Admin**\n\n**Group:** {chat.title}\n**Issue:** Bot needs admin privileges to create invite links",
                            parse_mode=ParseMode.MARKDOWN
                        )
                except Exception as e:
                    logger.error(f"Error checking bot admin status: {e}")

    async def handle_join_request(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            if not update.chat_join_request:
                return
            join_request = update.chat_join_request
            user = join_request.from_user
            chat = join_request.chat
            if user.is_bot or user.id == ADMIN_ID:
                await context.bot.approve_chat_join_request(chat.id, user.id)
                return
            if user.id in self.blocked_users:
                await context.bot.decline_chat_join_request(chat.id, user.id)
                self.db.log_spam_incident(user.id, "join_request_rejected", "Blocked user tried to join")
                return
            if not self.rate_limiter.can_join(user.id):
                await context.bot.decline_chat_join_request(chat.id, user.id)
                self.db.log_spam_incident(user.id, "join_request_rate_limit", "Too many join attempts")
                return
            self.rate_limiter.record_join(user.id)
            self.db.add_join_request(user.id, chat.id)
            if self.db.is_verified(user.id):
                stored_phone = self.db.get_user_phone(user.id)
                if stored_phone:
                    phone_result = self.verifier.verify_phone_number(stored_phone)
                    if phone_result['is_filipino']:
                        await context.bot.approve_chat_join_request(chat.id, user.id)
                        self.db.update_join_request_status(user.id, chat.id, 'approved')
                        try:
                            await context.bot.send_message(user.id,
                                f"🎉 **Welcome!** ✅\n\nHi {user.first_name}, you've been auto-approved to join **{chat.title}**!\n\nYour Filipino verification status is confirmed. Enjoy the community! 🇵🇭",
                                parse_mode=ParseMode.MARKDOWN
                            )
                        except Exception: pass
                        try:
                            await context.bot.send_message(ADMIN_ID,
                                f"✅ **Auto-Approved Join Request**\n\n**User:** {user.first_name} (@{user.username or 'no_username'})\n**ID:** `{user.id}`\n**Chat:** {chat.title} (`{chat.id}`)\n**Status:** Verified Filipino User - Auto-approved",
                                parse_mode=ParseMode.MARKDOWN
                            )
                        except Exception: pass
                        return
            verification_msg = (
                f"🇵🇭 **Filipino Verification Required**\n\n"
                f"Hi {user.first_name}! To join **{chat.title}**, please verify your Filipino status by sharing your Philippine phone number.\n\n"
                f"**How to verify:**\n"
                f"1. Click the button below to share your phone number\n"
                f"2. Only Philippine numbers (+63) are accepted\n"
                f"3. You'll be auto-approved once verified\n\n"
                f"**Your privacy:** Your phone number is only used for verification purposes.\n\n"
                f"👇 **Click to share your phone number:**"
            )
            contact_keyboard = [[KeyboardButton("📱 Share my Philippine Phone Number", request_contact=True)]]
            contact_markup = ReplyKeyboardMarkup(contact_keyboard, one_time_keyboard=True, resize_keyboard=True)
            try:
                await context.bot.send_message(user.id, verification_msg, reply_markup=contact_markup, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.error(f"Could not send verification message to user {user.id}: {e}")
                await context.bot.decline_chat_join_request(chat.id, user.id)
                self.db.update_join_request_status(user.id, chat.id, 'rejected')
        except Exception as e:
            logger.error(f"Error handling join request: {e}")

    async def start_verification(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user.id in self.blocked_users:
            await update.message.reply_text("❌ You are blocked due to spam or abuse.", reply_markup=ReplyKeyboardRemove())
            return
        if self.db.is_verified(user.id):
            await update.message.reply_text(
                "✅ You are already verified as a Filipino user! 🇵🇭\n\nYou can now join Filipino groups and channels without additional verification.",
                reply_markup=ReplyKeyboardRemove()
            )
            return
        contact_keyboard = [[KeyboardButton("📱 I-Share ang Phone Number Ko", request_contact=True)]]
        contact_markup = ReplyKeyboardMarkup(contact_keyboard, one_time_keyboard=True, resize_keyboard=True)
        verification_msg = (
            f"🇵🇭 **Filipino Verification**\n\n"
            f"Hi {user.first_name}! To verify your Filipino status, please share your Philippine phone number by clicking the button below.\n\n"
            f"**Requirements:**\n"
            f"• Philippine number (+63) only\n"
            f"• Click the button below to share\n"
            f"• Auto-approval once verified\n\n"
            f"**Benefits:**\n"
            f"• Access to Filipino groups/channels\n"
            f"• Auto-approval for future join requests\n"
            f"• One-time verification process\n\n"
            f"👇 **Click to share:**"
        )
        await update.message.reply_text(verification_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=contact_markup)

    async def handle_contact_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message.contact:
            return
        user = update.effective_user
        contact = update.message.contact
        if user.id in self.blocked_users:
            await update.message.reply_text("❌ You are blocked due to spam or abuse.", reply_markup=ReplyKeyboardRemove())
            return
        if contact.user_id != user.id:
            await update.message.reply_text("❌ Only your own phone number can be verified!", reply_markup=ReplyKeyboardRemove())
            return
        if not self.rate_limiter.can_verify(user.id):
            await self.block_user(user.id, "Too many verification attempts", context)
            await update.message.reply_text("⚠️ Too many verification attempts. Please try again tomorrow.", reply_markup=ReplyKeyboardRemove())
            self.db.log_spam_incident(user.id, "verification_rate_limit", "Exceeded verification attempts")
            return
        self.rate_limiter.record_verification(user.id)
        phone_result = self.verifier.verify_phone_number(contact.phone_number)
        if phone_result['is_filipino']:
            self.db.add_verified_user(user.id, user.username, user.first_name, contact.phone_number)
            await update.message.reply_text(
                f"✅ **Verified!** 🇵🇭\n\nWelcome to the Filipino community, {user.first_name}!\n\n📱 **Verified Number:** {phone_result['formatted_number']}\n🎉 **Status:** Approved for all Filipino channels/groups\n🚀 **Benefit:** Auto-approval for future join requests!\n\n**🔗 Your Personal Invite Links:**\n{await self.generate_invite_links(context, user.id)}\n\n⚠️ **Important:** These links are ONE-TIME USE only and cannot be shared!",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ReplyKeyboardRemove()
            )
            try:
                await context.bot.send_message(ADMIN_ID,
                    f"✅ **New Verified User**\n\n**User:** {user.first_name} (@{user.username or 'no_username'})\n**ID:** `{user.id}`\n**Phone:** {phone_result['formatted_number']}\n**Status:** Successfully verified as Filipino",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception: pass
        else:
            await update.message.reply_text(
                f"❌ **Invalid Phone Number!**\n\n• **Number:** {phone_result['formatted_number']}\n• **Expected:** Philippines 🇵🇭 (+63)\n• **Detected Region:** {phone_result.get('region', 'Unknown')}\n\n**Please try again with a valid Philippine phone number.**\n\nCommon formats:\n• +63 9XX XXX XXXX\n• 09XX XXX XXXX\n• 63 9XX XXX XXXX",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ReplyKeyboardRemove()
            )
            self.db.log_spam_incident(user.id, "invalid_phone", f"Provided: {phone_result['formatted_number']}")

    async def handle_status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if self.db.is_verified(user.id):
            phone = self.db.get_user_phone(user.id)
            status_msg = f"✅ **Verification Status: VERIFIED** 🇵🇭\n\n**User:** {user.first_name}\n**Phone:** {phone}\n**Status:** Active Filipino User\n**Benefits:** Auto-approval for Filipino groups\n\nYou're all set! 🎉"
        else:
            status_msg = f"❌ **Verification Status: NOT VERIFIED**\n\n**User:** {user.first_name}\n**Status:** Unverified\n\nTo get verified, use /start and share your Philippine phone number."
        await update.message.reply_text(status_msg, parse_mode=ParseMode.MARKDOWN)

    async def handle_groups_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self.db.is_verified(user.id):
            await update.message.reply_text(
                "❌ You need to be verified first! Use /start to verify your Filipino phone number.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        invite_links_msg = await self.generate_invite_links(context, user.id)
        groups_msg = (
            f"🇵🇭 **Available Filipino Groups**\n\n"
            f"Hi {user.first_name}! Here are your personal invite links:\n\n"
            f"{invite_links_msg}\n\n"
            f"⚠️ **Important Notes:**\n"
            f"• These links are ONE-TIME USE only\n"
            f"• Cannot be shared with others\n"
            f"• Links expire after you join\n"
            f"• Use /groups again to get new links\n\n"
            f"💡 **Tip:** Join the groups you're interested in right away!"
        )
        await update.message.reply_text(groups_msg, parse_mode=ParseMode.MARKDOWN)

    async def handle_admin_add_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user.id != ADMIN_ID:
            await update.message.reply_text("❌ Admin only command!")
            return
        if not context.args or len(context.args) < 2:
            await update.message.reply_text(
                "Usage: /addgroup <chat_id> <group_name>\nExample: /addgroup -1001234567890 Filipino Community"
            )
            return
        try:
            chat_id = int(context.args[0])
            group_name = " ".join(context.args[1:])
            chat = await context.bot.get_chat(chat_id)
            bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
            if bot_member.status in ['administrator', 'creator']:
                self.db.add_managed_group(chat_id, group_name, chat.type)
                await update.message.reply_text(
                    f"✅ Added **{group_name}** to managed groups!\nChat ID: `{chat_id}`",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await update.message.reply_text(
                    f"❌ Bot is not admin in **{group_name}**!\nMake the bot admin first.",
                    parse_mode=ParseMode.MARKDOWN
                )
        except ValueError:
            await update.message.reply_text("❌ Invalid chat ID format!")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def handle_help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_msg = (
            "🇵🇭 **Filipino Verification Bot Help**\n\n"
            "**Commands:**\n"
            "• `/start` - Start verification process\n"
            "• `/status` - Check your verification status\n"
            "• `/groups` - Get your invite links\n"
            "• `/help` - Show this help message\n\n"
            "**How it works:**\n"
            "1. Use `/start` to begin verification\n"
            "2. Share your Philippine phone number\n"
            "3. Get auto-approved for Filipino groups\n"
            "4. One-time verification for all groups\n\n"
            "**Supported formats:**\n"
            "• +63 9XX XXX XXXX\n"
            "• 09XX XXX XXXX\n"
            "• 63 9XX XXX XXXX\n\n"
            "**Need help?** Contact the admin if you have issues."
        )
        await update.message.reply_text(help_msg, parse_mode=ParseMode.MARKDOWN)

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        logger.error(f"Exception while handling an update: {context.error}")

# --- Application and Main ---
application = None

def signal_handler(signum, frame):
    logger.info("Received shutdown signal, stopping bot...")
    if application:
        asyncio.create_task(application.stop())
    sys.exit(0)

def main():
    global application
    if not BOT_TOKEN or not ADMIN_ID:
        logger.error("BOT_TOKEN and ADMIN_ID environment variables are required!")
        return
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    bot_manager = FilipinoBotManager()
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_error_handler(bot_manager.error_handler)
    application.add_handler(CommandHandler("start", bot_manager.start_verification))
    application.add_handler(CommandHandler("status", bot_manager.handle_status_command))
    application.add_handler(CommandHandler("groups", bot_manager.handle_groups_command))
    application.add_handler(CommandHandler("addgroup", bot_manager.handle_admin_add_group))
    application.add_handler(CommandHandler("help", bot_manager.handle_help_command))
    application.add_handler(MessageHandler(filters.CONTACT, bot_manager.handle_contact_message))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, bot_manager.handle_new_chat_member))
    application.add_handler(ChatJoinRequestHandler(bot_manager.handle_join_request))
    logger.info("🇵🇭 Filipino Verification Bot starting...")
    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            close_loop=False
        )
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
    finally:
        logger.info("Bot stopped.")

if __name__ == '__main__':
    main()
