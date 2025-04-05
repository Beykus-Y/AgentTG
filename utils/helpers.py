# utils/helpers.py
import re
import logging
from typing import Optional, Set

# Импортируем настройки для доступа к ADMIN_IDS
try:
    from config import settings
except ImportError:
    # Заглушка на случай проблем с импортом config
    class MockSettings:
        admin_ids: Set[int] = set()
    settings = MockSettings()
    logging.warning("Could not import settings from config.py in helpers. Using mock settings.")

logger = logging.getLogger(__name__)

def is_admin(user_id: Optional[int]) -> bool:
    """
    Проверяет, является ли пользователь администратором бота.

    Args:
        user_id (Optional[int]): ID пользователя Telegram.

    Returns:
        bool: True, если пользователь является администратором, иначе False.
    """
    if user_id is None:
        return False
    # Проверяем наличие ID в множестве администраторов из настроек
    is_admin_flag = user_id in settings.admin_ids
    if is_admin_flag:
        logger.debug(f"Admin check: User {user_id} is an admin.")
    # else:
    #     logger.debug(f"Admin check: User {user_id} is NOT an admin.")
    return is_admin_flag


def escape_markdown_v2(text: Optional[str]) -> str:
    """
    Экранирует специальные символы для разметки Telegram MarkdownV2.

    Args:
        text (Optional[str]): Входной текст.

    Returns:
        str: Текст с экранированными символами или пустая строка, если на входе None.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
            logger.warning(f"escape_markdown_v2 received non-string type: {type(text)}. Converted to string.")
        except Exception:
             logger.error(f"escape_markdown_v2 failed to convert non-string input: {type(text)}.")
             return ""

    # Символы для экранирования в MarkdownV2
    # _ * [ ] ( ) ~ ` > # + - = | { } . !
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    # Создаем регулярное выражение для поиска этих символов
    # Экранируем сам символ '\' перед ним
    regex = re.compile(f'([{re.escape(escape_chars)}])')
    # Заменяем найденные символы на экранированные (добавляем '\' перед символом)
    return regex.sub(r'\\\1', text)


def remove_markdown(text: Optional[str]) -> str:
    """
    Удаляет основные символы разметки Markdown из текста.

    Args:
        text (Optional[str]): Входной текст.

    Returns:
        str: Текст без Markdown разметки или пустая строка, если на входе None.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
            logger.warning(f"remove_markdown received non-string type: {type(text)}. Converted to string.")
        except Exception:
             logger.error(f"remove_markdown failed to convert non-string input: {type(text)}.")
             return ""

    # Удаляем символы форматирования: *, _, ~, `, ```, [, ], (, )
    # Осторожно с [, ], (, ), так как они могут быть частью обычного текста.
    # Простое удаление может быть недостаточным для сложных случаев (например, вложенность).

    # Удаляем парные символы (**, __, ~~, ```, `)
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'__(.*?)__', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'~~(.*?)~~', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'```(.*?)```', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'`(.*?)`', r'\1', text, flags=re.DOTALL)

    # Удаляем одиночные символы (*, _) - могут быть в обычных словах, удаляем аккуратно
    # Этот шаг может быть излишне агрессивным, возможно, лучше оставить
    # text = re.sub(r'(?<!\\)[*_]', '', text) # Удаляем * и _, если перед ними нет \

    # Удаляем разметку ссылок [текст](url) -> текст
    text = re.sub(r'\[(.*?)\]\((.*?)\)', r'\1', text)

    return text

# Можно добавить другие вспомогательные функции по мере необходимости