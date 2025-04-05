# telegram_interface/middlewares/antiflood.py

import asyncio
import logging
from typing import Callable, Dict, Any, Awaitable, Optional

from aiogram import BaseMiddleware
from aiogram.types import Update, Message, TelegramObject
from aiogram.dispatcher.flags import get_flag

# Используем кеш для хранения времени последних сообщений пользователей
# В простом случае можно использовать dict, для большей масштабируемости - Redis или др.
THROTTLING_CACHE: Dict[int, float] = {} # user_id: timestamp
# Словарь для отслеживания отправленных предупреждений (user_id: timestamp)
WARNING_SENT_CACHE: Dict[int, float] = {}
LOCK_CACHE: Dict[int, asyncio.Lock] = {} # user_id: lock

logger = logging.getLogger(__name__)

DEFAULT_RATE_LIMIT = 0.7 # Секунд между сообщениями по умолчанию
DEFAULT_WARN_DELAY = 5 # Секунд показывать сообщение о флуде

class AntiFloodMiddleware(BaseMiddleware):
    """
    Middleware для ограничения частоты сообщений от пользователя (простой троттлинг).
    """
    def __init__(self, rate_limit: float = DEFAULT_RATE_LIMIT):
        self.rate_limit = rate_limit
        logger.info(f"AntiFloodMiddleware initialized with rate_limit={rate_limit}s")

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject, # Принимаем любой TelegramObject
        data: Dict[str, Any]
    ) -> Any:

        # Применяем только к сообщениям от пользователей
        if not isinstance(event, Message) or not event.from_user:
            return await handler(event, data)

        user_id = event.from_user.id

        # Используем Lock для предотвращения гонки состояний при одновременных запросах от одного пользователя
        if user_id not in LOCK_CACHE:
             LOCK_CACHE[user_id] = asyncio.Lock()
        lock = LOCK_CACHE[user_id]

        async with lock: # Захватываем лок для пользователя
            # Проверяем флаг 'ignore_flood' у хендлера
            # Это позволяет отключить антифлуд для конкретных команд/хендлеров
            if get_flag(data, "ignore_flood"):
                logger.debug(f"Ignoring flood check for user {user_id} due to 'ignore_flood' flag.")
                return await handler(event, data)

            current_time = asyncio.get_event_loop().time()

            # Получаем время последнего сообщения пользователя
            last_message_time = THROTTLING_CACHE.get(user_id, 0)

            # Проверяем, прошло ли достаточно времени
            if current_time - last_message_time < self.rate_limit:
                # --- Действие при флуде ---
                logger.warning(f"Flood detected from user {user_id} (rate limit: {self.rate_limit}s)")

                # Проверяем, не отправляли ли предупреждение недавно
                last_warn_time = WARNING_SENT_CACHE.get(user_id, 0)
                if current_time - last_warn_time > DEFAULT_WARN_DELAY * 2: # Отправляем повторно, если прошло время
                    try:
                        # Отправляем временное сообщение о флуде
                        warn_msg = await event.reply(f"⏳ Пожалуйста, не так часто! Лимит: {self.rate_limit} сек.")
                        WARNING_SENT_CACHE[user_id] = current_time # Запоминаем время отправки

                        # Задача для удаления сообщения через N секунд
                        async def delete_later(msg: Message, delay: int):
                            await asyncio.sleep(delay)
                            try:
                                await msg.delete()
                                # Очищаем кэш предупреждения после удаления
                                if user_id in WARNING_SENT_CACHE and WARNING_SENT_CACHE[user_id] == current_time:
                                     del WARNING_SENT_CACHE[user_id]
                                     logger.debug(f"Deleted flood warning message and cache for user {user_id}.")
                            except Exception as del_err:
                                logger.warning(f"Could not delete flood warning message for user {user_id}: {del_err}")

                        asyncio.create_task(delete_later(warn_msg, DEFAULT_WARN_DELAY))

                    except Exception as send_err:
                        logger.error(f"Failed to send flood warning to user {user_id}: {send_err}")
                else:
                     logger.debug(f"Flood warning already recently sent to user {user_id}.")


                # Не передаем событие дальше по цепочке middleware/хендлеров
                return None # или raise CancelHandler() из aiogram.dispatcher.event.handler
            else:
                # --- Если флуда нет ---
                # Обновляем время последнего сообщения
                THROTTLING_CACHE[user_id] = current_time
                # Передаем событие дальше
                return await handler(event, data)