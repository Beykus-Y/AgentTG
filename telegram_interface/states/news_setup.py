# telegram_interface/states/news_setup.py

from aiogram.fsm.state import State, StatesGroup

class NewsSetupStates(StatesGroup):
    """
    Состояния для конечного автомата настройки подписки на новости.
    """
    # Ожидание ввода канала (username, ID или пересланное сообщение)
    waiting_channel = State()
    # Ожидание ввода тем новостей (через запятую)
    waiting_topics = State()
    # Ожидание ввода расписания (через запятую или кнопка "каждый час")
    waiting_schedule = State()

# Можно добавить другие группы состояний для других FSM, если потребуется
# class AnotherFSM(StatesGroup):
#     state_1 = State()
#     state_2 = State()