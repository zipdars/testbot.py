import logging
from math import ceil
import datetime
import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram import ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes
import psycopg2
import asyncio
import asyncpg
from psycopg2 import sql
from psycopg2.extras import DictCursor
from telegram.constants import ParseMode
import re
from datetime import date, datetime

# Включение логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Состояния разговора
INIT, WAITING_INPUT, ADDITIONAL_CHANNELS, CHANNELS_INPUT, CONFIRMATION, FINISH_CONTEST, WAITING_FOR_TRACKED_DATE, WAITING_FOR_PENDING_DOP_CHANNELS, START, AWAITING_DATE, END, WAITING_FOR_PENDING_DATE = range(12)

# Параметры подключения к базе данных
DB_PARAMS = {
    "database": "Telegram",
    "user": "postgres",
    "password": "1",
    "host": "localhost",
    "port": "5432"
}

# Список администраторов
ADMIN_USERS = [1]  # Замените на реальные ID администраторов

async def error_handler(update, context):
    """Log the error and send a telegram message to notify the developer."""
    # Log the error before we do anything else, so we can see it even if something breaks.
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    # traceback.format_exception returns the usual python message about an exception, but as a
    # list of strings rather than a single string, so we have to join them together.
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = ''.join(tb_list)

    # Build the message with the traceback
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        f'An exception was raised while handling an update\n'
        f'<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}'
        '</pre>\n\n'
        f'<pre>{html.escape(tb_string)}</pre>'
    )

    # Finally, send the message
    await context.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.HTML)

# Функция для проверки, является ли текст ссылкой
def is_url(text):
    url_pattern = re.compile(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+')
    return bool(url_pattern.match(text))


# Функция для проверки, является ли текст датой
def is_date(text):
    date_pattern = re.compile(r'^\d{2}\.\d{2}$')
    return bool(date_pattern.match(text))

async def check_admin_status(user_id, context):
    if 'is_admin' not in context.user_data:
        context.user_data['is_admin'] = user_id in ADMIN_USERS
    return context.user_data['is_admin']

# Функция для получения соединения с базой данных через asyncpg
async def get_asyncpg_connection():
    return await asyncpg.connect(
        database=DB_PARAMS["database"],
        user=DB_PARAMS["user"],
        password=DB_PARAMS["password"],
        host=DB_PARAMS["host"],
        port=DB_PARAMS["port"]
    )

# Функция для подключения к базе данных
def get_db_connection():
    return psycopg2.connect(**DB_PARAMS)


# Функция для выполнения SQL-запросов
async def execute_query(query, params=None):
    conn = await asyncpg.connect(**DB_PARAMS)
    try:
        if params:
            result = await conn.fetch(query, *params)
        else:
            result = await conn.fetch(query)
        return result
    except Exception as e:
        logging.error(f"Error executing query: {query}")
        logging.error(f"With params: {params}")
        logging.error(f"Error details: {e}", exc_info=True)
        raise
    finally:
        await conn.close()

async def add_contest_to_db(link, date, dop_channels, status='Активен'):
    query = """
        INSERT INTO contests.contests (link, date, dop_channels, status)
        VALUES ($1, $2, $3, $4)
    """
    await execute_query(query, (link, date, dop_channels, status))


# Функция для получения основной клавиатуры
def get_main_keyboard(is_admin=False):
    keyboard = [
        [KeyboardButton("Показать все конкурсы")],
    ]
    if is_admin:
        keyboard.append([KeyboardButton("Добавить в БД")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def add_admin_buttons(keyboard, is_admin):
    if is_admin:
        keyboard.append([InlineKeyboardButton("📥 Показать ожидающие конкурсы", callback_data='show_pending_contests')])
    return keyboard

async def handle_date_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_text = update.message.text

    if is_date(date_text):
        context.user_data['date'] = process_date(date_text)
        return await ask_for_missing_info(update, context)
    else:
        await update.message.reply_text("Неверный формат даты. Пожалуйста, введите дату в формате ДД.ММ.")
        return WAITING_INPUT

# Новая функцию для обработки запроса на завершение конкурса
async def finish_contest_request(update, context):
    if update.effective_user.id not in ADMIN_USERS:
        await update.message.reply_text("У вас недостаточно прав для завершения конкурсов.")
        return WAITING_INPUT

    keyboard = [
        [KeyboardButton("Отмена")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "Пожалуйста, отправьте ссылку на конкурс, который вы хотите завершить, или нажмите 'Отмена' для возврата в главное меню.",
        reply_markup=reply_markup
    )
    return FINISH_CONTEST


# Функция для обновления статуса конкурса
async def update_contest_status(update, context):
    if update.effective_user.id not in ADMIN_USERS:
        await update.message.reply_text("У вас недостаточно прав для завершения конкурсов.")
        return WAITING_INPUT

    text = update.message.text

    if text == "Отмена":
        await update.message.reply_text(
            "Операция отменена. Что бы вы хотели сделать дальше?",
            reply_markup=get_main_keyboard()
        )
        return WAITING_INPUT

    if not is_url(text):
        await update.message.reply_text(
            "Это не похоже на корректную ссылку. Пожалуйста, попробуйте еще раз или нажмите 'Отмена'."
        )
        return FINISH_CONTEST

    query = """
        UPDATE contests.contests
        SET status = 'Завершен'
        WHERE link = \$1
        RETURNING *
    """

    result = await execute_query(query, (text,))

    if result:
        await update.message.reply_text(
            f"Статус конкурса успешно обновлен на 'Завершен'.",
            reply_markup=get_main_keyboard()
        )
    else:
        await update.message.reply_text(
            "Конкурс с такой ссылкой не найден в базе данных.",
            reply_markup=get_main_keyboard()
        )

    return WAITING_INPUT

async def handle_menu_input(update, context):
    text = update.message.text
    if text == "Добавить конкурс":
        if update.effective_user.id in ADMIN_USERS:
            context.user_data.clear()
            await update.message.reply_text("Отправьте ссылку на конкурс или дату в формате ДД.ММ")
            return WAITING_INPUT
        else:
            await update.message.reply_text("У вас недостаточно прав для добавления конкурсов.")
            return WAITING_INPUT
    elif text == "Показать конкурсы":
        await show_contests(update, context)
        return WAITING_INPUT
    elif text == "Завершить конкурс":
        return await finish_contest_request(update, context)
    elif text == "Отслеживание":
        return await show_tracked_contests(update, context)
    else:
        await update.message.reply_text(
            "Пожалуйста, выберите действие из меню.",
            reply_markup=get_main_keyboard()
        )
        return WAITING_INPUT

# Обработчик команды /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_admin = user_id in ADMIN_USERS
    context.user_data['is_admin'] = is_admin

    await show_contests(update, context)
    return WAITING_INPUT


# Функция для проверки наличия ссылки в базе данных
async def link_exists(link):
    query = "SELECT EXISTS(SELECT 1 FROM contests.contests WHERE link = \$1) AS exists"
    try:
        result = await execute_query(query, (link,))
        if result and len(result) > 0:
            return result[0]['exists']
        return False
    except Exception as e:
        logging.error(f"Error in link_exists: {e}", exc_info=True)
        return False

# Основной обработчик ввода пользователя

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_success_message(update, context)
    await delete_cancel_message(update, context)

    text = update.message.text

    # Удаляем приветственное сообщение, если оно есть
    if 'welcome_message_id' in context.user_data:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=context.user_data['welcome_message_id']
            )
        except Exception as e:
            print(f"Не удалось удалить приветственное сообщение: {e}")
        del context.user_data['welcome_message_id']

    # Обработка ввода для различных состояний
    if context.user_data.get('waiting_for_pending_date'):
        return await handle_pending_date_input(update, context)
    elif context.user_data.get('waiting_for_pending_dop_channels'):
        return await handle_pending_dop_channels_input(update, context)
    elif context.user_data.get('waiting_for_tracked_date'):
        return await handle_tracked_date_input(update, context)
    elif context.user_data.get('waiting_for_date'):
        return await handle_date_input(update, context)

    # Удаляем сообщение пользователя только если это не обработка дополнительных каналов
    if not context.user_data.get('waiting_for_pending_dop_channels'):
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)
        except Exception as e:
            print(f"Не удалось удалить сообщение пользователя: {e}")

    # Обработка URL
    if is_url(text):
        return await handle_url_input(update, context)

    # Обработка команд
    elif text == "Показать все конкурсы":
        await show_contests(update, context)
        return WAITING_INPUT
    elif text == "Обновить":
        await refresh_contests(update, context)
        return WAITING_INPUT
    elif text == "Завершить конкурс":
        return await finish_contest_request(update, context)

    # Если ввод не соответствует ни одному из ожидаемых форматов
    else:
        await update_or_send_message(
            update, context,
            "Я не понимаю. Пожалуйста, отправьте ссылку на конкурс, дату в формате ДД.ММ, или выберите действие из меню.",
            reply_markup=None
        )
        return WAITING_INPUT

# Удаляет сообщение пользователя после ввода даты, каналов и т.д.
async def update_or_send_message(update, context, text, reply_markup=None):
    if 'message_id' in context.user_data:
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=context.user_data['message_id'],
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
        except Exception as e:
            print(f"Не удалось отредактировать сообщение: {e}")
            message = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            context.user_data['message_id'] = message.message_id
    else:
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
        context.user_data['message_id'] = message.message_id

async def handle_url_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_cancel_message(update, context)

    url = update.message.text
    context.user_data['link'] = url

    logging.info(f"Handling URL input: {url}")

    keyboard = []
    try:
        exists = await link_exists(url)
        logging.info(f"Link exists: {exists}")
        if exists:
            if context.user_data.get('is_admin', False):
                keyboard.append([InlineKeyboardButton("Завершить конкурс", callback_data='finish_contest')])
    except Exception as e:
        logging.error(f"Error checking if link exists: {e}", exc_info=True)

    keyboard.append([InlineKeyboardButton("Добавить конкурс", callback_data='add_contest')])
    keyboard.append([InlineKeyboardButton("Отслеживание", callback_data='track_contest')])
    if context.user_data.get('is_admin', False):
        keyboard.append([InlineKeyboardButton("Добавить позже", callback_data='add_to_pending')])
    keyboard.append([InlineKeyboardButton("Отмена", callback_data='cancel')])

    reply_markup = InlineKeyboardMarkup(keyboard)

    message = await update.message.reply_text(
        f"Ссылка: {url}\n\nЧто вы хотите сделать?",
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )
    context.user_data['message_id'] = message.message_id

    return WAITING_INPUT

async def handle_url_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_success_message(update, context)
    query = update.callback_query
    await query.answer()

    try:
        if query.data == 'add_to_pending':
            if update.effective_user.id in ADMIN_USERS:
                context.user_data['waiting_for_pending_date'] = True
                await query.edit_message_text(
                    f"Пожалуйста, введите дату конкурса в формате ДД.ММ для ссылки:\n{context.user_data['link']}",
                    disable_web_page_preview=True
                )
            else:
                await query.edit_message_text("У вас недостаточно прав для добавления в БД.")
            return WAITING_INPUT

        elif query.data == 'finish_contest':
            if update.effective_user.id in ADMIN_USERS:
                await finish_contest(update, context)
            else:
                await query.edit_message_text("У вас недостаточно прав для завершения конкурсов.")
            return WAITING_INPUT

        elif query.data == 'add_contest':
            if update.effective_user.id in ADMIN_USERS:
                context.user_data['waiting_for_date'] = True
                await query.edit_message_text(
                    f"Пожалуйста, введите дату конкурса в формате ДД.ММ для ссылки:\n{context.user_data['link']}",
                    disable_web_page_preview=True
                )
            else:
                await query.edit_message_text("У вас недостаточно прав для добавления конкурсов.")
            return WAITING_INPUT

        elif query.data == 'track_contest':
            context.user_data['tracked_link'] = context.user_data['link']
            context.user_data['waiting_for_tracked_date'] = True
            await query.edit_message_text(
                f"Пожалуйста, введите дату конкурса в формате ДД.ММ для ссылки:\n{context.user_data['link']}",
                disable_web_page_preview=True
            )
            return WAITING_INPUT

        elif query.data == 'cancel':
            await show_contests(update, context)
            return WAITING_INPUT

        else:
            logging.warning(f"Неизвестное действие: {query.data}")
            await query.edit_message_text("Произошла ошибка. Пожалуйста, попробуйте снова.")
            return WAITING_INPUT

    except Exception as e:
        logging.error(f"Ошибка в handle_url_action: {e}", exc_info=True)
        await query.edit_message_text("Произошла ошибка. Пожалуйста, попробуйте снова.")
        return WAITING_INPUT

# Новая функция для отображения отслеживаемых конкурсов
async def show_tracked_contests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    tracked_contests = await get_tracked_contests(user_id)

    if not tracked_contests:
        message = "У вас нет отслеживаемых конкурсов."
    else:
        message = "Отслеживаемые конкурсы:\n\n"
        for contest in tracked_contests:
            link = contest.get('link', 'Ссылка отсутствует')
            date = contest.get('date', 'Дата не указана')
            message += f"<a href='{link}'>{link}</a>\nДата: {date}\n\n"

    keyboard = [
        [InlineKeyboardButton("🏆 Активные", callback_data='show_active'),
         InlineKeyboardButton("🏁 Завершенные", callback_data='show_completed'),
         InlineKeyboardButton("👀 Отслеживание", callback_data='show_tracked')],
        [InlineKeyboardButton("🔄 Обновить", callback_data='refresh_tracked')]
    ]

    is_admin = user_id in ADMIN_USERS
    keyboard = add_admin_buttons(keyboard, is_admin)

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text=message,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )

    return WAITING_INPUT


# Функция для добавления конкурса в отслеживаемые
async def add_tracked_contest(update, context):
    user_id = update.effective_user.id
    link = context.user_data.get('tracked_link')

    message_text = "Пожалуйста, введите дату конкурса в формате ДД.ММ:"

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text=message_text,
            disable_web_page_preview=True
        )
    else:
        await update.message.reply_text(
            text=message_text,
            disable_web_page_preview=True
        )

    return WAITING_FOR_TRACKED_DATE


async def handle_tracked_date_input(update, context):
    date_text = update.message.text
    if is_date(date_text):
        date = process_date(date_text)
        user_id = update.effective_user.id
        link = context.user_data.get('tracked_link')

        if not link:
            await update.message.reply_text("Ошибка: ссылка на конкурс не найдена. Пожалуйста, начните процесс добавления заново.")
            return WAITING_INPUT

        # Добавляем конкурс в базу данных
        add_tracked_contest_to_db(user_id, link, date)

        # Проверяем наличие ID сообщения для обновления
        if 'tracking_message_id' in context.user_data:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=context.user_data['tracking_message_id'],
                    text=f"Конкурс {link} добавлен в отслеживаемые с датой {date}.",
                    disable_web_page_preview=True
                )
            except Exception as e:
                logger.error(f"Ошибка при обновлении сообщения: {e}")
                # Если не удалось обновить сообщение, отправляем новое
                await update.message.reply_text(f"Конкурс {link} добавлен в отслеживаемые с датой {date}.")
        else:
            # Если ID сообщения не найден, отправляем новое сообщение
            await update.message.reply_text(f"Конкурс {link} добавлен в отслеживаемые с датой {date}.",
            disable_web_page_preview=True)

        # Очищаем данные пользователя
        context.user_data.clear()

        # Показываем список конкурсов
        await show_contests(update, context)

        return WAITING_INPUT
    else:
        await update.message.reply_text("Неверный формат даты. Пожалуйста, введите дату в формате ДД.ММ.")
        return WAITING_FOR_TRACKED_DATE

# Функция для добавления конкурса в базу данных
async def add_tracked_contest_to_db(user_id, link, date):
    query = """
        INSERT INTO contests.tracked_contests (user_id, link, date)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id, link) DO UPDATE
        SET date = EXCLUDED.date
    """
    await execute_query(query, (user_id, link, date))

# Функция для получения отслеживаемых конкурсов пользователя
async def get_tracked_contests(user_id):
    query = """
        SELECT link, date FROM contests.tracked_contests
        WHERE user_id = $1
    """
    return await execute_query(query, (user_id,))


# Функция для удаления конкурса из отслеживаемых
async def remove_tracked_contest(user_id, link):
    query = """
        DELETE FROM contests.tracked_contests
        WHERE user_id = $1 AND link = $2
    """
    await execute_query(query, (user_id, link))


# Функция для обработки запроса на удаление отслеживаемого конкурса
async def delete_tracked_contest(update, context):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    tracked_contests = await get_tracked_contests(user_id)  # Добавлен await

    if not tracked_contests:
        await query.edit_message_text("У вас нет отслеживаемых конкурсов.")
        return WAITING_INPUT

    keyboard = []
    for i, contest in enumerate(tracked_contests, 1):
        keyboard.append([InlineKeyboardButton(f"{i}. {contest['link']} - {contest['date']}", callback_data=f'delete_{i}')])

    keyboard.append([InlineKeyboardButton("Отмена", callback_data='cancel_delete')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text("Выберите конкурс для удаления:", reply_markup=reply_markup)
    return WAITING_INPUT

# Функция для обработки удаления выбранного отслеживаемого конкурса
async def handle_delete_tracked(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == 'cancel_delete':
        await show_tracked_contests(update, context)
        return WAITING_INPUT

    index = int(query.data.split('_')[1]) - 1
    user_id = update.effective_user.id
    tracked_contests = await get_tracked_contests(user_id)  # Добавлен await

    if 0 <= index < len(tracked_contests):
        link_to_delete = tracked_contests[index]['link']
        await remove_tracked_contest(user_id, link_to_delete)  # Добавлен await

        # Обновляем список отслеживаемых конкурсов после удаления
        updated_tracked_contests = await get_tracked_contests(user_id)  # Добавлен await

        if not updated_tracked_contests:
            message = "У вас больше нет отслеживаемых конкурсов."
        else:
            message = "Отслеживаемые конкурсы:\n\n"
            for i, contest in enumerate(updated_tracked_contests, 1):
                message += f"{i}. {contest['link']} - {contest['date']}\n"

        keyboard = [
            [InlineKeyboardButton("🏆 Активные", callback_data='show_active'),
             InlineKeyboardButton("🏁 Завершенные", callback_data='show_completed'),
             InlineKeyboardButton("👀 Отслеживание", callback_data='show_tracked')],
            [InlineKeyboardButton("➕ Добавить", callback_data='add_tracked'),
             InlineKeyboardButton("➖ Удалить", callback_data='delete_tracked')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            text=message,
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
    else:
        await query.edit_message_text("Произошла ошибка. Пожалуйста, попробуйте снова.")

    return WAITING_INPUT

# Функция для обработки добавления нового отслеживаемого конкурса
async def add_new_tracked_contest(update, context):
    query = update.callback_query
    await query.answer()

    message = await query.edit_message_text(
        "Добавление нового отслеживаемого конкурса:\n\n"
        "Пожалуйста, отправьте ссылку на конкурс.",
        disable_web_page_preview=True
    )
    context.user_data['tracking_message_id'] = message.message_id
    context.user_data['waiting_for_tracked_link'] = True
    return WAITING_INPUT


# Функция для обработки ввода ссылки для отслеживания
async def handle_tracked_link_input(update, context):
    if 'waiting_for_tracked_link' in context.user_data:
        del context.user_data['waiting_for_tracked_link']
        link = update.message.text

        if is_url(link):
            context.user_data['tracked_link'] = link
            await update.message.delete()
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=context.user_data['tracking_message_id'],
                text=f"Добавление нового отслеживаемого конкурса:\n\n"
                     f"Ссылка: {link}\n\n"
                     f"Пожалуйста, введите дату конкурса в формате ДД.ММ:",
                disable_web_page_preview=True
            )
            return WAITING_FOR_TRACKED_DATE
        else:
            await update.message.reply_text("Это не похоже на корректную ссылку. Пожалуйста, попробуйте снова.")
            return WAITING_INPUT

    return await handle_input(update, context)

async def finish_contest(update, context):
    await delete_success_message(update, context)
    query = update.callback_query
    await query.answer()

    link = context.user_data.get('link')
    if not link:
        await query.edit_message_text("Ошибка: ссылка на конкурс не найдена.")
        return WAITING_INPUT

    update_query = """
        UPDATE contests.contests
        SET status = 'Завершен'
        WHERE link = $1
        RETURNING *
    """
    result = await execute_query(update_query, (link,))  # Corrected placeholder usage

    if result:
        message = f"Конкурс по ссылке {link} успешно завершен. Что бы вы хотели сделать дальше?"
    else:
        message = f"Не удалось завершить конкурс по ссылке {link}. Возможно, он уже завершен или не существует. Что бы вы хотели сделать дальше?"

    # Создаем инлайн клавиатуру
    keyboard = [
        [InlineKeyboardButton("Показать все конкурсы", callback_data='show_all_contests')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text=message,
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )

    context.user_data.clear()
    return WAITING_INPUT

# Функция для обработки введенной даты
def process_date(date_text):
    current_year = datetime.now().year
    date = datetime.strptime(f"{date_text}.{current_year}", "%d.%m.%Y")
    return date.strftime("%d.%m.%Y")


# Функция для запроса недостающей информации
async def ask_for_missing_info(update, context):
    if 'waiting_for_date' in context.user_data:
        new_text = f"Добавление нового конкурса:\n\nСсылка: {context.user_data['link']}\n\nВведите дату конкурса в формате ДД.ММ"
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=context.user_data['message_id'],
                text=new_text,
                disable_web_page_preview=True
            )
        except telegram.error.BadRequest as e:
            if "Message is not modified" not in str(e):
                raise e
        del context.user_data['waiting_for_date']
    elif 'date' in context.user_data:
        keyboard = [
            [InlineKeyboardButton("Да", callback_data='yes'),
             InlineKeyboardButton("Нет", callback_data='no')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        new_text = f"Добавление нового конкурса:\n\nСсылка: {context.user_data['link']}\nДата: {context.user_data['date']}\n\nНужно подписаться на доп. каналы?"
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=context.user_data['message_id'],
                text=new_text,
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
        except telegram.error.BadRequest as e:
            if "Message is not modified" not in str(e):
                raise e
        return ADDITIONAL_CHANNELS
    return WAITING_INPUT


# Обработчик выбора дополнительных каналов
async def additional_channels(update, context):
    await delete_success_message(update, context)
    try:
        query = update.callback_query
        await query.answer()

        if query.data == 'refresh_contests':
            return await refresh_contests(update, context)
        elif query.data == 'yes':
            await context.bot.edit_message_text(
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
                text=f"Добавление нового конкурса:\n\nСсылка: {context.user_data['link']}\nДата: {context.user_data['date']}\n\nПожалуйста, напишите ссылки на доп. каналы через запятую."
            )
            context.user_data['waiting_for_dop_channels'] = True
            return CHANNELS_INPUT
        elif query.data == 'no':
            context.user_data['dop_channels'] = 'false'
            return await show_confirmation(update, context)
        else:
            logger.error(f"Unexpected callback data: {query.data}")
            await query.edit_message_text("Произошла ошибка. Пожалуйста, попробуйте снова.")
            return WAITING_INPUT
    except Exception as e:
        logger.error(f"Error in additional_channels: {e}")
        await query.edit_message_text("Произошла ошибка. Пожалуйста, попробуйте снова.")
        return WAITING_INPUT


# Обработчик ввода ссылок на дополнительные каналы
async def channels_input(update, context):
    await delete_success_message(update, context)
    text = update.message.text

    # Удаляем сообщение пользователя
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)

    if text == "Показать все конкурсы":
        await show_contests(update, context)
        return WAITING_INPUT
    elif text == "Добавить конкурс":
        context.user_data.clear()
        message = await update.message.reply_text("Добавление нового конкурса:\n\nОтправьте ссылку на конкурс.")
        context.user_data['message_id'] = message.message_id
        return WAITING_INPUT

    if context.user_data.get('waiting_for_dop_channels'):
        context.user_data['dop_channels'] = text
        del context.user_data['waiting_for_dop_channels']

    return await show_confirmation(update, context)


# Функция для отображения подтверждения
async def show_confirmation(update, context):
    data = context.user_data
    dop_channels_display = data.get('dop_channels', 'false')
    confirmation_text = f"Добавление нового конкурса:\n\nСсылка: {data['link']}\nДата: {data['date']}\nДоп. каналы: {dop_channels_display}\n\nДанные конкурса верны?"
    keyboard = [
        [InlineKeyboardButton("Да", callback_data='confirm'),
         InlineKeyboardButton("Нет", callback_data='cancel')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        message = await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=context.user_data['message_id'],
            text=confirmation_text,
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
    elif update.callback_query:
        message = await update.callback_query.edit_message_text(
            text=confirmation_text,
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
    else:
        logger.error("Unexpected update type in show_confirmation")
        return WAITING_INPUT

    context.user_data['confirmation_message_id'] = message.message_id
    return CONFIRMATION

# Обработчик подтверждения
async def confirmation(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == 'confirm':
        # Сохранение данных в базу
        insert_query = """
            INSERT INTO contests.contests (link, date, dop_channels, status)
            VALUES ($1, $2, $3, $4)
        """
        await execute_query(insert_query, (
            context.user_data['link'],
            context.user_data['date'],
            context.user_data.get('dop_channels', 'false'),
            'Активен'
        ))
        message = await query.edit_message_text(
            "Данные успешно сохранены в базу данных.",
            disable_web_page_preview=True
        )

        # Сохраняем ID сообщения об успешном сохранении
        context.user_data['success_message_id'] = message.message_id
    elif query.data == 'cancel':
        keyboard = [
            [InlineKeyboardButton("Показать все конкурсы", callback_data='show_all_contests')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "Операция отменена.",
            reply_markup=reply_markup
        )
    else:
        logger.error(f"Unexpected callback data: {query.data}")
        await query.edit_message_text("Произошла ошибка. Пожалуйста, попробуйте снова.")

    context.user_data.clear()
    return WAITING_INPUT


async def delete_message_after_delay(bot, chat_id, message_id, delay):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        print(f"Не удалось удалить сообщение: {e}")

async def delete_cancel_message(update, context):
    if 'cancel_message_id' in context.user_data:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=context.user_data['cancel_message_id']
            )
        except Exception as e:
            print(f"Не удалось удалить сообщение об отмене: {e}")
        del context.user_data['cancel_message_id']

async def delete_success_message(update, context):
    if 'success_message_id' in context.user_data:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=context.user_data['success_message_id']
            )
        except Exception as e:
            print(f"Не удалось удалить сообщение об успешном сохранении: {e}")
        del context.user_data['success_message_id']


# Функция для получения сообщения со списком конкурсов
async def get_contests_message(status='Активен', is_admin=False):
    today = date.today()
    if status == 'Активен':
        query = """
            SELECT c.link, c.date, c.status, COUNT(DISTINCT h.id) as participant_count
            FROM contests.contests c
            LEFT JOIN history.history h ON c.link = h.link
            WHERE c.status = $1
            GROUP BY c.link, c.date, c.status
            ORDER BY c.date::date
        """
        params = [status]
    else:
        query = """
            SELECT c.link, c.date, c.status, COUNT(DISTINCT h.id) as participant_count
            FROM contests.contests c
            LEFT JOIN history.history h ON c.link = h.link
            WHERE c.status = $1 AND DATE(c.date) = $2
            GROUP BY c.link, c.date, c.status
            ORDER BY c.date::date
        """
        params = [status, today]

    conn = await asyncpg.connect(**DB_PARAMS)
    try:
        contests = await conn.fetch(query, *params)
    finally:
        await conn.close()

    return contests if contests else []


# Функция для отображения всех конкурсов
async def show_contests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_success_message(update, context)
    await delete_cancel_message(update, context)

    is_admin = context.user_data.get('is_admin', False)
    contests = await get_contests_message('Активен', is_admin)

    if not contests:
        message = "На данный момент нет активных конкурсов."
        if update.callback_query:
            await update.callback_query.edit_message_text(message)
        elif update.message:
            await update.message.reply_text(message)
        return WAITING_INPUT

    page = 1
    contests_per_page = 5
    total_pages = ceil(len(contests) / contests_per_page)

    message = await get_paginated_contests(contests, page, contests_per_page, is_admin)

    update_time = datetime.now().strftime("%d.%m %H:%M")
    message = f"Активные конкурсы:\n\n{message}\n\nДанные обновлены: {update_time}"

    keyboard = [
        [InlineKeyboardButton("⬅️", callback_data=f'page_Активен_{page - 1}'),
         InlineKeyboardButton(f"{page}/{total_pages}", callback_data='current_page'),
         InlineKeyboardButton("➡️", callback_data=f'page_Активен_{page + 1}')],
        [InlineKeyboardButton("🏆 Активные", callback_data='show_active'),
         InlineKeyboardButton("🏁 Завершенные", callback_data='show_completed'),
         InlineKeyboardButton("👀 Отслеживание", callback_data='show_tracked')]
    ]

    if is_admin:
        keyboard.append([InlineKeyboardButton("📥 Показать ожидающие конкурсы", callback_data='show_pending_contests')])

    keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data='refresh_Активен')])

    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text=message,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup
        )
    elif update.message:
        await update.message.reply_text(
            message,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup
        )
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=message,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup
        )

    context.user_data['contests'] = contests
    context.user_data['total_pages'] = total_pages
    context.user_data['current_status'] = 'Активен'

    return WAITING_INPUT


# Новая функция для пагинации
async def get_paginated_contests(contests, page, contests_per_page, is_admin):
    start = (page - 1) * contests_per_page
    end = start + contests_per_page

    paginated_contests = contests[start:end]

    message = ""
    for contest in paginated_contests:
        message += f"Дата: {contest['date']}\n"
        message += f"Ссылка: <a href=\"{contest['link']}\">{contest['link']}</a>\n"
        if is_admin:
            message += f"Количество участников: {contest.get('participant_count', 'Н/Д')}\n"
        message += "\n"

    return message

# Новая функция для обработки нажатий на кнопки пагинации
async def handle_pagination(update, context):
    await delete_success_message(update, context)
    query = update.callback_query
    await query.answer()

    if query.data == 'current_page':
        return

    page = int(query.data.split('_')[1])
    await update_contests_page(update, context, page)


async def update_contests_page(update, context, page, status='Активен'):
    is_admin = await check_admin_status(update.effective_user.id, context)

    contests = await get_contests_message(status, is_admin)

    if not contests:
        message = "На данный момент нет активных конкурсов." if status == 'Активен' else "На сегодня нет завершенных конкурсов."
        keyboard = [
            [InlineKeyboardButton("🏆 Активные", callback_data='show_active'),
             InlineKeyboardButton("🏁 Завершенные", callback_data='show_completed'),
             InlineKeyboardButton("👀 Отслеживание", callback_data='show_tracked')],
            [InlineKeyboardButton("🔄 Обновить", callback_data=f'refresh_{status}')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.callback_query.edit_message_text(text=message, reply_markup=reply_markup)
        return WAITING_INPUT

    contests_per_page = 5
    total_pages = ceil(len(contests) / contests_per_page)

    page = max(1, min(page, total_pages))

    message = await get_paginated_contests(contests, page, contests_per_page, is_admin)

    update_time = datetime.now().strftime("%d.%m %H:%M")
    status_text = "Активные конкурсы" if status == 'Активен' else "Завершенные конкурсы за сегодня"
    message = f"{status_text}:\n\n{message}\n\nДанные обновлены: {update_time}"

    keyboard = [
        [InlineKeyboardButton("⬅️", callback_data=f'page_{status}_{page - 1}'),
         InlineKeyboardButton(f"{page}/{total_pages}", callback_data='current_page'),
         InlineKeyboardButton("➡️", callback_data=f'page_{status}_{page + 1}')],
        [InlineKeyboardButton("🏆 Активные", callback_data='show_active'),
         InlineKeyboardButton("🏁 Завершенные", callback_data='show_completed'),
         InlineKeyboardButton("👀 Отслеживание", callback_data='show_tracked')],
        [InlineKeyboardButton("🔄 Обновить", callback_data=f'refresh_{status}')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await update.callback_query.edit_message_text(
            text=message,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup
        )
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e

    context.user_data['contests'] = contests
    context.user_data['total_pages'] = total_pages
    context.user_data['current_status'] = status

async def handle_pagination(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == 'current_page':
        return

    status, page = query.data.split('_')[1:]
    page = int(page)

    await update_contests_page(update, context, page, status)


# Новая функция для обновления списка конкурсов
async def refresh_contests(update, context):
    await delete_success_message(update, context)
    query = update.callback_query
    await query.answer()

    status = query.data.split('_')[1]
    current_page = 1
    for button in query.message.reply_markup.inline_keyboard[0]:
        if button.callback_data == 'current_page':
            current_page = int(button.text.split('/')[0])
            break

    is_admin = update.effective_user.id in ADMIN_USERS
    context.user_data['is_admin'] = is_admin  # Обновляем статус админа

    await update_contests_page(update, context, current_page, status)

async def switch_contest_type(update, context):
    await delete_success_message(update, context)
    query = update.callback_query
    await query.answer()

    if query.data == 'show_active':
        status = 'Активен'
    elif query.data == 'show_completed':
        status = 'Завершен'
    elif query.data == 'show_tracked':
        return await show_tracked_contests(update, context)
    else:
        status = 'Активен'  # По умолчанию

    # Удаляем сообщение о добавлении конкурса в отслеживаемые
    if 'tracked_added_message_id' in context.user_data:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=context.user_data['tracked_added_message_id']
            )
        except Exception as e:
            print(f"Не удалось удалить сообщение о добавлении в отслеживаемые: {e}")
        del context.user_data['tracked_added_message_id']

    is_admin = update.effective_user.id in ADMIN_USERS
    context.user_data['is_admin'] = is_admin  # Обновляем статус админа

    await update_contests_page(update, context, 1, status)


# Функция для добавления ссылки в таблицу pending_contests
async def add_to_pending_contests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    link = context.user_data['link']

    await update_or_send_message(
        update, context,
        f"Добавление конкурса в список ожидающих:\n\nСсылка: {link}\n\nПожалуйста, введите дату конкурса в формате ДД.ММ"
    )
    context.user_data['waiting_for_pending_date'] = True
    return WAITING_FOR_PENDING_DATE


async def handle_pending_date_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_text = update.message.text

    if is_date(date_text):
        context.user_data['pending_contest'] = {
            'link': context.user_data['link'],
            'date': process_date(date_text)
        }
        await update.message.delete()
        return await ask_for_pending_dop_channels(update, context)
    else:
        await update_or_send_message(
            update, context,
            f"Добавление конкурса в список ожидающих:\n\nСсылка: {context.user_data['link']}\n\nНеверный формат даты. Пожалуйста, введите дату в формате ДД.ММ."
        )
        await update.message.delete()
        return WAITING_FOR_PENDING_DATE


async def ask_for_pending_dop_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Да", callback_data='pending_yes'),
         InlineKeyboardButton("Нет", callback_data='pending_no')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    message_text = (f"Добавление конкурса в список ожидающих:\n\n"
                    f"Ссылка: {context.user_data['pending_contest']['link']}\n"
                    f"Дата: {context.user_data['pending_contest']['date']}\n\n"
                    f"Нужно подписаться на доп. каналы?")

    await update_or_send_message(update, context, message_text, reply_markup)
    return WAITING_INPUT

async def handle_pending_dop_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == 'pending_yes':
        context.user_data['waiting_for_pending_dop_channels'] = True
        await update_or_send_message(
            update, context,
            f"Добавление конкурса в список ожидающих:\n\n"
            f"Ссылка: {context.user_data['pending_contest']['link']}\n"
            f"Дата: {context.user_data['pending_contest']['date']}\n\n"
            f"Пожалуйста, напишите ссылки на доп. каналы через запятую."
        )
        return WAITING_FOR_PENDING_DOP_CHANNELS
    elif query.data == 'pending_no':
        context.user_data['pending_contest']['dop_channels'] = False
        return await save_pending_contest(update, context)


async def handle_pending_dop_channels_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dop_channels = update.message.text.strip()

    if dop_channels:
        context.user_data['pending_contest']['dop_channels'] = dop_channels
    else:
        context.user_data['pending_contest']['dop_channels'] = False

    del context.user_data['waiting_for_pending_dop_channels']

    await update.message.delete()
    return await save_pending_contest(update, context)



async def save_pending_contest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = context.user_data
    pending_contest = user_data.get('pending_contest', {})

    logging.info(f"Attempting to save pending contest: {pending_contest}")

    if 'link' in pending_contest and 'date' in pending_contest and not user_data.get('contest_saved', False):
        query = "INSERT INTO contests.pending_contests (link, date, dop_channels) VALUES ($1, $2, $3)"

        dop_channels = pending_contest.get('dop_channels', 'false')
        if isinstance(dop_channels, str):
            dop_channels = dop_channels.strip()
            if dop_channels.lower() == 'false':
                dop_channels = False

        params = [
            pending_contest['link'],
            pending_contest['date'],
            dop_channels
        ]

        logging.info(f"SQL Query: {query}")
        logging.info(f"Params: {params}")

        try:
            await execute_query(query, params)

            success_message = (
                "Конкурс добавлен в список ожидающих:\n\n"
                f"Ссылка: {pending_contest['link']}\n"
                f"Дата: {pending_contest['date']}\n"
                f"Доп. каналы: {dop_channels if dop_channels else 'Нет'}\n\n"
                "Спасибо за участие!"
            )

            await update_or_send_message(update, context, success_message)

            # Отмечаем, что конкурс сохранен
            user_data['contest_saved'] = True

            # Очищаем данные пользователя
            del user_data['pending_contest']

        except Exception as e:
            logging.error(f"Error saving to database: {e}", exc_info=True)
            error_message = f"Произошла ошибка при сохранении конкурса: {str(e)}. Пожалуйста, попробуйте еще раз."
            await update_or_send_message(update, context, error_message)
    elif user_data.get('contest_saved', False):
        logging.info("Contest already saved, skipping.")
    else:
        logging.error(f"Missing data in pending_contest: {pending_contest}")
        error_message = "Ошибка: Не удалось сохранить конкурс. Проверьте введенные данные."
        await update_or_send_message(update, context, error_message)

    return WAITING_INPUT


# Функция для отображения списка ожидающих конкурсов
async def show_pending_contests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    pending_contests = await get_pending_contests(user_id)

    if not pending_contests:
        message = "У вас нет ожидающих конкурсов."
    else:
        message = "Ожидающие конкурсы:\n\n"
        for contest in pending_contests:
            link = contest.get('link', 'Ссылка отсутствует')
            date = contest.get('date', 'Дата не указана')
            message += f"<a href='{link}'>{link}</a>\nДата: {date}\n\n"

    keyboard = [
        [InlineKeyboardButton("🏆 Активные", callback_data='show_active'),
         InlineKeyboardButton("🏁 Завершенные", callback_data='show_completed'),
         InlineKeyboardButton("👀 Отслеживаемые", callback_data='show_tracked')],
        [InlineKeyboardButton("🔄 Обновить", callback_data='refresh_pending')]
    ]

    is_admin = user_id in ADMIN_USERS
    keyboard = add_admin_buttons(keyboard, is_admin)

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text=message,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )

    return WAITING_INPUT


# Функция для получения списка ожидающих конкурсов
async def get_pending_contests():
    query = "SELECT link FROM contests.pending_contests"
    return await execute_query(query)


# Функция для переноса конкурса в основную таблицу
async def transfer_to_main_db(update, context):
    query = update.callback_query
    await query.answer()

    if update.effective_user.id not in ADMIN_USERS:
        await query.edit_message_text("У вас недостаточно прав для выполнения этой операции.", disable_web_page_preview=True)
        return WAITING_INPUT

    contest_index = int(query.data.split('_')[-1]) - 1
    pending_contests = await get_pending_contests()

    if 0 <= contest_index < len(pending_contests):
        link = pending_contests[contest_index]['link']

        # Получаем данные о конкурсе из таблицы pending_contests
        get_pending_contest_query = "SELECT * FROM contests.pending_contests WHERE link = $1"
        pending_contest_data = await execute_query(get_pending_contest_query, (link,))

        if pending_contest_data:
            # Добавляем конкурс в основную таблицу
            add_query = """
            INSERT INTO contests.contests (link, date, dop_channels, status)
            VALUES ($1, $2, $3, 'Активен')
            """
            await execute_query(add_query, (link, pending_contest_data[0]['date'], pending_contest_data[0]['dop_channels']))

            # Удаляем конкурс из таблицы pending_contests
            delete_query = "DELETE FROM contests.pending_contests WHERE link = $1"
            await execute_query(delete_query, (link,))

            await query.edit_message_text(f"Конкурс {link} перенесен в основную базу данных.", disable_web_page_preview=True)
        else:
            await query.edit_message_text("Не удалось найти данные о конкурсе в таблице ожидающих конкурсов.", disable_web_page_preview=True)
    else:
        await query.edit_message_text("Произошла ошибка. Пожалуйста, попробуйте снова.", disable_web_page_preview=True)

    return WAITING_INPUT

async def handle_add_later(update, context):
    query = update.callback_query
    user_data = context.user_data

    link = query.message.reply_to_message.text if query.message.reply_to_message else None
    if link:
        if 'pending_contest' not in user_data:
            user_data['pending_contest'] = {}
        user_data['pending_contest']['link'] = link
        await query.edit_message_text("Пожалуйста, введите дату для вашего конкурса:")
        return AWAITING_DATE
    else:
        query.edit_message_text("Ошибка: Не удалось извлечь ссылку. Попробуйте еще раз.")
        return START

async def handle_date_entry(update, context):
    text = update.message.text
    user_data = context.user_data

    if 'pending_contest' in user_data and 'link' in user_data['pending_contest']:
        user_data['pending_contest']['date'] = text
        return await ask_for_pending_dop_channels(update, context)
    else:
        update.message.reply_text("Ошибка: Ссылка не найдена в черновике.")
        return AWAITING_DATE

def end_conversation(update, context):
    update.message.reply_text('Thank you, your contest has been saved.')
    return ConversationHandler.END

def main():
    application = Application.builder().token("1").build()
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            WAITING_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input),
                CallbackQueryHandler(handle_url_action, pattern='^(finish_contest|add_contest|track_contest|add_to_pending|cancel)$'),
                CallbackQueryHandler(additional_channels, pattern='^(yes|no)$'),
                CallbackQueryHandler(refresh_contests, pattern='^refresh_(Активен|Завершен)$'),
                CallbackQueryHandler(handle_pagination, pattern=r'^page_(Активен|Завершен)_\d+$'),
                CallbackQueryHandler(switch_contest_type, pattern='^show_(active|completed|tracked)$'),
                CallbackQueryHandler(show_contests, pattern='^show_all_contests$'),
                CallbackQueryHandler(finish_contest, pattern='^finish_contest$'),
                CallbackQueryHandler(delete_tracked_contest, pattern='^delete_tracked$'),
                CallbackQueryHandler(handle_delete_tracked, pattern=r'^delete_\d+$'),
                CallbackQueryHandler(handle_delete_tracked, pattern='^cancel_delete$'),
                CallbackQueryHandler(add_new_tracked_contest, pattern='^add_tracked$'),
                CallbackQueryHandler(show_pending_contests, pattern='^show_pending_contests$'),
                CallbackQueryHandler(transfer_to_main_db, pattern='^transfer_to_main_db_'),
                CallbackQueryHandler(handle_pending_dop_channels, pattern='^pending_(yes|no)$'),
            ],
            WAITING_FOR_TRACKED_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tracked_date_input),
            ],
            ADDITIONAL_CHANNELS: [
                CallbackQueryHandler(additional_channels, pattern='^(yes|no)$'),
            ],
            CHANNELS_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, channels_input),
            ],
            CONFIRMATION: [
                CallbackQueryHandler(confirmation, pattern='^(confirm|cancel)$'),
            ],
            FINISH_CONTEST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, update_contest_status),
            ],
            WAITING_FOR_PENDING_DOP_CHANNELS: [  # Новое состояние
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pending_dop_channels_input),
            ],
            START:
                [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_later),
            ],
            AWAITING_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_date_entry)],
            END:
                [MessageHandler(filters.TEXT & ~filters.COMMAND, end_conversation)],
            WAITING_FOR_PENDING_DATE: [
                 MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pending_date_input)
            ]
        },
        fallbacks=[CommandHandler('start', start)],
    )

    application.add_handler(conv_handler)
    application.run_polling()

if __name__ == '__main__':
    main()
