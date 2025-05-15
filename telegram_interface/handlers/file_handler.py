# telegram_interface/handlers/file_handler.py

import logging
import os
import asyncio
from typing import Optional, Tuple

# --- Aiogram и зависимости ---
dependencies_ok = True
try:
    from aiogram import F, types, Bot, Router
    from aiogram.enums import ContentType, ChatType, ParseMode
    from aiogram.exceptions import TelegramAPIError
    import aiofiles
    # Импортируем систему БД для сохранения сообщений пользователей и профилей
    import database
    from database.crud_ops.profiles import upsert_user_profile
    from database.crud_ops.history import add_message_to_history
    import json
except ImportError as e:
    logging.critical(f"CRITICAL: Failed to import aiogram components in file_handler: {e}", exc_info=True)
    dependencies_ok = False
    # Заглушки для базовой работы логгера
    def escape_markdown_v2(text: str) -> str: return text
    async def get_safe_chat_path(*args, **kwargs): return False, None
    bot_instance = None
    aiofiles = None
    database = None
    async def upsert_user_profile(*args, **kwargs): pass
    async def add_message_to_history(*args, **kwargs): pass


logger = logging.getLogger(__name__)

# --- Создание роутера ---
router = None
if dependencies_ok:
    try:
        router = Router(name="file_handler_router")
        logger.info("File handler router created.")
    except Exception as router_err:
        logger.critical(f"CRITICAL: Failed to create Router instance in file_handler! Error: {router_err}", exc_info=True)
        dependencies_ok = False
else:
    logger.error("Skipping file handler router creation due to dependency errors.")


# --- Регистрация хендлера ---
if dependencies_ok and router and aiofiles:

    @router.message(
        F.content_type == ContentType.DOCUMENT, # Ловим только документы
        (F.chat.type == ChatType.PRIVATE) |
        (F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
    )
    async def process_document_message(message: types.Message, bot: Bot):
        logger.info("<<<<< process_document_message HANDLER ENTERED >>>>>")
        """
        Обрабатывает входящие сообщения с документами (файлами).
        Скачивает файл и сохраняет его в /env/{chat_id}/downloads/.
        """
        logger.info(f"File handler triggered for message {message.message_id} in chat {message.chat.id}")

        # ==============================================================
        # ====== НАЧАЛО БЛОКА ЛОГИКИ ВНУТРИ ФУНКЦИИ process_document_message ======
        # ==============================================================

        # Проверки (Отступ 4 пробела)
        if not bot:
            logger.critical("Bot instance is unavailable inside document handler.")
            return
        if not message.document:
            logger.warning(f"Document handler triggered, but message.document is None (msg_id: {message.message_id})")
            return
        if not message.from_user:
            logger.debug(f"Ignoring document message without user in chat {message.chat.id}")
            return

        user_id = message.from_user.id
        chat_id = message.chat.id
        chat_type = message.chat.type
        document = message.document
        original_filename = document.file_name or f"file_{document.file_unique_id}.unknown"
        
        # Сохраняем документы из групповых чатов в базу данных для сохранения контекста
        if chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
            try:
                # Сначала обновляем профиль пользователя
                if database and hasattr(database, 'upsert_user_profile'):
                    await upsert_user_profile(
                        user_id=user_id,
                        username=message.from_user.username,
                        first_name=message.from_user.first_name,
                        last_name=message.from_user.last_name
                    )
                
                # Затем сохраняем документ в историю
                if database and hasattr(database, 'add_message_to_history'):
                    parts_json = create_file_parts_json(document)
                    await add_message_to_history(
                        chat_id=chat_id,
                        role="user",
                        user_id=user_id,
                        parts=parts_json
                    )
                    logger.debug(f"Saved document message from user {user_id} to chat history (chat_id={chat_id})")
            except Exception as save_err:
                logger.error(f"Error saving document message to history: {save_err}", exc_info=True)

        # --- Определяем путь для сохранения --- (Отступ 4 пробела)
        try:
            # 1. Получаем безопасный путь к директории чата (Отступ 8 пробелов)
            is_safe_base, chat_dir_path = await get_safe_chat_path(
                chat_id,
                ".",
                user_id=user_id,
                ensure_chat_dir_exists=True
            )

            if not is_safe_base or not chat_dir_path:
                logger.error(f"Cannot get safe path or ensure base chat directory for chat {chat_id}. Cannot save file.")
                await message.reply("❌ Ошибка: Не удалось определить безопасное место для сохранения файла.")
                return

            # 2. Создаем поддиректорию 'downloads' (Отступ 8 пробелов)
            downloads_dir_path = os.path.join(chat_dir_path, "downloads")
            await aiofiles.os.makedirs(downloads_dir_path, exist_ok=True)
            logger.debug(f"Ensured 'downloads' directory exists: {downloads_dir_path}")

            # 3. Формируем полный путь к файлу (Отступ 8 пробелов)
            target_filepath = os.path.join(downloads_dir_path, original_filename)

        except Exception as path_err: # (Отступ 4 пробела)
             logger.error(f"Failed to determine save path for file '{original_filename}' in chat {chat_id}: {path_err}", exc_info=True)
             await message.reply("❌ Ошибка: Не удалось подготовить место для сохранения файла.")
             return

        # --- Скачиваем файл --- (Отступ 4 пробела - этот блок теперь внутри функции)
        try:
            logger.info(f"Attempting to download file '{original_filename}' (file_id: {document.file_id}) to '{target_filepath}'")
            # Показываем статус "Отправка документа" пока скачиваем (Отступ 8 пробелов)
            await bot.send_chat_action(chat_id=chat_id, action="upload_document")

            await bot.download(
                file=document.file_id,
                destination=target_filepath
            )
            file_size_mb = round(document.file_size / (1024 * 1024), 2) if document.file_size else "N/A"
            logger.info(f"Successfully downloaded and saved file '{original_filename}' ({file_size_mb} MB) for chat {chat_id}")

            # Экранируем скобки '(', ')' и точку '.' в тексте ответа (Отступ 8 пробелов)
            await message.reply(
                f"✅ Файл `{escape_markdown_v2(original_filename)}` \\({file_size_mb} MB\\) успешно сохранен в окружении чата\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )

        except TelegramAPIError as download_err: # (Отступ 4 пробела)
            logger.error(f"Failed to download/save file '{original_filename}' for chat {chat_id} (TelegramAPIError): {download_err}", exc_info=False)
            await message.reply(
                f"❌ Ошибка Telegram при скачивании файла `{escape_markdown_v2(original_filename)}`: {escape_markdown_v2(download_err.message)}",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as download_err: # (Отступ 4 пробела)
            logger.error(f"Failed to download/save file '{original_filename}' for chat {chat_id} (Other Error): {download_err}", exc_info=True)
            await message.reply(
                f"❌ Непредвиденная ошибка при скачивании/сохранении файла `{escape_markdown_v2(original_filename)}`\\.",
                 parse_mode=ParseMode.MARKDOWN_V2
            )

        # ==============================================================
        # ====== КОНЕЦ БЛОКА ЛОГИКИ ВНУТРИ ФУНКЦИИ process_document_message ======
        # ==============================================================

# --- Этот блок должен быть ВНЕ функции process_document_message ---
elif not dependencies_ok:
     logger.error("File handler registration skipped due to dependency errors.")
elif aiofiles is None:
     logger.error("File handler registration skipped because 'aiofiles' library is missing.")
else: # router is None
     logger.error("File handler registration skipped because router is None (check creation step).")

# --- Лог в конце файла ---
if router and dependencies_ok and aiofiles:
    logger.info("--- file_handler.py loaded successfully. Router OK. Handler registered. ---")
else:
    logger.error("--- file_handler.py failed to load properly (check logs for missing dependencies or router errors). ---")

# Функция для создания parts_json с информацией о файле
def create_file_parts_json(document: types.Document) -> str:
    """Создает parts_json с информацией о документе для записи в БД."""
    parts = []
    
    # Добавляем caption документа, если есть
    if document.caption:
        parts.append({"type": "text", "content": document.caption})
    
    # Добавляем информацию о файле
    parts.append({
        "type": "document", 
        "file_id": document.file_id,
        "file_name": document.file_name or f"file_{document.file_unique_id}.unknown",
        "mime_type": document.mime_type,
        "file_size": document.file_size
    })
    
    return json.dumps(parts, ensure_ascii=False)