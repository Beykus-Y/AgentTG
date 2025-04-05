# telegram_interface/middlewares/stats_counter.py

import logging
from typing import Callable, Dict, Any, Awaitable, Optional

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from aiogram.enums import ChatType

# Импортируем функцию для инкремента счетчика из БД
try:
    from database.crud_ops.stats import increment_message_count
    # Импортируем функцию для обновления профиля, чтобы создать запись, если ее нет
    from database.crud_ops.profiles import upsert_user_profile
except ImportError:
    logging.getLogger(__name__).critical("CRITICAL: Failed to import DB functions (increment_message_count, upsert_user_profile) in StatsCounterMiddleware.", exc_info=True)
    # Заглушки
    async def increment_message_count(*args, **kwargs) -> bool: return False
    async def upsert_user_profile(*args, **kwargs) -> bool: return False

logger = logging.getLogger(__name__)

class StatsCounterMiddleware(BaseMiddleware):
    """
    Middleware для подсчета количества сообщений от каждого пользователя в каждом чате.
    Обновляет счетчик в базе данных при каждом входящем сообщении от пользователя.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject, # Работаем с TelegramObject для универсальности
        data: Dict[str, Any]
    ) -> Any:

        # --- Получаем пользователя и чат ---
        user: Optional[types.User] = None
        chat: Optional[types.Chat] = None

        if isinstance(event, Message):
            # Извлекаем из сообщения
            user = event.from_user
            chat = event.chat
            # Пропускаем сообщения от ботов
            if user and user.is_bot:
                return await handler(event, data)
        # elif isinstance(event, CallbackQuery): # Можно добавить обработку колбэков, если нужно считать и их
        #     user = event.from_user
        #     chat = event.message.chat if event.message else None

        # --- Если есть пользователь и чат, обновляем статистику ---
        if user and chat:
            user_id = user.id
            chat_id = chat.id
            # Проверяем, что тип чата подходит для сбора статистики (например, группы и личные)
            if chat.type in {ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP}:
                try:
                    # 1. Убедимся, что профиль пользователя существует в БД
                    # Это важно, т.к. message_stats ссылается на user_profiles через FOREIGN KEY
                    await upsert_user_profile(
                        user_id=user_id,
                        username=user.username,
                        first_name=user.first_name,
                        last_name=user.last_name
                    )

                    # 2. Инкрементируем счетчик сообщений
                    success = await increment_message_count(chat_id, user_id)
                    if not success:
                        logger.warning(f"Failed to increment message count for user {user_id} in chat {chat_id}.")
                    # else: logger.debug(f"Incremented message count for user {user_id} in chat {chat_id}") # Слишком частый лог

                except Exception as e:
                    # Логируем ошибку, но не прерываем обработку сообщения
                    logger.error(f"Error updating message stats for user {user_id} in chat {chat_id}: {e}", exc_info=True)

        # Передаем управление следующему middleware или хендлеру
        return await handler(event, data)