"""
Tranquis web subscription endpoints.

Public flow (no auth — opened by mobile app in browser):
  GET  /tranquis/subscribe              → plan picker or redirect to Stripe Checkout
  GET  /tranquis/subscribe/success      → post-payment success page
  GET  /tranquis/subscribe/cancel       → cancellation page

Webhook (no auth — verified by Stripe signature):
  POST /tranquis/webhooks/stripe        → subscription lifecycle events

Authenticated (mobile app polling):
  GET  /api/v1/tranquis/subscription/status  → current user's pro status
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.deps import get_current_user
from app.db.engine import get_db
from app.db.models import User
from app.services.tranquis_stripe import (
    TRANQUIS_PLANS,
    get_or_create_customer,
    get_or_create_price,
    get_plan,
    pro_expires_from_subscription,
)

logger = logging.getLogger(__name__)
settings = get_settings()

TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates" / "tranquis"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["tranquis-subscriptions"])

# ── Helpers ───────────────────────────────────────────────────────────────────


async def _get_user_by_id(db: AsyncSession, user_id: str) -> User:
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id")
    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def _activate_pro(
    db: AsyncSession,
    user: User,
    subscription,
    source: str = "stripe_web",
) -> None:
    """Mark user as Pro and persist the subscription period end."""
    user.is_pro = True
    user.pro_source = source
    user.pro_expires_at = pro_expires_from_subscription(subscription)
    await db.commit()


async def _deactivate_pro(db: AsyncSession, user: User) -> None:
    user.is_pro = False
    user.pro_expires_at = None
    await db.commit()


# ── Plan picker / checkout redirect ───────────────────────────────────────────


@router.get("/tranquis/subscribe", response_class=HTMLResponse)
async def subscribe(
    request: Request,
    user_id: str,
    plan: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """
    No plan → render plan picker.
    With plan → create Stripe Checkout session and redirect.
    """
    user = await _get_user_by_id(db, user_id)

    if not plan:
        return templates.TemplateResponse(
            "plan_picker.html",
            {"request": request, "user_id": user_id},
        )

    plan_config = get_plan(plan)
    if plan_config is None:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {plan}")

    stripe.api_key = settings.stripe_secret_key

    # Ensure Stripe customer exists and is linked to the user row
    customer_id = get_or_create_customer(user)
    if not user.stripe_customer_id:
        user.stripe_customer_id = customer_id
        await db.commit()

    price_id = get_or_create_price(plan_config)

    base = settings.tranquis_web_base_url
    session_kwargs: dict = dict(
        customer=customer_id,
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        client_reference_id=str(user.id),
        metadata={"user_id": str(user.id), "plan": plan},
        success_url=f"{base}/tranquis/subscribe/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base}/tranquis/subscribe/cancel?user_id={user_id}",
    )

    if plan_config.get("trial_days", 0) > 0:
        session_kwargs["subscription_data"] = {
            "trial_period_days": plan_config["trial_days"]
        }

    checkout_session = stripe.checkout.Session.create(**session_kwargs)
    return RedirectResponse(checkout_session.url, status_code=303)


# ── Post-payment pages ─────────────────────────────────────────────────────────


@router.get("/tranquis/subscribe/success", response_class=HTMLResponse)
async def subscribe_success(
    request: Request,
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    stripe.api_key = settings.stripe_secret_key

    try:
        checkout_session = stripe.checkout.Session.retrieve(
            session_id, expand=["subscription"]
        )
    except stripe.error.StripeError as exc:
        logger.error("Stripe session retrieve failed: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid session")

    user_id = (
        checkout_session.metadata.get("user_id")
        or checkout_session.client_reference_id
    )
    plan_id = checkout_session.metadata.get("plan", "")
    plan_config = get_plan(plan_id) or {}

    if checkout_session.payment_status in ("paid", "trialing", "no_payment_required"):
        user = await _get_user_by_id(db, user_id)
        subscription = checkout_session.subscription
        if subscription:
            await _activate_pro(db, user, subscription)

    # Format renewal date for display
    renews_at = None
    is_trial = False
    sub = checkout_session.subscription
    if sub:
        ts = getattr(sub, "current_period_end", None)
        if ts:
            renews_at = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%-d %B %Y")
        is_trial = getattr(sub, "status", "") == "trialing"

    return templates.TemplateResponse(
        "success.html",
        {
            "request": request,
            "plan_name": plan_config.get("name", "Tranquis Pro"),
            "renews_at": renews_at,
            "is_trial": is_trial,
            "app_url": settings.tranquis_app_url,
        },
    )


@router.get("/tranquis/subscribe/cancel", response_class=HTMLResponse)
async def subscribe_cancel(
    request: Request,
    user_id: str = "",
):
    return templates.TemplateResponse(
        "cancel.html",
        {
            "request": request,
            "user_id": user_id,
            "app_url": settings.tranquis_app_url,
        },
    )


# ── Stripe webhook ─────────────────────────────────────────────────────────────


@router.post("/tranquis/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except stripe.error.SignatureVerificationError:
        logger.warning("Stripe webhook signature verification failed")
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception as exc:
        logger.error("Webhook parse error: %s", exc)
        raise HTTPException(status_code=400, detail="Bad payload")

    event_type = event["type"]
    data_obj = event["data"]["object"]

    if event_type in ("customer.subscription.created", "customer.subscription.updated"):
        user_id = data_obj.get("metadata", {}).get("user_id")
        if not user_id:
            # Fall back: look up user by customer ID
            customer_id = data_obj.get("customer")
            if customer_id:
                result = await db.execute(
                    select(User).where(User.stripe_customer_id == customer_id)
                )
                user = result.scalar_one_or_none()
            else:
                user = None
        else:
            try:
                user = await _get_user_by_id(db, user_id)
            except HTTPException:
                user = None

        if user:
            status = data_obj.get("status", "")
            if status in ("active", "trialing"):
                await _activate_pro(db, user, data_obj)
            elif status in ("canceled", "unpaid", "past_due"):
                await _deactivate_pro(db, user)

    elif event_type == "customer.subscription.deleted":
        customer_id = data_obj.get("customer")
        user_id = data_obj.get("metadata", {}).get("user_id")

        user = None
        if user_id:
            try:
                user = await _get_user_by_id(db, user_id)
            except HTTPException:
                pass
        if not user and customer_id:
            result = await db.execute(
                select(User).where(User.stripe_customer_id == customer_id)
            )
            user = result.scalar_one_or_none()

        if user:
            await _deactivate_pro(db, user)

    elif event_type == "invoice.payment_failed":
        # Grace period: keep Pro for 3 days then let subscription.updated handle final expiry
        customer_id = data_obj.get("customer")
        if customer_id:
            result = await db.execute(
                select(User).where(User.stripe_customer_id == customer_id)
            )
            user = result.scalar_one_or_none()
            if user and user.is_pro:
                grace_until = datetime.now(timezone.utc) + timedelta(days=3)
                # Only shorten if current expiry is beyond grace period
                if not user.pro_expires_at or user.pro_expires_at > grace_until:
                    user.pro_expires_at = grace_until
                    await db.commit()

    else:
        logger.debug("Unhandled Stripe event: %s", event_type)

    return Response(status_code=200)


# ── Subscription status (authenticated, for mobile polling) ───────────────────


@router.get("/api/v1/tranquis/subscription/status")
async def subscription_status(
    user: User = Depends(get_current_user),
):
    """Return the current user's Pro subscription status."""
    now = datetime.now(timezone.utc)

    is_pro = user.is_pro
    # Enforce expiry
    if is_pro and user.pro_expires_at and user.pro_expires_at < now:
        is_pro = False

    return {
        "is_pro": is_pro,
        "pro_expires_at": user.pro_expires_at.isoformat() if user.pro_expires_at else None,
        "pro_source": user.pro_source,
    }
