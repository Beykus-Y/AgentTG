# telegram_interface/handlers/common_messages.py
import logging
import asyncio
from typing import Optional, Dict, Any

# --- УДАЛЕН ИМПОРТ RE ---
from aiogram.exceptions import TelegramAPIError

logger_cm = logging.getLogger(__name__)
logger_cm.info("--- Loading common_messages.py ---")

# --- Зависимости ---
handle_user_request = None
escape_markdown_v2 = None
remove_markdown = None
Router = None
F = None
types = None
Bot = None
aiogram_ChatType = None
aiogram_ContentType = None
aiogram_ActionType = None
ParseMode = None
dependencies_ok = True # Флаг успешной загрузки

try:
    logger_cm.debug("Importing aiogram basics...")
    # --- ДОБАВЛЯЕМ ПРОВЕРКУ ПОСЛЕ КАЖДОГО ИМПОРТА ---
    from aiogram import F as aiogram_F, types as aiogram_types, Bot as aiogram_Bot, Router as aiogram_Router
    if not all([aiogram_F, aiogram_types, aiogram_Bot, aiogram_Router]): raise ImportError("Failed to import basic aiogram components")
    logger_cm.debug("Imported F, types, Bot, Router.")

    from aiogram.enums import ChatType as aiogram_Enum_ChatType, ContentType as aiogram_Enum_ContentType
    if not all([aiogram_Enum_ChatType, aiogram_Enum_ContentType]): raise ImportError("Failed to import ChatType or ContentType")
    logger_cm.debug("Imported ChatType, ContentType.")

    from aiogram.enums.chat_action import ChatAction as aiogram_Enum_ChatAction
    if not aiogram_Enum_ChatAction: raise ImportError("Failed to import ChatAction")
    logger_cm.debug("Imported ChatAction.")

    from aiogram.enums import ParseMode as aiogram_Enum_ParseMode
    if not aiogram_Enum_ParseMode: raise ImportError("Failed to import ParseMode")
    logger_cm.debug("Imported ParseMode.")

    Router = aiogram_Router
    F = aiogram_F
    types = aiogram_types
    Bot = aiogram_Bot
    aiogram_ChatType = aiogram_Enum_ChatType
    aiogram_ContentType = aiogram_Enum_ContentType
    aiogram_ActionType = aiogram_Enum_ChatAction
    ParseMode = aiogram_Enum_ParseMode
    logger_cm.info("Assigned aiogram basics successfully.")

    logger_cm.debug("Attempting to import utils.helpers...")
    from utils.helpers import escape_markdown_v2 as escape_md_func, remove_markdown as remove_md_func
    if not all([escape_md_func, remove_md_func]): raise ImportError("Failed to import helper functions")
    escape_markdown_v2 = escape_md_func
    remove_markdown = remove_md_func
    logger_cm.info("Imported utils.helpers successfully.")

    logger_cm.debug("Attempting to import core_agent.agent_processor...")
    from core_agent.agent_processor import handle_user_request as core_handle_user_request
    if not core_handle_user_request: raise ImportError("Failed to import handle_user_request")
    handle_user_request = core_handle_user_request
    logger_cm.info("Imported core_agent.agent_processor.handle_user_request successfully.")

except ImportError as e:
    logger_cm.critical(f"CRITICAL: ImportError during common_messages setup! Error: {e}", exc_info=True)
    dependencies_ok = False
except Exception as e:
    logger_cm.critical(f"CRITICAL: Unexpected error during common_messages setup! Error: {e}", exc_info=True)
    dependencies_ok = False

# --- Создание Роутера ---
router = None # Инициализируем как None
if dependencies_ok:
    logger_cm.info("Dependencies OK. Defining router for common_messages...")
    try:
        router = Router(name="common_messages_router")
        logger_cm.info(f"Router defined: {router}")
    except Exception as router_err:
         logger_cm.critical(f"CRITICAL: Failed to create Router instance! Error: {router_err}", exc_info=True)
         dependencies_ok = False # Отмечаем ошибку
else:
    logger_cm.error("Skipping router creation due to dependency errors.")


# --- Регистрация хендлера ---
# Регистрируем, только если все зависимости и роутер созданы
if dependencies_ok and router:
    logger_cm.info(f"Attempting to register process_text_message handler on router: {router}")
    try:
        # Проверяем типы перед регистрацией
        if not aiogram_ContentType or not aiogram_ChatType:
             raise ValueError("ContentType or ChatType is None before handler registration!")

        @router.message(
            F.text,
            F.content_type == aiogram_ContentType.TEXT, # Используем проверенную переменную
            (F.chat.type == aiogram_ChatType.PRIVATE) | # Используем проверенную переменную
            (F.chat.type.in_({aiogram_ChatType.GROUP, aiogram_ChatType.SUPERGROUP})) # Используем проверенную переменную
        )
        async def process_text_message(message: types.Message, bot: Bot):
            """
            Обрабатывает текстовые сообщения в ЛС и группах.
            Передает управление в ядро агента (handle_user_request).
            """
            # Стало:

            logger_cm.info(f"!!! HANDLER process_text_message TRIGGERED for message {message.message_id} !!!")# <--- ВАЖНЫЙ ЛОГ

            # Проверка доступности handle_user_request (на всякий случай)
            if handle_user_request is None:
                logger_cm.critical("Core agent function 'handle_user_request' is unavailable inside handler.")
                return

            # Проверка бота
            if not bot:
                logger_cm.critical("Bot instance is unavailable inside handler.")
                return

            # Игнор ботов/сообщений без юзера
            user = message.from_user
            if not user or user.is_bot:
                logger_cm.debug(f"Handler ignoring message from bot or without user in chat {message.chat.id}")
                return

            

            # Вызов Ядра Агента
            core_response_text_processed: Optional[str] = None
            try:
                logger_cm.debug(f"Calling handle_user_request for user {user.id} chat {message.chat.id}")
                core_response_text_processed = await handle_user_request(message=message)
                logger_cm.debug(f"handle_user_request returned: {'Text' if core_response_text_processed else 'None'}")

            except Exception as core_agent_err:
                logger_cm.error(f"Error during handle_user_request call for chat {message.chat.id}: {core_agent_err}", exc_info=True)
                error_msg_esc = escape_markdown_v2("Произошла внутренняя ошибка при обработке.") if escape_markdown_v2 else "Произошла внутренняя ошибка при обработке."
                try: await message.reply(text=error_msg_esc, parse_mode=None)
                except Exception: pass
                return

            # Отправка Ответа Пользователю
            if core_response_text_processed:
                logger_cm.info(f"Sending final response (len={len(core_response_text_processed)}) to chat {message.chat.id}")
                try:
                    # Проверяем ParseMode
                    if not ParseMode: raise ValueError("ParseMode is None before sending message!")
                    await message.reply(text=core_response_text_processed, parse_mode=ParseMode.MARKDOWN_V2)
                except TelegramAPIError as send_error_md:
                    logger_cm.warning(f"Failed to send reply with MarkdownV2: {send_error_md}. Retrying without parse mode.")
                    try:
                        cleaned_text = remove_markdown(core_response_text_processed) if remove_markdown else core_response_text_processed
                        # ... (логика обрезки текста) ...
                        if len(cleaned_text) > 4096: cleaned_text = cleaned_text[:4076] + "..."
                        await message.reply(text=cleaned_text, parse_mode=None)
                    except Exception as send_error_plain:
                         logger_cm.error(f"Failed send reply fallback: {send_error_plain}")
                except Exception as e:
                    logger_cm.critical(f"Unexpected error sending reply: {e}", exc_info=True)
            else:
                logger_cm.info(f"Core Agent returned no text response. No message sent to chat {message.chat.id}.")

        logger_cm.info("Handler process_text_message registered successfully.")
    except Exception as register_err:
        logger_cm.critical(f"CRITICAL: Failed to register process_text_message handler! Error: {register_err}", exc_info=True)
        # Если регистрация упала, обнуляем роутер, чтобы main.py не пытался его использовать
        router = None

elif not dependencies_ok:
     logger_cm.error("Handler registration skipped due to dependency errors.")
else: # dependencies_ok is True, но router почему-то None
     logger_cm.error("Handler registration skipped because router is None (check creation step).")


# --- Лог в конце файла ---
if router and dependencies_ok:
    logger_cm.info("--- common_messages.py loaded successfully. Router OK. Handler registered. ---")
elif router is None and dependencies_ok:
     logger_cm.error("--- common_messages.py loaded, dependencies OK, BUT ROUTER IS NONE! ---")
else:
    logger_cm.error("--- common_messages.py failed to load properly (router or dependencies failed). Check CRITICAL logs. ---")