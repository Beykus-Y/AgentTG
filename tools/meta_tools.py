# tools/communication_tools.py (или tools/meta_tools.py)

import logging
import asyncio
from typing import Dict, Optional

# --- Импорты ---
try:
    from bot_loader import bot, dp # Нужен бот для отправки админам и dp для модели
    from config import settings    # Нужны ID админов
    import database               # Нужна функция добавления в БД
    from utils.helpers import escape_markdown_v2
    from aiogram.enums import ParseMode
    from aiogram.exceptions import TelegramAPIError
except ImportError:
    settings = type('obj', (object,), {'admin_ids': set()})() # Заглушка для admin_ids
    database = None

logger = logging.getLogger(__name__)


# --- НОВАЯ ФУНКЦИЯ ---
async def Developer_Feedback(
    chat_id: Optional[int],       # Будет добавлено из контекста FC
    user_id: Optional[int],       # Будет добавлено из контекста FC
    Degree_of_importance: str, # Аргументы от модели (могут быть CamelCase)
    Reason: str,
    Problem: str
) -> Dict[str, str]:
    """
    Записывает обратную связь от модели в БД и отправляет уведомление администраторам.
    Эта функция является обработчиком для вызова 'Developer_Feedback' моделью.

    Args: (из Function Calling)
        chat_id (Optional[int]): ID чата, где произошла проблема.
        user_id (Optional[int]): ID пользователя, взаимодействие с которым вызвало фидбек.
        Degree_of_importance (str): Важность проблемы ('high', 'medium', 'low', etc.)
        Reason (str): Краткая причина/категория фидбека.
        Problem (str): Детальное описание проблемы/предложения.

    Returns:
        Dict[str, str]: Словарь со статусом операции.
    """
    tool_name = "Developer_Feedback" # Имя, которое знает модель
    internal_tool_name = "developer_feedback_tool" # Имя Python функции
    logger.info(f"--- Tool Call: {tool_name} (handled by {internal_tool_name}) ---")
    logger.info(f"    Args: chat_id={chat_id}, user_id={user_id}, importance='{Degree_of_importance}', reason='{Reason}', problem='{Problem[:100]}...'")

    # Валидация входных данных от модели
    if not Degree_of_importance or not isinstance(Degree_of_importance, str):
        return {"status": "error", "message": "Argument 'Degree_of_importance' is missing or invalid."}
    if not Reason or not isinstance(Reason, str):
        return {"status": "error", "message": "Argument 'Reason' is missing or invalid."}
    if not Problem or not isinstance(Problem, str):
        return {"status": "error", "message": "Argument 'Problem' is missing or invalid."}

    # Получаем имя модели из dp (опционально)
    model_name = None
    try:
        # Попытка получить имя текущей Pro модели (если она хранится)
        # Пример:
        current_index = dp.workflow_data.get("current_api_key_index", 0)
        pro_models = dp.workflow_data.get("pro_models_list", [])
        if pro_models and current_index < len(pro_models):
            model_instance = pro_models[current_index]
            model_name = getattr(model_instance, '_model_name', 'Unknown Pro Model')
    except Exception as e:
        logger.warning(f"Could not determine model name for feedback log: {e}")

    # 1. Запись в БД
    db_success = False
    feedback_id = None
    if database:
        try:
            feedback_id = await database.add_developer_feedback(
                degree_of_importance=Degree_of_importance,
                reason=Reason,
                problem_description=Problem,
                chat_id=chat_id,
                user_id=user_id,
                model_name=model_name
            )
            if feedback_id is not None:
                db_success = True
                logger.info(f"Feedback saved to DB with ID: {feedback_id}")
            else:
                logger.error("Failed to save feedback to DB (returned None ID).")
        except Exception as db_err:
            logger.error(f"Error saving feedback to DB: {db_err}", exc_info=True)
    else:
        logger.error("Cannot save feedback to DB: database module unavailable.")

    # 2. Отправка уведомления администраторам
    admin_notify_success = False
    if bot and settings and settings.admin_ids:
        # Формируем сообщение для админов
        escaped_reason = escape_markdown_v2(Reason)
        escaped_problem = escape_markdown_v2(Problem)
        escaped_importance = escape_markdown_v2(Degree_of_importance.upper())
        db_status = f"DB ID: `{feedback_id}`" if feedback_id else "DB Save Failed"

        admin_message = (
            f"⚠️ *Developer Feedback Received*\n\n"
            f"*Importance:* `{escaped_importance}`\n"
            f"*Reason:* `{escaped_reason}`\n"
            f"*Model:* `{escape_markdown_v2(model_name or 'N/A')}`\n"
            f"*Chat ID:* `{chat_id or 'N/A'}`\n"
            f"*User ID:* `{user_id or 'N/A'}`\n"
            f"*DB Status:* {db_status}\n\n"
            f"*Description:*\n```\n{escaped_problem}\n```"
        )

        # Ограничиваем длину сообщения
        if len(admin_message) > 4000: # Чуть меньше лимита
            admin_message = admin_message[:4000] + "\n... (message truncated)"
            logger.warning("Admin feedback notification message truncated.")

        sent_to_admins = 0
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=admin_message,
                    parse_mode=ParseMode.MARKDOWN_V2
                )
                sent_to_admins += 1
            except TelegramAPIError as send_err:
                logger.error(f"Failed to send feedback notification to admin {admin_id}: {send_err}")
            except Exception as e:
                logger.error(f"Unexpected error sending notification to admin {admin_id}: {e}", exc_info=True)

        if sent_to_admins > 0:
            admin_notify_success = True
            logger.info(f"Feedback notification sent to {sent_to_admins} admin(s).")
        else:
            logger.error("Failed to send feedback notification to any admin.")

    elif not bot:
        logger.error("Cannot send admin notification: Bot instance unavailable.")
    elif not settings or not settings.admin_ids:
        logger.warning("Cannot send admin notification: Admin IDs not configured.")

    # 3. Формируем результат для модели
    if db_success and admin_notify_success:
        return {"status": "success", "message": "Feedback logged and administrators notified."}
    elif db_success:
        return {"status": "warning", "message": "Feedback logged to DB, but failed to notify administrators."}
    elif admin_notify_success:
        return {"status": "warning", "message": "Feedback notification sent to administrators, but failed to log to DB."}
    else:
        return {"status": "error", "message": "Failed to log feedback to DB and failed to notify administrators."}