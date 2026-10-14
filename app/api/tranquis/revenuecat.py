"""
Tranquis Apple / Google in-app purchases, via RevenueCat webhooks.

The app logs RevenueCat in with the Tranquis user id, so `app_user_id` (or one of its
aliases) is our User.id. Purchases and renewals set is_pro until the entitlement's
expiry; expiry clears it. The daily quota (app/core/quota.py) reads is_pro, so this is
what lifts the free limit for App Store subscribers.

  POST /api/v1/tranquis/webhooks/revenuecat   (Authorization: REVENUECAT_WEBHOOK_AUTH)
"""
import hmac
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.engine import get_db
from app.db.models import User

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter(tags=["tranquis-subscriptions"])

ENTITLEMENT_ID = "premium"
SOURCE = "revenuecat"

# Events that mean the entitlement is (still) active until expiration_at_ms
GRANT_EVENTS = {
    "INITIAL_PURCHASE", "RENEWAL", "UNCANCELLATION", "PRODUCT_CHANGE",
    "NON_RENEWING_PURCHASE", "SUBSCRIPTION_EXTENDED", "TEMPORARY_ENTITLEMENT_GRANT",
}
# CANCELLATION and BILLING_ISSUE keep access until expiry, so they change nothing here


def _user_ids(event: dict) -> list[uuid.UUID]:
    ids = []
    for raw in [event.get("app_user_id"), event.get("original_app_user_id"), *(event.get("aliases") or [])]:
        try:
            ids.append(uuid.UUID(str(raw)))
        except (ValueError, TypeError):
            continue  # anonymous "$RCAnonymousID:..." ids
    return ids


@router.post("/api/v1/tranquis/webhooks/revenuecat")
async def revenuecat_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    expected = settings.revenuecat_webhook_auth
    if not expected:
        raise HTTPException(503, "RevenueCat webhook is not configured")
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied.removeprefix("Bearer ").strip(), expected.removeprefix("Bearer ").strip()):
        raise HTTPException(401, "Invalid authorization")

    event = (await request.json()).get("event") or {}
    event_type = event.get("type", "")
    if event_type == "TEST":
        return {"ok": True}

    entitlements = event.get("entitlement_ids") or ([event["entitlement_id"]] if event.get("entitlement_id") else [])
    if entitlements and ENTITLEMENT_ID not in entitlements:
        return {"ok": True, "ignored": "other entitlement"}

    ids = _user_ids(event)
    user = None
    if ids:
        result = await db.execute(select(User).where(User.id.in_(ids), User.app_source == "tranquis"))
        user = result.scalars().first()
    if not user:
        # 200 so RevenueCat doesn't retry an event we can never apply
        logger.warning("RevenueCat %s for unknown user %s", event_type, event.get("app_user_id"))
        return {"ok": True, "ignored": "unknown user"}

    if event_type in GRANT_EVENTS:
        ms = event.get("expiration_at_ms")
        user.is_pro = True
        user.pro_source = SOURCE
        user.pro_expires_at = datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else None
        await db.commit()
    elif event_type == "EXPIRATION" and user.pro_source == SOURCE:
        # Leave Pro bought on the web (Stripe) alone
        user.is_pro = False
        user.pro_expires_at = None
        await db.commit()

    return {"ok": True}
