"""
Middleware для сохранения всех сообщений в групповых чатах для обеспечения полного контекста.
Обрабатывает не только текстовые сообщения, но и медиа, стикеры и другие типы контента.
"""

import logging
import json
from typing import Dict, Any, Callable, Awaitable, Optional

# --- Aiogram зависимости ---
dependencies_ok = True
try:
    from aiogram import BaseMiddleware
    from aiogram import types
    from aiogram.enums import ChatType
    from aiogram.types import TelegramObject, Message
    
    # Импортируем систему БД
    import database
    from database.crud_ops.profiles import upsert_user_profile
    from database.crud_ops.history import add_message_to_history
    
except ImportError as e:
    logging.getLogger(__name__).critical(f"CRITICAL: Failed to import dependencies in message_saver middleware: {e}", exc_info=True)
    dependencies_ok = False
    # Заглушки для импортов
    class BaseMiddleware: pass
    TelegramObject = object; Message = object; ChatType = object

logger = logging.getLogger(__name__)
logger.info("--- Loading message_saver middleware ---")

class MessageSaverMiddleware(BaseMiddleware):
    """
    Middleware для сохранения всех сообщений в групповых чатах в базу данных.
    Сохраняет все типы сообщений для обеспечения полного контекста диалога.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        # Сначала обработаем сообщение основным хендлером
        result = await handler(event, data)
        
        # Затем сохраняем сообщение в БД, если оно из группового чата
        if isinstance(event, Message) and event.chat and event.from_user:
            # Игнорируем сообщения от ботов
            if event.from_user.is_bot:
                return result
                
            # Проверяем, что это групповой чат
            if event.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
                chat_id = event.chat.id
                user_id = event.from_user.id
                
                try:
                    # Обновляем профиль пользователя
                    if database and hasattr(database, 'upsert_user_profile'):
                        await upsert_user_profile(
                            user_id=user_id,
                            username=event.from_user.username,
                            first_name=event.from_user.first_name,
                            last_name=event.from_user.last_name
                        )
                    
                    # Определяем тип сообщения и создаем parts_json
                    parts_json = await self._create_parts_json(event)
                    
                    # Сохраняем сообщение в историю, если parts_json не пустой
                    if parts_json and database and hasattr(database, 'add_message_to_history'):
                        await add_message_to_history(
                            chat_id=chat_id,
                            role="user",
                            user_id=user_id,
                            parts=parts_json
                        )
                        logger.debug(f"Middleware: Saved group message from user {user_id} to chat history (chat_id={chat_id})")
                        
                except Exception as save_err:
                    logger.error(f"Middleware: Error saving message to history: {save_err}", exc_info=True)
        
        return result
    
    async def _create_parts_json(self, message: Message) -> Optional[str]:
        """
        Создает JSON-представление частей сообщения в зависимости от его типа.
        Поддерживает текст, фото, видео, стикеры и другие типы медиа.
        """
        parts = []
        
        # Обработка текста сообщения
        if message.text:
            parts.append({"type": "text", "content": message.text})
        elif message.caption:
            parts.append({"type": "text", "content": message.caption})
            
        # Обработка фото
        if message.photo:
            # Берем фото с лучшим качеством (последнее в массиве)
            photo = message.photo[-1]
            parts.append({
                "type": "photo", 
                "file_id": photo.file_id,
                "width": photo.width,
                "height": photo.height
            })
            
        # Обработка видео
        elif message.video:
            parts.append({
                "type": "video",
                "file_id": message.video.file_id,
                "duration": message.video.duration,
                "width": message.video.width,
                "height": message.video.height
            })
            
        # Обработка стикеров
        elif message.sticker:
            parts.append({
                "type": "sticker",
                "file_id": message.sticker.file_id,
                "emoji": message.sticker.emoji if hasattr(message.sticker, "emoji") else None
            })
            
        # Обработка документов
        elif message.document:
            parts.append({
                "type": "document",
                "file_id": message.document.file_id,
                "file_name": message.document.file_name
            })
            
        # Обработка голосовых
        elif message.voice:
            parts.append({
                "type": "voice",
                "file_id": message.voice.file_id,
                "duration": message.voice.duration
            })
            
        # Обработка аудио
        elif message.audio:
            parts.append({
                "type": "audio",
                "file_id": message.audio.file_id,
                "duration": message.audio.duration,
                "title": message.audio.title,
                "performer": message.audio.performer
            })
        
        # Если нет контента для сохранения
        if not parts:
            return None
            
        try:
            return json.dumps(parts, ensure_ascii=False)
        except Exception as json_err:
            logger.error(f"Error serializing message parts to JSON: {json_err}", exc_info=True)
            return None 