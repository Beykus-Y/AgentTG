# telegram_interface/handlers/admin_commands.py

import logging
from typing import Optional

from aiogram import Router, types, Bot
from aiogram.filters import Command, CommandObject
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError

# --- Локальные импорты ---
try:
    # Фильтр администратора
    from ..filters.admin import IsAdminFilter
    # Модуль базы данных
    import database
    # Вспомогательные утилиты
    from utils.helpers import escape_markdown_v2, is_admin as check_if_admin
    # Экземпляр бота для получения информации о пользователе
    from bot_loader import bot as current_bot # Переименовываем, чтобы не конфликтовать с аргументом bot
    # Настройки (для лимита варнов, например)
    from config import settings
    # Инструмент для чартов
    from tools.basic_tools import get_music_charts
except ImportError:
    logging.critical("CRITICAL: Failed to import dependencies in admin_commands!", exc_info=True)
    # Заглушки
    IsAdminFilter = type('Filter', (object,), {'__call__': lambda self, u: True}) # type: ignore
    database = None # type: ignore
    def escape_markdown_v2(text: str) -> str: return text
    def check_if_admin(uid: Optional[int]) -> bool: return False
    current_bot = None # type: ignore
    settings = type('obj', (object,), {'warn_limit': 5})() # type: ignore
    async def get_music_charts(*args, **kwargs): return {"status": "error", "message": "Tool unavailable"}

logger = logging.getLogger(__name__)
router = Router(name="admin_commands_router")

# !!! Применяем фильтр IsAdminFilter ко всем хендлерам в этом роутере !!!
router.message.filter(IsAdminFilter())
# Опционально: Ограничить команды только группами
# router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))


# --- Вспомогательная функция для определения целевого пользователя ---
async def _get_target_user(message: types.Message, command: CommandObject, bot: Bot) -> Optional[types.User]:
    """
    Вспомогательная функция для определения целевого пользователя (из реплая или аргумента).
    Возвращает объект User или None при ошибке или если пользователя нельзя выбрать целью.
    """
    target_user: Optional[types.User] = None
    error_message: Optional[str] = None

    # 1. Проверка ответа на сообщение
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        logger.debug(f"Target user identified via reply: {target_user.id} ({target_user.full_name})")
    # 2. Проверка аргументов команды
    elif command and command.args:
        arg = command.args.strip()
        logger.debug(f"Attempting to find target user by argument: '{arg}'")
        # Пытаемся найти по ID
        if arg.isdigit() or (arg.startswith('-') and arg[1:].isdigit()):
             try:
                 target_user_id = int(arg)
                 target_user = await bot.get_chat(target_user_id) # get_chat может вернуть Chat или User
                 if not isinstance(target_user, types.User): # Убедимся, что это пользователь
                     error_message = f"❌ ID {target_user_id} не принадлежит пользователю."
                     target_user = None
                 else: logger.info(f"Found user {target_user_id} by ID argument.")
             except TelegramAPIError as e:
                 logger.warning(f"Could not get user by ID {arg}: {e}")
                 error_message = f"❌ Не удалось найти пользователя по ID `{arg}` в Telegram."
             except Exception as e: # Ловим другие ошибки get_chat
                 logger.error(f"Unexpected error getting user by ID {arg}: {e}", exc_info=True)
                 error_message = "❌ Ошибка при поиске пользователя по ID."
        # Если не ID, ищем по имени/юзернейму в БД
        elif database: # Проверяем доступность модуля БД
             db_user_id = await database.find_user_id_by_profile(arg)
             if db_user_id:
                  try:
                      target_user = await bot.get_chat(db_user_id)
                      if not isinstance(target_user, types.User): target_user = None
                      else: logger.info(f"Found user {db_user_id} by profile search for '{arg}'.")
                  except Exception as e:
                       logger.warning(f"Found user ID {db_user_id} in DB for '{arg}', but failed to get chat info: {e}")
                       error_message = f"⚠️ Найден ID `{db_user_id}`, но не удалось получить инфо из Telegram."
             else:
                  error_message = f"❌ Пользователь '{escape_markdown_v2(arg)}' не найден ни по ID, ни в базе данных."
        else: # Если БД недоступна
            error_message = "❌ Поиск по имени/юзернейму недоступен (ошибка БД)."
    else:
         error_message = "❌ Укажите пользователя (ответом на сообщение или ID/именем/юзернеймом после команды)."

    # Если была ошибка поиска
    if error_message:
        await message.reply(error_message) # Сообщение уже экранировано или содержит MarkdownV2
        return None

    # --- Проверки выбранного пользователя ---
    if target_user is None:
         await message.reply("❌ Не удалось определить целевого пользователя.")
         return None
    if target_user.is_bot:
         await message.reply("🚫 Команды нельзя применять к ботам.")
         return None
    # Проверка, не пытается ли админ применить команду к другому админу бота
    if target_user.id != message.from_user.id and check_if_admin(target_user.id):
         await message.reply("🚫 Нельзя применять эту команду к другому администратору бота.")
         return None
    # Проверка на админа чата (для групповых чатов)
    if message.chat.type != ChatType.PRIVATE:
         try:
            member = await bot.get_chat_member(message.chat.id, target_user.id)
            if member.status in ["administrator", "creator"]:
                 await message.reply("🚫 Нельзя применять эту команду к администратору чата.")
                 return None
         except TelegramAPIError as e:
              logger.error(f"Failed check chat admin status user={target_user.id} chat={message.chat.id}: {e}")
              await message.reply("⚠️ Не удалось проверить статус пользователя в чате.")
              return None
         except Exception as e: # Ловим другие ошибки get_chat_member
              logger.error(f"Unexpected error checking chat member status user={target_user.id} chat={message.chat.id}: {e}", exc_info=True)
              await message.reply("⚠️ Ошибка при проверке статуса пользователя.")
              return None

    return target_user # Возвращаем валидного пользователя


# --- Команды Варнов ---
@router.message(Command("warn"))
async def warn_user_command(message: types.Message, command: CommandObject, bot: Bot):
    """Выдает предупреждение пользователю."""
    if database is None: await message.reply("❌ База данных недоступна."); return

    target_user = await _get_target_user(message, command, bot)
    if not target_user: return

    chat_id = message.chat.id
    user_id = target_user.id

    new_warn_count = await database.add_user_warning(chat_id, user_id)

    if new_warn_count is None:
        await message.reply("❌ Ошибка при добавлении предупреждения в БД.")
        return

    warn_limit = getattr(settings, 'warn_limit', 5) # Берем лимит из настроек или ставим 5
    mention = target_user.mention_markdown(target_user.full_name)
    reply_text = f"⚠️ Пользователю {mention} выдано предупреждение\\! ({new_warn_count}/{warn_limit})"

    if new_warn_count >= warn_limit:
        reply_text += f"\n🚨 Достигнут лимит предупреждений\\! Пользователь забанен\\."
        try:
            # Баним пользователя в текущем чате
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            logger.info(f"User {user_id} banned in chat {chat_id} due to warn limit.")
            # Сбрасываем варны после бана
            await database.reset_user_warnings(chat_id, user_id)
        except TelegramAPIError as e:
            logger.error(f"Failed ban user {user_id} after warn limit chat={chat_id}: {e}")
            reply_text += "\n(Не удалось автоматически забанить пользователя\\.)"
        except Exception as e:
             logger.error(f"Unexpected error banning user {user_id} after warn limit chat={chat_id}: {e}", exc_info=True)
             reply_text += "\n(Ошибка при попытке бана\\.)"

    await message.reply(reply_text) # Текст уже содержит Markdown V2

@router.message(Command("unwarn"))
async def unwarn_user_command(message: types.Message, command: CommandObject, bot: Bot):
    """Снимает предупреждение(я) с пользователя."""
    if database is None: await message.reply("❌ База данных недоступна."); return

    target_user = await _get_target_user(message, command, bot)
    if not target_user: return

    chat_id = message.chat.id
    user_id = target_user.id
    count_to_remove = 1
    # Пытаемся получить количество из аргументов команды
    if command and command.args and command.args.isdigit():
        count_to_remove = max(1, min(int(command.args), 5)) # Снимаем от 1 до 5 варнов
        logger.debug(f"Attempting to remove {count_to_remove} warnings from user {user_id}.")

    current_warns = await database.get_user_warn_count(chat_id, user_id)
    if current_warns == 0:
         mention = target_user.mention_markdown(target_user.full_name)
         await message.reply(f"ℹ️ У пользователя {mention} нет предупреждений\\.")
         return

    new_warn_count = await database.remove_user_warning(chat_id, user_id, count_to_remove)

    if new_warn_count is None:
        await message.reply("❌ Ошибка при снятии предупреждения из БД.")
        return

    removed_actual = current_warns - new_warn_count
    mention = target_user.mention_markdown(target_user.full_name)
    await message.reply(f"✅ Снято {removed_actual} пред\\-ий с {mention}\\. Осталось: {new_warn_count}\\.")


@router.message(Command("warns"))
async def show_warns_command(message: types.Message, command: CommandObject, bot: Bot):
    """Показывает предупреждения пользователя или всего чата."""
    if database is None: await message.reply("❌ База данных недоступна."); return

    chat_id = message.chat.id
    target_user: Optional[types.User] = None

    # Если есть аргументы или реплай - показываем для конкретного пользователя
    if message.reply_to_message or (command and command.args):
        target_user = await _get_target_user(message, command, bot)
        if not target_user: return # Ошибка или нельзя применить
        user_id = target_user.id
        warn_count = await database.get_user_warn_count(chat_id, user_id)
        warn_limit = getattr(settings, 'warn_limit', 5)
        mention = target_user.mention_markdown(target_user.full_name)
        await message.reply(
            f"⚠️ У пользователя {mention} {warn_count}/{warn_limit} предупреждений в этом чате\\."
        )
    else:
        # Показываем все варны в чате
        all_chat_warnings = await database.get_chat_warnings(chat_id)
        if not all_chat_warnings:
            await message.reply("✅ В этом чате нет пользователей с предупреждениями\\.")
            return

        warn_list_text = ["🚨 *Список пользователей с предупреждениями:*"]
        user_mentions: Dict[int, str] = {}
        # Сначала получаем информацию о пользователях
        user_ids = list(all_chat_warnings.keys())
        # Пытаемся получить имена пачкой (если возможно) или по одному
        for user_id in user_ids:
             try:
                 member = await bot.get_chat_member(chat_id, user_id)
                 user_mentions[user_id] = member.user.mention_markdown(member.user.full_name)
             except Exception:
                 user_mentions[user_id] = f"Пользователь\\_ID:`{user_id}`" # Экранируем ID

        # Формируем список
        for user_id, count in all_chat_warnings.items():
            mention = user_mentions.get(user_id, f"Пользователь\\_ID:`{user_id}`")
            warn_list_text.append(f"  • {mention}: {count} пред\\.")

        await message.reply("\n".join(warn_list_text))

# --- Команды Бана ---
@router.message(Command("ban"))
async def ban_user_command(message: types.Message, command: CommandObject, bot: Bot):
    """Банит пользователя в чате."""
    if database is None: await message.reply("❌ База данных недоступна."); return

    target_user = await _get_target_user(message, command, bot)
    if not target_user: return

    chat_id = message.chat.id
    user_id = target_user.id
    mention = target_user.mention_markdown(target_user.full_name)

    try:
        # Баним пользователя
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        logger.info(f"Admin {message.from_user.id} banned user {user_id} in chat {chat_id}")
        # Сбрасываем варны после бана
        await database.reset_user_warnings(chat_id, user_id)
        await message.reply(f"🚨 Пользователь {mention} забанен\\. Предупреждения сброшены\\.")
    except TelegramAPIError as e:
        logger.error(f"Failed to ban user {user_id} in chat {chat_id}: {e}")
        await message.reply(f"❌ Не удалось забанить пользователя: {escape_markdown_v2(str(e))}")
    except Exception as e:
         logger.error(f"Unexpected error banning user {user_id} chat={chat_id}: {e}", exc_info=True)
         await message.reply("❌ Произошла непредвиденная ошибка при бане\\.")


@router.message(Command("unban"))
async def unban_user_command(message: types.Message, command: CommandObject, bot: Bot):
    """Разбанивает пользователя в чате."""
    if database is None: await message.reply("❌ База данных недоступна."); return

    # Unban работает по ID или username, reply тут не поможет найти ID забаненного
    if not command or not command.args:
        await message.reply("❌ Укажите ID или username пользователя для разбана после команды\\.")
        return

    target_query = command.args.strip()
    target_user_id: Optional[int] = None
    target_mention: str = escape_markdown_v2(target_query) # По умолчанию упоминаем как есть

    # Пытаемся получить ID
    if target_query.isdigit() or (target_query.startswith('-') and target_query[1:].isdigit()):
        target_user_id = int(target_query)
        target_mention = f"ID `{target_user_id}`"
    else:
        # Ищем в БД, чтобы подтвердить, что такой пользователь был
        target_user_id = await database.find_user_id_by_profile(target_query)
        if not target_user_id:
             await message.reply(f"❌ Не удалось найти ID пользователя для '{escape_markdown_v2(target_query)}'\\. Попробуйте точный ID\\.")
             return
        else:
             target_mention = f"'{escape_markdown_v2(target_query)}' \\(ID `{target_user_id}`\\)"

    chat_id = message.chat.id
    try:
        # Пытаемся разбанить
        await bot.unban_chat_member(chat_id=chat_id, user_id=target_user_id, only_if_banned=True)
        logger.info(f"Admin {message.from_user.id} unbanned user {target_user_id} in chat {chat_id}")
        await message.reply(f"✅ Пользователь {target_mention} разбанен \\(если был забанен\\)\\.")
    except TelegramAPIError as e:
        logger.error(f"Failed to unban user {target_user_id} in chat {chat_id}: {e}")
        await message.reply(f"❌ Не удалось разбанить пользователя: {escape_markdown_v2(str(e))}")
    except Exception as e:
         logger.error(f"Unexpected error unbanning user {target_user_id} chat={chat_id}: {e}", exc_info=True)
         await message.reply("❌ Произошла непредвиденная ошибка при разбане\\.")


# --- Команды Настроек AI ---
@router.message(Command("set_prompt"))
async def set_prompt_command(message: types.Message):
    """Устанавливает кастомный системный промпт для текущего чата."""
    if database is None: await message.reply("❌ База данных недоступна."); return

    if not message.reply_to_message or not message.reply_to_message.text:
        await message.reply("❌ Ответьте на сообщение с текстом нового системного промпта\\!")
        return

    chat_id = message.chat.id
    new_prompt = message.reply_to_message.text.strip()

    if not new_prompt:
         await message.reply("❌ Новый промпт не может быть пустым\\.")
         return

    if await database.upsert_chat_settings(chat_id, custom_prompt=new_prompt):
        await message.reply("✅ Системный промпт для этого чата обновлен\\! История будет очищена для применения\\.")
        # Очищаем историю, чтобы новый промпт применился к следующему диалогу
        await database.clear_chat_history(chat_id)
    else:
        await message.reply("❌ Не удалось сохранить новый промпт в базе данных\\.")


@router.message(Command("reset_prompt"))
async def reset_prompt_command(message: types.Message):
    """Сбрасывает системный промпт чата к стандартному."""
    if database is None: await message.reply("❌ База данных недоступна."); return

    chat_id = message.chat.id
    # Устанавливаем пустую строку, чтобы использовался дефолтный промпт из config.py
    if await database.upsert_chat_settings(chat_id, custom_prompt=""):
        await message.reply("✅ Системный промпт сброшен к стандартному\\! История будет очищена для применения\\.")
        await database.clear_chat_history(chat_id)
    else:
        await message.reply("❌ Не удалось сбросить промпт в базе данных\\.")


@router.message(Command("set_ai"))
async def set_ai_mode_command(message: types.Message, command: CommandObject):
    """Устанавливает режим AI (pro/default) для чата."""
    if database is None: await message.reply("❌ База данных недоступна."); return

    mode_pro = database.AI_MODE_PRO # Получаем константы из модуля БД
    mode_default = database.AI_MODE_DEFAULT

    if not command or not command.args or command.args.lower() not in [mode_pro, mode_default]:
        await message.reply(
            f"❌ Неверный формат\\! Используйте: `/set_ai {mode_pro}` или `/set_ai {mode_default}`"
        )
        return

    chat_id = message.chat.id
    new_mode = command.args.lower()

    if await database.upsert_chat_settings(chat_id, ai_mode=new_mode):
        mode_name = "Gemini (Pro)" if new_mode == mode_pro else "Стандартный (Default)"
        await message.reply(f"✅ Режим AI для этого чата изменен на: {escape_markdown_v2(mode_name)}\\.")
        # Очистка истории не обязательна при смене режима, но желательна
        # await database.clear_chat_history(chat_id)
    else:
        await message.reply("❌ Не удалось изменить режим AI в базе данных\\.")


@router.message(Command("set_model"))
async def set_gemini_model_command(message: types.Message, command: CommandObject):
    """Устанавливает конкретную модель Gemini для чата (автоматически включает режим 'pro')."""
    if database is None: await message.reply("❌ База данных недоступна."); return

    # Получаем доступные модели из config (если они там определены как список/enum)
    # Или хардкодим здесь для простоты
    available_models = [
        settings.pro_gemini_model_name,
        settings.lite_gemini_model_name
        # Добавить другие, если есть
    ]
    available_models_str = ", ".join([f"`{m}`" for m in available_models])

    if not command or not command.args or command.args not in available_models:
         await message.reply(
             f"❌ Неверный формат или модель\\! Используйте: `/set_model [имя_модели]`\n"
             f"Доступные модели: {available_models_str}"
         )
         return

    chat_id = message.chat.id
    new_model = command.args

    # Устанавливаем модель и принудительно режим 'pro'
    if await database.upsert_chat_settings(chat_id, gemini_model=new_model, ai_mode=database.AI_MODE_PRO):
        await message.reply(f"✅ Модель Gemini для чата установлена: `{escape_markdown_v2(new_model)}`\\. Режим AI: `{database.AI_MODE_PRO}`\\. История будет очищена\\.")
        await database.clear_chat_history(chat_id) # Рекомендуется очищать историю при смене модели
    else:
        await message.reply("❌ Не удалось изменить модель Gemini в базе данных\\.")


# --- Другие Админские Команды ---

@router.message(Command("clear"))
async def clear_history_command(message: types.Message):
    """Очищает историю диалога для текущего чата."""
    if database is None: await message.reply("❌ База данных недоступна."); return

    chat_id = message.chat.id
    deleted_count = await database.clear_chat_history(chat_id)
    await message.reply(f"🔄 История диалога для чата очищена \\({deleted_count} записей удалено\\)\\.")


@router.message(Command("del"))
async def delete_message_command(message: types.Message, bot: Bot):
    """Удаляет сообщение, на которое ответили."""
    if not message.reply_to_message:
        await message.reply("ℹ️ Ответьте на сообщение, которое нужно удалить\\!")
        return

    try:
        await bot.delete_message(message.chat.id, message.reply_to_message.message_id)
        logger.info(f"Admin {message.from_user.id} deleted message {message.reply_to_message.message_id} in chat {message.chat.id}")
        # Удаляем и саму команду /del
        await message.delete()
    except TelegramAPIError as e:
        logger.error(f"Failed to delete message {message.reply_to_message.message_id} chat={message.chat.id}: {e}")
        # Не отвечаем в чат, если не удалось удалить исходное сообщение
        # await message.reply(f"❌ Не удалось удалить сообщение: {escape_markdown_v2(str(e))}")
    except Exception as e:
         logger.error(f"Unexpected error deleting msg {message.reply_to_message.message_id} chat={message.chat.id}: {e}", exc_info=True)


@router.message(Command("stats"))
async def show_stats_command(message: types.Message, bot: Bot):
    """Показывает топ-10 активных пользователей чата."""
    if database is None: await message.reply("❌ База данных недоступна."); return

    chat_id = message.chat.id
    top_users_data = await database.get_chat_stats_top_users(chat_id, limit=10)

    if not top_users_data:
        await message.reply("📊 Статистика для этого чата пока пуста или не загружена\\.")
        return

    stats_text = ["🏆 *Топ активных пользователей чата:*"]
    user_mentions: Dict[int, str] = {}
    # Получаем информацию о пользователях
    user_ids_to_fetch = [uid for uid, count in top_users_data]
    # Можно оптимизировать, получая пользователей пачкой, если API позволяет
    for user_id in user_ids_to_fetch:
         try:
             # Пытаемся получить пользователя через get_chat_member
             member = await bot.get_chat_member(chat_id, user_id)
             user_mentions[user_id] = member.user.mention_markdown(member.user.full_name)
         except Exception:
             # Если не получилось (пользователь ушел, ошибка API), используем ID
             user_mentions[user_id] = f"Пользователь\\_ID:`{user_id}`"

    # Формируем список
    for i, (user_id, count) in enumerate(top_users_data, 1):
        mention = user_mentions.get(user_id, f"Пользователь\\_ID:`{user_id}`")
        stats_text.append(f"{i}\\. {mention} \\- {count} сообщ\\.")

    await message.reply("\n".join(stats_text))


@router.message(Command('charts'))
async def charts_command_handler(message: types.Message):
    """Обработчик команды /charts (вызывает инструмент)."""
    args = message.text.split()
    limit = 10
    if len(args) > 1 and args[1].isdigit():
        limit = max(1, min(int(args[1]), 50)) # Ограничиваем лимит

    try:
        result = await get_music_charts(source="yandex", limit=limit) # Вызываем асинхронный инструмент

        if isinstance(result, dict) and result.get("status") == "success":
            chart_source = result.get("chart_source", "Музыкальный чарт")
            top_tracks = result.get("top_tracks", [])
            if top_tracks:
                 response_lines = [f"🎶 *Топ-{len(top_tracks)} из {escape_markdown_v2(chart_source)}:*\n"]
                 for track in top_tracks:
                      title = escape_markdown_v2(track.get('title', 'N/A'))
                      artist = escape_markdown_v2(track.get('artist', 'N/A'))
                      pos = track.get('position', '')
                      url = track.get('url')
                      line = f"{pos}\\. {title} \\- {artist}"
                      if url and url != "N/A":
                          line = f"{pos}\\. [{title} \\- {artist}]({url})" # Делаем ссылку, если есть URL
                      response_lines.append(line)
                 await message.reply("\n".join(response_lines), disable_web_page_preview=True)
            else:
                 await message.reply(f"ℹ️ Не удалось получить треки из чарта {escape_markdown_v2(chart_source)}\\.")

        else: # Если статус не success или result не словарь
            error_text = result.get("message", "Неизвестная ошибка") if isinstance(result, dict) else str(result)
            await message.reply(f"❌ Не удалось получить чарт: {escape_markdown_v2(error_text)}")
    except Exception as e:
         logger.error(f"Error handling /charts command: {e}", exc_info=True)
         await message.reply("❌ Произошла ошибка при получении чарта\\.")