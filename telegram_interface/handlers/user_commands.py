# telegram_interface/handlers/user_commands.py

import logging
from aiogram import Router, types
from aiogram.filters import Command, CommandStart

# --- Локальные импорты ---
try:
    from utils.helpers import escape_markdown_v2
    # Если команды будут вызывать AI, импортируем ядро:
    # from core_agent.agent_processor import handle_user_request
    # Если команды будут работать с БД (например, /my_notes):
    # import database
except ImportError:
    logging.critical("CRITICAL: Failed to import dependencies in user_commands!", exc_info=True)
    def escape_markdown_v2(text: str) -> str: return text

logger = logging.getLogger(__name__)
router = Router(name="user_commands_router")

@router.message(CommandStart())
async def handle_start(message: types.Message):
    """Обработчик команды /start."""
    user = message.from_user
    if not user: return # На всякий случай

    user_name = user.full_name
    await message.reply(
        f"Привет, {escape_markdown_v2(user_name)}\\! 👋\n"
        "Я твой AI-ассистент\\. Чем могу помочь?\n"
        "Напиши /help, чтобы узнать о моих возможностях\\."
    )
    # --- Опционально: Регистрация/обновление профиля пользователя в БД ---
    # try:
    #     import database # Импортируем здесь, если еще не импортирован
    #     if database:
    #         await database.upsert_user_profile(
    #             user_id=user.id,
    #             username=user.username,
    #             first_name=user.first_name,
    #             last_name=user.last_name
    #         )
    # except Exception as e:
    #      logger.error(f"Failed to upsert profile for user {user.id} on /start: {e}", exc_info=True)

@router.message(Command("help"))
async def handle_help(message: types.Message):
    """Обработчик команды /help."""
    # Используем актуальный текст помощи, согласованный с реализованными функциями
    help_text = """
*Основные возможности:*
- Просто напиши мне в личном чате, и я постараюсь ответить.
- В группах отвечаю на упоминания (`@имя_бота`) или ответы на мои сообщения.
- Могу работать с файлами и выполнять код в безопасном окружении (спрошу подтверждения для опасных операций).
- Узнаю погоду, курсы акций (спросите меня).
- Могу получить чарт Яндекс.Музыки (/charts).
- Помогу с поиском информации в интернете (используя Deep Search).
- Запомню или забуду информацию о вас или других пользователях, если попросите.
- Могу получить описание аватара пользователя.

*Другие команды:*
/start - Начать диалог
/help - Показать это сообщение

*Административные команды* (доступны только администраторам бота):
`/warn`, `/unwarn`, `/warns` - Управление предупреждениями
`/ban`, `/unban` - Блокировка/разблокировка пользователя
`/del` - Удалить сообщение (ответом)
`/stats` - Показать статистику активности чата
`/clear` - Очистить мою память (историю) для этого чата
`/set_prompt` - Установить системный промпт (ответом)
`/reset_prompt` - Сбросить системный промпт
`/set_ai [pro/default]` - Выбрать режим AI (Pro рекомендуется)
`/set_model [имя_модели]` - Выбрать модель Gemini
`/news_setup` - Настроить автопостинг новостей
"""
    # Отправляем без Markdown, т.к. текст уже содержит экранирование
    await message.reply(help_text, parse_mode="None")

# --- Добавьте другие пользовательские команды сюда ---
# Например:
# @router.message(Command("my_notes"))
# async def handle_my_notes(message: types.Message):
#     """Показывает заметки пользователя."""
#     if database is None: await message.reply("❌ База данных недоступна."); return
#     user_id = message.from_user.id
#     notes = await database.get_user_notes(user_id, parse_json=False) # Получаем как строки
#     if not notes:
#         await message.reply("У вас пока нет сохраненных заметок.")
#         return
#     response_lines = ["*Ваши заметки:*"]
#     for category, value in notes.items():
#         response_lines.append(f"- *{escape_markdown_v2(category)}*: {escape_markdown_v2(value[:100])}{'...' if len(value)>100 else ''}")
#     await message.reply("\n".join(response_lines))