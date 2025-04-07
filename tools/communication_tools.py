# tools/communication_tools.py

import logging
import asyncio
from typing import Dict, Optional

# --- Зависимости ---
try:
    # Импортируем экземпляр бота из bot_loader
    from bot_loader import bot, dp

    # Импортируем утилиту экранирования
    from aiogram.enums import ParseMode
    from utils.helpers import escape_markdown_v2
    # Импортируем базовый класс исключений aiogram
    from aiogram.exceptions import TelegramAPIError
except ImportError:
    logging.critical("CRITICAL: Failed to import dependencies (bot_loader, helpers, aiogram) in communication_tools.", exc_info=True)
    # Создаем заглушки, чтобы модуль хотя бы импортировался
    class MockBot:
        async def send_message(self, chat_id: int, text: str, **kwargs):
            print(f"[!] MockBot: Would send to {chat_id}: {text[:70]}...")
            await asyncio.sleep(0.01)
            # Возвращаем фейковый объект сообщения для имитации
            return type('obj', (object,), {'message_id': 123})()
    bot = MockBot()
    def escape_markdown_v2(text: str) -> str: return text
    TelegramAPIError = Exception # Используем базовый Exception как заглушку
    logging.warning("Using MockBot, mock escape function, and base Exception for TelegramAPIError in communication_tools.")

logger = logging.getLogger(__name__)

async def send_telegram_message(
    chat_id: int,
    text: str,
    delay_seconds: int = 0
) -> Dict[str, str]:
    """
    Асинхронно отправляет текстовое сообщение в указанный чат Telegram от имени бота.
    Автоматически экранирует текст для MarkdownV2 перед отправкой.

    Args:
        chat_id (int): ID чата Telegram.
        text (str): Текст сообщения (будет экранирован).
        delay_seconds (int): Задержка перед отправкой в секундах.
        requires_user_response (bool): Используется циклом FC для определения необходимости паузы (не используется внутри этой функции).

    Returns:
        Dict[str, str]: Словарь со статусом операции ('success' или 'error') и сообщением.
    """
    tool_name = "send_telegram_message"
    # Валидация аргументов... (остается как было) ...
    if not isinstance(chat_id, int): ...
    if text is None or not isinstance(text, str): ...
    if not text.strip(): ...
    if not isinstance(delay_seconds, int) or delay_seconds < 0: ...

    logger.info(f"--- Tool Call: {tool_name}(chat={chat_id}, delay={delay_seconds}, text='{text[:70]}...') ---")

    try:
        # <<< ИЗМЕНЕНО: Упрощенная проверка и установка задержки >>>
        actual_delay = 0
        if isinstance(delay_seconds, int) and delay_seconds > 0:
            actual_delay = delay_seconds
        # Не логируем предупреждение, если delay_seconds == 0 или None

        if actual_delay > 0:
            logger.info(f"{tool_name}: Delaying message send by {actual_delay} seconds for chat {chat_id}")
            await asyncio.sleep(actual_delay)

        escaped_text = escape_markdown_v2(text)

        sent_message = await bot.send_message(
            chat_id=chat_id,
            text=escaped_text,
            parse_mode=ParseMode.MARKDOWN_V2
        )
        sent_message_id = sent_message.message_id if sent_message else "N/A"
        logger.info(f"{tool_name}: Successfully sent message (ID: {sent_message_id}) with MarkdownV2 to chat {chat_id}")
        return {"status": "success", "message": f"Message sent successfully (ID: {sent_message_id})."}

    except TelegramAPIError as e:
        # Если ошибка парсинга даже ПОСЛЕ экранирования (маловероятно, но возможно из-за длины или редких случаев)
        if "can't parse entities" in str(e).lower():
             logger.warning(f"{tool_name}: Failed send with MarkdownV2 even after escaping (chat {chat_id}): {e}. Retrying without parse_mode.")
             try:
                  # Отправляем ОРИГИНАЛЬНЫЙ текст без разметки в fallback
                  sent_message = await bot.send_message(chat_id=chat_id, text=text, parse_mode=None)
                  sent_message_id = sent_message.message_id if sent_message else "N/A"
                  logger.info(f"{tool_name}: Successfully sent message (ID: {sent_message_id}) to chat {chat_id} (fallback without parse_mode).")
                  return {"status": "success", "message": f"Message sent successfully (ID: {sent_message_id})."}
             except Exception as fallback_e:
                  logger.error(f"{tool_name}: Fallback send failed to chat {chat_id}: {fallback_e}", exc_info=True)
                  # Возвращаем исходную ошибку парсинга
                  return {"status": "error", "message": f"Telegram API error (MarkdownV2 parse failed after escaping): {e}"}
        else:
             # Другие ошибки API
             error_msg = f"Failed to send message via {tool_name} to chat {chat_id}: TelegramAPIError - {e}"
             logger.error(error_msg, exc_info=False)
             return {"status": "error", "message": f"Telegram API error: {e}"}
    except Exception as e:
        error_msg = f"Unexpected error sending message via {tool_name} to chat {chat_id}: {e}"
        logger.error(error_msg, exc_info=True)
        return {"status": "error", "message": f"Internal error: {e}"}