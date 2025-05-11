# manager_and_bot.py
import asyncio
import os
import logging
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

from user_media_saver import run_user_instance # Your Telethon script
from telethon import errors as telethon_errors # For specific error handling

# Load environment variables from .env file
load_dotenv()

APP_API_ID = os.getenv('API_ID')
APP_API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')
HANDLER_COMMAND = os.getenv('HANDLER_COMMAND', '.d')
USER_IDS_FILE = "logged_in_user_ids.txt" # File to store user IDs

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot_activity.log"), # Log to a file
        logging.StreamHandler()                  # Log to console
    ]
)
logger = logging.getLogger(__name__)

# --- Environment Variable Checks ---
if not BOT_TOKEN:
    logger.critical("FATAL: BOT_TOKEN not set in .env. Exiting.")
    exit()
if not APP_API_ID or not APP_API_HASH:
    logger.warning("WARNING: API_ID or API_HASH not set in .env. Login for new users will fail.")
    # Allow bot to run for existing sessions, but log a clear warning.
try:
    if APP_API_ID: APP_API_ID = int(APP_API_ID)
except ValueError:
    logger.critical("FATAL: API_ID must be an integer if set. Exiting.")
    exit()


# --- Conversation States for Login ---
AWAIT_PHONE, AWAIT_CODE, AWAIT_PASSWORD = range(3) # Renamed for clarity

# --- Store for active user Telethon tasks ---
# Key: bot_user_id (from Update), Value: asyncio.Task (the task running run_user_instance)
active_user_telethon_tasks = {}

# --- Function to manage user IDs file ---
async def add_user_id_to_store(user_id_to_add: int):
    """Adds a user ID to the storage file, ensuring no duplicates."""
    user_id_to_add_str = str(user_id_to_add) # Store as string
    try:
        existing_ids = set()
        if os.path.exists(USER_IDS_FILE):
            with open(USER_IDS_FILE, 'r') as f:
                for line in f:
                    existing_ids.add(line.strip())
        
        if user_id_to_add_str not in existing_ids:
            # Add and resort numerically for cleaner file
            numeric_ids = {int(id_str) for id_str in existing_ids if id_str.isdigit()}
            numeric_ids.add(int(user_id_to_add_str))
            
            with open(USER_IDS_FILE, 'w') as f:
                for uid_val in sorted(list(numeric_ids)):
                    f.write(str(uid_val) + '\n')
            logger.info(f"User ID {user_id_to_add_str} (Telethon Account) added to {USER_IDS_FILE}.")
        # else:
            # logger.info(f"User ID {user_id_to_add_str} already exists in {USER_IDS_FILE}.")
    except IOError as e:
        logger.error(f"IOError while updating {USER_IDS_FILE}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error while updating {USER_IDS_FILE}: {e}", exc_info=True)

# --- Bot Command Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    logger.info(f"User {user.id} ({user.username}) started chat with bot.")
    await update.message.reply_html(
        rf"Hi {user.mention_html()}!",
        f"\nWelcome! I can help you save media to your 'Saved Messages'."
        f"\n➡️ Use /login to start the setup process."
        f"\n\nℹ️ Once logged in, reply with '<b>{HANDLER_COMMAND}</b>' to any message with media to save it."
        f"\n\n➡️ Use /logout to stop the service for your account."
        f"\n➡️ Use /help for more info."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"❓ How to use:\n"
        f"1. Type /login to begin authorizing the bot with your Telegram account.\n"
        f"2. I will ask for your phone number (international format, e.g., +12345678900).\n"
        f"3. Then, I'll ask for the login code Telegram sends to your account.\n"
        f"4. If you have Two-Factor Authentication (2FA) enabled, I'll ask for your 2FA password.\n"
        f"5. Once logged in, go to any chat. Reply directly to a message containing media using just the command: {HANDLER_COMMAND}\n"
        f"6. The media will be downloaded by the service and sent to your 'Saved Messages' chat.\n"
        f"7. Use /logout to stop the service. This will also delete your session file from my server."
    )

# --- Login Conversation Functions ---
async def login_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the login conversation."""
    user_id = update.effective_user.id # This is the ID of the user talking to the BOT
    if not APP_API_ID or not APP_API_HASH:
        await update.message.reply_text("Sorry, the bot is not configured correctly by the administrator (API details missing). Login is currently unavailable.")
        return ConversationHandler.END

    if user_id in active_user_telethon_tasks and not active_user_telethon_tasks[user_id].done():
        # Check if task is truly running or if it's an old entry for a failed task
        # This part could be more robust, e.g. by checking task's exception status
        await update.message.reply_text("You seem to have an active session. Use /logout first if you want to start a new one.")
        return ConversationHandler.END
    
    logger.info(f"User {user_id} starting login process.")
    await update.message.reply_text("Okay, let's log you in. Please send me your phone number in international format (e.g., +12345678900). Type /cancel to stop.")
    return AWAIT_PHONE

async def received_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles receiving the phone number."""
    bot_user_id = update.effective_user.id
    phone = update.message.text.strip()

    if not (phone.startswith('+') and phone[1:].isdigit() and len(phone) > 7): # Basic validation
        await update.message.reply_text("That doesn't look like a valid phone number. Please try again (e.g., +12345678900) or type /cancel.")
        return AWAIT_PHONE # Stay in the same state

    context.user_data['phone_to_login'] = phone
    context.user_data['code_future'] = asyncio.Future()
    context.user_data['password_future'] = asyncio.Future()
    
    session_name = f"user_{bot_user_id}" # Session file name, unique to the bot user ID

    # Define the callbacks that Telethon's run_user_instance will use
    async def get_code_from_bot_callback() -> str | None:
        logger.info(f"Telethon (bot_user_id {bot_user_id}, phone {phone}) is now awaiting code_future.")
        try:
            code = await asyncio.wait_for(context.user_data['code_future'], timeout=300.0) # 5 min timeout
            return code
        except asyncio.TimeoutError:
            logger.warning(f"User {bot_user_id} (phone {phone}) timed out providing code to bot.")
            # The ConversationHandler should ideally inform the user about the timeout.
            return None 

    async def get_password_from_bot_callback() -> str | None:
        logger.info(f"Telethon (bot_user_id {bot_user_id}, phone {phone}) is now awaiting password_future.")
        try:
            password = await asyncio.wait_for(context.user_data['password_future'], timeout=300.0)
            return password
        except asyncio.TimeoutError:
            logger.warning(f"User {bot_user_id} (phone {phone}) timed out providing 2FA password to bot.")
            return None

    await update.message.reply_text(f"Thank you. Attempting to log in with {phone}.\n"
                                     "A code will be sent to your Telegram account. Please send that code back to me when I ask for it.")

    # Start the Telethon login task in the background
    telethon_task = asyncio.create_task(
        run_user_instance(
            session_name, APP_API_ID, APP_API_HASH, HANDLER_COMMAND,
            phone, get_code_from_bot_callback, get_password_from_bot_callback
        )
    )
    # Store this task; it will return the Telethon user ID or None/raise error
    active_user_telethon_tasks[bot_user_id] = telethon_task
    context.user_data['current_login_task'] = telethon_task 

    # After a brief moment (for Telethon's send_code_request to likely execute), ask user for code.
    await asyncio.sleep(1.5) # Small delay, adjust if needed
    await update.message.reply_text("Please send me the login code you received from Telegram (it's usually a 5 or 6 digit number).")
    return AWAIT_CODE


async def received_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles receiving the login code."""
    bot_user_id = update.effective_user.id
    code = update.message.text.strip()
    phone = context.user_data.get('phone_to_login', 'N/A')


    if 'code_future' in context.user_data and not context.user_data['code_future'].done():
        logger.info(f"Bot user {bot_user_id} (phone {phone}) submitted code: {code[:2]}***")
        context.user_data['code_future'].set_result(code) # Unblock Telethon task
        await update.message.reply_text("Got it! Processing code...")
        
        # The Telethon task will now proceed. It might finish login, or it might need 2FA.
        # If 2FA is needed, run_user_instance will call get_password_from_bot_callback.
        # That callback awaits password_future, so we transition the conversation to AWAIT_PASSWORD.
        await update.message.reply_text("If Two-Factor Authentication (2FA) is enabled for your account, please send your 2FA password now. Otherwise, the login will complete if the code was correct.")
        return AWAIT_PASSWORD
    else:
        logger.warning(f"User {bot_user_id} (phone {phone}) sent code, but code_future was not ready or already done.")
        await update.message.reply_text("Something went wrong, or the code was already processed. If login fails, please try /cancel and then /login again.")
        # Don't end conversation here, let received_password handle the next step or failure.
        # Or, could try to re-check task status. For simplicity, proceed to AWAIT_PASSWORD.
        return AWAIT_PASSWORD

async def received_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles receiving the 2FA password."""
    bot_user_id = update.effective_user.id
    password = update.message.text.strip() # Don't log password
    phone = context.user_data.get('phone_to_login', 'N/A')
    logger.info(f"Bot user {bot_user_id} (phone {phone}) submitted 2FA password.")

    # Fulfill the password_future if it's waiting
    if 'password_future' in context.user_data and not context.user_data['password_future'].done():
        context.user_data['password_future'].set_result(password)
        await update.message.reply_text("Password received. Finalizing login, please wait...")
    # else: This state might be reached if no 2FA was needed and login succeeded/failed after code.

    # Now, await the result of the Telethon login task.
    login_task = context.user_data.get('current_login_task')
    if login_task:
        try:
            # Wait for the run_user_instance task to return.
            # It returns Telethon me.id on success, None on failure, or raises specific login errors.
            # Give it a reasonable time to complete the sign_in process after password.
            logged_in_telethon_user_id = await asyncio.wait_for(login_task, timeout=30.0) # Increased timeout

            if isinstance(logged_in_telethon_user_id, int): # Success!
                await add_user_id_to_store(logged_in_telethon_user_id) # Store the Telethon account ID
                await update.message.reply_text(
                    f"✅ Login successful! (Telegram Account ID: {logged_in_telethon_user_id})\n"
                    f"I am now monitoring your outgoing replies for '<b>{HANDLER_COMMAND}</b>'.",
                    parse_mode='HTML'
                )
                # The task in active_user_telethon_tasks[bot_user_id] is the one for run_user_instance.
                # run_user_instance itself spawns the run_until_disconnected task.
                # For logout, we cancel the main run_user_instance task.
            elif logged_in_telethon_user_id is None: # Explicit failure from run_user_instance
                await update.message.reply_text("❌ Login failed. The details might have been incorrect. Please try /login again or /cancel.")
                active_user_telethon_tasks.pop(bot_user_id, None) # Remove failed task entry
            else: # Should not happen if return type is int | None
                await update.message.reply_text("❓ Login status unclear. Please try /login again or /cancel.")
                active_user_telethon_tasks.pop(bot_user_id, None)

        except asyncio.TimeoutError:
            logger.error(f"Login task for user {bot_user_id} (phone {phone}) timed out after password submission.")
            await update.message.reply_text("❌ Login process timed out. Please try /login again or /cancel.")
            active_user_telethon_tasks.pop(bot_user_id, None)
        except (telethon_errors.PhoneCodeInvalidError, telethon_errors.PhoneNumberInvalidError, telethon_errors.PasswordHashInvalidError) as specific_telethon_error:
            error_message = str(specific_telethon_error)
            if isinstance(specific_telethon_error, telethon_errors.PasswordHashInvalidError):
                error_message = "The 2FA password you entered was incorrect."
            elif isinstance(specific_telethon_error, telethon_errors.PhoneCodeInvalidError):
                error_message = "The login code you entered was incorrect."

            await update.message.reply_text(f"❌ Login failed: {error_message}. Please try /login again or /cancel.")
            active_user_telethon_tasks.pop(bot_user_id, None)
        except Exception as e: # Catch other exceptions from awaiting login_task
            logger.error(f"Error during final login step for user {bot_user_id} (phone {phone}): {e}", exc_info=True)
            await update.message.reply_text(f"❌ An unexpected error occurred during login: {e}. Please try /login again or /cancel.")
            active_user_telethon_tasks.pop(bot_user_id, None)
        finally:
            context.user_data.clear() # Clean up user_data for this conversation
            return ConversationHandler.END # End conversation
    else:
        logger.warning(f"User {bot_user_id} (phone {phone}) sent password, but login_task not found in context.")
        await update.message.reply_text("Error: Could not find the login task. Please try /login again or /cancel.")
        context.user_data.clear()
        return ConversationHandler.END

async def cancel_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the ongoing login conversation."""
    bot_user_id = update.effective_user.id
    phone = context.user_data.get('phone_to_login', 'N/A')
    logger.info(f"User {bot_user_id} (phone {phone}) cancelled login process.")
    await update.message.reply_text("Login process cancelled.")
    
    # Signal futures to unblock Telethon task if it's waiting, allowing it to terminate cleanly
    if context.user_data.get('code_future') and not context.user_data['code_future'].done():
        context.user_data['code_future'].set_result(None)
    if context.user_data.get('password_future') and not context.user_data['password_future'].done():
        context.user_data['password_future'].set_result(None)

    # Cancel the main Telethon task associated with this user's login attempt
    task = active_user_telethon_tasks.pop(bot_user_id, None)
    if task and not task.done():
        task.cancel()
        logger.info(f"Cancelled Telethon task for user {bot_user_id} (phone {phone}) due to /cancel command.")

    context.user_data.clear() # Clear any stored data for this user's conversation
    return ConversationHandler.END

async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs out the user by stopping their Telethon task and deleting the session file."""
    bot_user_id = update.effective_user.id
    logger.info(f"User {bot_user_id} initiated logout.")
    task = active_user_telethon_tasks.pop(bot_user_id, None) # Remove task from active dict
    
    if task:
        if not task.done():
            task.cancel() # Cancel the asyncio.Task that runs run_user_instance
            logger.info(f"Attempting to cancel Telethon task for user {bot_user_id} upon logout.")
            try:
                # Optionally wait for task to acknowledge cancellation, but don't block too long
                await asyncio.wait_for(task, timeout=5.0) 
            except asyncio.CancelledError:
                logger.info(f"Telethon task for user {bot_user_id} was cancelled successfully.")
            except asyncio.TimeoutError:
                logger.warning(f"Telethon task for user {bot_user_id} did not fully cancel within timeout on logout.")
            except Exception as e: # Other exceptions during task cleanup
                logger.error(f"Exception while waiting for Telethon task cancellation for user {bot_user_id}: {e}")
        else: # Task was already done (e.g. failed previously)
            logger.info(f"Telethon task for user {bot_user_id} was already done before logout.")

        session_file = os.path.join('sessions', f"user_{bot_user_id}.session")
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
                await update.message.reply_text("✅ You have been logged out. Your session file has been removed.")
                logger.info(f"Session file {session_file} for user {bot_user_id} removed.")
            except OSError as e:
                await update.message.reply_text("⚠️ Logged out, but failed to remove session file. Please contact admin.")
                logger.error(f"Failed to remove session file {session_file} for user {bot_user_id}: {e}")
        else:
            await update.message.reply_text("✅ You have been logged out (no session file found to remove).")
    else:
        await update.message.reply_text("ℹ️ You were not logged in, or your session was already inactive.")

def main() -> None:
    """Starts the bot."""
    # Ensure essential directories exist at startup
    if not os.path.exists('sessions'): os.makedirs('sessions')
    if not os.path.exists(DOWNLOAD_PATH_BASE): os.makedirs(DOWNLOAD_PATH_BASE)

    application = Application.builder().token(BOT_TOKEN).build()

    # Login Conversation Handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('login', login_entry)],
        states={
            AWAIT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_phone)],
            AWAIT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_code)],
            AWAIT_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_password)],
        },
        fallbacks=[CommandHandler('cancel', cancel_login)],
        per_user=True, 
        per_chat=True, # For user_data isolation, though this bot is likely 1-to-1
        conversation_timeout=600 # Optional: 10 minutes for the whole login conversation
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("logout", logout_command))

    logger.info("Bot starting and ready to poll for updates...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()