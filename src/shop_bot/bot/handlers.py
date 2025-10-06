import logging
import os
import uuid
import qrcode
import aiohttp
import re
import aiohttp
import hashlib
import json
import base64
import asyncio

from urllib.parse import urlencode
from hmac import compare_digest
from functools import wraps
from yookassa import Payment
from io import BytesIO
from datetime import datetime, timedelta
from aiosend import CryptoPay, TESTNET
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict

from pytonconnect import TonConnect
from pytonconnect.exceptions import UserRejectsError

from aiogram import Bot, Router, F, types, html
from aiogram.types import BufferedInputFile, LabeledPrice, PreCheckoutQuery
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ChatMemberStatus
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shop_bot.bot import keyboards
from shop_bot.modules import remnawave_api
from shop_bot.data_manager import remnawave_repository as rw_repo
from shop_bot.data_manager.remnawave_repository import (
    get_user, get_user_keys, update_user_stats,
    register_user_if_not_exists, get_next_key_number,
    set_trial_used, set_terms_agreed, get_setting, get_all_hosts,
    get_plans_for_host, get_plan_by_id, log_transaction, get_referral_count,
    create_pending_transaction, get_all_users,
    create_support_ticket, add_support_message, get_user_tickets,
    get_ticket, get_ticket_messages, set_ticket_status, update_ticket_thread_info,
    get_ticket_by_thread,
    get_balance, deduct_from_balance,
    add_to_balance,
    add_to_referral_balance_all, get_referral_balance_all,
    get_referral_balance,
    is_admin,
    set_referral_start_bonus_received,
    update_key_host_and_info,
    find_and_complete_pending_transaction,
    get_latest_pending_for_user,
    create_payload_pending,
    get_pending_status,
    get_pending_metadata,
)

from shop_bot.config import (
    get_profile_text, get_vpn_active_text, VPN_INACTIVE_TEXT, VPN_NO_DATA_TEXT,
    get_key_info_text, CHOOSE_PAYMENT_METHOD_MESSAGE, get_purchase_success_text
)

TELEGRAM_BOT_USERNAME = None
PAYMENT_METHODS = None
ADMIN_ID = None  # устаревшее: используйте is_admin()
logger = logging.getLogger(__name__)

async def _create_heleket_payment_request(
    user_id: int,
    price: float,
    months: int,
    host_name: str | None,
    state_data: dict,
) -> str | None:
    """
    Создание инвойса в Heleket и возврат payment URL.

    Требования API:
      - POST https://api.heleket.com/v1/payment
      - Заголовки: merchant, sign (md5(base64(json_body)+API_KEY))
      - Тело (минимум): { amount, currency, order_id }
      - Дополнительно: url_callback (наш вебхук), description (положим JSON метаданных)
    """
    # Подготовим ключи и проверим настройки
    merchant_id = (get_setting("heleket_merchant_id") or "").strip()
    api_key = (get_setting("heleket_api_key") or "").strip()
    if not (merchant_id and api_key):
        logger.error("Heleket: не заданы merchant_id/api_key в настройках.")
        return None

    # payment_id используем как order_id; параллельно сохраним pending по нему
    payment_id = str(uuid.uuid4())

    # Соберём metadata, на которое полагается обработка оплаты
    metadata = {
        "user_id": int(user_id),
        "months": int(months or 0),
        "price": float(Decimal(str(price)).quantize(Decimal("0.01"))),
        "action": state_data.get("action"),
        "key_id": state_data.get("key_id"),
        "host_name": host_name or state_data.get("host_name"),
        "plan_id": state_data.get("plan_id"),
        "customer_email": state_data.get("customer_email"),
        "payment_method": "Heleket",
        "payment_id": payment_id,
    }

    # Сохраняем pending — даже если вебхук вернёт полное описание, фолбэк не помешает
    try:
        create_payload_pending(payment_id, user_id, float(metadata["price"]), metadata)
    except Exception as e:
        logger.warning(f"Heleket: не удалось создать pending: {e}")

    # Соберём тело запроса Heleket
    amount_str = f"{Decimal(str(price)).quantize(Decimal('0.01'))}"
    body: dict = {
        "amount": amount_str,
        "currency": "RUB",  # оплачиваем в рублях; плательщик выберет криптовалюту на стороне Heleket
        "order_id": payment_id,
        # Передаём метаданные строкой JSON — наш вебхук читает поле 'description'
        "description": json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
    }
    # Вебхук для Heleket — если указан domain в настройках
    try:
        domain = (get_setting("domain") or "").strip()
    except Exception:
        domain = ""
    if domain:
        # Ожидаемый путь вебхука уже реализован во Flask: '/heleket-webhook'
        # Если domain без завершающего '/', добавим
        cb = f"{domain.rstrip('/')}/heleket-webhook"
        body["url_callback"] = cb

    # Подпись запроса
    body_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    base64_payload = base64.b64encode(body_json.encode()).decode()
    sign = hashlib.md5((base64_payload + api_key).encode()).hexdigest()

    headers = {
        "merchant": merchant_id,
        "sign": sign,
        "Content-Type": "application/json",
    }

    url = "https://api.heleket.com/v1/payment"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=body, timeout=20) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"Heleket: HTTP {resp.status}: {text}")
                    return None
                data = await resp.json(content_type=None)
                # Успех: ожидаем state == 0 и presence of result.url
                if isinstance(data, dict) and data.get("state") == 0:
                    try:
                        result = data.get("result") or {}
                        pay_url = result.get("url")
                        if pay_url:
                            return pay_url
                    except Exception:
                        pass
                logger.error(f"Heleket: неожиданный ответ API: {data}")
                return None
    except Exception as e:
        logger.error(f"Heleket: ошибка при создании инвойса: {e}", exc_info=True)
        return None

async def _create_cryptobot_invoice(
    user_id: int,
    price_rub: float,
    months: int,
    host_name: str | None,
    state_data: dict,
) -> tuple[str, int] | None:
    """
    Создание инвойса в Crypto Pay (CryptoBot) и возврат bot_invoice_url.

    Эндпоинт: POST https://pay.crypt.bot/api/createInvoice
    Заголовки: { 'Crypto-Pay-API-Token': <token>, 'Content-Type': 'application/json' }

    Мы создаём инвойс в фиате RUB, чтобы не конвертировать курсы вручную.
    В payload записываем строку, которую ожидает наш вебхук '/cryptobot-webhook'.
    """
    token = (get_setting("cryptobot_token") or "").strip()
    if not token:
        logger.error("CryptoBot: не указан токен API в настройках.")
        return None

    # Собираем payload строго в формате, который парсит вебхук
    # parts: user_id:months:price:action:key_id:host_name:plan_id:customer_email:payment_method
    action = state_data.get("action")
    key_id = state_data.get("key_id")
    plan_id = state_data.get("plan_id")
    customer_email = state_data.get("customer_email")
    pm = "CryptoBot"

    # price в вебхуке строкой, оставим 2 знака после запятой
    price_str = f"{Decimal(str(price_rub)).quantize(Decimal('0.01'))}"
    payload_str = ":".join([
        str(int(user_id)),
        str(int(months or 0)),
        price_str,
        str(action or ""),
        str(key_id if key_id is not None else "None"),
        str((host_name or state_data.get('host_name') or "")),
        str(plan_id if plan_id is not None else "None"),
        str(customer_email if customer_email is not None else "None"),
        pm,
    ])

    body = {
        "amount": price_str,
        "currency_type": "fiat",
        "fiat": "RUB",
        "payload": payload_str,
        # Не ограничиваем ассеты, пусть CryptoBot предложит доступные
        # Можно добавить: accepted_assets=["USDT", "TON", ...]
    }

    headers = {
        "Crypto-Pay-API-Token": token,
        "Content-Type": "application/json",
    }

    url = "https://pay.crypt.bot/api/createInvoice"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=body, timeout=20) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"CryptoBot: HTTP {resp.status}: {text}")
                    return None
                data = await resp.json(content_type=None)
                # Ответ Crypto Pay обычно: { ok: true, result: { invoice_id, bot_invoice_url, ... } }
                if isinstance(data, dict) and data.get("ok") and isinstance(data.get("result"), dict):
                    res = data["result"]
                    pay_url = res.get("bot_invoice_url") or res.get("invoice_url")
                    invoice_id = res.get("invoice_id")
                    if pay_url and invoice_id is not None:
                        return pay_url, int(invoice_id)
                logger.error(f"CryptoBot: неожиданный ответ API: {data}")
                return None
    except Exception as e:
        logger.error(f"CryptoBot: ошибка при создании инвойса: {e}", exc_info=True)
        return None

    # payment_id используем как order_id; параллельно сохраним pending по нему
    payment_id = str(uuid.uuid4())

    # Соберём metadata, на которое полагается обработка оплаты
    metadata = {
        "user_id": int(user_id),
        "months": int(months or 0),
        "price": float(Decimal(str(price)).quantize(Decimal("0.01"))),
        "action": state_data.get("action"),
        "key_id": state_data.get("key_id"),
        "host_name": host_name or state_data.get("host_name"),
        "plan_id": state_data.get("plan_id"),
        "customer_email": state_data.get("customer_email"),
        "payment_method": "Heleket",
        "payment_id": payment_id,
    }

    # Сохраняем pending — даже если вебхук вернёт полное описание, фолбэк не помешает
    try:
        create_payload_pending(payment_id, user_id, float(metadata["price"]), metadata)
    except Exception as e:
        logger.warning(f"Heleket: не удалось создать pending: {e}")

    # Соберём тело запроса Heleket
    amount_str = f"{Decimal(str(price)).quantize(Decimal('0.01'))}"
    body: dict = {
        "amount": amount_str,
        "currency": "RUB",  # оплачиваем в рублях; плательщик выберет криптовалюту на стороне Heleket
        "order_id": payment_id,
        # Передаём метаданные строкой JSON — наш вебхук читает поле 'description'
        "description": json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
    }
    # Вебхук для Heleket — если указан domain в настройках
    try:
        domain = (get_setting("domain") or "").strip()
    except Exception:
        domain = ""
    if domain:
        # Ожидаемый путь вебхука уже реализован во Flask: '/heleket-webhook'
        # Если domain без завершающего '/', добавим
        cb = f"{domain.rstrip('/')}/heleket-webhook"
        body["url_callback"] = cb

    # Подпись запроса
    body_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    base64_payload = base64.b64encode(body_json.encode()).decode()
    sign = hashlib.md5((base64_payload + api_key).encode()).hexdigest()

    headers = {
        "merchant": merchant_id,
        "sign": sign,
        "Content-Type": "application/json",
    }

    url = "https://api.heleket.com/v1/payment"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=body, timeout=20) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"Heleket: HTTP {resp.status}: {text}")
                    return None
                data = await resp.json(content_type=None)
                # Успех: ожидаем state == 0 и presence of result.url
                if isinstance(data, dict) and data.get("state") == 0:
                    try:
                        result = data.get("result") or {}
                        pay_url = result.get("url")
                        if pay_url:
                            return pay_url
                    except Exception:
                        pass
                logger.error(f"Heleket: неожиданный ответ API: {data}")
                return None
    except Exception as e:
        logger.error(f"Heleket: ошибка при создании инвойса: {e}", exc_info=True)
        return None

class KeyPurchase(StatesGroup):
    waiting_for_host_selection = State()
    waiting_for_plan_selection = State()

class Onboarding(StatesGroup):
    waiting_for_subscription_and_agreement = State()

class PaymentProcess(StatesGroup):
    waiting_for_email = State()
    waiting_for_payment_method = State()

 
class TopUpProcess(StatesGroup):
    waiting_for_amount = State()
    waiting_for_topup_method = State()


class SupportDialog(StatesGroup):
    waiting_for_subject = State()
    waiting_for_message = State()
    waiting_for_reply = State()

def is_valid_email(email: str) -> bool:
    pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    return re.match(pattern, email) is not None

async def show_main_menu(message: types.Message, edit_message: bool = False):
    user_id = message.chat.id
    user_db_data = get_user(user_id)
    user_keys = get_user_keys(user_id)
    
    trial_available = not (user_db_data and user_db_data.get('trial_used'))
    is_admin_flag = is_admin(user_id)

    # Текст главного меню — можно настроить в панели (bot_settings.main_menu_text)
    text = get_setting("main_menu_text") or "🏠 <b>Главное меню</b>\n\nВыберите действие:"
    keyboard = keyboards.create_main_menu_keyboard(user_keys, trial_available, is_admin_flag)
    # Отправляем только текст без фотографии
    if edit_message:
        try:
            await message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest:
            pass
    else:
        await message.answer(text, reply_markup=keyboard)

async def process_successful_onboarding(callback: types.CallbackQuery, state: FSMContext):
    """Завершает онбординг: ставит флаг согласия и открывает главное меню."""
    user_id = callback.from_user.id
    try:
        set_terms_agreed(user_id)
    except Exception as e:
        logger.error(f"Failed to set_terms_agreed for user {user_id}: {e}")
    try:
        await callback.answer()
    except Exception:
        pass
    try:
        await show_main_menu(callback.message, edit_message=True)
    except Exception:
        try:
            await callback.message.answer("✅ Требования выполнены. Открываю меню...")
        except Exception:
            pass
    try:
        await state.clear()
    except Exception:
        pass

def registration_required(f):
    @wraps(f)
    async def decorated_function(event: types.Update, *args, **kwargs):
        user_id = event.from_user.id
        user_data = get_user(user_id)
        if user_data:
            return await f(event, *args, **kwargs)
        else:
            message_text = "Пожалуйста, для начала работы со мной, отправьте команду /start"
            if isinstance(event, types.CallbackQuery):
                await event.answer(message_text, show_alert=True)
            else:
                await event.answer(message_text)
    return decorated_function

def get_user_router() -> Router:
    user_router = Router()

    @user_router.message(CommandStart())
    async def start_handler(message: types.Message, state: FSMContext, bot: Bot, command: CommandObject):
        user_id = message.from_user.id
        username = message.from_user.username or message.from_user.full_name
        referrer_id = None

        if command.args and command.args.startswith('ref_'):
            try:
                potential_referrer_id = int(command.args.split('_')[1])
                if potential_referrer_id != user_id:
                    referrer_id = potential_referrer_id
                    logger.info(f"New user {user_id} was referred by {referrer_id}")
            except (IndexError, ValueError):
                logger.warning(f"Invalid referral code received: {command.args}")
                
        register_user_if_not_exists(user_id, username, referrer_id)
        user_id = message.from_user.id
        username = message.from_user.username or message.from_user.full_name
        user_data = get_user(user_id)

        # Бонус при старте для пригласившего (fixed_start_referrer): единоразово, когда новый пользователь запускает бота по реферальной ссылке
        try:
            reward_type = (get_setting("referral_reward_type") or "percent_purchase").strip()
        except Exception:
            reward_type = "percent_purchase"
        if reward_type == "fixed_start_referrer" and referrer_id and user_data and not user_data.get('referral_start_bonus_received'):
            try:
                amount_raw = get_setting("referral_on_start_referrer_amount") or "20"
                start_bonus = Decimal(str(amount_raw)).quantize(Decimal("0.01"))
            except Exception:
                start_bonus = Decimal("20.00")
            if start_bonus > 0:
                try:
                    ok = add_to_balance(int(referrer_id), float(start_bonus))
                except Exception as e:
                    logger.warning(f"Referral start bonus: add_to_balance failed for referrer {referrer_id}: {e}")
                    ok = False
                # Увеличиваем суммарный заработок по рефералке
                try:
                    add_to_referral_balance_all(int(referrer_id), float(start_bonus))
                except Exception as e:
                    logger.warning(f"Referral start bonus: failed to increment referral_balance_all for {referrer_id}: {e}")
                # Помечаем, что для этого нового пользователя старт уже обработан, чтобы не дублировать при повторном /start
                try:
                    set_referral_start_bonus_received(user_id)
                except Exception:
                    pass
                # Уведомим пригласившего
                try:
                    await bot.send_message(
                        chat_id=int(referrer_id),
                        text=(
                            "🎁 Начисление за приглашение!\n"
                            f"Новый пользователь: {message.from_user.full_name} (ID: {user_id})\n"
                            f"Бонус: {float(start_bonus):.2f} RUB"
                        )
                    )
                except Exception:
                    pass

        if user_data and user_data.get('agreed_to_terms'):
            await message.answer(
                f"👋 Снова здравствуйте, {html.bold(message.from_user.full_name)}!",
                reply_markup=keyboards.main_reply_keyboard
            )
            await show_main_menu(message)
            return

        terms_url = get_setting("terms_url")
        privacy_url = get_setting("privacy_url")
        channel_url = get_setting("channel_url")

        if not channel_url and (not terms_url or not privacy_url):
            set_terms_agreed(user_id)
            await show_main_menu(message)
            return

        is_subscription_forced = get_setting("force_subscription") == "true"
        
        show_welcome_screen = (is_subscription_forced and channel_url) or (terms_url and privacy_url)

        if not show_welcome_screen:
            set_terms_agreed(user_id)
            await show_main_menu(message)
            return

        welcome_parts = ["<b>Добро пожаловать!</b>\n"]
        
        if is_subscription_forced and channel_url:
            welcome_parts.append("Для доступа ко всем функциям, пожалуйста, подпишитесь на наш канал.")
        
        if terms_url and privacy_url:
            welcome_parts.append(
                "Также необходимо ознакомиться и принять наши "
                f"<a href='{terms_url}'>Условия использования</a> и "
                f"<a href='{privacy_url}'>Политику конфиденциальности</a>."
            )
        
        welcome_parts.append("\nПосле этого нажмите кнопку ниже.")
        final_text = "\n".join(welcome_parts)
        
        await message.answer(
            final_text,
            reply_markup=keyboards.create_welcome_keyboard(
                channel_url=channel_url,
                is_subscription_forced=is_subscription_forced
            ),
            disable_web_page_preview=True
        )
        await state.set_state(Onboarding.waiting_for_subscription_and_agreement)

    @user_router.callback_query(Onboarding.waiting_for_subscription_and_agreement, F.data == "check_subscription_and_agree")
    async def check_subscription_handler(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
        user_id = callback.from_user.id
        channel_url = get_setting("channel_url")
        is_subscription_forced = get_setting("force_subscription") == "true"

        if not is_subscription_forced or not channel_url:
            await process_successful_onboarding(callback, state)
            return
            
        try:
            if '@' not in channel_url and 't.me/' not in channel_url:
                logger.error(f"Неверный формат URL канала: {channel_url}. Пропускаем проверку подписки.")
                await process_successful_onboarding(callback, state)
                return

            channel_id = '@' + channel_url.split('/')[-1] if 't.me/' in channel_url else channel_url
            member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
            
            if member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
                await process_successful_onboarding(callback, state)
            else:
                await callback.answer("Вы еще не подписались на канал. Пожалуйста, подпишитесь и попробуйте снова.", show_alert=True)

        except Exception as e:
            logger.error(f"Ошибка при проверке подписки для user_id {user_id} на канал {channel_url}: {e}")
            await callback.answer("Не удалось проверить подписку. Убедитесь, что бот является администратором канала. Попробуйте позже.", show_alert=True)

    @user_router.message(Onboarding.waiting_for_subscription_and_agreement)
    async def onboarding_fallback_handler(message: types.Message):
        await message.answer("Пожалуйста, выполните требуемые действия и нажмите на кнопку в сообщении выше.")

    @user_router.message(F.text == "🏠 Главное меню")
    @registration_required
    async def main_menu_handler(message: types.Message):
        await show_main_menu(message)

    @user_router.callback_query(F.data == "back_to_main_menu")
    @registration_required
    async def back_to_main_menu_handler(callback: types.CallbackQuery):
        await callback.answer()
        await show_main_menu(callback.message, edit_message=True)

    @user_router.callback_query(F.data == "show_main_menu")
    @registration_required
    async def show_main_menu_cb(callback: types.CallbackQuery):
        await callback.answer()
        await show_main_menu(callback.message, edit_message=True)

    @user_router.callback_query(F.data == "show_profile")
    @registration_required
    async def profile_handler_callback(callback: types.CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        user_db_data = get_user(user_id)
        user_keys = get_user_keys(user_id)
        if not user_db_data:
            await callback.answer("Не удалось получить данные профиля.", show_alert=True)
            return
        username = html.bold(user_db_data.get('username', 'Пользователь'))
        total_spent, total_months = user_db_data.get('total_spent', 0), user_db_data.get('total_months', 0)
        now = datetime.now()
        active_keys = [key for key in user_keys if datetime.fromisoformat(key['expiry_date']) > now]
        if active_keys:
            latest_key = max(active_keys, key=lambda k: datetime.fromisoformat(k['expiry_date']))
            latest_expiry_date = datetime.fromisoformat(latest_key['expiry_date'])
            time_left = latest_expiry_date - now
            vpn_status_text = get_vpn_active_text(time_left.days, time_left.seconds // 3600)
        elif user_keys: vpn_status_text = VPN_INACTIVE_TEXT
        else: vpn_status_text = VPN_NO_DATA_TEXT
        final_text = get_profile_text(username, total_spent, total_months, vpn_status_text)
        # Баланс: основной + реферальные метрики
        try:
            main_balance = get_balance(user_id)
        except Exception:
            main_balance = 0.0
        final_text += f"\n\n💼 <b>Основной баланс:</b> {main_balance:.0f} RUB"
        # Реферальная информация
        try:
            referral_count = get_referral_count(user_id)
        except Exception:
            referral_count = 0
        try:
            total_ref_earned = float(get_referral_balance_all(user_id))
        except Exception:
            total_ref_earned = 0.0
        final_text += (
            f"\n🤝 <b>Рефералы:</b> {referral_count}"
            f"\n💰 <b>Заработано по рефералке (всего):</b> {total_ref_earned:.2f} RUB"
        )
        await callback.message.edit_text(final_text, reply_markup=keyboards.create_profile_keyboard())

    @user_router.callback_query(F.data == "top_up_start")
    @registration_required
    async def topup_start_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await callback.message.edit_text(
            "Введите сумму пополнения в рублях (например, 300):\nМинимум: 10 RUB, максимум: 100000 RUB",
            reply_markup=keyboards.create_back_to_menu_keyboard()
        )
        await state.set_state(TopUpProcess.waiting_for_amount)

    @user_router.message(TopUpProcess.waiting_for_amount)
    async def topup_amount_input(message: types.Message, state: FSMContext):
        text = (message.text or "").replace(",", ".").strip()
        try:
            amount = Decimal(text)
        except Exception:
            await message.answer("❌ Введите корректную сумму, например: 300", reply_markup=keyboards.create_back_to_menu_keyboard())
            return
        if amount <= 0:
            await message.answer("❌ Сумма должна быть положительной", reply_markup=keyboards.create_back_to_menu_keyboard())
            return
        if amount < Decimal("10"):
            await message.answer("❌ Минимальная сумма пополнения: 10 RUB", reply_markup=keyboards.create_back_to_menu_keyboard())
            return
        if amount > Decimal("100000"):
            await message.answer("❌ Максимальная сумма пополнения: 100000 RUB", reply_markup=keyboards.create_back_to_menu_keyboard())
            return
        final_amount = amount.quantize(Decimal("0.01"))
        await state.update_data(topup_amount=float(final_amount))
        await message.answer(
            f"К пополнению: {final_amount:.2f} RUB\nВыберите способ оплаты:",
            reply_markup=keyboards.create_topup_payment_method_keyboard(PAYMENT_METHODS)
        )
        await state.set_state(TopUpProcess.waiting_for_topup_method)

    @user_router.callback_query(TopUpProcess.waiting_for_topup_method, F.data == "topup_pay_yookassa")
    async def topup_pay_yookassa(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("Создаю ссылку на оплату...")
        data = await state.get_data()
        amount = Decimal(str(data.get('topup_amount', 0)))
        if amount <= 0:
            await callback.message.edit_text("❌ Некорректная сумма пополнения. Повторите ввод.")
            await state.clear()
            return
        user_id = callback.from_user.id
        price_str_for_api = f"{amount:.2f}"
        price_float_for_metadata = float(amount)

        try:
            # Сформируем чек, если указан email для чеков
            customer_email = get_setting("receipt_email")
            receipt = None
            if customer_email and is_valid_email(customer_email):
                receipt = {
                    "customer": {"email": customer_email},
                    "items": [{
                        "description": f"Пополнение баланса",
                        "quantity": "1.00",
                        "amount": {"value": price_str_for_api, "currency": "RUB"},
                        "vat_code": "1",
                        "payment_subject": "service",
                        "payment_mode": "full_payment"
                    }]
                }

            payment_payload = {
                "amount": {"value": price_str_for_api, "currency": "RUB"},
                "confirmation": {"type": "redirect", "return_url": f"https://t.me/{TELEGRAM_BOT_USERNAME}"},
                "capture": True,
                "description": f"Пополнение баланса на {price_str_for_api} RUB",
                "metadata": {
                    "user_id": user_id,
                    "price": price_float_for_metadata,
                    "action": "top_up",
                    "payment_method": "YooKassa"
                }
            }
            if receipt:
                payment_payload['receipt'] = receipt
            payment = Payment.create(payment_payload, uuid.uuid4())
            await state.clear()
            await callback.message.edit_text(
                "Нажмите на кнопку ниже для оплаты:",
                reply_markup=keyboards.create_payment_keyboard(payment.confirmation.confirmation_url)
            )
        except Exception as e:
            logger.error(f"Failed to create YooKassa topup payment: {e}", exc_info=True)
            await callback.message.answer("Не удалось создать ссылку на оплату.")
            await state.clear()

    # --- Telegram Stars (XTR) ---
    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_stars")
    async def create_stars_invoice_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("Готовлю счёт в Telegram Stars...")
        data = await state.get_data()
        plan = get_plan_by_id(data.get('plan_id'))
        if not plan:
            await callback.message.edit_text("❌ Ошибка: Тариф не найден.")
            await state.clear()
            return
        user_id = callback.from_user.id
        # Итого к оплате
        price_rub = Decimal(str(data.get('final_price', plan['price'])))
        try:
            stars_ratio_raw = get_setting("stars_per_rub") or '0'
            stars_ratio = Decimal(stars_ratio_raw)
        except Exception:
            stars_ratio = Decimal('0')
        if stars_ratio <= 0:
            await callback.message.edit_text("❌ Оплата в Stars временно недоступна.")
            await state.clear()
            return
        # Кол-во звёзд — целое число
        stars_amount = int((price_rub * stars_ratio).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        if stars_amount <= 0:
            stars_amount = 1

        payment_id = str(uuid.uuid4())
        metadata = {
            "user_id": user_id,
            "months": int(plan['months']),
            "price": float(price_rub),
            "action": data.get('action'),
            "key_id": data.get('key_id'),
            "host_name": data.get('host_name'),
            "plan_id": data.get('plan_id'),
            "customer_email": data.get('customer_email'),
            "payment_method": "Telegram Stars",
            "payment_id": payment_id,
        }
        try:
            ok = create_payload_pending(payment_id, user_id, float(price_rub), metadata)
            logger.info(f"Stars pending created: ok={ok}, payment_id={payment_id}, user_id={user_id}, price_rub={price_rub}")
        except Exception as e:
            logger.error(f"Failed to create pending for Stars payment_id={payment_id}: {e}", exc_info=True)

        title = f"Подписка на {int(plan['months'])} мес."
        description = f"Оплата VPN на {int(plan['months'])} мес."
        try:
            await callback.message.answer_invoice(
                title=title,
                description=description,
                prices=[LabeledPrice(label=title, amount=stars_amount)],
                payload=payment_id,
                currency="XTR",
            )
            await state.clear()
        except Exception as e:
            logger.error(f"Failed to create Stars invoice: {e}")
            await callback.message.edit_text("❌ Не удалось создать счёт в Stars. Попробуйте другой способ оплаты.")
            await state.clear()

    @user_router.callback_query(TopUpProcess.waiting_for_topup_method, F.data == "topup_pay_stars")
    async def topup_stars_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("Готовлю счёт в Telegram Stars...")
        data = await state.get_data()
        user_id = callback.from_user.id
        amount_rub = Decimal(str(data.get('topup_amount', 0)))
        if amount_rub <= 0:
            await callback.message.edit_text("❌ Некорректная сумма пополнения.")
            await state.clear()
            return
        try:
            stars_ratio_raw = get_setting("stars_per_rub") or '0'
            stars_ratio = Decimal(stars_ratio_raw)
        except Exception:
            stars_ratio = Decimal('0')
        if stars_ratio <= 0:
            await callback.message.edit_text("❌ Оплата в Stars временно недоступна.")
            await state.clear()
            return
        stars_amount = int((amount_rub * stars_ratio).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        if stars_amount <= 0:
            stars_amount = 1
        payment_id = str(uuid.uuid4())
        metadata = {
            "user_id": user_id,
            "price": float(amount_rub),
            "action": "top_up",
            "payment_method": "Telegram Stars",
            "payment_id": payment_id,
        }
        try:
            ok = create_payload_pending(payment_id, user_id, float(amount_rub), metadata)
            logger.info(f"Stars topup pending created: ok={ok}, payment_id={payment_id}, user_id={user_id}, amount_rub={amount_rub}")
        except Exception as e:
            logger.error(f"Failed to create pending for Stars topup payment_id={payment_id}: {e}", exc_info=True)
        try:
            await callback.message.answer_invoice(
                title="Пополнение баланса",
                description=f"Пополнение на {amount_rub:.2f} RUB",
                prices=[LabeledPrice(label="Пополнение", amount=stars_amount)],
                payload=payment_id,
                currency="XTR",
            )
            await state.clear()
        except Exception as e:
            logger.error(f"Failed to create Stars topup invoice: {e}")
            await callback.message.edit_text("❌ Не удалось создать счёт в Stars.")
            await state.clear()

    # Пре-чек Stars: подтверждаем все инвойсы
    @user_router.pre_checkout_query()
    async def pre_checkout_handler(pre_checkout_q: PreCheckoutQuery):
        try:
            await pre_checkout_q.answer(ok=True)
        except Exception:
            pass

    # Успешная оплата Stars: обрабатываем payload как payment_id
    @user_router.message(F.successful_payment)
    async def stars_success_handler(message: types.Message, bot: Bot):
        try:
            payload = message.successful_payment.invoice_payload if message.successful_payment else None
        except Exception:
            payload = None
        if not payload:
            return
        metadata = find_and_complete_pending_transaction(payload)
        if not metadata:
            logger.warning(f"Stars payment: metadata not found for payload {payload}")
            # Fallback: try latest pending for this user (in case payload mismatch)
            try:
                fallback = get_latest_pending_for_user(message.from_user.id)
            except Exception as e:
                fallback = None
                logger.error(f"Stars payment: fallback lookup failed for user {message.from_user.id}: {e}", exc_info=True)
            if fallback and (fallback.get('payment_method') == 'Telegram Stars'):
                pid = fallback.get('payment_id') or payload
                logger.info(f"Stars payment: using fallback pending for user {message.from_user.id}, pid={pid}")
                metadata = find_and_complete_pending_transaction(pid)
        if not metadata:
            # Last resort: reconstruct top_up from successful_payment total_amount using stars_per_rub
            try:
                total_stars = int(getattr(message.successful_payment, 'total_amount', 0) or 0)
            except Exception:
                total_stars = 0
            try:
                stars_ratio_raw = get_setting("stars_per_rub") or '0'
                stars_ratio = Decimal(stars_ratio_raw)
            except Exception:
                stars_ratio = Decimal('0')
            if total_stars > 0 and stars_ratio > 0:
                amount_rub = (Decimal(total_stars) / stars_ratio).quantize(Decimal('0.01'))
                metadata = {
                    "user_id": message.from_user.id,
                    "price": float(amount_rub),
                    "action": "top_up",
                    "payment_method": "Telegram Stars",
                    "payment_id": payload,
                }
                logger.info(f"Stars payment: reconstructing top_up from total_stars={total_stars}, ratio={stars_ratio}, amount_rub={amount_rub}")
            else:
                # still nothing — stop
                logger.warning("Stars payment: cannot reconstruct payment metadata; skipping")
                return
        # Enrich metadata with current tg username if available for logging
        try:
            if message.from_user and message.from_user.username:
                metadata.setdefault('tg_username', message.from_user.username)
        except Exception:
            pass
        await process_successful_payment(bot, metadata)

    # --- YooMoney ---
    def _build_yoomoney_link(receiver: str, amount_rub: Decimal, label: str) -> str:
        base = "https://yoomoney.ru/quickpay/confirm.xml"
        params = {
            "receiver": (receiver or "").strip(),
            "quickpay-form": "donate",  # P2P форма для перевода на кошелек
            "targets": "Оплата подписки",
            "formcomment": "Оплата подписки",
            "short-dest": "Оплата подписки",
            "sum": f"{amount_rub:.2f}",
            "label": label,
            "successURL": f"https://t.me/{TELEGRAM_BOT_USERNAME}",
            # Не фиксируем paymentType, чтобы YooMoney сам предложил доступные способы (исключает ошибки у некоторых кошельков)
        }
        url = base + "?" + urlencode(params)
        return url

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_yoomoney")
    async def pay_yoomoney_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("Готовлю ссылку YooMoney...")
        data = await state.get_data()
        plan = get_plan_by_id(data.get('plan_id'))
        if not plan:
            await callback.message.edit_text("❌ Ошибка: Тариф не найден.")
            await state.clear()
            return
        wallet = get_setting("yoomoney_wallet")
        secret = get_setting("yoomoney_secret")
        if not wallet or not secret:
            await callback.message.edit_text("❌ YooMoney временно недоступен.")
            await state.clear()
            return
        # Валидация кошелька (обычно начинается с 410 и состоит из цифр)
        w = (wallet or "").strip()
        if not (w.isdigit() and len(w) >= 11):
            await callback.message.edit_text("❌ Некорректный номер кошелька YooMoney. Проверьте в панели настроек.")
            await state.clear()
            return
        price_rub = Decimal(str(data.get('final_price', plan['price'])))
        if price_rub < Decimal("1.00"):
            await callback.message.edit_text("❌ Минимальная сумма перевода YooMoney — 1 RUB. Выберите другой тариф или способ оплаты.")
            await state.clear()
            return
        user_id = callback.from_user.id
        payment_id = str(uuid.uuid4())
        metadata = {
            "user_id": user_id,
            "months": int(plan['months']),
            "price": float(price_rub),
            "action": data.get('action'),
            "key_id": data.get('key_id'),
            "host_name": data.get('host_name'),
            "plan_id": data.get('plan_id'),
            "customer_email": data.get('customer_email'),
            "payment_method": "YooMoney",
            "payment_id": payment_id,
        }
        create_payload_pending(payment_id, user_id, float(price_rub), metadata)
        pay_url = _build_yoomoney_link(wallet, price_rub, payment_id)
        await callback.message.edit_text(
            "Нажмите на кнопку ниже для оплаты:",
            reply_markup=keyboards.create_yoomoney_payment_keyboard(pay_url, payment_id)
        )
        await state.clear()

    @user_router.callback_query(TopUpProcess.waiting_for_topup_method, F.data == "topup_pay_yoomoney")
    async def topup_yoomoney_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("Готовлю YooMoney...")
        data = await state.get_data()
        amount_rub = Decimal(str(data.get('topup_amount', 0)))
        wallet = get_setting("yoomoney_wallet")
        secret = get_setting("yoomoney_secret")
        if not wallet or not secret or amount_rub <= 0:
            await callback.message.edit_text("❌ YooMoney временно недоступен.")
            await state.clear()
            return
        w = (wallet or "").strip()
        if not (w.isdigit() and len(w) >= 11):
            await callback.message.edit_text("❌ Некорректный номер кошелька YooMoney. Проверьте в панели настроек.")
            await state.clear()
            return
        if amount_rub < Decimal("1.00"):
            await callback.message.edit_text("❌ Минимальная сумма перевода YooMoney — 1 RUB. Введите сумму побольше.")
            await state.clear()
            return
        user_id = callback.from_user.id
        payment_id = str(uuid.uuid4())
        metadata = {
            "user_id": user_id,
            "price": float(amount_rub),
            "action": "top_up",
            "payment_method": "YooMoney",
            "payment_id": payment_id,
        }
        create_payload_pending(payment_id, user_id, float(amount_rub), metadata)
        pay_url = _build_yoomoney_link(wallet, amount_rub, payment_id)
        await callback.message.edit_text(
            "Нажмите на кнопку ниже для оплаты:",
            reply_markup=keyboards.create_yoomoney_payment_keyboard(pay_url, payment_id)
        )
        await state.clear()

    @user_router.callback_query(F.data.startswith("check_pending:"))
    async def check_pending_payment_handler(callback: types.CallbackQuery, bot: Bot):
        try:
            pid = callback.data.split(":", 1)[1]
        except Exception:
            await callback.answer("Некорректный идентификатор платежа.", show_alert=True)
            return
        try:
            status = get_pending_status(pid) or ""
        except Exception as e:
            logger.error(f"check_pending failed for {pid}: {e}")
            status = ""
        if status and status.lower() == 'paid':
            await callback.answer("✅ Оплата получена! Профиль/баланс скоро обновится.", show_alert=True)
            return

        # Если ещё pending — попробуем проверить через OAuth operation-history по метке
        token = (get_setting('yoomoney_api_token') or '').strip()
        if not token:
            # Нет токена для проверки — только локальный статус
            if not status:
                await callback.answer("❌ Платёж не найден. Проверьте позже.", show_alert=True)
            else:
                await callback.answer("⏳ Оплата ещё не поступила. Попробуйте через минуту.", show_alert=True)
            return

        try:
            async with aiohttp.ClientSession() as session:
                data = {"label": pid, "records": "10"}
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                }
                async with session.post("https://yoomoney.ru/api/operation-history", data=data, headers=headers, timeout=15) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        await callback.answer("⚠️ Не удалось проверить оплату через YooMoney. Попробуйте позже.", show_alert=True)
                        return
        except Exception:
            await callback.answer("⚠️ Ошибка связи с YooMoney. Попробуйте позже.", show_alert=True)
            return
        try:
            payload = json.loads(text)
        except Exception:
            payload = {}
        ops = payload.get('operations') or []
        paid = False
        for op in ops:
            try:
                if str(op.get('label')) == pid and str(op.get('status','')).lower() in {"success","done"}:
                    paid = True
                    break
            except Exception:
                continue
        if paid:
            try:
                metadata = find_and_complete_pending_transaction(pid)
            except Exception:
                metadata = None
            if metadata:
                try:
                    await process_successful_payment(bot, metadata)
                except Exception as e:
                    logger.warning(f"process_successful_payment failed after YM check: {e}")
            await callback.answer("✅ Оплата получена! Профиль/баланс скоро обновится.", show_alert=True)
            return
        # Иначе
        await callback.answer("⏳ Оплата ещё не поступила. Попробуйте через минуту.", show_alert=True)
    @user_router.callback_query(TopUpProcess.waiting_for_topup_method, F.data == "topup_pay_heleket")
    async def topup_pay_heleket_like(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("Создаю счёт...")
        data = await state.get_data()
        user_id = callback.from_user.id
        amount = float(data.get('topup_amount', 0))
        if amount <= 0:
            await callback.message.edit_text("❌ Некорректная сумма пополнения. Повторите ввод.")
            await state.clear()
            return
        # Сформируем state_data минимально необходимым
        state_data = {
            "action": "top_up",
            "customer_email": None,
            "plan_id": None,
            "host_name": None,
            "key_id": None,
        }
        try:
            pay_url = await _create_heleket_payment_request(
                user_id=user_id,
                price=float(amount),
                months=0,
                host_name="",
                state_data=state_data
            )
            if pay_url:
                await callback.message.edit_text(
                    "Нажмите на кнопку ниже для оплаты:",
                    reply_markup=keyboards.create_payment_keyboard(pay_url)
                )
                await state.clear()
            else:
                await callback.message.edit_text("❌ Не удалось создать счёт. Попробуйте другой способ оплаты.")
        except Exception as e:
            logger.error(f"Failed to create topup Heleket-like invoice: {e}", exc_info=True)
            await callback.message.edit_text("❌ Не удалось создать счёт. Попробуйте другой способ оплаты.")
            await state.clear()

    @user_router.callback_query(TopUpProcess.waiting_for_topup_method, F.data == "topup_pay_cryptobot")
    async def topup_pay_cryptobot(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("Создаю счёт в Crypto Pay...")
        data = await state.get_data()
        user_id = callback.from_user.id
        amount = float(data.get('topup_amount', 0))
        if amount <= 0:
            await callback.message.edit_text("❌ Некорректная сумма пополнения. Повторите ввод.")
            await state.clear()
            return
        state_data = {
            "action": "top_up",
            "customer_email": None,
            "plan_id": None,
            "host_name": None,
            "key_id": None,
        }
        try:
            result = await _create_cryptobot_invoice(
                user_id=user_id,
                price_rub=float(amount),
                months=0,
                host_name="",
                state_data=state_data,
            )
            if result:
                pay_url, invoice_id = result
                await callback.message.edit_text(
                    "Нажмите на кнопку ниже для оплаты:",
                    reply_markup=keyboards.create_cryptobot_payment_keyboard(pay_url, invoice_id)
                )
                await state.clear()
            else:
                await callback.message.edit_text("❌ Не удалось создать счёт в CryptoBot. Попробуйте другой способ оплаты.")
        except Exception as e:
            logger.error(f"Failed to create CryptoBot topup invoice: {e}", exc_info=True)
            await callback.message.edit_text("❌ Не удалось создать счёт в CryptoBot. Попробуйте другой способ оплаты.")
            await state.clear()

    @user_router.callback_query(TopUpProcess.waiting_for_topup_method, F.data == "topup_pay_tonconnect")
    async def topup_pay_tonconnect(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("Готовлю TON Connect...")
        data = await state.get_data()
        user_id = callback.from_user.id
        amount_rub = Decimal(str(data.get('topup_amount', 0)))
        if amount_rub <= 0:
            await callback.message.edit_text("❌ Некорректная сумма пополнения. Повторите ввод.")
            await state.clear()
            return

        wallet_address = get_setting("ton_wallet_address")
        if not wallet_address:
            await callback.message.edit_text("❌ Оплата через TON временно недоступна.")
            await state.clear()
            return

        usdt_rub_rate = await get_usdt_rub_rate()
        ton_usdt_rate = await get_ton_usdt_rate()
        if not usdt_rub_rate or not ton_usdt_rate:
            await callback.message.edit_text("❌ Не удалось получить курс TON. Попробуйте позже.")
            await state.clear()
            return

        price_ton = (amount_rub / usdt_rub_rate / ton_usdt_rate).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        amount_nanoton = int(price_ton * 1_000_000_000)

        payment_id = str(uuid.uuid4())
        metadata = {
            "user_id": user_id,
            "price": float(amount_rub),
            "action": "top_up",
            "payment_method": "TON Connect"
        }
        create_pending_transaction(payment_id, user_id, float(amount_rub), metadata)

        transaction_payload = {
            'messages': [{'address': wallet_address, 'amount': str(amount_nanoton), 'payload': payment_id}],
            'valid_until': int(datetime.now().timestamp()) + 600
        }

        try:
            connect_url = await _start_ton_connect_process(user_id, transaction_payload)
            qr_img = qrcode.make(connect_url)
            bio = BytesIO(); qr_img.save(bio, "PNG"); qr_file = BufferedInputFile(bio.getvalue(), "ton_qr.png")
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.answer_photo(
                photo=qr_file,
                caption=(
                    f"💎 Оплата через TON Connect\n\n"
                    f"Сумма к оплате: `{price_ton}` TON\n\n"
                    f"Нажмите кнопку ниже, чтобы открыть кошелёк и подтвердить перевод."
                ),
                reply_markup=keyboards.create_ton_connect_keyboard(connect_url)
            )
            await state.clear()
        except Exception as e:
            logger.error(f"Failed to start TON Connect topup: {e}", exc_info=True)
            await callback.message.edit_text("❌ Не удалось подготовить оплату TON Connect.")
            await state.clear()

    @user_router.callback_query(F.data == "show_referral_program")
    @registration_required
    async def referral_program_handler(callback: types.CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        user_data = get_user(user_id)
        bot_username = (await callback.bot.get_me()).username
        
        referral_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        referral_count = get_referral_count(user_id)
        try:
            total_ref_earned = float(get_referral_balance_all(user_id))
        except Exception:
            total_ref_earned = 0.0
        text = (
            "🤝 <b>Реферальная программа</b>\n\n"
            f"<b>Ваша реферальная ссылка:</b>\n<code>{referral_link}</code>\n\n"
            f"<b>Приглашено пользователей:</b> {referral_count}\n"
            f"<b>Заработано по рефералке:</b> {total_ref_earned:.2f} RUB"
        )

        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="back_to_main_menu")
        await callback.message.edit_text(
            text, reply_markup=builder.as_markup()
        )


    @user_router.callback_query(F.data == "show_about")
    @registration_required
    async def about_handler(callback: types.CallbackQuery):
        await callback.answer()
        
        about_text = get_setting("about_text")
        terms_url = get_setting("terms_url")
        privacy_url = get_setting("privacy_url")
        channel_url = get_setting("channel_url")

        final_text = about_text if about_text else "Информация о проекте не добавлена."

        keyboard = keyboards.create_about_keyboard(channel_url, terms_url, privacy_url)

        await callback.message.edit_text(
            final_text,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )

    # --- User: Просмотр последних результатов Speedtest (SSH-цели) ---
    @user_router.callback_query(F.data == "user_speedtest_last")
    @registration_required
    async def user_speedtest_last_handler(callback: types.CallbackQuery):
        await callback.answer()
        try:
            targets = rw_repo.get_all_ssh_targets() or []
        except Exception:
            targets = []
        lines = []
        for t in targets:
            name = (t.get('target_name') or '').strip()
            if not name:
                continue
            try:
                last = rw_repo.get_latest_speedtest(name)
            except Exception:
                last = None
            if not last:
                lines.append(f"• <b>{name}</b>: данных нет")
                continue
            ping = last.get('ping_ms')
            down = last.get('download_mbps')
            up = last.get('upload_mbps')
            ok_badge = '✅' if last.get('ok') else '❌'
            ping_s = f"{float(ping):.2f}" if isinstance(ping, (int, float)) else '—'
            down_s = f"{float(down):.0f}" if isinstance(down, (int, float)) else '—'
            up_s = f"{float(up):.0f}" if isinstance(up, (int, float)) else '—'
            ts_raw = last.get('created_at') or ''
            ts_s = ''
            if ts_raw:
                try:
                    dt = datetime.fromisoformat(str(ts_raw).replace('Z', '+00:00'))
                    # Только день и время (без года), например: 22.09 14:35
                    ts_s = dt.strftime('%d.%m %H:%M')
                except Exception:
                    ts_s = str(ts_raw)
            # Красивый и короткий вывод: только задержка, скорости и время
            lines.append(
                f"• <b>{name}</b> — SSH: {ok_badge} · ⏱ {ping_s} ms · ↓ {down_s} Mbps · ↑ {up_s} Mbps · 🕒 {ts_s}"
            )
        text = (
            "⚡ <b>Последние результаты Speedtest</b>\n"
            + ("\n".join(lines) if lines else "(цели не настроены)")
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ В меню", callback_data="back_to_main_menu")
        try:
            await callback.message.edit_text(text, reply_markup=kb.as_markup())
        except Exception:
            await callback.message.answer(text, reply_markup=kb.as_markup())

    @user_router.callback_query(F.data == "show_help")
    @registration_required
    async def about_handler(callback: types.CallbackQuery):
        await callback.answer()
        support_bot_username = get_setting("support_bot_username")
        support_text = get_setting("support_text") or "Раздел поддержки. Нажмите кнопку ниже, чтобы открыть чат с поддержкой."
        if support_bot_username:
            await callback.message.edit_text(
                support_text,
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username)
            )
        else:
            support_user = get_setting("support_user")
            if support_user:
                await callback.message.edit_text(
                    "Для связи с поддержкой используйте кнопку ниже.",
                    reply_markup=keyboards.create_support_keyboard(support_user)
                )
            else:
                await callback.message.edit_text("Контакты поддержки не настроены.", reply_markup=keyboards.create_back_to_menu_keyboard())

    @user_router.callback_query(F.data == "support_menu")
    @registration_required
    async def support_menu_handler(callback: types.CallbackQuery):
        await callback.answer()
        support_bot_username = get_setting("support_bot_username")
        support_text = get_setting("support_text") or "Раздел поддержки. Нажмите кнопку ниже, чтобы открыть чат с поддержкой."
        if support_bot_username:
            await callback.message.edit_text(
                support_text,
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username)
            )
        else:
            support_user = get_setting("support_user")
            if support_user:
                await callback.message.edit_text(
                    "Для связи с поддержкой используйте кнопку ниже.",
                    reply_markup=keyboards.create_support_keyboard(support_user)
                )
            else:
                await callback.message.edit_text("Контакты поддержки не настроены.", reply_markup=keyboards.create_back_to_menu_keyboard())

    @user_router.callback_query(F.data == "support_external")
    @registration_required
    async def support_external_handler(callback: types.CallbackQuery):
        await callback.answer()
        support_bot_username = get_setting("support_bot_username")
        if support_bot_username:
            await callback.message.edit_text(
                get_setting("support_text") or "Раздел поддержки.",
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username)
            )
            return
        support_user = get_setting("support_user")
        if not support_user:
            await callback.message.edit_text("Внешний контакт поддержки не настроен.", reply_markup=keyboards.create_back_to_menu_keyboard())
            return
        await callback.message.edit_text(
            "Для связи с поддержкой используйте кнопку ниже.",
            reply_markup=keyboards.create_support_keyboard(support_user)
        )

    @user_router.callback_query(F.data == "support_new_ticket")
    @registration_required
    async def support_new_ticket_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        support_bot_username = get_setting("support_bot_username")
        if support_bot_username:
            await callback.message.edit_text(
                "Раздел поддержки вынесен в отдельного бота.",
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username)
            )
        else:
            await callback.message.edit_text("Контакты поддержки не настроены.", reply_markup=keyboards.create_back_to_menu_keyboard())

    @user_router.message(SupportDialog.waiting_for_subject)
    @registration_required
    async def support_subject_received(message: types.Message, state: FSMContext):
        await state.clear()
        support_bot_username = get_setting("support_bot_username")
        if support_bot_username:
            await message.answer(
                "Создание тикетов доступно в отдельном боте поддержки.",
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username)
            )
        else:
            await message.answer("Контакты поддержки не настроены.")

    @user_router.message(SupportDialog.waiting_for_message)
    @registration_required
    async def support_message_received(message: types.Message, state: FSMContext, bot: Bot):
        await state.clear()
        support_bot_username = get_setting("support_bot_username")
        if support_bot_username:
            await message.answer(
                "Создание тикетов доступно в отдельном боте поддержки.",
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username)
            )
        else:
            await message.answer("Контакты поддержки не настроены.")

    @user_router.callback_query(F.data == "support_my_tickets")
    @registration_required
    async def support_my_tickets_handler(callback: types.CallbackQuery):
        await callback.answer()
        support_bot_username = get_setting("support_bot_username")
        if support_bot_username:
            await callback.message.edit_text(
                "Список обращений доступен в отдельном боте поддержки.",
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username)
            )
        else:
            await callback.message.edit_text("Контакты поддержки не настроены.", reply_markup=keyboards.create_back_to_menu_keyboard())

    @user_router.callback_query(F.data.startswith("support_view_"))
    @registration_required
    async def support_view_ticket_handler(callback: types.CallbackQuery):
        await callback.answer()
        support_bot_username = get_setting("support_bot_username")
        if support_bot_username:
            await callback.message.edit_text(
                "Просмотр тикетов доступен в отдельном боте поддержки.",
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username)
            )
        else:
            await callback.message.edit_text("Контакты поддержки не настроены.", reply_markup=keyboards.create_back_to_menu_keyboard())

    @user_router.callback_query(F.data.startswith("support_reply_"))
    @registration_required
    async def support_reply_prompt_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.clear()
        support_bot_username = get_setting("support_bot_username")
        if support_bot_username:
            await callback.message.edit_text(
                "Отправка ответов доступна в отдельном боте поддержки.",
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username)
            )
        else:
            await callback.message.edit_text("Контакты поддержки не настроены.", reply_markup=keyboards.create_back_to_menu_keyboard())

    @user_router.message(SupportDialog.waiting_for_reply)
    @registration_required
    async def support_reply_received(message: types.Message, state: FSMContext, bot: Bot):
        await state.clear()
        support_bot_username = get_setting("support_bot_username")
        if support_bot_username:
            await message.answer(
                "Отправка ответов доступна в отдельном боте поддержки.",
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username)
            )
        else:
            await message.answer("Контакты поддержки не настроены.")

    @user_router.message(F.is_topic_message == True)
    async def forum_thread_message_handler(message: types.Message, bot: Bot):
        try:
            support_bot_username = get_setting("support_bot_username")
            me = await bot.get_me()
            if support_bot_username and (me.username or "").lower() != support_bot_username.lower():
                return
            if not message.message_thread_id:
                return
            forum_chat_id = message.chat.id
            thread_id = message.message_thread_id
            ticket = get_ticket_by_thread(str(forum_chat_id), int(thread_id))
            if not ticket:
                return
            user_id = int(ticket.get('user_id'))
            if message.from_user and message.from_user.id == me.id:
                return
            # Проверка многоадминная
            is_admin_by_setting = is_admin(message.from_user.id)
            is_admin_in_chat = False
            try:
                member = await bot.get_chat_member(chat_id=forum_chat_id, user_id=message.from_user.id)
                is_admin_in_chat = member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
            except Exception:
                pass
            if not (is_admin_by_setting or is_admin_in_chat):
                return
            content = (message.text or message.caption or "").strip()
            if content:
                add_support_message(ticket_id=int(ticket['ticket_id']), sender='admin', content=content)
            header = await bot.send_message(
                chat_id=user_id,
                text=f"💬 Ответ поддержки по тикету #{ticket['ticket_id']}"
            )
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                    reply_to_message_id=header.message_id
                )
            except Exception:
                if content:
                    await bot.send_message(chat_id=user_id, text=content)
        except Exception as e:
            logger.warning(f"Failed to relay forum thread message: {e}")

    @user_router.callback_query(F.data.startswith("support_close_"))
    @registration_required
    async def support_close_ticket_handler(callback: types.CallbackQuery):
        await callback.answer()
        support_bot_username = get_setting("support_bot_username")
        if support_bot_username:
            await callback.message.edit_text(
                "Управление тикетами доступно в отдельном боте поддержки.",
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username)
            )
            return
        await callback.message.edit_text("Контакты поддержки не настроены.", reply_markup=keyboards.create_back_to_menu_keyboard())

    @user_router.callback_query(F.data == "manage_keys")
    @registration_required
    async def manage_keys_handler(callback: types.CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        user_keys = get_user_keys(user_id)
        await callback.message.edit_text(
            "Ваши ключи:" if user_keys else "У вас пока нет ключей.",
            reply_markup=keyboards.create_keys_management_keyboard(user_keys)
        )

    @user_router.callback_query(F.data == "get_trial")
    @registration_required
    async def trial_period_handler(callback: types.CallbackQuery, state: FSMContext):
        user_id = callback.from_user.id
        user_db_data = get_user(user_id)
        if user_db_data and user_db_data.get('trial_used'):
            await callback.answer("Вы уже использовали бесплатный пробный период.", show_alert=True)
            return

        hosts = get_all_hosts()
        if not hosts:
            await callback.message.edit_text("❌ В данный момент нет доступных серверов для создания пробного ключа.")
            return
            
        if len(hosts) == 1:
            await callback.answer()
            await process_trial_key_creation(callback.message, hosts[0]['host_name'])
        else:
            await callback.answer()
            await callback.message.edit_text(
                "Выберите сервер, на котором хотите получить пробный ключ:",
                reply_markup=keyboards.create_host_selection_keyboard(hosts, action="trial")
            )

    @user_router.callback_query(F.data.startswith("select_host_trial_"))
    @registration_required
    async def trial_host_selection_handler(callback: types.CallbackQuery):
        await callback.answer()
        host_name = callback.data[len("select_host_trial_"):]
        await process_trial_key_creation(callback.message, host_name)

    async def process_trial_key_creation(message: types.Message, host_name: str):
        user_id = message.chat.id
        await message.edit_text(f"Отлично! Создаю для вас бесплатный ключ на {get_setting('trial_duration_days')} дня на сервере \"{host_name}\"...")

        try:
            # email: trial_{username}@bot.local с авто-суффиксом при коллизиях
            user_data = get_user(user_id) or {}
            raw_username = (user_data.get('username') or f'user{user_id}').lower()
            username_slug = re.sub(r"[^a-z0-9._-]", "_", raw_username).strip("_")[:16] or f"user{user_id}"
            base_local = f"trial_{username_slug}"
            candidate_local = base_local
            attempt = 1
            while True:
                candidate_email = f"{candidate_local}@bot.local"
                if not rw_repo.get_key_by_email(candidate_email):
                    break
                attempt += 1
                candidate_local = f"{base_local}-{attempt}"
                if attempt > 100:
                    candidate_local = f"{base_local}-{int(datetime.now().timestamp())}"
                    candidate_email = f"{candidate_local}@bot.local"
                    break

            result = await remnawave_api.create_or_update_key_on_host(
                host_name=host_name,
                email=candidate_email,
                days_to_add=int(get_setting("trial_duration_days"))
            )
            if not result:
                await message.edit_text("❌ Не удалось создать пробный ключ. Ошибка на сервере.")
                return

            set_trial_used(user_id)
            
            new_key_id = rw_repo.record_key_from_payload(
                user_id=user_id,
                payload=result,
                host_name=host_name,
            )
            
            await message.delete()
            new_expiry_date = datetime.fromtimestamp(result['expiry_timestamp_ms'] / 1000)
            final_text = get_purchase_success_text("new", get_next_key_number(user_id) -1, new_expiry_date, result['connection_string'])
            await message.answer(text=final_text, reply_markup=keyboards.create_key_info_keyboard(new_key_id))

        except Exception as e:
            logger.error(f"Error creating trial key for user {user_id} on host {host_name}: {e}", exc_info=True)
            await message.edit_text("❌ Произошла ошибка при создании пробного ключа.")

    @user_router.callback_query(F.data.startswith("show_key_"))
    @registration_required
    async def show_key_handler(callback: types.CallbackQuery):
        key_id_to_show = int(callback.data.split("_")[2])
        await callback.message.edit_text("Загружаю информацию о ключе...")
        user_id = callback.from_user.id
        key_data = rw_repo.get_key_by_id(key_id_to_show)

        if not key_data or key_data['user_id'] != user_id:
            await callback.message.edit_text("❌ Ошибка: ключ не найден.")
            return
            
        try:
            details = await remnawave_api.get_key_details_from_host(key_data)
            if not details or not details['connection_string']:
                await callback.message.edit_text("❌ Ошибка на сервере. Не удалось получить данные ключа.")
                return

            connection_string = details['connection_string']
            expiry_date = datetime.fromisoformat(key_data['expiry_date'])
            created_date = datetime.fromisoformat(key_data['created_date'])
            
            all_user_keys = get_user_keys(user_id)
            key_number = next((i + 1 for i, key in enumerate(all_user_keys) if key['key_id'] == key_id_to_show), 0)
            
            final_text = get_key_info_text(key_number, expiry_date, created_date, connection_string)
            
            await callback.message.edit_text(
                text=final_text,
                reply_markup=keyboards.create_key_info_keyboard(key_id_to_show)
            )
        except Exception as e:
            logger.error(f"Error showing key {key_id_to_show}: {e}")
            await callback.message.edit_text("❌ Произошла ошибка при получении данных ключа.")

    @user_router.callback_query(F.data.startswith("switch_server_"))
    @registration_required
    async def switch_server_start(callback: types.CallbackQuery):
        await callback.answer()
        try:
            key_id = int(callback.data[len("switch_server_"):])
        except ValueError:
            await callback.answer("Некорректный идентификатор ключа.", show_alert=True)
            return

        key_data = rw_repo.get_key_by_id(key_id)
        if not key_data or key_data.get('user_id') != callback.from_user.id:
            await callback.answer("Ключ не найден.", show_alert=True)
            return

        hosts = get_all_hosts()
        if not hosts:
            await callback.answer("Нет доступных серверов.", show_alert=True)
            return

        current_host = key_data.get('host_name')
        hosts = [h for h in hosts if h.get('host_name') != current_host]
        if not hosts:
            await callback.answer("Другие серверы отсутствуют.", show_alert=True)
            return

        await callback.message.edit_text(
            "Выберите новый сервер (локацию) для этого ключа:",
            reply_markup=keyboards.create_host_selection_keyboard(hosts, action=f"switch_{key_id}")
        )

    @user_router.callback_query(F.data.startswith("select_host_switch_"))
    @registration_required
    async def select_host_for_switch(callback: types.CallbackQuery):
        await callback.answer()
        payload = callback.data[len("select_host_switch_"):]
        parts = payload.split("_", 1)
        if len(parts) != 2:
            await callback.answer("Некорректные данные выбора сервера.", show_alert=True)
            return
        try:
            key_id = int(parts[0])
        except ValueError:
            await callback.answer("Некорректный идентификатор ключа.", show_alert=True)
            return
        new_host_name = parts[1]

        key_data = rw_repo.get_key_by_id(key_id)

        if not key_data or key_data.get('user_id') != callback.from_user.id:
            await callback.answer("Ключ не найден.", show_alert=True)
            return

        old_host = key_data.get('host_name')
        if not old_host:
            await callback.answer("Для ключа не указан текущий сервер.", show_alert=True)
            return
        if new_host_name == old_host:
            await callback.answer("Это уже текущий сервер.", show_alert=True)
            return

        # Точное сохранение срока действия при переносе (без увеличения времени)
        try:
            expiry_dt = datetime.fromisoformat(key_data['expiry_date'])
            expiry_timestamp_ms_exact = int(expiry_dt.timestamp() * 1000)
        except Exception:
            # Fallback: хотя бы 1 день, если дата в БД повреждена
            now_dt = datetime.now()
            expiry_timestamp_ms_exact = int((now_dt + timedelta(days=1)).timestamp() * 1000)

        await callback.message.edit_text(
            f"⏳ Переношу ключ на сервер \"{new_host_name}\"..."
        )

        email = key_data.get('key_email')
        try:
            # Передаём точный expiry_timestamp_ms, чтобы не увеличивать срок на панели при переносе
            result = await remnawave_api.create_or_update_key_on_host(
                new_host_name,
                email,
                days_to_add=None,
                expiry_timestamp_ms=expiry_timestamp_ms_exact
            )
            if not result:
                await callback.message.edit_text(
                    f"❌ Не удалось перенести ключ на сервер \"{new_host_name}\". Попробуйте позже."
                )
                return

            # Сначала удаляем на старом сервере, пока локально сохранен старый UUID по email
            try:
                await remnawave_api.delete_client_on_host(old_host, email)
            except Exception:
                pass

            # Затем обновляем локальную БД новым хостом и UUID
            update_key_host_and_info(
                key_id=key_id,
                new_host_name=new_host_name,
                new_remnawave_uuid=result['client_uuid'],
                new_expiry_ms=result['expiry_timestamp_ms']
            )

            # Показываем сразу обновлённые данные ключа
            try:
                updated_key = rw_repo.get_key_by_id(key_id)
                details = await remnawave_api.get_key_details_from_host(updated_key)
                if details and details.get('connection_string'):
                    connection_string = details['connection_string']
                    expiry_date = datetime.fromisoformat(updated_key['expiry_date'])
                    created_date = datetime.fromisoformat(updated_key['created_date'])
                    all_user_keys = get_user_keys(callback.from_user.id)
                    key_number = next((i + 1 for i, k in enumerate(all_user_keys) if k['key_id'] == key_id), 0)
                    final_text = get_key_info_text(key_number, expiry_date, created_date, connection_string)
                    await callback.message.edit_text(
                        text=final_text,
                        reply_markup=keyboards.create_key_info_keyboard(key_id)
                    )
                else:
                    # Fallback: показать сообщение об успехе
                    await callback.message.edit_text(
                        f"✅ Готово! Ключ перенесён на сервер \"{new_host_name}\".\n"
                        "Обновите подписку/конфиг в клиенте, если требуется.",
                        reply_markup=keyboards.create_back_to_menu_keyboard()
                    )
            except Exception:
                await callback.message.edit_text(
                    f"✅ Готово! Ключ перенесён на сервер \"{new_host_name}\".\n"
                    "Обновите подписку/конфиг в клиенте, если требуется.",
                    reply_markup=keyboards.create_back_to_menu_keyboard()
                )
        except Exception as e:
            logger.error(f"Error switching key {key_id} to host {new_host_name}: {e}", exc_info=True)
            await callback.message.edit_text(
                "❌ Произошла ошибка при переносе ключа. Попробуйте позже."
            )

    @user_router.callback_query(F.data.startswith("show_qr_"))
    @registration_required
    async def show_qr_handler(callback: types.CallbackQuery):
        await callback.answer("Генерирую QR-код...")
        key_id = int(callback.data.split("_")[2])
        key_data = rw_repo.get_key_by_id(key_id)
        if not key_data or key_data['user_id'] != callback.from_user.id: return
        
        try:
            details = await remnawave_api.get_key_details_from_host(key_data)
            if not details or not details['connection_string']:
                await callback.answer("Ошибка: Не удалось сгенерировать QR-код.", show_alert=True)
                return

            connection_string = details['connection_string']
            qr_img = qrcode.make(connection_string)
            bio = BytesIO(); qr_img.save(bio, "PNG"); bio.seek(0)
            qr_code_file = BufferedInputFile(bio.read(), filename="vpn_qr.png")
            await callback.message.answer_photo(photo=qr_code_file)
        except Exception as e:
            logger.error(f"Error showing QR for key {key_id}: {e}")

    @user_router.callback_query(F.data.startswith("howto_vless_"))
    @registration_required
    async def show_instruction_handler(callback: types.CallbackQuery):
        await callback.answer()
        key_id = int(callback.data.split("_")[2])

        intro_text = get_setting("howto_intro_text") or "Выберите вашу платформу для инструкции по подключению VLESS:"
        await callback.message.edit_text(
            intro_text,
            reply_markup=keyboards.create_howto_vless_keyboard_key(key_id),
            disable_web_page_preview=True
        )
    
    @user_router.callback_query(F.data.startswith("howto_vless"))
    @registration_required
    async def show_instruction_handler(callback: types.CallbackQuery):
        await callback.answer()

        intro_text = get_setting("howto_intro_text") or "Выберите вашу платформу для инструкции по подключению VLESS:"
        await callback.message.edit_text(
            intro_text,
            reply_markup=keyboards.create_howto_vless_keyboard(),
            disable_web_page_preview=True
        )

    @user_router.callback_query(F.data == "howto_android")
    @registration_required
    async def howto_android_handler(callback: types.CallbackQuery):
        await callback.answer()
        text = get_setting("howto_android_text") or (
            "<b>Подключение на Android</b>\n\n"
            "1. <b>Установите приложение V2RayTun:</b> Загрузите и установите приложение V2RayTun из Google Play Store.\n"
            "2. <b>Скопируйте свой ключ (vless://)</b> Перейдите в раздел «Моя подписка» в нашем боте и скопируйте свой ключ.\n"
            "3. <b>Импортируйте конфигурацию:</b>\n"
            "   • Откройте V2RayTun.\n"
            "   • Нажмите на значок + в правом нижнем углу.\n"
            "   • Выберите «Импортировать конфигурацию из буфера обмена» (или аналогичный пункт).\n"
            "4. <b>Выберите сервер:</b> Выберите появившийся сервер в списке.\n"
            "5. <b>Подключитесь к VPN:</b> Нажмите на кнопку подключения (значок «V» или воспроизведения). Возможно, потребуется разрешение на создание VPN-подключения.\n"
            "6. <b>Проверьте подключение:</b> После подключения проверьте свой IP-адрес, например, на https://whatismyipaddress.com/. Он должен отличаться от вашего реального IP."
        )
        await callback.message.edit_text(
            text,
            reply_markup=keyboards.create_howto_vless_keyboard(),
            disable_web_page_preview=True
        )

    @user_router.callback_query(F.data == "howto_ios")
    @registration_required
    async def howto_ios_handler(callback: types.CallbackQuery):
        await callback.answer()
        text = get_setting("howto_ios_text") or (
            "<b>Подключение на iOS (iPhone/iPad)</b>\n\n"
            "1. <b>Установите приложение V2RayTun:</b> Загрузите и установите приложение V2RayTun из App Store.\n"
            "2. <b>Скопируйте свой ключ (vless://):</b> Перейдите в раздел «Моя подписка» в нашем боте и скопируйте свой ключ.\n"
            "3. <b>Импортируйте конфигурацию:</b>\n"
            "   • Откройте V2RayTun.\n"
            "   • Нажмите на значок +.\n"
            "   • Выберите «Импортировать конфигурацию из буфера обмена» (или аналогичный пункт).\n"
            "4. <b>Выберите сервер:</b> Выберите появившийся сервер в списке.\n"
            "5. <b>Подключитесь к VPN:</b> Включите главный переключатель в V2RayTun. Возможно, потребуется разрешить создание VPN-подключения.\n"
            "6. <b>Проверьте подключение:</b> После подключения проверьте свой IP-адрес, например, на https://whatismyipaddress.com/. Он должен отличаться от вашего реального IP."
        )
        await callback.message.edit_text(
            text,
            reply_markup=keyboards.create_howto_vless_keyboard(),
            disable_web_page_preview=True
        )

    @user_router.callback_query(F.data == "howto_windows")
    @registration_required
    async def howto_windows_handler(callback: types.CallbackQuery):
        await callback.answer()
        text = get_setting("howto_windows_text") or (
            "<b>Подключение на Windows</b>\n\n"
            "1. <b>Установите приложение Nekoray:</b> Загрузите Nekoray с https://github.com/MatsuriDayo/Nekoray/releases. Выберите подходящую версию (например, Nekoray-x64.exe).\n"
            "2. <b>Распакуйте архив:</b> Распакуйте скачанный архив в удобное место.\n"
            "3. <b>Запустите Nekoray.exe:</b> Откройте исполняемый файл.\n"
            "4. <b>Скопируйте свой ключ (vless://)</b> Перейдите в раздел «Моя подписка» в нашем боте и скопируйте свой ключ.\n"
            "5. <b>Импортируйте конфигурацию:</b>\n"
            "   • В Nekoray нажмите «Сервер» (Server).\n"
            "   • Выберите «Импортировать из буфера обмена».\n"
            "   • Nekoray автоматически импортирует конфигурацию.\n"
            "6. <b>Обновите серверы (если нужно):</b> Если серверы не появились, нажмите «Серверы» → «Обновить все серверы».\n"
            "7. Сверху включите пункт 'Режим TUN' ('Tun Mode')\n"
            "8. <b>Выберите сервер:</b> В главном окне выберите появившийся сервер.\n"
            "9. <b>Подключитесь к VPN:</b> Нажмите «Подключить» (Connect).\n"
            "10. <b>Проверьте подключение:</b> Откройте браузер и проверьте IP на https://whatismyipaddress.com/. Он должен отличаться от вашего реального IP."
        )
        await callback.message.edit_text(
            text,
            reply_markup=keyboards.create_howto_vless_keyboard(),
            disable_web_page_preview=True
        )

    @user_router.callback_query(F.data == "howto_linux")
    @registration_required
    async def howto_linux_handler(callback: types.CallbackQuery):
        await callback.answer()
        text = get_setting("howto_linux_text") or (
            "<b>Подключение на Linux</b>\n\n"
            "1. <b>Скачайте и распакуйте Nekoray:</b> Перейдите на https://github.com/MatsuriDayo/Nekoray/releases и скачайте архив для Linux. Распакуйте его в удобную папку.\n"
            "2. <b>Запустите Nekoray:</b> Откройте терминал, перейдите в папку с Nekoray и выполните <code>./nekoray</code> (или используйте графический запуск, если доступен).\n"
            "3. <b>Скопируйте свой ключ (vless://)</b> Перейдите в раздел «Моя подписка» в нашем боте и скопируйте свой ключ.\n"
            "4. <b>Импортируйте конфигурацию:</b>\n"
            "   • В Nekoray нажмите «Сервер» (Server).\n"
            "   • Выберите «Импортировать из буфера обмена».\n"
            "   • Nekoray автоматически импортирует конфигурацию.\n"
            "5. <b>Обновите серверы (если нужно):</b> Если серверы не появились, нажмите «Серверы» → «Обновить все серверы».\n"
            "6. Сверху включите пункт 'Режим TUN' ('Tun Mode')\n"
            "7. <b>Выберите сервер:</b> В главном окне выберите появившийся сервер.\n"
            "8. <b>Подключитесь к VPN:</b> Нажмите «Подключить» (Connect).\n"
            "9. <b>Проверьте подключение:</b> Откройте браузер и проверьте IP на https://whatismyipaddress.com/. Он должен отличаться от вашего реального IP."
        )
        await callback.message.edit_text(
            text,
            reply_markup=keyboards.create_howto_vless_keyboard(),
            disable_web_page_preview=True
        )

    @user_router.callback_query(F.data == "buy_new_key")
    @registration_required
    async def buy_new_key_handler(callback: types.CallbackQuery):
        await callback.answer()
        hosts = get_all_hosts()
        if not hosts:
            await callback.message.edit_text("❌ В данный момент нет доступных серверов для покупки.")
            return
        
        await callback.message.edit_text(
            "Выберите сервер, на котором хотите приобрести ключ:",
            reply_markup=keyboards.create_host_selection_keyboard(hosts, action="new")
        )

    @user_router.callback_query(F.data.startswith("select_host_new_"))
    @registration_required
    async def select_host_for_purchase_handler(callback: types.CallbackQuery):
        await callback.answer()
        host_name = callback.data[len("select_host_new_"):]
        plans = get_plans_for_host(host_name)
        if not plans:
            await callback.message.edit_text(f"❌ Для сервера \"{host_name}\" не настроены тарифы.")
            return
        await callback.message.edit_text(
            "Выберите тариф для нового ключа:", 
            reply_markup=keyboards.create_plans_keyboard(plans, action="new", host_name=host_name)
        )

    @user_router.callback_query(F.data.startswith("extend_key_"))
    @registration_required
    async def extend_key_handler(callback: types.CallbackQuery):
        await callback.answer()

        try:
            key_id = int(callback.data.split("_")[2])
        except (IndexError, ValueError):
            await callback.message.edit_text("❌ Произошла ошибка. Неверный формат ключа.")
            return

        key_data = rw_repo.get_key_by_id(key_id)

        if not key_data or key_data['user_id'] != callback.from_user.id:
            await callback.message.edit_text("❌ Ошибка: Ключ не найден или не принадлежит вам.")
            return
        
        host_name = key_data.get('host_name')
        if not host_name:
            await callback.message.edit_text("❌ Ошибка: У этого ключа не указан сервер. Обратитесь в поддержку.")
            return

        plans = get_plans_for_host(host_name)

        if not plans:
            await callback.message.edit_text(
                f"❌ Извините, для сервера \"{host_name}\" в данный момент не настроены тарифы для продления."
            )
            return

        await callback.message.edit_text(
            f"Выберите тариф для продления ключа на сервере \"{host_name}\":",
            reply_markup=keyboards.create_plans_keyboard(
                plans=plans,
                action="extend",
                host_name=host_name,
                key_id=key_id
            )
        )

    @user_router.callback_query(F.data.startswith("buy_"))
    @registration_required
    async def plan_selection_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        
        parts = callback.data.split("_")[1:]
        action = parts[-2]
        key_id = int(parts[-1])
        plan_id = int(parts[-3])
        host_name = "_".join(parts[:-3])

        await state.update_data(
            action=action, key_id=key_id, plan_id=plan_id, host_name=host_name
        )
        
        await callback.message.edit_text(
            "📧 Пожалуйста, введите ваш email для отправки чека об оплате.\n\n"
            "Если вы не хотите указывать почту, нажмите кнопку ниже.",
            reply_markup=keyboards.create_skip_email_keyboard()
        )
        await state.set_state(PaymentProcess.waiting_for_email)

    @user_router.callback_query(PaymentProcess.waiting_for_email, F.data == "back_to_plans")
    async def back_to_plans_handler(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        await state.clear()
        action = (data.get('action') or '').strip()

        # Re-open the plans list depending on action
        if action == 'new':
            host_name = data.get('host_name') or ''
            if not host_name:
                await callback.message.edit_text(
                    "❌ Не удалось определить сервер. Вернитесь в меню.",
                    reply_markup=keyboards.create_back_to_menu_keyboard()
                )
                return
            plans = get_plans_for_host(host_name)
            if not plans:
                await callback.message.edit_text(f"❌ Для сервера \"{host_name}\" не настроены тарифы.")
                return
            await callback.message.edit_text(
                "Выберите тариф для нового ключа:",
                reply_markup=keyboards.create_plans_keyboard(plans, action="new", host_name=host_name)
            )
            return

        if action == 'extend':
            try:
                key_id = int(data.get('key_id') or 0)
            except Exception:
                key_id = 0
            if key_id <= 0:
                await callback.message.edit_text(
                    "❌ Не удалось определить ключ для продления.",
                    reply_markup=keyboards.create_back_to_menu_keyboard()
                )
                return
            key_data = rw_repo.get_key_by_id(key_id)
            if not key_data or key_data.get('user_id') != callback.from_user.id:
                await callback.message.edit_text("❌ Ошибка: Ключ не найден или не принадлежит вам.")
                return
            host_name = key_data.get('host_name')
            if not host_name:
                await callback.message.edit_text("❌ Ошибка: У этого ключа не указан сервер. Обратитесь в поддержку.")
                return
            plans = get_plans_for_host(host_name)
            if not plans:
                await callback.message.edit_text(
                    f"❌ Извините, для сервера \"{host_name}\" в данный момент не настроены тарифы для продления."
                )
                return
            await callback.message.edit_text(
                f"Выберите тариф для продления ключа на сервере \"{host_name}\":",
                reply_markup=keyboards.create_plans_keyboard(
                    plans=plans,
                    action="extend",
                    host_name=host_name,
                    key_id=key_id
                )
            )
            return

        # Fallback
        await back_to_main_menu_handler(callback)

    @user_router.message(PaymentProcess.waiting_for_email)
    async def process_email_handler(message: types.Message, state: FSMContext):
        if is_valid_email(message.text):
            await state.update_data(customer_email=message.text)
            await message.answer(f"✅ Email принят: {message.text}")

            # Показываем опции оплаты с учетом балансов и цены
            await show_payment_options(message, state)
            logger.info(f"User {message.chat.id}: State set to waiting_for_payment_method via show_payment_options")
        else:
            await message.answer("❌ Неверный формат email. Попробуйте еще раз.")

    @user_router.callback_query(PaymentProcess.waiting_for_email, F.data == "skip_email")
    async def skip_email_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.update_data(customer_email=None)

        # Показываем опции оплаты с учетом балансов и цены
        await show_payment_options(callback.message, state)
        logger.info(f"User {callback.from_user.id}: State set to waiting_for_payment_method via show_payment_options")

    async def show_payment_options(message: types.Message, state: FSMContext):
        data = await state.get_data()
        user_data = get_user(message.chat.id)
        plan = get_plan_by_id(data.get('plan_id'))
        
        if not plan:
            try:
                await message.edit_text("❌ Ошибка: Тариф не найден.")
            except TelegramBadRequest:
                await message.answer("❌ Ошибка: Тариф не найден.")
            await state.clear()
            return
        
        price = Decimal(str(plan['price']))
        final_price = price
        discount_applied = False
        message_text = CHOOSE_PAYMENT_METHOD_MESSAGE

        if user_data.get('referred_by') and user_data.get('total_spent', 0) == 0:
            discount_percentage_str = get_setting("referral_discount") or "0"
            discount_percentage = Decimal(discount_percentage_str)
            
            if discount_percentage > 0:
                discount_amount = (price * discount_percentage / 100).quantize(Decimal("0.01"))
                final_price = price - discount_amount

                message_text = (
                    f"🎉 Как приглашенному пользователю, на вашу первую покупку предоставляется скидка {discount_percentage_str}%!\n"
                    f"Старая цена: <s>{price:.2f} RUB</s>\n"
                    f"<b>Новая цена: {final_price:.2f} RUB</b>\n\n"
                ) + CHOOSE_PAYMENT_METHOD_MESSAGE

        await state.update_data(final_price=float(final_price))

        # Получаем основной баланс для показа кнопки оплаты с баланса
        try:
            main_balance = get_balance(message.chat.id)
        except Exception:
            main_balance = 0.0

        show_balance_btn = main_balance >= float(final_price)

        try:
            await message.edit_text(
                message_text,
                reply_markup=keyboards.create_payment_method_keyboard(
                    payment_methods=PAYMENT_METHODS,
                    action=data.get('action'),
                    key_id=data.get('key_id'),
                    show_balance=show_balance_btn,
                    main_balance=main_balance,
                    price=float(final_price)
                )
            )
        except TelegramBadRequest:
            await message.answer(
                message_text,
                reply_markup=keyboards.create_payment_method_keyboard(
                    payment_methods=PAYMENT_METHODS,
                    action=data.get('action'),
                    key_id=data.get('key_id'),
                    show_balance=show_balance_btn,
                    main_balance=main_balance,
                    price=float(final_price)
                )
            )
        await state.set_state(PaymentProcess.waiting_for_payment_method)
        
    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "back_to_email_prompt")
    async def back_to_email_prompt_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.message.edit_text(
            "📧 Пожалуйста, введите ваш email для отправки чека об оплате.\n\n"
            "Если вы не хотите указывать почту, нажмите кнопку ниже.",
            reply_markup=keyboards.create_skip_email_keyboard()
        )
        await state.set_state(PaymentProcess.waiting_for_email)

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_yookassa")
    async def create_yookassa_payment_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("Создаю ссылку на оплату...")
        
        data = await state.get_data()
        user_data = get_user(callback.from_user.id)
        
        plan_id = data.get('plan_id')
        plan = get_plan_by_id(plan_id)

        if not plan:
            await callback.message.answer("Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        base_price = Decimal(str(plan['price']))
        price_rub = base_price

        if user_data.get('referred_by') and user_data.get('total_spent', 0) == 0:
            discount_percentage_str = get_setting("referral_discount") or "0"
            discount_percentage = Decimal(discount_percentage_str)
            if discount_percentage > 0:
                discount_amount = (base_price * discount_percentage / 100).quantize(Decimal("0.01"))
                price_rub = base_price - discount_amount

        plan_id = data.get('plan_id')
        customer_email = data.get('customer_email')
        host_name = data.get('host_name')
        action = data.get('action')
        key_id = data.get('key_id')
        
        if not customer_email:
            customer_email = get_setting("receipt_email")

        plan = get_plan_by_id(plan_id)
        if not plan:
            await callback.message.answer("Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        months = plan['months']
        user_id = callback.from_user.id

        try:
            price_str_for_api = f"{price_rub:.2f}"
            price_float_for_metadata = float(price_rub)

            receipt = None
            if customer_email and is_valid_email(customer_email):
                receipt = {
                    "customer": {"email": customer_email},
                    "items": [{
                        "description": f"Подписка на {months} мес.",
                        "quantity": "1.00",
                        "amount": {"value": price_str_for_api, "currency": "RUB"},
                        "vat_code": "1",
                        "payment_subject": "service",
                        "payment_mode": "full_payment"
                    }]
                }
            payment_payload = {
                "amount": {"value": price_str_for_api, "currency": "RUB"},
                "confirmation": {"type": "redirect", "return_url": f"https://t.me/{TELEGRAM_BOT_USERNAME}"},
                "capture": True,
                "description": f"Подписка на {months} мес.",
                "metadata": {
                    "user_id": user_id, "months": months, "price": price_float_for_metadata, 
                    "action": action, "key_id": key_id, "host_name": host_name,
                    "plan_id": plan_id, "customer_email": customer_email,
                    "payment_method": "YooKassa"
                }
            }
            if receipt:
                payment_payload['receipt'] = receipt

            payment = Payment.create(payment_payload, uuid.uuid4())
            
            await state.clear()
            
            await callback.message.edit_text(
                "Нажмите на кнопку ниже для оплаты:",
                reply_markup=keyboards.create_payment_keyboard(payment.confirmation.confirmation_url)
            )
        except Exception as e:
            logger.error(f"Failed to create YooKassa payment: {e}", exc_info=True)
            await callback.message.answer("Не удалось создать ссылку на оплату.")
            await state.clear()

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_cryptobot")
    async def create_cryptobot_invoice_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("Создаю счет в Crypto Pay...")
        
        data = await state.get_data()
        user_data = get_user(callback.from_user.id)
        
        plan_id = data.get('plan_id')
        user_id = data.get('user_id', callback.from_user.id)
        customer_email = data.get('customer_email')
        host_name = data.get('host_name')
        action = data.get('action')
        key_id = data.get('key_id')

        cryptobot_token = get_setting('cryptobot_token')
        if not cryptobot_token:
            logger.error(f"Attempt to create Crypto Pay invoice failed for user {user_id}: cryptobot_token is not set.")
            await callback.message.edit_text("❌ Оплата криптовалютой временно недоступна. (Администратор не указал токен).")
            await state.clear()
            return

        plan = get_plan_by_id(plan_id)
        if not plan:
            logger.error(f"Attempt to create Crypto Pay invoice failed for user {user_id}: Plan with id {plan_id} not found.")
            await callback.message.edit_text("❌ Произошла ошибка при выборе тарифа.")
            await state.clear()
            return
        
        plan_id = data.get('plan_id')
        plan = get_plan_by_id(plan_id)

        if not plan:
            await callback.message.answer("Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        base_price = Decimal(str(plan['price']))
        price_rub_decimal = base_price

        if user_data.get('referred_by') and user_data.get('total_spent', 0) == 0:
            discount_percentage_str = get_setting("referral_discount") or "0"
            discount_percentage = Decimal(discount_percentage_str)
            if discount_percentage > 0:
                discount_amount = (base_price * discount_percentage / 100).quantize(Decimal("0.01"))
                price_rub_decimal = base_price - discount_amount
        months = plan['months']
        
        final_price_float = float(price_rub_decimal)

        result = await _create_cryptobot_invoice(
            user_id=callback.from_user.id,
            price_rub=final_price_float,
            months=plan['months'],
            host_name=data.get('host_name'),
            state_data=data
        )
        
        if result:
            pay_url, invoice_id = result
            await callback.message.edit_text(
                "Нажмите на кнопку ниже для оплаты:",
                reply_markup=keyboards.create_cryptobot_payment_keyboard(pay_url, invoice_id)
            )
            await state.clear()
        else:
            await callback.message.edit_text("❌ Не удалось создать счёт в CryptoBot. Попробуйте другой способ оплаты.")

    @user_router.callback_query(F.data.startswith("check_crypto_invoice:"))
    async def check_crypto_invoice_handler(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
        await callback.answer("Проверяю статус оплаты...")
        try:
            parts = (callback.data or "").split(":", 1)
            invoice_id_str = parts[1] if len(parts) > 1 else ""
            invoice_id = int(invoice_id_str)
        except Exception:
            await callback.message.answer("❌ Некорректный идентификатор инвойса.")
            return

        token = (get_setting("cryptobot_token") or "").strip()
        if not token:
            await callback.message.answer("❌ CryptoBot токен не задан.")
            return

        url = "https://pay.crypt.bot/api/getInvoices"
        headers = {
            "Crypto-Pay-API-Token": token,
            "Content-Type": "application/json",
        }
        body = {"invoice_ids": [invoice_id]}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=body, timeout=20) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error(f"CryptoBot getInvoices HTTP {resp.status}: {text}")
                        await callback.message.answer("⏳ Оплата ещё не поступила. Попробуйте позже.")
                        return
                    data = await resp.json(content_type=None)
        except Exception as e:
            logger.error(f"CryptoBot getInvoices failed: {e}", exc_info=True)
            await callback.message.answer("⏳ Не удалось проверить статус. Попробуйте позже.")
            return

        # Разбираем результат: возможные варианты структуры
        invoices = []
        if isinstance(data, dict) and data.get("ok"):
            res = data.get("result")
            if isinstance(res, dict) and isinstance(res.get("items"), list):
                invoices = res.get("items")
            elif isinstance(res, list):
                invoices = res

        if not invoices:
            await callback.message.answer("⏳ Оплата ещё не поступила. Попробуйте позже.")
            return

        inv = invoices[0]
        status = (inv.get("status") or inv.get("invoice_status") or "").lower()
        if status != "paid":
            await callback.message.answer("⏳ Оплата ещё не поступила. Попробуйте позже.")
            return

        payload_string = inv.get("payload")
        if not payload_string:
            await callback.message.answer("⚠️ Оплата получена, но отсутствует payload. Обратитесь в поддержку.")
            return

        # Парсим payload по тому же формату, что и вебхук в app.py
        p = payload_string.split(":")
        if len(p) < 9:
            await callback.message.answer("⚠️ Оплата получена, но формат данных некорректен. Обратитесь в поддержку.")
            return

        metadata = {
            "user_id": p[0],
            "months": p[1],
            "price": p[2],
            "action": p[3],
            "key_id": p[4],
            "host_name": p[5],
            "plan_id": p[6],
            "customer_email": (p[7] if p[7] != 'None' else None),
            "payment_method": p[8],
            "transaction_id": str(invoice_id),
        }

        try:
            await process_successful_payment(bot, metadata)
            await callback.message.answer("✅ Оплата получена! Профиль/баланс скоро обновится.")
        except Exception as e:
            logger.error(f"CryptoBot manual check: process_successful_payment failed: {e}", exc_info=True)
            await callback.message.answer("⚠️ Оплата получена, но обработка не завершена. Обратитесь в поддержку.")

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_tonconnect")
    async def create_ton_invoice_handler(callback: types.CallbackQuery, state: FSMContext):
        logger.info(f"User {callback.from_user.id}: Entered create_ton_invoice_handler.")
        data = await state.get_data()
        user_id = callback.from_user.id
        wallet_address = get_setting("ton_wallet_address")
        plan = get_plan_by_id(data.get('plan_id'))
        
        if not wallet_address or not plan:
            await callback.message.edit_text("❌ Оплата через TON временно недоступна.")
            await state.clear()
            return

        await callback.answer("Создаю ссылку и QR-код для TON Connect...")
            
        price_rub = Decimal(str(data.get('final_price', plan['price'])))

        usdt_rub_rate = await get_usdt_rub_rate()
        ton_usdt_rate = await get_ton_usdt_rate()

        if not usdt_rub_rate or not ton_usdt_rate:
            await callback.message.edit_text("❌ Не удалось получить курс TON. Попробуйте позже.")
            await state.clear()
            return

        price_ton = (price_rub / usdt_rub_rate / ton_usdt_rate).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        amount_nanoton = int(price_ton * 1_000_000_000)
        
        payment_id = str(uuid.uuid4())
        metadata = {
            "user_id": user_id, "months": plan['months'], "price": float(price_rub),
            "action": data.get('action'), "key_id": data.get('key_id'),
            "host_name": data.get('host_name'), "plan_id": data.get('plan_id'),
            "customer_email": data.get('customer_email'), "payment_method": "TON Connect"
        }
        create_pending_transaction(payment_id, user_id, float(price_rub), metadata)

        transaction_payload = {
            'messages': [{'address': wallet_address, 'amount': str(amount_nanoton), 'payload': payment_id}],
            'valid_until': int(datetime.now().timestamp()) + 600
        }

        try:
            connect_url = await _start_ton_connect_process(user_id, transaction_payload)
            
            qr_img = qrcode.make(connect_url)
            bio = BytesIO()
            qr_img.save(bio, "PNG")
            qr_file = BufferedInputFile(bio.getvalue(), "ton_qr.png")

            await callback.message.delete()
            await callback.message.answer_photo(
                photo=qr_file,
                caption=(
                    f"💎 **Оплата через TON Connect**\n\n"
                    f"Сумма к оплате: `{price_ton}` **TON**\n\n"
                    f"✅ **Способ 1 (на телефоне):** Нажмите кнопку **'Открыть кошелек'** ниже.\n"
                    f"✅ **Способ 2 (на компьютере):** Отсканируйте QR-код кошельком.\n\n"
                    f"После подключения кошелька подтвердите транзакцию."
                ),
                parse_mode="Markdown",
                reply_markup=keyboards.create_ton_connect_keyboard(connect_url)
            )
            await state.clear()

        except Exception as e:
            logger.error(f"Failed to generate TON Connect link for user {user_id}: {e}", exc_info=True)
            await callback.message.answer("❌ Не удалось создать ссылку для TON Connect. Попробуйте позже.")
            await state.clear()

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_balance")
    async def pay_with_main_balance_handler(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
        await callback.answer()
        data = await state.get_data()
        user_id = callback.from_user.id
        plan = get_plan_by_id(data.get('plan_id'))
        if not plan:
            await callback.message.edit_text("❌ Ошибка: Тариф не найден.")
            await state.clear()
            return
        months = int(plan['months'])
        price = float(data.get('final_price', plan['price']))

        # Пытаемся списать средства с основного баланса
        if not deduct_from_balance(user_id, price):
            await callback.answer("Недостаточно средств на основном балансе.", show_alert=True)
            return

        metadata = {
            "user_id": user_id,
            "months": months,
            "price": price,
            "action": data.get('action'),
            "key_id": data.get('key_id'),
            "host_name": data.get('host_name'),
            "plan_id": data.get('plan_id'),
            "customer_email": data.get('customer_email'),
            "payment_method": "Balance",
            "chat_id": callback.message.chat.id,
            "message_id": callback.message.message_id
        }

        await state.clear()
        await process_successful_payment(bot, metadata)

    

    return user_router

async def notify_admin_of_purchase(bot: Bot, metadata: dict):
    try:
        admin_id_raw = get_setting("admin_telegram_id")
        if not admin_id_raw:
            return
        admin_id = int(admin_id_raw)
        user_id = metadata.get('user_id')
        host_name = metadata.get('host_name')
        months = metadata.get('months')
        price = metadata.get('price')
        action = metadata.get('action')
        payment_method = metadata.get('payment_method') or 'Unknown'
        # Локализация методов оплаты для уведомления админу
        payment_method_map = {
            'Balance': 'Баланс',
            'Card': 'Карта',
            'Crypto': 'Крипто',
            'USDT': 'USDT',
            'TON': 'TON',
        }
        payment_method_display = payment_method_map.get(payment_method, payment_method)
        plan_id = metadata.get('plan_id')
        plan = get_plan_by_id(plan_id)
        plan_name = plan.get('plan_name', 'Unknown') if plan else 'Unknown'

        text = (
            "📥 Новая оплата\n"
            f"👤 Пользователь: {user_id}\n"
            f"🗺️ Хост: {host_name}\n"
            f"📦 Тариф: {plan_name} ({months} мес.)\n"
            f"💳 Метод: {payment_method_display}\n"
            f"💰 Сумма: {float(price):.2f} RUB\n"
            f"⚙️ Действие: {'Новый ключ' if action == 'new' else 'Продление'}"
        )
        await bot.send_message(admin_id, text)
    except Exception as e:
        logger.warning(f"notify_admin_of_purchase failed: {e}")

async def process_successful_payment(bot: Bot, metadata: dict):
    try:
        action = metadata.get('action')
        user_id = int(metadata.get('user_id'))
        price = float(metadata.get('price'))
        # Поля ниже нужны только для покупок ключей/продлений
        def _to_int(val, default=0):
            try:
                if val in (None, '', 'None', 'null'):
                    return default
                return int(val)
            except (ValueError, TypeError):
                return default

        months = _to_int(metadata.get('months'), 0)
        key_id = _to_int(metadata.get('key_id'), 0)
        host_name = metadata.get('host_name', '')
        plan_id = _to_int(metadata.get('plan_id'), 0)
        customer_email = metadata.get('customer_email')
        payment_method = metadata.get('payment_method')

        chat_id_to_delete = metadata.get('chat_id')
        message_id_to_delete = metadata.get('message_id')
        
    except (ValueError, TypeError) as e:
        logger.error(f"FATAL: Could not parse metadata. Error: {e}. Metadata: {metadata}")
        return

    if chat_id_to_delete and message_id_to_delete:
        try:
            await bot.delete_message(chat_id=chat_id_to_delete, message_id=message_id_to_delete)
        except TelegramBadRequest as e:
            logger.warning(f"Could not delete payment message: {e}")

    # Спец-ветка: пополнение баланса
    if action == "top_up":
        try:
            ok = add_to_balance(user_id, float(price))
        except Exception as e:
            logger.error(f"Failed to add to balance for user {user_id}: {e}", exc_info=True)
            ok = False
        # Лог транзакции
        try:
            # Предпочитаем username из metadata (может быть актуальнее)
            log_username = (metadata.get('tg_username') or '').strip() if isinstance(metadata, dict) else ''
            if not log_username:
                user_info = get_user(user_id)
                log_username = (user_info.get('username') if user_info else '') or f"@{user_id}"
            log_transaction(
                username=log_username,
                transaction_id=None,
                payment_id=str(uuid.uuid4()),
                user_id=user_id,
                status='paid',
                amount_rub=float(price),
                amount_currency=None,
                currency_name=None,
                payment_method=payment_method or 'Unknown',
                metadata=json.dumps({"action": "top_up"})
            )
        except Exception:
            pass

        # Реферальное вознаграждение за пополнение баланса (только внешние оплаты)
        try:
            pm_for_ref = (payment_method or '').strip().lower()
            if pm_for_ref == 'balance':
                logger.info(f"Referral(top_up): skip accrual for user {user_id} because top-up was made from internal balance.")
            else:
                user_data = get_user(user_id) or {}
                referrer_id = user_data.get('referred_by')
                if referrer_id:
                    try:
                        referrer_id = int(referrer_id)
                    except Exception:
                        logger.warning(f"Referral(top_up): invalid referrer_id={referrer_id} for user {user_id}")
                        referrer_id = None
                if referrer_id:
                    try:
                        reward_type = (get_setting("referral_reward_type") or "percent_purchase").strip()
                    except Exception:
                        reward_type = "percent_purchase"
                    reward = Decimal("0")
                    if reward_type == "fixed_start_referrer":
                        reward = Decimal("0")
                    elif reward_type == "fixed_purchase":
                        try:
                            amount_raw = get_setting("fixed_referral_bonus_amount") or "50"
                            reward = Decimal(str(amount_raw)).quantize(Decimal("0.01"))
                        except Exception:
                            reward = Decimal("50.00")
                    else:
                        # percent_purchase
                        try:
                            percentage = Decimal(get_setting("referral_percentage") or "0")
                        except Exception:
                            percentage = Decimal("0")
                        reward = (Decimal(str(price)) * percentage / 100).quantize(Decimal("0.01"))
                    logger.info(f"Referral(top_up): user={user_id}, referrer={referrer_id}, type={reward_type}, reward={float(reward):.2f}")
                    if float(reward) > 0:
                        try:
                            ok_ref = add_to_balance(referrer_id, float(reward))
                        except Exception as e:
                            logger.warning(f"Referral(top_up): add_to_balance failed for referrer {referrer_id}: {e}")
                            ok_ref = False
                        try:
                            add_to_referral_balance_all(referrer_id, float(reward))
                        except Exception as e:
                            logger.warning(f"Referral(top_up): failed to increment referral_balance_all for {referrer_id}: {e}")
                        referrer_username = user_data.get('username', 'пользователь')
                        if ok_ref:
                            try:
                                await bot.send_message(
                                    chat_id=referrer_id,
                                    text=(
                                        "💰 Вам начислено реферальное вознаграждение за пополнение баланса!\n"
                                        f"Пользователь: {referrer_username} (ID: {user_id})\n"
                                        f"Сумма: {float(reward):.2f} RUB"
                                    )
                                )
                            except Exception as e:
                                logger.warning(f"Referral(top_up): could not send reward notification to {referrer_id}: {e}")
        except Exception as e:
            logger.warning(f"Referral(top_up): unexpected error while processing reward for user {user_id}: {e}")

        try:
            current_balance = 0.0
            try:
                current_balance = float(get_balance(user_id))
            except Exception:
                pass
            if ok:
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"✅ Оплата получена!\n"
                        f"💼 Баланс пополнен на {float(price):.2f} RUB.\n"
                        f"Текущий баланс: {current_balance:.2f} RUB."
                    ),
                    reply_markup=keyboards.create_profile_keyboard()
                )
            else:
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        "⚠️ Оплата получена, но не удалось обновить баланс. "
                        "Обратитесь в поддержку."
                    ),
                    reply_markup=keyboards.create_support_keyboard()
                )
        except Exception:
            pass
        # Админ-уведомление о пополнении (по возможности)
        try:
            admins = [u for u in (get_all_users() or []) if is_admin(u.get('telegram_id') or 0)]
            for a in admins:
                admin_id = a.get('telegram_id')
                if admin_id:
                    await bot.send_message(admin_id, f"📥 Пополнение: пользователь {user_id}, сумма {float(price):.2f} RUB")
        except Exception:
            pass
        return

    processing_message = await bot.send_message(
        chat_id=user_id,
        text=f"✅ Оплата получена! Обрабатываю ваш запрос на сервере \"{host_name}\"..."
    )
    try:
        email = ""
        # Цена нужна ниже вне зависимости от ветки
        price = float(metadata.get('price'))
        result = None
        # Определяем email для операции и вызываем панель для обеих веток (new/extend)
        if action == "new":
            # Сформируем email в формате {username}@bot.local с авто-суффиксом при коллизиях
            user_data = get_user(user_id) or {}
            raw_username = (user_data.get('username') or f'user{user_id}').lower()
            username_slug = re.sub(r"[^a-z0-9._-]", "_", raw_username).strip("_")[:16] or f"user{user_id}"
            base_local = f"{username_slug}"
            candidate_local = base_local
            attempt = 1
            while True:
                candidate_email = f"{candidate_local}@bot.local"
                if not rw_repo.get_key_by_email(candidate_email):
                    break
                attempt += 1
                candidate_local = f"{base_local}-{attempt}"
                if attempt > 100:
                    candidate_local = f"{base_local}-{int(datetime.now().timestamp())}"
                    candidate_email = f"{candidate_local}@bot.local"
                    break
        else:
            # Продление существующего ключа — достаём email по key_id
            existing_key = rw_repo.get_key_by_id(key_id)
            if not existing_key or not existing_key.get('key_email'):
                await processing_message.edit_text("❌ Не удалось найти ключ для продления.")
                return
            candidate_email = existing_key['key_email']

        result = await remnawave_api.create_or_update_key_on_host(
            host_name=host_name,
            email=candidate_email,
            days_to_add=int(months * 30)
        )
        if not result:
            await processing_message.edit_text("❌ Не удалось создать/обновить ключ на панели Remnawave.")
            return

        if action == "new":
            key_id = rw_repo.record_key_from_payload(
                user_id=user_id,
                payload=result,
                host_name=host_name,
            )
            if not key_id:
                await processing_message.edit_text("❌ Не удалось сохранить ключ. Попробуйте позже.")
                return
        elif action == "extend":
            if not rw_repo.update_key(
                key_id,
                remnawave_user_uuid=result['client_uuid'],
                expire_at_ms=result['expiry_timestamp_ms'],
            ):
                await processing_message.edit_text("❌ Не удалось обновить информацию о ключе. Попробуйте позже.")
                return

        # Начисляем реферальное вознаграждение по покупке — для любых внешних оплат (new и extend), кроме оплат с внутреннего баланса
        try:
            pm_for_ref = (payment_method or '').strip().lower()
            if pm_for_ref == 'balance':
                logger.info(f"Referral: skip accrual for user {user_id} because payment was made from internal balance.")
            else:
                user_data = get_user(user_id) or {}
                referrer_id = user_data.get('referred_by')
                if referrer_id:
                    try:
                        referrer_id = int(referrer_id)
                    except Exception:
                        logger.warning(f"Referral: invalid referrer_id={referrer_id} for user {user_id}")
                        referrer_id = None
                if referrer_id:
                    # Выбор логики по типу: процент от покупки, фикс за покупку; для fixed_start_referrer — вознаграждение по покупкам не начисляем
                    try:
                        reward_type = (get_setting("referral_reward_type") or "percent_purchase").strip()
                    except Exception:
                        reward_type = "percent_purchase"
                    reward = Decimal("0")
                    if reward_type == "fixed_start_referrer":
                        reward = Decimal("0")
                    elif reward_type == "fixed_purchase":
                        try:
                            amount_raw = get_setting("fixed_referral_bonus_amount") or "50"
                            reward = Decimal(str(amount_raw)).quantize(Decimal("0.01"))
                        except Exception:
                            reward = Decimal("50.00")
                    else:
                        # percent_purchase (по умолчанию)
                        try:
                            percentage = Decimal(get_setting("referral_percentage") or "0")
                        except Exception:
                            percentage = Decimal("0")
                        reward = (Decimal(str(price)) * percentage / 100).quantize(Decimal("0.01"))
                    logger.info(f"Referral: user={user_id}, referrer={referrer_id}, type={reward_type}, reward={float(reward):.2f}")
                    if float(reward) > 0:
                        try:
                            ok = add_to_balance(referrer_id, float(reward))
                        except Exception as e:
                            logger.warning(f"Referral: add_to_balance failed for referrer {referrer_id}: {e}")
                            ok = False
                        try:
                            add_to_referral_balance_all(referrer_id, float(reward))
                        except Exception as e:
                            logger.warning(f"Failed to increment referral_balance_all for {referrer_id}: {e}")
                        referrer_username = user_data.get('username', 'пользователь')
                        if ok:
                            try:
                                await bot.send_message(
                                    chat_id=referrer_id,
                                    text=(
                                        "💰 Вам начислено реферальное вознаграждение!\n"
                                        f"Пользователь: {referrer_username} (ID: {user_id})\n"
                                        f"Сумма: {float(reward):.2f} RUB"
                                    )
                                )
                            except Exception as e:
                                logger.warning(f"Could not send referral reward notification to {referrer_id}: {e}")
        except Exception as e:
            logger.warning(f"Referral: unexpected error while processing reward for user {user_id}: {e}")

        # Не считаем покупки, оплаченные с внутреннего баланса, в "Потрачено всего"
        pm = (payment_method or '').strip().lower()
        spent_for_stats = 0.0 if pm == 'balance' else price
        update_user_stats(user_id, spent_for_stats, months)
        
        user_info = get_user(user_id)

        log_username = user_info.get('username', 'N/A') if user_info else 'N/A'
        log_status = 'paid'
        log_amount_rub = float(price)
        log_method = metadata.get('payment_method', 'Unknown')
        
        log_metadata = json.dumps({
            "plan_id": metadata.get('plan_id'),
            "plan_name": get_plan_by_id(metadata.get('plan_id')).get('plan_name', 'Unknown') if get_plan_by_id(metadata.get('plan_id')) else 'Unknown',
            "host_name": metadata.get('host_name'),
            "customer_email": metadata.get('customer_email')
        })

        # Определяем payment_id для лога: берём из metadata, если есть (например, при отложенных транзакциях), иначе генерируем новый UUID
        payment_id_for_log = metadata.get('payment_id') or str(uuid.uuid4())

        log_transaction(
            username=log_username,
            transaction_id=None,
            payment_id=payment_id_for_log,
            user_id=user_id,
            status=log_status,
            amount_rub=log_amount_rub,
            amount_currency=None,
            currency_name=None,
            payment_method=log_method,
            metadata=log_metadata
        )
        
        await processing_message.delete()
        
        connection_string = None
        new_expiry_date = None
        try:
            connection_string = result.get('connection_string') if isinstance(result, dict) else None
            new_expiry_date = datetime.fromtimestamp(result['expiry_timestamp_ms'] / 1000) if isinstance(result, dict) and 'expiry_timestamp_ms' in result else None
        except Exception:
            connection_string = None
            new_expiry_date = None
        
        all_user_keys = get_user_keys(user_id)
        key_number = next((i + 1 for i, key in enumerate(all_user_keys) if key['key_id'] == key_id), len(all_user_keys))

        final_text = get_purchase_success_text(
            action="extend" if action == "extend" else "new",
            key_number=key_number,
            expiry_date=new_expiry_date or datetime.now(),
            connection_string=connection_string or ""
        )
        
        await bot.send_message(
            chat_id=user_id,
            text=final_text,
            reply_markup=keyboards.create_key_info_keyboard(key_id)
        )

        try:
            await notify_admin_of_purchase(bot, metadata)
        except Exception as e:
            logger.warning(f"Failed to notify admin of purchase: {e}")
        
    except Exception as e:
        logger.error(f"Error processing payment for user {user_id} on host {host_name}: {e}", exc_info=True)
        try:
            await processing_message.edit_text("❌ Ошибка при выдаче ключа.")
        except Exception:
            try:
                await bot.send_message(chat_id=user_id, text="❌ Ошибка при выдаче ключа.")
            except Exception:
                pass

