from aiogram.fsm.state import State, StatesGroup


class AddSourceState(StatesGroup):
    waiting_link = State()
    waiting_region = State()
    confirm = State()
    waiting_file = State()
    confirm_file = State()


class SearchState(StatesGroup):
    waiting_query = State()


class EditRegionState(StatesGroup):
    waiting_region = State()


class AdminState(StatesGroup):
    waiting_interval = State()
    waiting_proxy_input = State()
    waiting_vk_accounts = State()
    waiting_tg_accounts = State()
    waiting_user_id = State()
