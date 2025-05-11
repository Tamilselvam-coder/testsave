# user_media_saver.py
import asyncio
import os
import logging
from datetime import datetime as dt # Not strictly used in current save logic but good for potential future use
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession # Not used for file session but good to have if switching

logger = logging.getLogger(__name__) # Get logger named after the module
DOWNLOAD_PATH_BASE = "user_downloads/"


async def run_user_instance(
    session_name: str,
    api_id: int,
    api_hash: str,
    command_trigger: str,
    phone_to_login: str,
    get_code_callback: callable, # async def func() -> str | None
    get_password_callback: callable # async def func() -> str | None
) -> int | None: # Returns Telethon user ID on success, None on failure
    """
    Runs a Telethon client instance for a specific user, handling login via bot callbacks.
    Returns the user's Telegram ID if login is successful and monitoring starts, None otherwise.
    """
    # Ensure necessary directories exist
    if not os.path.exists('sessions'):
        os.makedirs('sessions')
    if not os.path.exists(DOWNLOAD_PATH_BASE):
        os.makedirs(DOWNLOAD_PATH_BASE)

    client_session_path = os.path.join('sessions', session_name)
    client = TelegramClient(client_session_path, api_id, api_hash)

    logger.info(f"[{session_name}] Attempting to connect for {phone_to_login}...")

    try:
        if not await client.connect():
            logger.error(f"[{session_name}] Failed to connect to Telegram infrastructure.")
            return None

        me = None # Initialize 'me' to store user entity
        if not await client.is_user_authorized():
            logger.info(f"[{session_name}] User not authorized. Initiating login for {phone_to_login}.")
            try:
                await client.send_code_request(phone_to_login)
                logger.info(f"[{session_name}] Code request sent. Awaiting code via bot callback for {phone_to_login}.")
                user_code = await get_code_callback() # This callback is provided by manager_and_bot.py
                if user_code is None:
                    logger.warning(f"[{session_name}] Did not receive code from user {phone_to_login} via bot (cancelled or timed out).")
                    await client.disconnect()
                    return None

                logger.info(f"[{session_name}] Received code for {phone_to_login}, attempting to sign in.")
                # Try to sign in with the code
                me = await client.sign_in(phone=phone_to_login, code=user_code)

            except errors.SessionPasswordNeededError:
                logger.info(f"[{session_name}] 2FA password needed for {phone_to_login}. Awaiting password via bot callback.")
                user_password = await get_password_callback() # Callback provided by manager_and_bot.py
                if user_password is None:
                    logger.warning(f"[{session_name}] Did not receive 2FA password from user {phone_to_login} via bot.")
                    await client.disconnect()
                    return None
                
                logger.info(f"[{session_name}] Received 2FA password for {phone_to_login}, attempting to sign in.")
                try:
                    me = await client.sign_in(password=user_password)
                except errors.PasswordHashInvalidError:
                    logger.error(f"[{session_name}] Invalid 2FA password provided for {phone_to_login}.")
                    await client.disconnect()
                    raise # Re-raise to be caught by manager_and_bot.py for specific user message
                except Exception as e_pw_signin:
                    logger.error(f"[{session_name}] Sign-in with 2FA password for {phone_to_login} failed: {e_pw_signin}")
                    await client.disconnect()
                    return None
            
            except errors.PhoneCodeInvalidError:
                logger.error(f"[{session_name}] Invalid phone code provided for {phone_to_login}.")
                await client.disconnect()
                raise # Re-raise
            except errors.PhoneNumberInvalidError:
                logger.error(f"[{session_name}] Invalid phone number: {phone_to_login}.")
                await client.disconnect()
                raise # Re-raise
            except Exception as e_login: # Catch other potential login errors
                logger.error(f"[{session_name}] Login process for {phone_to_login} encountered an error: {e_login}")
                await client.disconnect()
                return None
        else: # Already authorized from a previous session file
            logger.info(f"[{session_name}] User already authorized for {phone_to_login} (session file exists).")
            me = await client.get_me()


        if not await client.is_user_authorized() or not me:
            logger.error(f"[{session_name}] Still not authorized for {phone_to_login} after login attempt or failed to get 'me'.")
            if client.is_connected(): await client.disconnect()
            return None

        # Save the session to file now that we are authorized
        with open(client_session_path, 'w') as f_session:
            f_session.write(client.session.save())
        logger.info(f"[{session_name}] Session for {phone_to_login} (User ID: {me.id}) saved to file: {client_session_path}")

        my_id = me.id # Get the ID of the logged-in Telethon user
        logger.info(f"[{session_name}] Login successful. Running for: {me.username or me.first_name} (ID: {my_id}). Trigger: '{command_trigger}'")

        @client.on(events.NewMessage(outgoing=True))
        async def handle_outgoing_reply(event: events.NewMessage.Event):
            if event.is_reply and event.raw_text == command_trigger:
                # Ensure this event is from the correct user for this client instance
                if event.sender_id != my_id:
                    logger.warning(f"[{my_id}] Outgoing message from unexpected sender_id {event.sender_id}. Ignoring.")
                    return

                replied_to_msg_id = event.reply_to_msg_id
                chat_id = event.chat_id
                status_msg = None
                try:
                    # Reply to the command message itself for status updates
                    status_msg = await event.reply("⏳ Processing...")
                except Exception as e_status:
                    logger.warning(f"[{my_id}] Could not send status message in chat {chat_id}: {e_status}")

                target_message = None
                try:
                    target_message = await client.get_messages(chat_id, ids=replied_to_msg_id)
                except Exception as e_get_msg:
                     logger.error(f"[{my_id}] Failed to get replied message {replied_to_msg_id} in chat {chat_id}: {e_get_msg}")
                     if status_msg: await status_msg.edit("❌ Error: Could not fetch the replied message.")
                     return


                if not target_message:
                    err_text = "❌ Error: Could not fetch the replied message (it might have been deleted)."
                    if status_msg: await status_msg.edit(err_text)
                    else: logger.info(f"[{my_id}] {err_text}")
                    return

                if not target_message.media:
                    err_text = "ℹ️ The replied message does not contain media."
                    if status_msg: await status_msg.edit(err_text)
                    else: logger.info(f"[{my_id}] {err_text}")
                    if status_msg:
                        await asyncio.sleep(5)
                        try: await status_msg.delete()
                        except Exception: pass # Ignore if already deleted
                    return
                
                media_sender = await target_message.get_sender()
                sssender_info = "Unknown User"
                if media_sender:
                    sssender_info = f"{media_sender.first_name or ''}"
                    if media_sender.last_name: sssender_info += f" {media_sender.last_name}"
                    sssender_info += f" (ID: {media_sender.id})"
                
                user_specific_download_path = os.path.join(DOWNLOAD_PATH_BASE, str(my_id))
                if not os.path.exists(user_specific_download_path):
                    os.makedirs(user_specific_download_path)

                downloaded_file_path = None
                try:
                    logger.info(f"[{my_id}] Downloading media from message ID {target_message.id}...")
                    downloaded_file_path = await client.download_media(
                        target_message.media,
                        file=user_specific_download_path # Telethon appends original filename
                    )
                    if not downloaded_file_path: # Should not happen if download_media doesn't error
                        raise Exception("Download returned None path, but no error was raised.")
                    logger.info(f"[{my_id}] Media downloaded to: {downloaded_file_path}")
                except Exception as err:
                    error_msg = f"❌ Failed to download file: {err}"
                    logger.error(f"[{my_id}] Download error: {err}", exc_info=True)
                    if status_msg: await status_msg.edit(error_msg)
                    return

                if downloaded_file_path and os.path.exists(downloaded_file_path):
                    file_name_only = os.path.basename(downloaded_file_path)
                    caption_text = (f"✅ Saved: {file_name_only}\n"
                                    f"👤 Originally from: {sssender_info}\n"
                                    f"💬 Replied in chat: {event.chat.title if hasattr(event.chat, 'title') and event.chat.title else 'DM/Unknown Chat'}")
                    try:
                        await client.send_file(
                            "me", # Send to User's "Saved Messages"
                            downloaded_file_path,
                            caption=caption_text
                        )
                        success_text = "✅ Media saved to your Saved Messages!"
                        if status_msg: await status_msg.edit(success_text)
                        else: logger.info(f"[{my_id}] {success_text}")
                        
                        # Optionally, delete the status message after a delay
                        if status_msg:
                            await asyncio.sleep(10)
                            try: await status_msg.delete()
                            except Exception: pass
                    except Exception as send_err:
                        error_msg = f"❌ Failed to send file to Saved Messages: {send_err}"
                        logger.error(f"[{my_id}] Send error: {send_err}", exc_info=True)
                        if status_msg: await status_msg.edit(error_msg)
                    finally:
                        # Clean up the downloaded file from server
                        try:
                            os.remove(downloaded_file_path)
                            logger.info(f"[{my_id}] Cleaned up temporary file: {downloaded_file_path}")
                        except OSError as e_os:
                            logger.error(f"[{my_id}] Error removing temp file {downloaded_file_path}: {e_os}")
                else:
                    error_msg = "❌ File not found after download, or download failed silently."
                    logger.error(f"[{my_id}] {error_msg}")
                    if status_msg: await status_msg.edit(error_msg)

        logger.info(f"[{session_name}] Event handler set for user {my_id}. Monitoring started.")
        # Create a separate task for run_until_disconnected so this function can return the user ID
        # The calling function (manager_and_bot) will store this task if it needs to cancel it later.
        monitoring_task = asyncio.create_task(client.run_until_disconnected())
        # Store the monitoring task in a way that manager_and_bot.py can access it for logout
        # This is a bit tricky. For now, we rely on the manager to handle its own task for `run_user_instance`.
        # The `monitoring_task` here is local. If manager cancels `run_user_instance` task, this also goes.
        
        return my_id # Return the Telethon user's ID on successful setup and monitoring start

    except (errors.PhoneCodeInvalidError, errors.PhoneNumberInvalidError, errors.PasswordHashInvalidError) as e_login_specific:
        logger.error(f"[{session_name}] Specific login error for {phone_to_login}: {e_login_specific}")
        if client.is_connected(): await client.disconnect()
        raise # Re-raise for manager_and_bot to catch and inform user specifically
    except Exception as e_outer:
        logger.error(f"[{session_name}] Major error in run_user_instance for {phone_to_login}: {e_outer}", exc_info=True)
        if client.is_connected(): await client.disconnect()
        return None # General failure
    # No finally block with disconnect here, it's handled in error paths.
    # If run_until_disconnected finishes, the client will disconnect itself.
