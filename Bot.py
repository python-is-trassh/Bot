import os
import logging
from decimal import Decimal
import random
import asyncio
from datetime import datetime, timedelta
import psycopg2
from psycopg2 import sql
from psycopg2.extras import DictCursor
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils import executor
import requests
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Конфигурация бота
API_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS').split(',')))
BITCOIN_WALLET = os.getenv('BITCOIN_WALLET')
BLOCKCHAIN_API_URL = 'https://blockchain.info/'

# Подключение к базе данных
def get_db_connection():
    return psycopg2.connect(
        dbname=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        host=os.getenv('DB_HOST'),
        port=os.getenv('DB_PORT')
    )

# Инициализация бота
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Состояния FSM
class OrderStates(StatesGroup):
    selecting_category = State()
    selecting_product = State()
    selecting_location = State()
    waiting_payment = State()

class AdminStates(StatesGroup):
    adding_category = State()
    adding_product = State()
    adding_location = State()
    editing_shop_info = State()
    editing_product = State()
    editing_location = State()

# Глобальные переменные для кэширования
bitcoin_rate = None
rate_last_updated = None

# Утилиты
async def get_bitcoin_rate():
    global bitcoin_rate, rate_last_updated
    
    if rate_last_updated is None or (datetime.now() - rate_last_updated).seconds > 300:
        try:
            response = requests.get(f'{BLOCKCHAIN_API_URL}ticker')
            response.raise_for_status()
            data = response.json()
            bitcoin_rate = Decimal(data['RUB']['last'])
            rate_last_updated = datetime.now()
            logger.info(f"Updated Bitcoin rate: {bitcoin_rate} RUB")
        except Exception as e:
            logger.error(f"Error updating Bitcoin rate: {e}")
            if bitcoin_rate is None:
                bitcoin_rate = Decimal('3000000')
    return bitcoin_rate

def satoshi_to_btc(satoshi):
    return Decimal(satoshi) / Decimal('1e8')

def generate_unique_satoshi():
    return random.randint(1, 300)

async def convert_rub_to_btc(rub_amount):
    rate = await get_bitcoin_rate()
    btc_amount = Decimal(rub_amount) / rate
    unique_satoshi = generate_unique_satoshi()
    btc_amount += satoshi_to_btc(unique_satoshi)
    return btc_amount, unique_satoshi

async def check_payment(btc_address, expected_btc):
    try:
        response = requests.get(f'{BLOCKCHAIN_API_URL}rawaddr/{btc_address}')
        response.raise_for_status()
        data = response.json()
        
        total_received = satoshi_to_btc(data['total_received'])
        if total_received >= expected_btc:
            return True, total_received
        return False, total_received
    except Exception as e:
        logger.error(f"Error checking payment: {e}")
        return False, Decimal('0')

async def get_available_link(location_id):
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=DictCursor) as cursor:
            cursor.execute("""
                SELECT id, content_link 
                FROM location_links 
                WHERE location_id = %s AND is_used = FALSE 
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            """, (location_id,))
            link = cursor.fetchone()
            
            if link:
                cursor.execute("""
                    UPDATE location_links 
                    SET is_used = TRUE 
                    WHERE id = %s
                """, (link['id'],))
                conn.commit()
                return link['content_link']
            return None
    finally:
        conn.close()

# Меню
async def set_main_menu(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = ["Каталог", "О магазине", "Курс Bitcoin"]
    markup.add(*buttons)
    await bot.send_message(user_id, "Главное меню:", reply_markup=markup)

async def set_admin_menu(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        "Добавить категорию", "Добавить товар",
        "Добавить локацию", "Редактировать информацию",
        "Выйти из админки"
    ]
    markup.add(*buttons)
    await bot.send_message(user_id, "Админ меню:", reply_markup=markup)

# Команды для пользователей
@dp.message_handler(commands=['start', 'help'])
async def cmd_start(message: types.Message):
    if message.from_user.id in ADMIN_IDS:
        await set_admin_menu(message.from_user.id)
    else:
        await set_main_menu(message.from_user.id)
    await message.answer("Добро пожаловать в наш магазин!")

@dp.message_handler(text="Каталог")
async def cmd_categories(message: types.Message):
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=DictCursor) as cursor:
            cursor.execute("SELECT id, name FROM categories WHERE is_active = TRUE")
            categories = cursor.fetchall()
            
            if not categories:
                await message.answer("Категории товаров временно отсутствуют.")
                return
            
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            for category in categories:
                keyboard.add(types.InlineKeyboardButton(
                    text=category['name'],
                    callback_data=f"category_{category['id']}"
                ))
            
            await message.answer("Выберите категорию:", reply_markup=keyboard)
    finally:
        conn.close()

@dp.message_handler(text="О магазине")
async def cmd_about(message: types.Message):
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=DictCursor) as cursor:
            cursor.execute("SELECT about_text FROM shop_info LIMIT 1")
            about_text = cursor.fetchone()['about_text']
            await message.answer(about_text)
    finally:
        conn.close()

@dp.message_handler(text="Курс Bitcoin")
async def cmd_rate(message: types.Message):
    rate = await get_bitcoin_rate()
    await message.answer(f"Текущий курс Bitcoin: {rate:.2f} RUB")

@dp.callback_query_handler(lambda c: c.data.startswith('category_'))
async def process_category(callback_query: types.CallbackQuery):
    category_id = int(callback_query.data.split('_')[1])
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=DictCursor) as cursor:
            cursor.execute("""
                SELECT id, name, description, price_rub 
                FROM products 
                WHERE category_id = %s AND is_active = TRUE
            """, (category_id,))
            products = cursor.fetchall()
            
            if not products:
                await bot.answer_callback_query(callback_query.id, "В этой категории нет товаров.")
                return
            
            keyboard = types.InlineKeyboardMarkup(row_width=1)
            for product in products:
                keyboard.add(types.InlineKeyboardButton(
                    text=f"{product['name']} - {product['price_rub']} RUB",
                    callback_data=f"product_{product['id']}"
                ))
            
            await bot.send_message(
                callback_query.from_user.id,
                "Выберите товар:",
                reply_markup=keyboard
            )
    finally:
        conn.close()
        await bot.answer_callback_query(callback_query.id)

@dp.callback_query_handler(lambda c: c.data.startswith('product_'))
async def process_product(callback_query: types.CallbackQuery, state: FSMContext):
    product_id = int(callback_query.data.split('_')[1])
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=DictCursor) as cursor:
            cursor.execute("""
                SELECT p.id, p.name, p.description, p.price_rub, c.name as category_name
                FROM products p
                JOIN categories c ON p.category_id = c.id
                WHERE p.id = %s
            """, (product_id,))
            product = cursor.fetchone()
            
            if not product:
                await bot.answer_callback_query(callback_query.id, "Товар не найден.")
                return
            
            cursor.execute("SELECT id, name FROM locations WHERE is_active = TRUE")
            locations = cursor.fetchall()
            
            if not locations:
                await bot.answer_callback_query(callback_query.id, "Нет доступных локаций.")
                return
            
            keyboard = types.InlineKeyboardMarkup(row_width=1)
            for location in locations:
                keyboard.add(types.InlineKeyboardButton(
                    text=location['name'],
                    callback_data=f"location_{product_id}_{location['id']}"
                ))
            
            await state.update_data(product_id=product_id, price_rub=product['price_rub'])
            await OrderStates.selecting_location.set()
            
            await bot.send_message(
                callback_query.from_user.id,
                f"Товар: {product['name']}\n"
                f"Категория: {product['category_name']}\n"
                f"Описание: {product['description']}\n"
                f"Цена: {product['price_rub']} RUB\n\n"
                "Выберите локацию:",
                reply_markup=keyboard
            )
    finally:
        conn.close()
        await bot.answer_callback_query(callback_query.id)

@dp.callback_query_handler(lambda c: c.data.startswith('location_'), state=OrderStates.selecting_location)
async def process_location(callback_query: types.CallbackQuery, state: FSMContext):
    _, product_id, location_id = callback_query.data.split('_')
    product_id = int(product_id)
    location_id = int(location_id)
    
    user_data = await state.get_data()
    price_rub = user_data['price_rub']
    
    btc_amount, unique_satoshi = await convert_rub_to_btc(price_rub)
    
    await state.update_data(
        product_id=product_id,
        location_id=location_id,
        btc_amount=btc_amount,
        unique_satoshi=unique_satoshi,
        order_time=datetime.now()
    )
    
    await OrderStates.waiting_payment.set()
    
    await bot.send_message(
        callback_query.from_user.id,
        f"Пожалуйста, отправьте {btc_amount:.8f} BTC на адрес:\n"
        f"`{BITCOIN_WALLET}`\n\n"
        f"Уникальный идентификатор платежа: {unique_satoshi} сатоши\n\n"
        "После оплаты нажмите кнопку 'Проверить оплату'.",
        parse_mode='Markdown',
        reply_markup=types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton(
                text="Проверить оплату",
                callback_data="check_payment"
            )
        )
    )
    await bot.answer_callback_query(callback_query.id)

@dp.callback_query_handler(lambda c: c.data == 'check_payment', state=OrderStates.waiting_payment)
async def check_payment_handler(callback_query: types.CallbackQuery, state: FSMContext):
    user_data = await state.get_data()
    btc_amount = user_data['btc_amount']
    unique_satoshi = user_data['unique_satoshi']
    
    is_paid, received_amount = await check_payment(BITCOIN_WALLET, btc_amount)
    
    if is_paid:
        content_link = await get_available_link(user_data['location_id'])
        if content_link:
            await bot.send_message(
                callback_query.from_user.id,
                f"Оплата подтверждена! Получено: {received_amount:.8f} BTC\n\n"
                f"Ваша ссылка на контент:\n{content_link}"
            )
            
            # Уведомление администратора
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin_id,
                        f"Новый заказ!\n"
                        f"Пользователь: @{callback_query.from_user.username}\n"
                        f"Товар ID: {user_data['product_id']}\n"
                        f"Локация ID: {user_data['location_id']}\n"
                        f"Сумма: {received_amount:.8f} BTC\n"
                        f"Ссылка: {content_link}"
                    )
                except Exception as e:
                    logger.error(f"Error notifying admin {admin_id}: {e}")
            
            await state.finish()
            await set_main_menu(callback_query.from_user.id)
        else:
            await bot.send_message(
                callback_query.from_user.id,
                "Извините, в этой локации закончились доступные ссылки. "
                "Мы вернем вам деньги в ближайшее время."
            )
    else:
        order_time = user_data['order_time']
        time_left = timedelta(minutes=30) - (datetime.now() - order_time)
        minutes_left = max(0, int(time_left.total_seconds() / 60))
        
        await bot.send_message(
            callback_query.from_user.id,
            f"Оплата не найдена. Ожидаемая сумма: {btc_amount:.8f} BTC\n"
            f"Получено: {received_amount:.8f} BTC\n\n"
            f"Оставшееся время для оплаты: {minutes_left} минут\n\n"
            "Пожалуйста, попробуйте позже или свяжитесь с поддержкой.",
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton(
                    text="Проверить оплату",
                    callback_data="check_payment"
                )
            )
        )
    
    await bot.answer_callback_query(callback_query.id)

# Административные команды
@dp.message_handler(text="Выйти из админки", user_id=ADMIN_IDS)
async def exit_admin_mode(message: types.Message):
    await set_main_menu(message.from_user.id)
    await message.answer("Вы вышли из админ-панели")

@dp.message_handler(text="Добавить категорию", user_id=ADMIN_IDS)
async def admin_add_category(message: types.Message):
    await AdminStates.adding_category.set()
    await message.answer("Введите название новой категории:", reply_markup=types.ReplyKeyboardRemove())

@dp.message_handler(state=AdminStates.adding_category, user_id=ADMIN_IDS)
async def process_add_category(message: types.Message, state: FSMContext):
    category_name = message.text
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO categories (name, is_active) VALUES (%s, TRUE) RETURNING id",
                (category_name,)
            )
            category_id = cursor.fetchone()[0]
            conn.commit()
            await message.answer(f"Категория '{category_name}' добавлена с ID: {category_id}")
            await set_admin_menu(message.from_user.id)
    except Exception as e:
        logger.error(f"Error adding category: {e}")
        await message.answer("Ошибка при добавлении категории.")
    finally:
        conn.close()
        await state.finish()

@dp.message_handler(text="Добавить товар", user_id=ADMIN_IDS)
async def admin_add_product(message: types.Message):
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=DictCursor) as cursor:
            cursor.execute("SELECT id, name FROM categories WHERE is_active = TRUE")
            categories = cursor.fetchall()
            
            if not categories:
                await message.answer("Нет активных категорий. Сначала добавьте категорию.")
                return
            
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
            buttons = [f"Категория {c['id']}: {c['name']}" for c in categories]
            markup.add(*buttons)
            markup.add("Отмена")
            
            await AdminStates.adding_product.set()
            await state.update_data(categories={c['id']: c['name'] for c in categories})
            await message.answer("Выберите категорию для товара:", reply_markup=markup)
    finally:
        conn.close()

@dp.message_handler(state=AdminStates.adding_product, user_id=ADMIN_IDS)
async def process_add_product_step1(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    categories = user_data.get('categories', {})
    
    category_selected = None
    for cid, cname in categories.items():
        if message.text.startswith(f"Категория {cid}:"):
            category_selected = cid
            break
    
    if category_selected is None:
        await message.answer("Пожалуйста, выберите категорию из списка.")
        return
    
    await state.update_data(category_id=category_selected)
    await message.answer(
        "Введите данные товара в формате:\n"
        "Название|Описание|Цена в RUB\n\n"
        "Пример:\n"
        "Курс Python|Подробный курс по Python|2999",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message_handler(state=AdminStates.adding_product, user_id=ADMIN_IDS)
async def process_add_product_step2(message: types.Message, state: FSMContext):
    try:
        name, description, price_rub = message.text.split('|')
        price_rub = Decimal(price_rub.strip())
        
        user_data = await state.get_data()
        category_id = user_data['category_id']
        
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO products (name, description, price_rub, category_id, is_active)
                    VALUES (%s, %s, %s, %s, TRUE)
                    RETURNING id
                """, (name.strip(), description.strip(), price_rub, category_id))
                product_id = cursor.fetchone()[0]
                conn.commit()
                await message.answer(f"Товар '{name}' добавлен с ID: {product_id}")
                await set_admin_menu(message.from_user.id)
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error adding product: {e}")
        await message.answer("Неверный формат данных. Пожалуйста, попробуйте снова.")
    finally:
        await state.finish()

@dp.message_handler(text="Добавить локацию", user_id=ADMIN_IDS)
async def admin_add_location(message: types.Message):
    await AdminStates.adding_location.set()
    await message.answer("Введите название новой локации:", reply_markup=types.ReplyKeyboardRemove())

@dp.message_handler(state=AdminStates.adding_location, user_id=ADMIN_IDS)
async def process_add_location(message: types.Message, state: FSMContext):
    location_name = message.text
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO locations (name, is_active) VALUES (%s, TRUE) RETURNING id",
                (location_name,)
            )
            location_id = cursor.fetchone()[0]
            conn.commit()
            await message.answer(f"Локация '{location_name}' добавлена с ID: {location_id}")
            await set_admin_menu(message.from_user.id)
    except Exception as e:
        logger.error(f"Error adding location: {e}")
        await message.answer("Ошибка при добавлении локации.")
    finally:
        conn.close()
        await state.finish()

@dp.message_handler(text="Редактировать информацию", user_id=ADMIN_IDS)
async def admin_edit_shop_info(message: types.Message):
    await AdminStates.editing_shop_info.set()
    await message.answer("Введите новый текст для раздела 'О магазине':", reply_markup=types.ReplyKeyboardRemove())

@dp.message_handler(state=AdminStates.editing_shop_info, user_id=ADMIN_IDS)
async def process_edit_shop_info(message: types.Message, state: FSMContext):
    new_text = message.text
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE shop_info SET about_text = %s, updated_at = NOW()",
                (new_text,)
            )
            conn.commit()
            await message.answer("Текст 'О магазине' успешно обновлен!")
            await set_admin_menu(message.from_user.id)
    except Exception as e:
        logger.error(f"Error updating shop info: {e}")
        await message.answer("Ошибка при обновлении информации.")
    finally:
        conn.close()
        await state.finish()

# Проверка просроченных заказов
async def check_expired_orders():
    while True:
        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=DictCursor) as cursor:
                expired_time = datetime.now() - timedelta(minutes=30)
                cursor.execute("""
                    SELECT * FROM orders 
                    WHERE is_paid = FALSE AND created_at < %s
                """, (expired_time,))
                expired_orders = cursor.fetchall()
                
                for order in expired_orders:
                    try:
                        await bot.send_message(
                            order['user_id'],
                            f"Ваш заказ #{order['id']} был отменен, так как оплата не поступила в течение 30 минут."
                        )
                    except Exception as e:
                        logger.error(f"Error notifying user about expired order: {e}")
                    
                    cursor.execute("""
                        UPDATE orders SET is_cancelled = TRUE WHERE id = %s
                    """, (order['id'],))
                    conn.commit()
        except Exception as e:
            logger.error(f"Error checking expired orders: {e}")
        finally:
            conn.close()
        
        await asyncio.sleep(60)

# Запуск бота
async def on_startup(dp):
    asyncio.create_task(check_expired_orders())
    logger.info("Bot started")
    await bot.delete_my_commands()

if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
