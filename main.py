import os
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    filters, ChatJoinRequestHandler, ChatMemberHandler
)
from telegram.error import TelegramError
import phonenumbers
from phonenumbers import NumberParseException
import aiosqlite  # Using aiosqlite for async database operations
import signal
import sys
import asyncio

# --- Configuration ---
# It's recommended to load these from a .env file or environment variables for security.
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
# Set this to your bot's username (without the '@')
BOT_USERNAME = os.getenv('BOT_USERNAME', 'YourBotUsername') 

# --- Logging Setup ---
# A more detailed logging format can be helpful for debugging.
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - [%(funcName)s] - %(message)s',
    level=logging.INFO
)
# Suppress noisy logs from the HTTPX library used by python-telegram-bot
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Rate Limiter ---
# This class helps prevent spam and abuse by limiting user actions.
class RateLimiter:
    def __init__(self):
        # Using defaultdict with a default factory simplifies adding new users.
        self.attempts = defaultdict(lambda: {'verification': [], 'join': [], 'message': []})
        self.limits = {
            'verification': (3, timedelta(days=1)),    # 3 attempts per 24 hours
            'join': (5, timedelta(days=1)),            # 5 attempts per 24 hours
            'message': (20, timedelta(minutes=1)),     # 20 messages per minute
        }

    def _cleanup_and_check(self, user_id: int, key: str) -> bool:
        """Removes expired timestamps and checks if the user is within the limit."""
        now = datetime.now()
        limit, window = self.limits[key]
        # Filter out old timestamps
        valid_attempts = [t for t in self.attempts[user_id][key] if now - t < window]
        self.attempts[user_id][key] = valid_attempts
        return len(valid_attempts) < limit

    def record_attempt(self, user_id: int, key: str):
        """Records a new timestamp for a user's action."""
        self.attempts[user_id][key].append(datetime.now())

    def can_verify(self, user_id: int) -> bool:
        return self._cleanup_and_check(user_id, 'verification')

    def can_join(self, user_id: int) -> bool:
        return self._cleanup_and_check(user_id, 'join')

    def can_message(self, user_id: int) -> bool:
        return self._cleanup_and_check(user_id, 'message')

# --- Database Manager ---
# CRITICAL FIX: Using `aiosqlite` for non-blocking database operations, which is
# essential for an `asyncio`-based application like this bot. Using standard
# `sqlite3` would block the entire bot.
class DatabaseManager:
    def __init__(self, db_path: str = "filipino_bot.db"):
        self.db_path = db_path

    async def init_database(self):
        """Initializes the database schema."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS verified_users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    phone_number TEXT,
                    verified_date TIMESTAMP,
                    is_banned BOOLEAN DEFAULT FALSE
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS join_requests (
                    user_id INTEGER,
                    chat_id INTEGER,
                    request_date TIMESTAMP,
                    status TEXT DEFAULT 'pending',
                    PRIMARY KEY (user_id, chat_id)
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS managed_groups (
                    chat_id INTEGER PRIMARY KEY,
                    chat_title TEXT,
                    chat_type TEXT,
                    added_date TIMESTAMP,
                    is_active BOOLEAN DEFAULT TRUE
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS spam_tracking (
                    user_id INTEGER,
                    incident_type TEXT,
                    incident_time TIMESTAMP,
                    details TEXT,
                    PRIMARY KEY (user_id, incident_time)
                )
            ''')
            await db.commit()
            logger.info("Database initialized successfully.")

    async def add_verified_user(self, user_id: int, username: str, first_name: str, phone_number: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT OR REPLACE INTO verified_users 
                (user_id, username, first_name, phone_number, verified_date, is_banned)
                VALUES (?, ?, ?, ?, ?, FALSE)
            ''', (user_id, username or "", first_name or "", phone_number, datetime.now()))
            await db.commit()

    async def is_verified(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute('SELECT 1 FROM verified_users WHERE user_id = ? AND is_banned = FALSE', (user_id,)) as cursor:
                return await cursor.fetchone() is not None

    async def get_user_phone(self, user_id: int) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute('SELECT phone_number FROM verified_users WHERE user_id = ?', (user_id,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    async def ban_user(self, user_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('UPDATE verified_users SET is_banned = TRUE WHERE user_id = ?', (user_id,))
            await db.commit()

    async def add_managed_group(self, chat_id: int, chat_title: str, chat_type: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT OR REPLACE INTO managed_groups 
                (chat_id, chat_title, chat_type, added_date, is_active)
                VALUES (?, ?, ?, ?, TRUE)
            ''', (chat_id, chat_title, chat_type, datetime.now()))
            await db.commit()

    async def get_managed_groups(self) -> list:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute('SELECT chat_id, chat_title, chat_type FROM managed_groups WHERE is_active = TRUE') as cursor:
                return await cursor.fetchall()

    async def log_spam_incident(self, user_id: int, incident_type: str, details: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT INTO spam_tracking (user_id, incident_type, incident_time, details)
                VALUES (?, ?, ?, ?)
            ''', (user_id, incident_type, datetime.now(), details))
            await db.commit()

# --- Phone Number Verification ---
class PhoneVerifier:
    @staticmethod
    def verify_phone_number(phone_number: str) -> dict:
        """
        Cleans and validates a phone number, specifically checking if it's a valid Philippine number.
        The cleaning logic is enhanced to handle more common user input formats.
        """
        if not phone_number:
            return {'is_filipino': False, 'is_valid': False, 'formatted_number': ''}
            
        try:
            # Normalize the number: remove common separators and handle local formats
            cleaned_number = ''.join(filter(str.isdigit, phone_number))
            if len(cleaned_number) == 10 and cleaned_number.startswith('9'):
                # Format: 9171234567 -> +639171234567
                cleaned_number = f"+63{cleaned_number}"
            elif len(cleaned_number) == 11 and cleaned_number.startswith('09'):
                # Format: 09171234567 -> +639171234567
                cleaned_number = f"+63{cleaned_number[1:]}"
            elif len(cleaned_number) == 12 and cleaned_number.startswith('639'):
                # Format: 639171234567 -> +639171234567
                cleaned_number = f"+{cleaned_number}"
            elif not phone_number.startswith('+'):
                 # Add '+' if it's missing but looks like an international number
                 cleaned_number = f"+{cleaned_number}"
            else:
                 cleaned_number = phone_number

            parsed = phonenumbers.parse(cleaned_number)
            is_valid = phonenumbers.is_valid_number(parsed)
            is_ph = phonenumbers.region_code_for_number(parsed) == 'PH'

            return {
                'is_filipino': is_ph and is_valid,
                'is_valid': is_valid,
                'formatted_number': phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL) if is_valid else phone_number,
                'region': phonenumbers.region_code_for_number(parsed) if is_valid else 'Unknown'
            }
        except NumberParseException as e:
            logger.warning(f"Phone number parsing failed for '{phone_number}': {e}")
            return {'is_filipino': False, 'is_valid': False, 'formatted_number': phone_number, 'region': 'Error'}

# --- Main Bot Logic ---
class FilipinoBotManager:
    def __init__(self, db: DatabaseManager, limiter: RateLimiter):
        self.db = db
        self.rate_limiter = limiter
        # A simple set of words to detect potential spam.
        self.spam_words = {'spam', 'viagra', 'crypto', 'earn money', 'bit.ly', 'porn', 'xxx', 'loan'}
        self.blocked_user_cache = set()

    async def load_blocked_users(self):
        """Loads banned user IDs into a cache for faster checks."""
        async with aiosqlite.connect(self.db.db_path) as db:
            async with db.execute('SELECT user_id FROM verified_users WHERE is_banned = TRUE') as cursor:
                rows = await cursor.fetchall()
                self.blocked_user_cache = {row[0] for row in rows}
        logger.info(f"Loaded {len(self.blocked_user_cache)} blocked users into cache.")

    async def block_user(self, context: ContextTypes.DEFAULT_TYPE, user_id: int, reason: str):
        """Blocks a user, updates the database, and notifies the admin."""
        if user_id in self.blocked_user_cache:
            return
        self.blocked_user_cache.add(user_id)
        await self.db.ban_user(user_id)
        await self.db.log_spam_incident(user_id, "block", reason)
        logger.warning(f"User {user_id} blocked. Reason: {reason}")
        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"🚫 **User Blocked**\n\nUser ID: `{user_id}`\nReason: {reason}",
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as e:
            logger.error(f"Failed to send admin notification about block: {e}")

    async def generate_invite_links(self, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
        """Generates one-time invite links for all managed groups."""
        managed_groups = await self.db.get_managed_groups()
        if not managed_groups:
            return "❌ No managed groups found. Please ask the admin to configure the bot."
            
        link_tasks = []
        for group in managed_groups:
            link_tasks.append(self._create_single_invite_link(context, group, user_id))
            
        results = await asyncio.gather(*link_tasks)
        return "\n\n".join(results)

    async def _create_single_invite_link(self, context, group, user_id):
        """Helper to create an invite link for one group. Catches errors gracefully."""
        try:
            # creates_join_request should be True if you want to approve them via the bot
            expire_date = datetime.now() + timedelta(days=1)
            invite_link = await context.bot.create_chat_invite_link(
                chat_id=group['chat_id'],
                member_limit=1,
                name=f"Invite for user {user_id}",
                expire_date=expire_date
            )
            group_type_icon = "👥" if group['chat_type'] == "group" else "📢"
            return f"{group_type_icon} **{group['chat_title']}**\n🔗 {invite_link.invite_link}"
        except TelegramError as e:
            logger.error(f"Failed to create invite link for {group['chat_title']} ({group['chat_id']}): {e}")
            await context.bot.send_message(ADMIN_ID, f"Error creating invite for {group['chat_title']}: {e}")
            return f"❌ **{group['chat_title']}** - Could not create an invite link. The bot might not have the correct permissions."

    # --- Command Handlers ---

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user.id in self.blocked_user_cache:
            await update.message.reply_text("❌ You are blocked from using this service.")
            return

        if await self.db.is_verified(user.id):
            await update.message.reply_text(
                "✅ You are already verified! Use /groups to get new invite links.",
                reply_markup=ReplyKeyboardRemove()
            )
            return
        
        verification_msg = (
            f"🇵🇭 **Filipino Verification**\n\n"
            f"Hello {user.first_name}! To join our exclusive groups, we need to verify that you are from the Philippines.\n\n"
            f"Please tap the button below to share your phone number. This is a one-time verification."
        )
        contact_keyboard = [[KeyboardButton("📱 Share my Philippine Phone Number", request_contact=True)]]
        contact_markup = ReplyKeyboardMarkup(contact_keyboard, one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text(verification_msg, reply_markup=contact_markup)

    async def contact_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        contact = update.message.contact
        
        if not contact or contact.user_id != user.id:
            await update.message.reply_text("❌ Please share your own contact information using the button.", reply_markup=ReplyKeyboardRemove())
            return

        if user.id in self.blocked_user_cache:
            await update.message.reply_text("❌ You are blocked.", reply_markup=ReplyKeyboardRemove())
            return
            
        if not self.rate_limiter.can_verify(user.id):
            await self.block_user(context, user.id, "Exceeded verification attempts")
            await update.message.reply_text("⚠️ You have made too many verification attempts and have been blocked.", reply_markup=ReplyKeyboardRemove())
            return
        
        self.rate_limiter.record_attempt(user.id, 'verification')
        phone_result = self.verifier.verify_phone_number(contact.phone_number)

        if phone_result['is_filipino']:
            await self.db.add_verified_user(user.id, user.username, user.first_name, phone_result['formatted_number'])
            
            # Notify admin
            await context.bot.send_message(
                ADMIN_ID,
                f"✅ **New Verified User**\n\n"
                f"**User:** {user.mention_markdown()}\n"
                f"**ID:** `{user.id}`\n"
                f"**Phone:** `{phone_result['formatted_number']}`",
                parse_mode=ParseMode.MARKDOWN
            )

            # Send links to user
            invite_links = await self.generate_invite_links(context, user.id)
            success_msg = (
                f"✅ **Verification Successful!** 🇵🇭\n\n"
                f"Welcome, {user.first_name}! You are now verified.\n\n"
                f"Here are your personal, one-time-use invite links. Please do not share them.\n\n"
                f"{invite_links}"
            )
            await update.message.reply_text(success_msg, reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN)

        else:
            await update.message.reply_text(
                f"❌ **Verification Failed**\n\n"
                f"The number you provided (`{phone_result['formatted_number']}`) does not appear to be a valid Philippine phone number. "
                f"Please try again with a +63 number.",
                reply_markup=ReplyKeyboardRemove()
            )
            await self.db.log_spam_incident(user.id, "invalid_phone", f"Provided: {phone_result['formatted_number']}")

    async def groups_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user.id in self.blocked_user_cache:
            await update.message.reply_text("❌ You are blocked.")
            return

        if not await self.db.is_verified(user.id):
            await update.message.reply_text("❌ You must be verified first. Please use the /start command.")
            return

        invite_links = await self.generate_invite_links(context, user.id)
        await update.message.reply_text(
            f"✅ Here are your new personal invite links:\n\n{invite_links}",
            parse_mode=ParseMode.MARKDOWN
        )

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user.id in self.blocked_user_cache:
            await update.message.reply_text("Status: **BLOCKED** 🚫")
            return
        
        if await self.db.is_verified(user.id):
            phone = await self.db.get_user_phone(user.id)
            await update.message.reply_text(f"✅ Status: **VERIFIED** 🇵🇭\nPhone on record: `{phone}`")
        else:
            await update.message.reply_text(" Status: **NOT VERIFIED** ❌\nUse /start to begin verification.")

    # --- Admin and Event Handlers ---

    async def my_chat_member_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handles when the bot's status changes in a group (added, promoted, removed)."""
        chat = update.my_chat_member.chat
        new_status = update.my_chat_member.new_chat_member
        
        if new_status.status == new_status.ADMINISTRATOR:
            logger.info(f"Bot was promoted to admin in {chat.title} ({chat.id}).")
            # We check for can_invite_users permission specifically.
            if new_status.can_invite_users:
                await self.db.add_managed_group(chat.id, chat.title, chat.type)
                logger.info(f"Auto-registered group: {chat.title}")
                await context.bot.send_message(
                    ADMIN_ID,
                    f"✅ **Auto-Registered Group**\n\nThe bot was made an admin with invite permissions in:\n"
                    f"**Title:** {chat.title}\n"
                    f"**ID:** `{chat.id}`"
                )
            else:
                 await context.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ **Admin Promotion Incomplete**\n\nThe bot was made an admin in {chat.title} but lacks the 'Invite Users' permission, so it was not added to managed groups."
                 )
        elif new_status.status in [new_status.MEMBER, new_status.LEFT, new_status.KICKED]:
            # If bot is demoted or removed, you might want to deactivate it in the DB.
            logger.info(f"Bot was removed or demoted in {chat.title} ({chat.id}).")

    async def join_request_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handles new users trying to join a group where the bot is an admin."""
        join_request = update.chat_join_request
        user = join_request.from_user
        chat = join_request.chat

        if user.id in self.blocked_user_cache:
            await context.bot.decline_chat_join_request(chat.id, user.id)
            logger.info(f"Declined join request from blocked user {user.id} for chat {chat.id}")
            return
            
        if not self.rate_limiter.can_join(user.id):
            await context.bot.decline_chat_join_request(chat.id, user.id)
            await self.db.log_spam_incident(user.id, "join_rate_limit", f"Chat: {chat.id}")
            logger.warning(f"Rate limited join request from {user.id} for chat {chat.id}")
            return
            
        self.rate_limiter.record_attempt(user.id, 'join')

        if await self.db.is_verified(user.id):
            try:
                await context.bot.approve_chat_join_request(chat.id, user.id)
                logger.info(f"Auto-approved verified user {user.id} for chat {chat.id}")
                await context.bot.send_message(user.id, f"✅ Your request to join **{chat.title}** was automatically approved!", parse_mode=ParseMode.MARKDOWN)
            except TelegramError as e:
                logger.error(f"Failed to approve join request for {user.id}: {e}")
        else:
            # User is not verified, prompt them to start verification.
            try:
                await context.bot.send_message(
                    user.id,
                    f"👋 Hello! To join **{chat.title}**, you first need to verify your identity with me.\n\n"
                    f"Please click here -> /start to begin the one-time verification process.",
                    parse_mode=ParseMode.MARKDOWN
                )
                # You can choose to decline immediately or leave it pending. Declining is cleaner.
                await context.bot.decline_chat_join_request(chat.id, user.id)
            except TelegramError as e:
                logger.warning(f"Could not send verification prompt to user {user.id}: {e}")
                
    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Log Errors caused by Updates."""
        logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)
        
        # Optionally, notify the admin about critical errors
        if isinstance(context.error, TelegramError):
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"🚨 **Bot Error**\n\n"
                    f"An error occurred: `{context.error}`\n\n"
                    f"Update: `{update}`"
                )
            except Exception as e:
                logger.error(f"Failed to send error notification to admin: {e}")

# --- Application Setup ---
async def main():
    """Main function to set up and run the bot."""
    if not all([BOT_TOKEN, ADMIN_ID, BOT_USERNAME]):
        logger.critical("FATAL: BOT_TOKEN, ADMIN_ID, and BOT_USERNAME environment variables must be set.")
        sys.exit(1)

    # Initialize components
    db_manager = DatabaseManager()
    await db_manager.init_database()
    
    rate_limiter = RateLimiter()
    bot_manager = FilipinoBotManager(db_manager, rate_limiter)
    await bot_manager.load_blocked_users()

    # Build the application
    application = Application.builder().token(BOT_TOKEN).build()

    # --- Add Handlers ---
    # Command Handlers
    application.add_handler(CommandHandler("start", bot_manager.start_command, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("status", bot_manager.status_command, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("groups", bot_manager.groups_command, filters=filters.ChatType.PRIVATE))

    # Message Handlers
    application.add_handler(MessageHandler(filters.CONTACT & filters.ChatType.PRIVATE, bot_manager.contact_handler))
    
    # Event Handlers
    application.add_handler(ChatMemberHandler(bot_manager.my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(ChatJoinRequestHandler(bot_manager.join_request_handler))

    # Error Handler
    application.add_error_handler(bot_manager.error_handler)

    # Start the bot
    logger.info("Starting bot...")
    try:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info(f"Bot started successfully as @{BOT_USERNAME}")
        
        # Keep the script running
        while True:
            await asyncio.sleep(3600) # Sleep for an hour

    except Exception as e:
        logger.critical(f"Bot failed to start: {e}")
    finally:
        logger.info("Stopping bot...")
        await application.stop()
        await application.shutdown()
        logger.info("Bot stopped.")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutdown signal received.")
