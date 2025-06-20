import os
import logging
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv
import sqlite3
from datetime import datetime
import phonenumbers
from phonenumbers import NumberParseException

# Load environment variables from .env
load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- DatabaseManager Class ---
class DatabaseManager:
    def __init__(self, db_path: str = "FilipinoBot.db"):
        self.db_path = db_path
        self.init_database()

    def init_database(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Creating verified users table
        cursor.execute('''CREATE TABLE IF NOT EXISTS verified_users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            phone_number TEXT,
            verified_date TIMESTAMP,
            is_banned BOOLEAN DEFAULT FALSE
        )''')
        
        # Creating join requests table
        cursor.execute('''CREATE TABLE IF NOT EXISTS join_requests (
            user_id INTEGER,
            chat_id INTEGER,
            request_date TIMESTAMP,
            status TEXT DEFAULT 'pending',
            PRIMARY KEY (user_id, chat_id)
        )''')
        
        conn.commit()
        conn.close()

    def add_verified_user(self, user_id: int, username: str, first_name: str, phone_number: str):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''INSERT OR REPLACE INTO verified_users 
                          (user_id, username, first_name, phone_number, verified_date, is_banned)
                          VALUES (?, ?, ?, ?, ?, FALSE)''', 
                          (user_id, username or "", first_name or "", phone_number, datetime.now()))
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

# --- Phone Verifier Class ---
class PhoneVerifier:
    @staticmethod
    def verify_phone_number(phone_number: str) -> dict:
        """Verify if phone number is from Philippines"""
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
                'country_code': parsed.country_code,
                'region': region,
                'formatted_number': phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL),
                'is_valid': is_valid
            }
        except NumberParseException as e:
            logger.error(f"Phone parsing error: {e}")
            return {
                'is_filipino': False,
                'country_code': None,
                'region': None,
                'formatted_number': phone_number,
                'is_valid': False
            }

# --- FilipinoBotManager Class ---
class FilipinoBotManager:
    def __init__(self):
        if not BOT_TOKEN:
            raise ValueError("BOT_TOKEN environment variable is required!")
        if not ADMIN_ID:
            raise ValueError("ADMIN_ID environment variable is required!")
        
        self.db = DatabaseManager()
        self.verifier = PhoneVerifier()

    # --- Handle Verified User and Generate Invite Link ---
    async def handle_verified_user(self, user: Update.effective_user, context: ContextTypes.DEFAULT_TYPE):
        """Generate private invite link for the verified user"""
        try:
            # Get the chat object where the user is supposed to join
            chat_id = YOUR_CHAT_ID  # Replace with your actual channel or group ID
            
            # Generate private invite link for the verified user
            invite_link = await context.bot.export_chat_invite_link(chat_id)
            
            # Send the private invite link to the verified user
            await context.bot.send_message(user.id, f"🎉 Welcome to the community! Here's your private invitation link: {invite_link}")

            # Notify admin
            admin_notification = f"✅ User {user.first_name} has been verified and received the private invite link."
            await context.bot.send_message(ADMIN_ID, admin_notification, parse_mode=ParseMode.MARKDOWN)

        except Exception as e:
            logger.error(f"Error generating invite link for user {user.id}: {e}")
            await context.bot.send_message(user.id, "❌ Sorry, there was an issue generating your invite link. Please contact the admin.")

    async def handle_contact_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle phone number verification"""
        if not update.message.contact:
            return
        
        contact = update.message.contact
        user = update.effective_user
        
        # Security check
        if contact.user_id != user.id:
            await update.message.reply_text(
                "❌ Sariling phone number mo lang ang pwedeng i-verify!",
                reply_markup=ReplyKeyboardRemove()
            )
            return
        
        # Remove keyboard
        await update.message.reply_text(
            "📱 Ini-verify ang phone number...",
            reply_markup=ReplyKeyboardRemove()
        )
        
        # Verify phone number
        phone_result = self.verifier.verify_phone_number(contact.phone_number)
        
        if phone_result['is_filipino']:
            # SUCCESS - Add to verified users
            self.db.add_verified_user(
                user.id, 
                user.username, 
                user.first_name, 
                contact.phone_number
            )
            
            success_msg = f"""
✅ **VERIFIED!** 🇵🇭

Welcome sa Filipino community, {user.first_name}!

📱 **Verified Number:** {phone_result['formatted_number']}
🎉 **Status:** Approved for all Filipino channels/groups

🚀 **NEW BENEFIT:** Auto-approval sa future join requests!
Hindi mo na kailangan maghintay sa admin approval.

**Next steps:**
• Pwede mo na i-rejoin ang mga groups na pending
• Auto-approve ka na sa new Filipino groups
• One-time verification lang ito!
            """
            
            await update.message.reply_text(success_msg, parse_mode=ParseMode.MARKDOWN)
            
            # Call method to send private invite link
            await self.handle_verified_user(user, context)
            
        else:
            # FAILED - suspicious number, notify admin and block user
            country_info = phone_result.get('region', 'Unknown')
            fail_msg = f"""
❌ **Hindi Philippine Number**

**Detected:**
• Number: {phone_result['formatted_number']}
• Country: {country_info}
• Expected: Philippines 🇵🇭 (+63)

**Para ma-verify:**
• Gamitin ang Philippine number mo
• I-try ulit ang `/start`
            """
            
            await update.message.reply_text(fail_msg, parse_mode=ParseMode.MARKDOWN)
            
            # Notify admin of suspicious number
            admin_suspicious_msg = f"""
⚠️ **Suspicious User - Invalid Phone Number Detected!**

**User:** {user.first_name} (@{user.username or 'no_username'})
**ID:** `{user.id}`
**Phone Number:** {phone_result['formatted_number']}
**Region:** {country_info}

**Action Required:**
• Manual review and possible rejection of the join request.
            """
            await context.bot.send_message(ADMIN_ID, admin_suspicious_msg, parse_mode=ParseMode.MARKDOWN)

            # Cancel or block user from group
            await context.bot.kick_chat_member(update.effective_chat.id, user.id)
            logger.info(f"User {user.id} blocked due to invalid phone number")

# --- Main Function ---
def main():
    if not BOT_TOKEN or not ADMIN_ID:
        logger.error("BOT_TOKEN and ADMIN_ID environment variables are required!")
        return

    bot_manager = FilipinoBotManager()
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", bot_manager.start_command))
    application.add_handler(MessageHandler(filters.CONTACT, bot_manager.handle_contact_message))
    
    logger.info("🇵🇭 Filipino Verification Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
