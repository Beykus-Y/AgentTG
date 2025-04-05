# telegram_interface/handlers/news_setup_fsm.py

import logging
import re
import json
from typing import List, Set, Optional

from aiogram import Router, F, types, Bot
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import default_state
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message, ChatMemberAdministrator, ChatMemberOwner
from aiogram.exceptions import TelegramAPIError

# --- Локальные импорты ---
try:
    # Состояния FSM
    from ..states.news_setup import NewsSetupStates
    # Доступ к БД
    import database
    # Доступ к настройкам (для RSS_MAPPING)
    from config import settings
    # Вспомогательные функции
    from utils.helpers import escape_markdown_v2
except ImportError:
    logging.critical("CRITICAL: Failed to import dependencies in news_setup_fsm!", exc_info=True)
    # Заглушки
    class NewsSetupStates: waiting_channel, waiting_topics, waiting_schedule = "s1", "s2", "s3" # type: ignore
    database = None # type: ignore
    settings = type('obj', (object,), {'rss_mapping': {'тест': ['url']}})() # type: ignore
    def escape_markdown_v2(text: str) -> str: return text

logger = logging.getLogger(__name__)
router = Router(name="news_setup_fsm_router")

# --- Кнопка отмены ---
cancel_button = InlineKeyboardButton(text="❌ Отмена", callback_data="news_setup:cancel")
cancel_keyboard = InlineKeyboardMarkup(inline_keyboard=[[cancel_button]])

# --- Обработка команды /news_setup ---
@router.message(Command("news_setup"), StateFilter(default_state))
async def cmd_news_setup_start(message: Message, state: FSMContext):
    """Начало настройки автоновостей."""
    user_id = message.from_user.id if message.from_user else 0
    logger.info(f"User {user_id} initiated news setup.")
    try:
        # Используем MarkdownV2 для форматирования
        await message.answer(
            "📰 *Настройка Автоновостей*\n\n"
            "Чтобы я мог публиковать новости в вашем канале, мне нужны права администратора с возможностью *публикации сообщений*\\.\n\n"
            "*Шаг 1/3:* Пожалуйста, **перешлите любое сообщение из вашего канала** сюда\\. "
            "Или отправьте его **username** \\(например, `@mychannel`\\) или **ID** \\(например, `-100123456789`\\)\\.\n\n"
            "_Убедитесь, что бот уже добавлен в администраторы канала\\!_",
            reply_markup=cancel_keyboard # Добавляем кнопку отмены
        )
        await state.set_state(NewsSetupStates.waiting_channel)
    except Exception as e:
        logger.error(f"Error starting news setup for user {user_id}: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка при запуске настройки\\. Попробуйте позже\\.")

# --- Обработка отмены на любом шаге ---
@router.callback_query(F.data == "news_setup:cancel", StateFilter(NewsSetupStates))
async def cancel_handler_callback(callback: CallbackQuery, state: FSMContext):
    """Обработка нажатия кнопки отмены."""
    current_state = await state.get_state()
    if current_state is None: return # Если состояние уже сброшено

    user_id = callback.from_user.id
    logger.info(f"User {user_id} cancelled news setup from state {current_state}.")
    await state.clear()
    try:
        # Пытаемся отредактировать сообщение, с которого пришел колбек
        await callback.message.edit_text("❌ Настройка автоновостей отменена\\.")
    except Exception:
        # Если не удалось отредактировать, отправляем новое сообщение
        await callback.message.answer("❌ Настройка автоновостей отменена\\.")
    await callback.answer() # Убираем часики

@router.message(Command("cancel"), StateFilter(NewsSetupStates))
async def cancel_handler_command(message: Message, state: FSMContext):
    """Обработка команды /cancel во время настройки."""
    current_state = await state.get_state()
    if current_state is None: return

    user_id = message.from_user.id if message.from_user else 0
    logger.info(f"User {user_id} cancelled news setup via command from state {current_state}.")
    await state.clear()
    await message.reply("❌ Настройка автоновостей отменена\\.")


# --- Шаг 1: Получение и проверка канала ---
@router.message(StateFilter(NewsSetupStates.waiting_channel), F.forward_from_chat | F.text)
async def process_channel_input(message: Message, state: FSMContext, bot: Bot):
    """Обрабатывает ввод пользователя для определения канала."""
    user_id = message.from_user.id if message.from_user else 0
    channel_id: Optional[int] = None
    channel_title: Optional[str] = None
    error_msg: Optional[str] = None
    target_chat: Optional[types.Chat] = None

    # Вариант 1: Пересланное сообщение
    if message.forward_from_chat and message.forward_from_chat.type == 'channel':
        target_chat = message.forward_from_chat
        logger.debug(f"Received forwarded message from channel ID: {target_chat.id}")
    # Вариант 2: Текстовый ввод (username или ID)
    elif message.text:
        text_input = message.text.strip()
        try:
            target_chat = await bot.get_chat(text_input)
            if target_chat.type != 'channel':
                error_msg = f"❌ Указанный идентификатор (`{escape_markdown_v2(text_input)}`) принадлежит чату типа `{target_chat.type}`, а не каналу\\."
                target_chat = None
            else:
                logger.debug(f"Resolved channel from input '{text_input}' to ID: {target_chat.id}")
        except TelegramAPIError as e:
            logger.warning(f"Failed to get chat by input '{text_input}': {e}")
            error_msg = f"❌ Не удалось найти канал по '{escape_markdown_v2(text_input)}'\\. Убедитесь, что username/ID указан верно, и бот имеет к нему доступ\\."
        except Exception as e:
            logger.error(f"Unexpected error getting chat '{text_input}': {e}", exc_info=True)
            error_msg = "❌ Произошла непредвиденная ошибка при проверке канала\\."
    else:
        # Некорректный ввод
        await message.reply("Пожалуйста, перешлите сообщение из канала или отправьте его username/ID\\.", reply_markup=cancel_keyboard)
        return

    # Если не удалось определить канал
    if target_chat is None:
        await message.reply(error_msg or "Не удалось определить канал\\. Попробуйте снова\\.", reply_markup=cancel_keyboard)
        return

    channel_id = target_chat.id
    channel_title = target_chat.title or f"Канал {channel_id}"

    # Проверка прав бота и пользователя в канале
    try:
        me = await bot.get_me()
        bot_member = await bot.get_chat_member(channel_id, me.id)

        # Проверяем, является ли бот админом с правом публикации
        if not isinstance(bot_member, (ChatMemberAdministrator, ChatMemberOwner)) or \
           (isinstance(bot_member, ChatMemberAdministrator) and not bot_member.can_post_messages):
             error_msg = f"Бот не является администратором канала '{escape_markdown_v2(channel_title)}' или **не имеет права публиковать сообщения**\\. Пожалуйста, проверьте права бота в настройках канала\\."

        # Проверяем, является ли пользователь админом/владельцем канала
        if user_id != 0 and not error_msg: # Проверяем пользователя, только если нет ошибки с ботом
             user_member = await bot.get_chat_member(channel_id, user_id)
             if user_member.status not in ["administrator", "creator"]:
                  error_msg = "Вы должны быть администратором канала, чтобы настроить для него автопостинг новостей\\."

    except TelegramAPIError as e:
         logger.error(f"API error checking permissions channel={channel_id}: {e}")
         error_msg = f"Ошибка при проверке прав в канале '{escape_markdown_v2(channel_title)}' \\(ID: `{channel_id}`\\)\\. Возможно, у бота нет прав для просмотра администраторов или канал не существует\\. Ошибка API: {escape_markdown_v2(str(e))}"
    except Exception as e:
         logger.error(f"Unexpected error checking permissions channel={channel_id}: {e}", exc_info=True)
         error_msg = "Произошла непредвиденная ошибка при проверке прав в канале\\."

    # Если были ошибки при проверке прав
    if error_msg:
        await message.reply(error_msg, reply_markup=cancel_keyboard)
        return

    # Если все проверки пройдены
    logger.info(f"Channel '{channel_title}' (ID: {channel_id}) verified for user {user_id}. Bot has posting rights.")
    await state.update_data(channel_id=channel_id, channel_title=channel_title)

    # Формируем список доступных тем из настроек
    available_topics = list(settings.rss_mapping.keys())
    if not available_topics:
         logger.error("Configuration error: No available RSS topics found in settings.rss_mapping.")
         await message.reply("⚠️ Ошибка конфигурации: не найдены доступные темы новостей\\. Настройка невозможна\\.")
         await state.clear()
         return

    topics_text = "\n".join([f"• `{topic}`" for topic in available_topics]) # Используем code для тем

    await message.answer(
        f"✅ Канал *{escape_markdown_v2(channel_title)}* \\(ID: `{channel_id}`\\) подтвержден\\.\n\n"
        "*Шаг 2/3:* Выберите **темы новостей**, которые вы хотите публиковать\\. "
        "Отправьте названия тем через запятую\\.\n\n"
        f"*Доступные темы:*\n{topics_text}",
        reply_markup=cancel_keyboard
    )
    await state.set_state(NewsSetupStates.waiting_topics)


# --- Шаг 2: Получение тем новостей ---
@router.message(StateFilter(NewsSetupStates.waiting_topics), F.text)
async def process_topics_input(message: Message, state: FSMContext):
    """Обрабатывает ввод тем новостей."""
    user_id = message.from_user.id if message.from_user else 0
    user_input_topics = [t.strip().lower() for t in message.text.split(',') if t.strip()]
    valid_topics: Set[str] = set()
    invalid_topics: List[str] = []

    available_topics_map = settings.rss_mapping # Карта тем из конфига

    for topic in user_input_topics:
        if topic in available_topics_map:
            valid_topics.add(topic)
        else:
            invalid_topics.append(topic)

    if invalid_topics:
        escaped_invalid = ", ".join(f"`{escape_markdown_v2(t)}`" for t in invalid_topics)
        escaped_available = ", ".join(f"`{escape_markdown_v2(t)}`" for t in available_topics_map.keys())
        await message.reply(
            f"❌ Обнаружены неизвестные темы: {escaped_invalid}\\.\n"
            f"Пожалуйста, выберите только из доступных: {escaped_available}",
            reply_markup=cancel_keyboard
        )
        return # Оставляем пользователя на том же шаге

    if not valid_topics:
        await message.reply("❌ Вы не выбрали ни одной доступной темы\\. Пожалуйста, укажите хотя бы одну\\.", reply_markup=cancel_keyboard)
        return

    logger.info(f"User {user_id} selected valid topics: {valid_topics}")
    await state.update_data(selected_topics=list(valid_topics)) # Сохраняем как список

    # Кнопка для ежечасного постинга
    hourly_button = InlineKeyboardButton(text="⏰ Публиковать каждый час", callback_data="news_schedule:hourly")
    schedule_keyboard = InlineKeyboardMarkup(inline_keyboard=[[hourly_button], [cancel_button]])

    await message.answer(
        "✅ Темы выбраны\\.\n\n"
        "*Шаг 3/3:* Теперь укажите **время для публикаций** новостей\\. "
        "Отправьте время в формате `ЧЧ:ММ` через запятую \\(например, `09:00, 15:30, 21:00`\\)\\. Время указывайте в UTC\\.\n\n"
        "Или нажмите кнопку ниже для публикации **каждый час**\\.",
        reply_markup=schedule_keyboard
    )
    await state.set_state(NewsSetupStates.waiting_schedule)


# --- Шаг 3: Получение расписания ---
@router.message(StateFilter(NewsSetupStates.waiting_schedule), F.text)
async def process_schedule_input(message: Message, state: FSMContext):
    """Обрабатывает ввод времени для расписания."""
    user_id = message.from_user.id if message.from_user else 0
    time_input = message.text
    # Паттерн для валидации времени ЧЧ:ММ
    time_pattern = re.compile(r'^([01]?[0-9]|2[0-3]):([0-5][0-9])$')
    schedule_times: Set[str] = set()
    invalid_times: List[str] = []

    raw_times = [t.strip() for t in time_input.split(',') if t.strip()]

    if not raw_times:
         await message.reply("❌ Вы не указали время\\. Пожалуйста, введите время в формате `ЧЧ:ММ` через запятую\\.", reply_markup=cancel_keyboard)
         return

    for time_str in raw_times:
        if time_pattern.match(time_str):
            # Нормализуем формат к HH:MM
            hour, minute = map(int, time_str.split(':'))
            normalized_time = f"{hour:02d}:{minute:02d}"
            schedule_times.add(normalized_time)
        else:
            invalid_times.append(time_str)

    if invalid_times:
        escaped_invalid = ", ".join(f"`{escape_markdown_v2(t)}`" for t in invalid_times)
        # Клавиатура с кнопкой "Каждый час"
        hourly_button = InlineKeyboardButton(text="⏰ Публиковать каждый час", callback_data="news_schedule:hourly")
        schedule_keyboard = InlineKeyboardMarkup(inline_keyboard=[[hourly_button], [cancel_button]])
        await message.reply(
            f"❌ Неверный формат времени: {escaped_invalid}\\.\n"
            "Пожалуйста, используйте формат `ЧЧ:ММ` \\(например, `08:00`, `19:35`\\) через запятую\\.",
            reply_markup=schedule_keyboard
        )
        return

    if not schedule_times:
        await message.reply("❌ Вы не указали корректное время для расписания\\.", reply_markup=cancel_keyboard)
        return

    # Сохраняем подписку
    await _save_subscription_and_finish(state, list(sorted(schedule_times)), message)

@router.callback_query(F.data == "news_schedule:hourly", StateFilter(NewsSetupStates.waiting_schedule))
async def process_schedule_hourly_button(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает нажатие кнопки 'Публиковать каждый час'."""
    user_id = callback.from_user.id
    logger.info(f"User {user_id} chose hourly schedule.")
    hourly_schedule = [f"{h:02d}:00" for h in range(24)]
    await callback.answer("Выбрана ежечасная публикация.")
    # Сохраняем подписку
    await _save_subscription_and_finish(state, hourly_schedule, callback.message)
    # Удаляем кнопки из сообщения, где нажали
    try:
         await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as e:
         logger.warning(f"Could not edit reply markup after hourly schedule selection: {e}")


async def _save_subscription_and_finish(state: FSMContext, schedule: List[str], message_or_callback_message: Message):
    """Вспомогательная функция для сохранения данных подписки и завершения FSM."""
    user_id = message_or_callback_message.from_user.id if message_or_callback_message.from_user else 0
    if database is None:
        await message_or_callback_message.answer("❌ База данных недоступна. Настройки не сохранены.")
        await state.clear()
        return
    try:
        data = await state.get_data()
        channel_id = data.get('channel_id')
        channel_title = data.get('channel_title', 'N/A')
        selected_topics = data.get('selected_topics')

        if not channel_id or not selected_topics:
            logger.error(f"Missing channel_id or topics in FSM state for user {user_id}.")
            await message_or_callback_message.answer("❌ Ошибка: данные настройки повреждены\\. Пожалуйста, начните заново командой /news_setup")
            await state.clear()
            return

        # Сохраняем в БД
        success = await database.add_or_update_subscription(
            channel_id=channel_id,
            topics=selected_topics,
            schedule=schedule
        )

        if success:
            topics_str = ", ".join(f"`{t}`" for t in selected_topics)
            schedule_str = "Каждый час \\(UTC\\)" if len(schedule) == 24 else ", ".join(f"`{t}`" for t in schedule) + " \\(UTC\\)"
            response_text = (
                f"✅ *Настройка автоновостей завершена для канала {escape_markdown_v2(channel_title)} \\(ID: `{channel_id}`\\)!*\n\n"
                f"• *Выбранные темы:* {topics_str}\n"
                f"• *Расписание публикаций:* {schedule_str}"
            )
            logger.info(f"News subscription saved channel={channel_id}, user={user_id}. Topics: {selected_topics}, Schedule: {schedule_str}")
            await message_or_callback_message.answer(response_text)
        else:
            logger.error(f"Failed to save subscription to DB channel={channel_id}, user={user_id}.")
            await message_or_callback_message.answer("❌ Не удалось сохранить настройки подписки в базе данных\\. Попробуйте позже\\.")

    except Exception as e:
        logger.error(f"Error saving subscription user={user_id}: {e}", exc_info=True)
        await message_or_callback_message.answer("❌ Произошла непредвиденная ошибка при сохранении настроек\\.")
    finally:
        await state.clear() # Завершаем состояние FSM