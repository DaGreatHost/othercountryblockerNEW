import os
import logging
from datetime import datetime
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, ChatJoinRequestHandler
import phonenumbers
from phonenumbers import NumberParseException
import sqlite3
import time

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))

# Database Manager Class
class DatabaseManager:
    def __init__(self, db_path: str = "filipino_bot.db"):
        self.db_path = db_path
        self.init_database()

    def init_database(self):
        conn = sqlite3.connect(self.db_path)
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
        conn.commit()
        conn.close()

    def add_verified_user(self, user_id: int, username: str, first_name: str, phone_number: str):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO verified_users 
            (user_id, username, first_name, phone_number, verified_date, is_banned)
            VALUES (?, ?, ?, ?, ?, FALSE)
        ''', (user_id, username or "", first_name or "", phone_number, datetime.now()))
        conn.commit()
        conn.close()

    def is_verified(self, user_id: int) -> bool:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM verified_users WHERE user_id = ? AND is_banned = FALSE', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return result is not None

    def ban_user(self, user_id: int):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('UPDATE verified_users SET is_banned = TRUE WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()

    def add_join_request(self, user_id: int, chat_id: int):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO join_requests 
            (user_id, chat_id, request_date, status)
            VALUES (?, ?, ?, 'pending')
        ''', (user_id, chat_id, datetime.now()))
        conn.commit()
        conn.close()

    def update_join_request_status(self, user_id: int, chat_id: int, status: str):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE join_requests 
            SET status = ? 
            WHERE user_id = ? AND chat_id = ?
        ''', (status, user_id, chat_id))
        conn.commit()
        conn.close()

# Phone Number Verification Logic
class PhoneVerifier:
    @staticmethod
    def verify_phone_number(phone_number: str) -> dict:
        """Verify if phone number is from the Philippines"""
        try:
            cleaned_number = phone_number.replace(" ", "").replace("-", "")
            if cleaned_number.startswith("09"):
                cleaned_number = "+63" + cleaned_number[1:]
            elif cleaned_number.startswith("9") and len(cleaned_number) == 10:
                cleaned_number = "+63" + cleaned_number
            elif cleaned_number.startswith("63") and not cleaned_number.startswith("+63"):
                cleaned_number = "+" + cleaned_number
            elif not cleaned_number.startswith("+") and len(cleaned_number) == 10:
                cleaned_number = "+63" + cleaned_number[1:]
            
            parsed = phonenumbers.parse(cleaned_number)
            region = phonenumbers.region_code_for_number(parsed)
            is_valid = phonenumbers.is_valid_number(parsed)
            is_ph = region == 'PH' and parsed.country_code == 63 and is_valid
            
            return {
                'is_filipino': is_ph,
                'formatted_number': phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL),
                'is_valid': is_valid
            }
        except NumberParseException as e:
            logger.error(f"Phone parsing error: {e}")
            return {
                'is_filipino': False,
                'formatted_number': phone_number,
                'is_valid': False
            }

# FilipinoBotManager class to handle join requests, verifications, and group management
class FilipinoBotManager:
    def __init__(self):
        if not BOT_TOKEN:
            raise ValueError("BOT_TOKEN environment variable is required!")
        if not ADMIN_ID:
            raise ValueError("ADMIN_ID environment variable is required!")
        
        self.db = DatabaseManager()
        self.verifier = PhoneVerifier()

    async def handle_join_request(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle join requests - reject non-PH numbers and remove join request"""
        try:
            if not update.chat_join_request:
                return

            join_request = update.chat_join_request
            user = join_request.from_user
            chat = join_request.chat

            # Skip bots and admin
            if user.is_bot or user.id == ADMIN_ID:
                return

            # Track join request
            self.db.add_join_request(user.id, chat.id)

            # Handle case where user hasn't shared their phone number
            if not self.db.is_verified(user.id):
                # Request user to share phone number
                verification_msg = """
Hi! Please share your phone number to verify your Filipino status.

Click the button below to share your phone number:
"""
                contact_keyboard = [[KeyboardButton("📱 Share my Phone Number", request_contact=True)]]
                contact_markup = ReplyKeyboardMarkup(contact_keyboard, one_time_keyboard=True, resize_keyboard=True)
                
                await context.bot.send_message(user.id, verification_msg, reply_markup=contact_markup)
                return

            # Now verify the phone number provided by the user
            phone_result = self.verifier.verify_phone_number(user.phone_number)
            
            if phone_result['is_filipino']:
                # ✅ Verified user - Auto-approve
                await context.bot.approve_chat_join_request(chat.id, user.id)
                self.db.update_join_request_status(user.id, chat.id, 'approved')

                # Send private invite link
                invite_link = await context.bot.export_chat_invite_link(chat.id)
                welcome_msg = f"""
🎉 **Welcome!** ✅

Hi {user.first_name}, you've been auto-approved to join {chat.title}!

Here’s your private invitation link: {invite_link}
                """
                await context.bot.send_message(user.id, welcome_msg, parse_mode=ParseMode.MARKDOWN)

                # Notify admin
                admin_notification = f"""
✅ **Auto-Approved Join Request**

**User:** {user.first_name} (@{user.username or 'no_username'})
**ID:** `{user.id}`
**Chat:** {chat.title} (`{chat.id}`)
**Status:** Verified Filipino User - Auto-approved
                """
                await context.bot.send_message(ADMIN_ID, admin_notification, parse_mode=ParseMode.MARKDOWN)

            else:
                # ❌ Non-PH number, reject and remove from group
                await context.bot.decline_chat_join_request(chat.id, user.id)
                self.db.update_join_request_status(user.id, chat.id, 'rejected')

                # Notify user and admin
                rejection_msg = f"""
❌ **Join Request Rejected!**

Hi {user.first_name}, unfortunately, your phone number is not from the Philippines. You cannot join **{chat.title}**.

If you believe this is a mistake, please try again with a valid Philippine phone number.
                """
                await context.bot.send_message(user.id, rejection_msg, parse_mode=ParseMode.MARKDOWN)

                admin_notification = f"""
⚠️ **Non-PH Number Detected!**

**User:** {user.first_name} (@{user.username or 'no_username'})
**ID:** `{user.id}`
**Chat:** {chat.title} (`{chat.id}`)
**Status:** Rejected due to invalid phone number.
                """
                await context.bot.send_message(ADMIN_ID, admin_notification, parse_mode=ParseMode.MARKDOWN)

        except Exception as e:
            logger.error(f"Error handling join request: {e}")

    async def start_verification(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start phone verification process"""
        user = update.effective_user
        
        contact_keyboard = [[KeyboardButton("📱 I-Share ang Phone Number Ko", request_contact=True)]]
        contact_markup = ReplyKeyboardMarkup(
            contact_keyboard, 
            one_time_keyboard=True, 
            resize_keyboard=True
        )
        
        verification_msg = f"""
🇵🇭 *Filipino Verification*

Hi {user.first_name}, to verify your Filipino status, please share your phone number by clicking the button below.

**Requirements:**
• Philippine number (+63) only
• Click the button below to share
• Auto-approval once verified

👇 *Click to share:*
        """
        
        await update.message.reply_text(
            verification_msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=contact_markup
        )

    async def handle_contact_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle phone number verification"""
        if not update.message.contact:
            return
        
        contact = update.message.contact
        user = update.effective_user
        
        # Check if the contact shared is from the user
        if contact.user_id != user.id:
            await update.message.reply_text("❌ Only your own phone number can be verified!", reply_markup=ReplyKeyboardRemove())
            return
        
        # Verify the phone number
        phone_result = self.verifier.verify_phone_number(contact.phone_number)
        
        if phone_result['is_filipino']:
            # Add to verified users
            self.db.add_verified_user(user.id, user.username, user.first_name, contact.phone_number)
            
            # Notify user
            success_msg = f"""
✅ **Verified!** 🇵🇭

Welcome to the Filipino community, {user.first_name}!

📱 **Verified Number:** {phone_result['formatted_number']}
🎉 **Status:** Approved for all Filipino channels/groups

🚀 **Benefit:** Auto-approval for future join requests!
            """
            await update.message.reply_text(success_msg, parse_mode=ParseMode.MARKDOWN)

        else:
            # Notify user if not a valid Filipino number
            fail_msg = f"""
❌ **Invalid Phone Number!**

• **Detected:** {phone_result['formatted_number']}
• **Expected:** Philippines 🇵🇭 (+63)

**Please try again with a valid Philippine phone number.**
            """
            await update.message.reply_text(fail_msg, parse_mode=ParseMode.MARKDOWN)

# Main function to run the bot
def main():
    """Main function"""
    if not BOT_TOKEN or not ADMIN_ID:
        logger.error("BOT_TOKEN and ADMIN_ID environment variables are required!")
        return
    
    bot_manager = FilipinoBotManager()
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", bot_manager.start_verification))
    application.add_handler(MessageHandler(filters.CONTACT, bot_manager.handle_contact_message))
    application.add_handler(ChatJoinRequestHandler(bot_manager.handle_join_request))
    
    logger.info("🇵🇭 Filipino Verification Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
