# telegram_interface/handlers/error_handler.py
import logging
import html
import traceback

from aiogram import Router, types
from aiogram.exceptions import TelegramAPIError # Импортируем остальные по мере необходимости
from aiogram.utils.markdown import hcode, bold, italic # Импортируем нужные элементы форматирования
from aiogram.enums import ParseMode # Импортируем ParseMode

# --- Локальные импорты ---
try:
    from utils.helpers import escape_markdown_v2
    from config import settings
except ImportError:
    # ... (заглушки)
    pass # Оставляем pass, т.к. заглушки уже есть

logger = logging.getLogger(__name__)
router = Router(name="error_handler_router")

# --- Обработчик ошибок ---
@router.errors()
async def handle_errors(update: types.ErrorEvent):
    """
    Ловит все ошибки, возникающие при обработке обновлений.
    Логирует детали ошибки и опционально уведомляет администраторов.
    """
    exception = update.exception
    exception_name = type(exception).__name__
    update_json = update.update.model_dump_json(indent=2, exclude_none=True)

    # --- Логирование ---
    tb_list = traceback.format_exception(None, exception, exception.__traceback__)
    tb_string = "".join(tb_list)
    logger.error(
        f"!!! Caught exception: {exception_name}\n"
        f"    Update: {update_json}\n"
        f"    Exception: {exception}\n"
        f"    Traceback:\n{tb_string}"
    )

    # --- Уведомление администраторов ---
    if settings and settings.admin_ids:
        # Собираем сырые данные
        chat_info_raw = "N/A"
        user_info_raw = "N/A"
        message_text_raw = "N/A"
        event = update.update
        if event.message:
            chat_info_raw = f"Chat: {event.message.chat.id} ({event.message.chat.type})"
            if event.message.from_user: user_info_raw = f"User: {event.message.from_user.id} (@{event.message.from_user.username})"
            if event.message.text: message_text_raw = event.message.text[:100]
        elif event.callback_query:
            if event.callback_query.message: chat_info_raw = f"Chat: {event.callback_query.message.chat.id} ({event.callback_query.message.chat.type})"
            if event.callback_query.from_user: user_info_raw = f"User: {event.callback_query.from_user.id} (@{event.callback_query.from_user.username})"
            message_text_raw = f"CallbackData: {event.callback_query.data}"

        # <<< ИСПРАВЛЕНО: Собираем сообщение с тщательным экранированием >>>
        admin_message_parts = [
            bold("🚨 Bot Error:"), # Используем хелперы aiogram
            hcode(exception_name),
            bold("Exception:"),
            hcode(escape_markdown_v2(str(exception))), # Экранируем сообщение исключения
            bold("Context:"),
            italic(escape_markdown_v2(chat_info_raw) + ", " + escape_markdown_v2(user_info_raw)), # Экранируем контекст
            bold("Trigger:"),
            hcode(escape_markdown_v2(message_text_raw)), # Экранируем текст сообщения/данных
        ]

        # Добавляем трейсбек
        short_traceback = "\n".join(traceback.format_exception(None, exception, exception.__traceback__, limit=5))
        admin_message_parts.extend([
            bold("Traceback (short):"),
            # hcode() сам должен обрабатывать спецсимволы внутри кода, но экранируем на всякий случай
            hcode(escape_markdown_v2(short_traceback))
        ])

        full_admin_message = "\n".join(admin_message_parts)

        # Ограничиваем длину
        MAX_LEN = 4096
        if len(full_admin_message) > MAX_LEN:
            # Попробуем обрезать трейсбек
            base_len = len("\n".join(admin_message_parts[:-2])) # Длина без трейсбека
            available_space = MAX_LEN - base_len - 40
            if available_space > 100:
                 short_traceback_limited = short_traceback[:available_space]
                 # Пересобираем последнюю часть
                 admin_message_parts = admin_message_parts[:-2] + [
                     bold("Traceback (truncated):"),
                     hcode(escape_markdown_v2(short_traceback_limited))
                 ]
                 full_admin_message = "\n".join(admin_message_parts)
            else: # Обрезаем все сообщение
                 full_admin_message = full_admin_message[:MAX_LEN - 20] + "\n... (message truncated)"
            logger.warning("Admin error notification message truncated.")


        from bot_loader import bot
        if bot:
             for admin_id in settings.admin_ids:
                 try:
                     # <<< ИСПРАВЛЕНО: Отправляем с MarkdownV2 >>>
                     await bot.send_message(
                         chat_id=admin_id,
                         text=full_admin_message,
                         parse_mode=ParseMode.MARKDOWN_V2 # Используем константу
                     )
                 except TelegramAPIError as notify_err_md:
                     logger.error(f"Failed notify admin {admin_id} with MarkdownV2: {notify_err_md}")
                     # Попытка отправить без разметки
                     try:
                          await bot.send_message(chat_id=admin_id, text=full_admin_message, parse_mode=None)
                     except Exception as notify_err_plain:
                          logger.error(f"Failed notify admin {admin_id} even without parse mode: {notify_err_plain}")
                 except Exception as notify_err: # Ловим другие ошибки
                     logger.error(f"Unexpected error notifying admin {admin_id}: {notify_err}")
        else:
            logger.error("Cannot notify admins: Bot instance is unavailable.")

    return True # Указываем, что ошибка обработана