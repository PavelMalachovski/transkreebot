import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery

import db
from config import settings

logger = logging.getLogger(__name__)

router = Router(name="payments")

SUBSCRIPTION_PAYLOAD = "subscription_1_month"


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message) -> None:
    user = await db.get_or_create_user(message.from_user.id, message.from_user.username)

    if db.has_active_subscription(user) and not user["cancel_at_period_end"]:
        until = user["subscription_until"].strftime("%d.%m.%Y")
        await message.answer(f"У тебя уже есть активная подписка до {until}. 🎉")
        return

    if not settings.provider_token:
        logger.error("PAYMENTS_PROVIDER_TOKEN is not set, cannot send invoice")
        await message.answer("Оплата временно недоступна, попробуй позже. 🙏")
        return

    await message.answer_invoice(
        title="Подписка на 1 месяц",
        description="Безлимитная расшифровка видео на 30 дней.",
        payload=SUBSCRIPTION_PAYLOAD,
        provider_token=settings.provider_token,
        currency="EUR",
        prices=[LabeledPrice(label="Подписка на месяц", amount=settings.subscription_price_cents)],
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    payment = message.successful_payment
    logger.info(
        "Payment received: user=%s amount=%s %s charge_id=%s",
        message.from_user.id,
        payment.total_amount,
        payment.currency,
        payment.telegram_payment_charge_id,
    )
    until = await db.activate_subscription(message.from_user.id, settings.subscription_days)
    await message.answer(
        f"Оплата прошла успешно! 🎉\n"
        f"Подписка активна до {until.strftime('%d.%m.%Y')}. "
        "Присылай ссылки — расшифрую без ограничений."
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    user = await db.get_or_create_user(message.from_user.id, message.from_user.username)

    if not db.has_active_subscription(user):
        await message.answer("У тебя нет активной подписки. Оформить: /subscribe")
        return

    if user["cancel_at_period_end"]:
        until = user["subscription_until"].strftime("%d.%m.%Y")
        await message.answer(f"Продление уже отключено. Подписка действует до {until}.")
        return

    await db.set_cancel_at_period_end(message.from_user.id)
    until = user["subscription_until"].strftime("%d.%m.%Y")
    await message.answer(
        f"Your subscription stays active until {until}, then won't renew."
    )
