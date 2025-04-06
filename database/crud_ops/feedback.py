# database/crud_ops/feedback.py

import logging
import aiosqlite
from typing import Optional

try:
    from ..connection import get_connection
except ImportError:
    async def get_connection(): raise ImportError("Connection module not loaded")

logger = logging.getLogger(__name__)

async def add_developer_feedback(
    degree_of_importance: str,
    reason: str,
    problem_description: str,
    chat_id: Optional[int] = None,
    user_id: Optional[int] = None,
    model_name: Optional[str] = None
) -> Optional[int]:
    """
    Добавляет запись обратной связи в таблицу developer_feedback.

    Args:
        degree_of_importance (str): Важность ('high', 'medium', 'low', etc.).
        reason (str): Краткая причина/категория.
        problem_description (str): Детальное описание проблемы.
        chat_id (Optional[int]): ID чата (если есть).
        user_id (Optional[int]): ID пользователя (если есть).
        model_name (Optional[str]): Имя модели (если известно).

    Returns:
        Optional[int]: ID созданной записи или None при ошибке.
    """
    conn: aiosqlite.Connection
    inserted_id: Optional[int] = None

    # Валидация важности (опционально, можно положиться на CHECK в БД)
    valid_degrees = {'high', 'medium', 'low', 'critical', 'suggestion'}
    if degree_of_importance.lower() not in valid_degrees:
        logger.warning(f"Invalid degree_of_importance '{degree_of_importance}'. Using 'medium'.")
        degree_of_importance = 'medium'

    try:
        conn = await get_connection()
        cursor = await conn.execute(
            """
            INSERT INTO developer_feedback (
                chat_id, user_id, model_name, degree_of_importance, reason, problem_description
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id, user_id, model_name, degree_of_importance.lower(),
                reason, problem_description
            )
        )
        inserted_id = cursor.lastrowid
        await conn.commit()
        await cursor.close()

        if inserted_id is not None:
            logger.info(f"Added developer feedback log ID: {inserted_id}. Importance: {degree_of_importance}, Reason: {reason[:50]}...")
        else:
            logger.warning("Could not retrieve lastrowid after inserting developer feedback.")

        return inserted_id

    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Failed to add developer feedback: {e}", exc_info=True)
        if 'conn' in locals() and conn and not isinstance(e, ImportError):
            try: await conn.rollback()
            except Exception as rb_err: logger.error(f"Rollback failed after feedback insert error: {rb_err}")
        return None
    except Exception as e:
         logger.error(f"Unexpected error adding developer feedback: {e}", exc_info=True)
         return None