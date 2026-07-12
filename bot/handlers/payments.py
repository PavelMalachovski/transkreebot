import logging
from datetime import timedelta, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

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
            await message.answer(f"You already have an active subscription until {until}. 🎉")
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
                await message.answer(f"Renewal is back on. Subscription active until {until}. 🎉")
                return
            except TelegramBadRequest as e:
                logger.warning("Could not re-enable subscription renewal: %s", e.message)
                # fall through to a fresh invoice

    # Stars subscriptions must be exported invoice links (sendInvoice with
    # subscription_period fails with SUBSCRIPTION_EXPORT_MISSING)
    try:
        link = await message.bot.create_invoice_link(
            title="Transkreebot subscription — 1 month",
            description=(
                "Unlimited transcription of videos up to 2 hours long. "
                "Renews automatically, cancel anytime."
            ),
            payload=SUBSCRIPTION_PAYLOAD,
            currency="XTR",  # Telegram Stars
            prices=[LabeledPrice(label="Monthly subscription", amount=settings.subscription_stars)],
            subscription_period=MONTH_SECONDS,
        )
    except TelegramBadRequest as e:
        logger.error("Failed to create subscription invoice link: %s", e.message)
        await message.answer("Payments are temporarily unavailable, please try again later. 🙏")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"Subscribe for {settings.subscription_stars} ⭐ / month", url=link
        )
    ]])
    await message.answer(
        "⭐ <b>Transkreebot subscription</b>\n\n"
        f"Unlimited transcription of videos up to {settings.sub_max_duration // 3600} hours long, "
        "priority in the queue.\n"
        f"{settings.subscription_stars} Stars per month, renews automatically — "
        "cancel anytime with /cancel.",
        parse_mode="HTML",
        reply_markup=keyboard,
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
        await message.answer(f"Subscription renewed until {until.strftime('%d.%m.%Y')}. Thank you! 🎉")
    else:
        await message.answer(
            f"Payment successful! 🎉\n"
            f"Your subscription is active until {until.strftime('%d.%m.%Y')} and renews "
            "automatically. Send me links — unlimited transcriptions await. Cancel: /cancel"
        )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    user = await db.get_or_create_user(message.from_user.id, message.from_user.username)

    if not db.has_active_subscription(user):
        await message.answer("You don't have an active subscription. Subscribe: /subscribe")
        return

    until = user["subscription_until"].strftime("%d.%m.%Y")
    if user["cancel_at_period_end"]:
        await message.answer(f"Renewal is already off. Your subscription lasts until {until}.")
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
        f"Your subscription stays active until {until}, then won't renew."
    )
