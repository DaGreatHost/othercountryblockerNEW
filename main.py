import os
import logging
from datetime import datetime
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, ChatJoinRequestHandler
import phonenumbers
from phonenumbers import NumberParseException
import sqlite3

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

    def get_user_phone(self, user_id: int) -> str:
        """Get verified phone number for a user"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT phone_number FROM verified_users WHERE user_id = ? AND is_banned = FALSE', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else None

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
            cleaned_number = phone_number.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
            
            # Handle various Philippine number formats
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
        """Handle join requests - approve PH numbers, request verification for unverified users"""
        try:
            if not update.chat_join_request:
                return

            join_request = update.chat_join_request
            user = join_request.from_user
            chat = join_request.chat

            # Skip bots and admin
            if user.is_bot or user.id == ADMIN_ID:
                await context.bot.approve_chat_join_request(chat.id, user.id)
                return

            # Track join request
            self.db.add_join_request(user.id, chat.id)

            # Check if user is already verified
            if self.db.is_verified(user.id):
                # Get stored phone number for verification
                stored_phone = self.db.get_user_phone(user.id)
                if stored_phone:
                    phone_result = self.verifier.verify_phone_number(stored_phone)
                    
                    if phone_result['is_filipino']:
                        # ✅ Verified Filipino user - Auto-approve
                        await context.bot.approve_chat_join_request(chat.id, user.id)
                        self.db.update_join_request_status(user.id, chat.id, 'approved')

                        # Send welcome message
                        welcome_msg = f"""
🎉 **Welcome!** ✅

Hi {user.first_name}, you've been auto-approved to join **{chat.title}**!

Your Filipino verification status is confirmed. Enjoy the community! 🇵🇭
                        """
                        try:
                            await context.bot.send_message(user.id, welcome_msg, parse_mode=ParseMode.MARKDOWN)
                        except Exception as e:
                            logger.warning(f"Could not send welcome message to user {user.id}: {e}")

                        # Notify admin
                        admin_notification = f"""
✅ **Auto-Approved Join Request**

**User:** {user.first_name} (@{user.username or 'no_username'})
**ID:** `{user.id}`
**Chat:** {chat.title} (`{chat.id}`)
**Status:** Verified Filipino User - Auto-approved
                        """
                        try:
                            await context.bot.send_message(ADMIN_ID, admin_notification, parse_mode=ParseMode.MARKDOWN)
                        except Exception as e:
                            logger.warning(f"Could not send admin notification: {e}")
                        return
            
            # User is not verified - request phone verification
            verification_msg = f"""
🇵🇭 **Filipino Verification Required**

Hi {user.first_name}! To join **{chat.title}**, please verify your Filipino status by sharing your Philippine phone number.

**How to verify:**
1. Click the button below to share your phone number
2. Only Philippine numbers (+63) are accepted
3. You'll be auto-approved once verified

**Your privacy:** Your phone number is only used for verification purposes.

👇 **Click to share your phone number:**
            """
            contact_keyboard = [[KeyboardButton("📱 Share my Philippine Phone Number", request_contact=True)]]
            contact_markup = ReplyKeyboardMarkup(contact_keyboard, one_time_keyboard=True, resize_keyboard=True)
            
            try:
                await context.bot.send_message(user.id, verification_msg, reply_markup=contact_markup, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.error(f"Could not send verification message to user {user.id}: {e}")
                # Decline the request if we can't contact the user
                await context.bot.decline_chat_join_request(chat.id, user.id)
                self.db.update_join_request_status(user.id, chat.id, 'rejected')

        except Exception as e:
            logger.error(f"Error handling join request: {e}")

    async def start_verification(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start phone verification process"""
        user = update.effective_user
        
        if self.db.is_verified(user.id):
            await update.message.reply_text(
                "✅ You are already verified as a Filipino user! 🇵🇭\n\n"
                "You can now join Filipino groups and channels without additional verification.",
                reply_markup=ReplyKeyboardRemove()
            )
            return
        
        contact_keyboard = [[KeyboardButton("📱 I-Share ang Phone Number Ko", request_contact=True)]]
        contact_markup = ReplyKeyboardMarkup(
            contact_keyboard, 
            one_time_keyboard=True, 
            resize_keyboard=True
        )
        
        verification_msg = f"""
🇵🇭 **Filipino Verification**

Hi {user.first_name}! To verify your Filipino status, please share your Philippine phone number by clicking the button below.

**Requirements:**
• Philippine number (+63) only
• Click the button below to share
• Auto-approval once verified

**Benefits:**
• Access to Filipino groups/channels
• Auto-approval for future join requests
• One-time verification process

👇 **Click to share:**
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
            await update.message.reply_text(
                "❌ Only your own phone number can be verified!", 
                reply_markup=ReplyKeyboardRemove()
            )
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

You can now join Filipino groups and will be automatically approved! 🎊
            """
            await update.message.reply_text(
                success_msg, 
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ReplyKeyboardRemove()
            )

            # Notify admin
            admin_notification = f"""
✅ **New Verified User**

**User:** {user.first_name} (@{user.username or 'no_username'})
**ID:** `{user.id}`
**Phone:** {phone_result['formatted_number']}
**Status:** Successfully verified as Filipino
            """
            try:
                await context.bot.send_message(ADMIN_ID, admin_notification, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.warning(f"Could not send admin notification: {e}")

        else:
            # Notify user if not a valid Filipino number
            fail_msg = f"""
❌ **Invalid Phone Number!**

• **Number:** {phone_result['formatted_number']}
• **Expected:** Philippines 🇵🇭 (+63)
• **Detected Region:** {phone_result.get('region', 'Unknown')}

**Please try again with a valid Philippine phone number.**

Common formats:
• +63 9XX XXX XXXX
• 09XX XXX XXXX
• 63 9XX XXX XXXX
            """
            await update.message.reply_text(
                fail_msg, 
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ReplyKeyboardRemove()
            )

    async def handle_status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Check verification status"""
        user = update.effective_user
        
        if self.db.is_verified(user.id):
            phone = self.db.get_user_phone(user.id)
            status_msg = f"""
✅ **Verification Status: VERIFIED** 🇵🇭

**User:** {user.first_name}
**Phone:** {phone}
**Status:** Active Filipino User
**Benefits:** Auto-approval for Filipino groups

You're all set! 🎉
            """
        else:
            status_msg = f"""
❌ **Verification Status: NOT VERIFIED**

**User:** {user.first_name}
**Status:** Unverified

To get verified, use /start and share your Philippine phone number.
            """
        
        await update.message.reply_text(status_msg, parse_mode=ParseMode.MARKDOWN)

    async def handle_help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show help information"""
        help_msg = """
🇵🇭 **Filipino Verification Bot Help**

**Commands:**
• `/start` - Start verification process
• `/status` - Check your verification status
• `/help` - Show this help message

**How it works:**
1. Use `/start` to begin verification
2. Share your Philippine phone number
3. Get auto-approved for Filipino groups
4. One-time verification for all groups

**Supported formats:**
• +63 9XX XXX XXXX
• 09XX XXX XXXX
• 63 9XX XXX XXXX

**Need help?** Contact the admin if you have issues.
        """
        await update.message.reply_text(help_msg, parse_mode=ParseMode.MARKDOWN)

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
    application.add_handler(CommandHandler("status", bot_manager.handle_status_command))
    application.add_handler(CommandHandler("help", bot_manager.handle_help_command))
    application.add_handler(MessageHandler(filters.CONTACT, bot_manager.handle_contact_message))
    application.add_handler(ChatJoinRequestHandler(bot_manager.handle_join_request))
    
    logger.info("🇵🇭 Filipino Verification Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
