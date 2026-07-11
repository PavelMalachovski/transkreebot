import logging
from datetime import timedelta, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery

import db
from config import settings

logger = logging.getLogger(__name__)

router = Router(name="payments")

SUBSCRIPTION_PAYLOAD = "subscription_1_month"
MONTH_SECONDS = 2592000  # the only subscription period Telegram supports


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message) -> None:
    user = await db.get_or_create_user(message.from_user.id, message.from_user.username)

    if db.has_active_subscription(user):
        until = user["subscription_until"].strftime("%d.%m.%Y")
        if not user["cancel_at_period_end"]:
            await message.answer(f"У тебя уже есть активная подписка до {until}. 🎉")
            return
        # renewal was cancelled — try to switch it back on
        if user["telegram_charge_id"]:
            try:
                await message.bot.edit_user_star_subscription(
                    user_id=message.from_user.id,
                    telegram_payment_charge_id=user["telegram_charge_id"],
                    is_canceled=False,
                )
                await db.set_cancel_at_period_end(message.from_user.id, False)
                await message.answer(f"Продление снова включено. Подписка активна до {until}. 🎉")
                return
            except TelegramBadRequest as e:
                logger.warning("Could not re-enable subscription renewal: %s", e.message)
                # fall through to a fresh invoice

    await message.answer_invoice(
        title="Подписка Transkreebot — 1 месяц",
        description=(
            "Безлимитная расшифровка видео до 2 часов длиной. "
            "Продлевается автоматически, отменить можно в любой момент: /cancel"
        ),
        payload=SUBSCRIPTION_PAYLOAD,
        currency="XTR",  # Telegram Stars
        prices=[LabeledPrice(label="Подписка на месяц", amount=settings.subscription_stars)],
        subscription_period=MONTH_SECONDS,
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    payment = message.successful_payment
    logger.info(
        "Payment received: user=%s amount=%s %s charge_id=%s recurring=%s first=%s",
        message.from_user.id,
        payment.total_amount,
        payment.currency,
        payment.telegram_payment_charge_id,
        payment.is_recurring,
        payment.is_first_recurring,
    )
    until = payment.subscription_expiration_date
    if until is not None:
        # naive UTC to match the TIMESTAMP column
        until = until.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        until = db.utcnow() + timedelta(days=settings.subscription_days)
    await db.activate_subscription(
        message.from_user.id, until, payment.telegram_payment_charge_id
    )

    renewal = payment.is_recurring and not payment.is_first_recurring
    if renewal:
        await message.answer(f"Подписка продлена до {until.strftime('%d.%m.%Y')}. Спасибо! 🎉")
    else:
        await message.answer(
            f"Оплата прошла успешно! 🎉\n"
            f"Подписка активна до {until.strftime('%d.%m.%Y')} и продлится автоматически. "
            "Присылай ссылки — расшифрую без ограничений. Отмена: /cancel"
        )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    user = await db.get_or_create_user(message.from_user.id, message.from_user.username)

    if not db.has_active_subscription(user):
        await message.answer("У тебя нет активной подписки. Оформить: /subscribe")
        return

    until = user["subscription_until"].strftime("%d.%m.%Y")
    if user["cancel_at_period_end"]:
        await message.answer(f"Продление уже отключено. Подписка действует до {until}.")
        return

    if user["telegram_charge_id"]:
        try:
            await message.bot.edit_user_star_subscription(
                user_id=message.from_user.id,
                telegram_payment_charge_id=user["telegram_charge_id"],
                is_canceled=True,
            )
        except TelegramBadRequest as e:
            # e.g. a legacy non-Stars payment; the local flag still stops us
            # from treating the subscription as renewable
            logger.warning("edit_user_star_subscription failed: %s", e.message)

    await db.set_cancel_at_period_end(message.from_user.id, True)
    await message.answer(
        f"Подписка останется активной до {until}, дальше продлеваться не будет."
    )
