# telegram_interface/filters/admin.py
import logging
from typing import Union

from aiogram.filters import BaseFilter
from aiogram.types import Message, CallbackQuery

# Импортируем нашу утилиту проверки админа
try:
    from utils.helpers import is_admin
except ImportError:
    # Заглушка на случай ошибки импорта
    def is_admin(user_id: int) -> bool:
        logging.getLogger(__name__).warning("Using mock is_admin filter (always False).")
        return False
    logging.getLogger(__name__).critical("Failed to import is_admin from utils.helpers for Admin filter.")


logger = logging.getLogger(__name__)

class IsAdminFilter(BaseFilter):
    """
    Фильтр для проверки, является ли пользователь администратором бота.
    Работает как для Message, так и для CallbackQuery.
    """
    key = "is_admin" # Необязательный ключ для использования в хендлерах

    async def __call__(self, update: Union[Message, CallbackQuery]) -> bool:
        """
        Выполняет проверку прав администратора.
        """
        user = None
        # Получаем пользователя из Message или CallbackQuery
        if isinstance(update, Message):
            user = update.from_user
        elif isinstance(update, CallbackQuery):
            user = update.from_user

        if user:
            user_id = user.id
            # Вызываем нашу функцию проверки админа
            admin_check_result = is_admin(user_id)
            if not admin_check_result:
                 logger.debug(f"Access denied by IsAdminFilter for user {user_id}.")
            return admin_check_result
        else:
            # Если пользователя нет (очень редкий случай для этих типов update)
            logger.warning("IsAdminFilter could not determine user from update.")
            return False