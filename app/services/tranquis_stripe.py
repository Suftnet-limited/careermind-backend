"""
Tranquis web subscription service.
Stripe prices are created once (or looked up by metadata) and stored in config.
"""
import stripe
from datetime import datetime, timezone
from app.config import get_settings

settings = get_settings()

TRANQUIS_PLANS = [
    {
        "id": "weekly",
        "name": "Tranquis Weekly",
        "price_gbp": 199,   # pence
        "interval": "week",
        "interval_count": 1,
        "trial_days": 0,
        "display_price": "£1.99",
        "display_period": "/ week",
    },
    {
        "id": "monthly",
        "name": "Tranquis Monthly",
        "price_gbp": 499,
        "interval": "month",
        "interval_count": 1,
        "trial_days": 7,
        "display_price": "£4.99",
        "display_period": "/ month",
    },
    {
        "id": "quarterly",
        "name": "Tranquis 3-Month",
        "price_gbp": 999,
        "interval": "month",
        "interval_count": 3,
        "trial_days": 0,
        "display_price": "£9.99",
        "display_period": "/ 3 months",
    },
    {
        "id": "annual",
        "name": "Tranquis Annual",
        "price_gbp": 2999,
        "interval": "year",
        "interval_count": 1,
        "trial_days": 0,
        "display_price": "£29.99",
        "display_period": "/ year",
    },
]

_PLAN_BY_ID = {p["id"]: p for p in TRANQUIS_PLANS}


def get_plan(plan_id: str) -> dict | None:
    return _PLAN_BY_ID.get(plan_id)


def get_or_create_price(plan: dict) -> str:
    """Return existing Stripe price ID for this plan, creating product+price if needed."""
    stripe.api_key = settings.stripe_secret_key

    # Look up by metadata
    prices = stripe.Price.search(
        query=f'metadata["tranquis_plan_id"]:"{plan["id"]}"',
        limit=1,
    )
    if prices.data:
        return prices.data[0].id

    # Create product
    product = stripe.Product.create(
        name=plan["name"],
        metadata={"app": "tranquis", "plan_id": plan["id"]},
    )
    # Create price
    price = stripe.Price.create(
        product=product.id,
        currency="gbp",
        unit_amount=plan["price_gbp"],
        recurring={
            "interval": plan["interval"],
            "interval_count": plan["interval_count"],
        },
        metadata={"tranquis_plan_id": plan["id"]},
    )
    return price.id


def get_or_create_customer(user) -> str:
    """Return existing Stripe customer ID for user, creating one if needed."""
    stripe.api_key = settings.stripe_secret_key

    if user.stripe_customer_id:
        return user.stripe_customer_id

    customer = stripe.Customer.create(
        email=user.email,
        name=user.name,
        metadata={"user_id": str(user.id), "app": "tranquis"},
    )
    return customer.id


def pro_expires_from_subscription(subscription) -> datetime | None:
    """Convert Stripe subscription current_period_end to a timezone-aware datetime."""
    ts = getattr(subscription, "current_period_end", None)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)
