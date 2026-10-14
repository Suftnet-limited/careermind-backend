"""Self-service password reset for Tranquis (and CareerMind)."""
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import hash_password
from app.core.rate_limit import limiter, client_ip
from app.db.engine import get_db
from app.db.models import User, PasswordResetToken, PasswordResetCode
from app.services.email import TRANQUIS_FROM_NAME, send_email, base_template, h1, p, btn, escape_html
from app.config import get_settings

settings = get_settings()
router = APIRouter(prefix="/auth", tags=["Auth"])

RESET_TTL_HOURS = 1
RESET_RATE_LIMIT = "5/hour"


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


TRANQUIS_TEAL = "#14B8A6"


def _brand_tranquis(html: str) -> str:
    """Swap the Interquis branding in base_template for Tranquis."""
    return html.replace(
        '<div style="font-size:20px;line-height:24px;font-weight:800;letter-spacing:-0.02em;color:#111827">Inter<span style="color:#6366f1">quis</span></div>',
        f'<div style="font-size:20px;line-height:24px;font-weight:800;letter-spacing:-0.02em;color:#111827">Tran<span style="color:{TRANQUIS_TEAL}">quis</span></div>',
    ).replace(
        '<div style="font-size:11px;line-height:16px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:#94a3b8">Practice interviews. Land the job.</div>',
        '<div style="font-size:11px;line-height:16px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:#94a3b8">Translate the world.</div>',
    ).replace(
        '<tr><td height="5" bgcolor="#6366f1"',
        f'<tr><td height="5" bgcolor="{TRANQUIS_TEAL}"',
    )


async def send_password_reset_email(to: str, name: str, raw_token: str, app_source: str = "careermind") -> bool:
    first = escape_html(name.split()[0] if name.strip() else "there")

    if app_source == "tranquis":
        brand = "Tranquis"
        brand_color = "#14B8A6"
        tagline = "Translate the world."
        reset_url = f"tranquis://reset-password?token={raw_token}"
        support_email = "support@suftnet.com"
    else:
        brand = "Interquis"
        brand_color = "#6366f1"
        tagline = "Practice interviews. Land the job."
        reset_url = f"{settings.frontend_url}/reset-password?token={raw_token}"
        support_email = settings.brevo_from_email

    # Build a minimal branded template for the reset email
    content = (
        h1("Reset your password")
        + p(f"Hi {first}, we received a request to reset your {brand} password.")
        + p("Tap the button below to choose a new password. This link expires in 1 hour.")
        + btn("Reset password", reset_url)
        + p(
            f'<span style="font-size:13px;color:#94a3b8">'
            f"If you didn't request a password reset, you can ignore this email — your account is safe.<br/>"
            f"Need help? Contact <a href='mailto:{support_email}' style='color:#64748b'>{support_email}</a></span>"
        )
    )

    html = base_template(content, preheader=f"Reset your {brand} password")
    if app_source == "tranquis":
        html = _brand_tranquis(html)

    return await send_email(to, f"Reset your {brand} password", html,
                            from_name=TRANQUIS_FROM_NAME if app_source == "tranquis" else None)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr
    app_source: str = "careermind"  # 'careermind' | 'tranquis'


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


@router.post("/forgot-password")
@limiter.limit(RESET_RATE_LIMIT, key_func=client_ip)
async def forgot_password(
    request: Request,
    req: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Request a password reset link.
    Always returns 200 so attackers can't enumerate accounts.
    """
    result = await db.execute(select(User).where(User.email == req.email.lower()))
    user = result.scalar_one_or_none()

    if user:
        raw_token = secrets.token_urlsafe(32)
        reset_token = PasswordResetToken(
            id=uuid.uuid4(),
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            used=False,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=RESET_TTL_HOURS),
        )
        db.add(reset_token)
        await db.commit()

        app_source = req.app_source if req.app_source in ("careermind", "tranquis") else "careermind"
        background_tasks.add_task(
            send_password_reset_email,
            user.email, user.name, raw_token, app_source,
        )

    return {"sent": True}


@router.post("/reset-password")
async def reset_password(
    req: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """Consume a reset token and change the user's password."""
    if len(req.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    token_hash = _hash_token(req.token)
    result = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.used == False,  # noqa: E712
        )
    )
    reset_token = result.scalar_one_or_none()

    if not reset_token:
        raise HTTPException(400, "This reset link is invalid or has already been used")

    if reset_token.expires_at < datetime.now(timezone.utc):
        raise HTTPException(400, "This reset link has expired — please request a new one")

    # Mark token as used and update password
    reset_token.used = True
    await db.execute(
        update(User)
        .where(User.id == reset_token.user_id)
        .values(hashed_password=hash_password(req.new_password))
    )
    await db.commit()

    return {"reset": True}


# ── Tranquis: emailed 6-digit reset code ──────────────────────────────────────
# Replaces /auth/reset-password-instant, which changed the password from the email
# address alone. The code is never stored: only an HMAC of it, keyed by the server
# secret and bound to the user, so a database leak doesn't reveal live codes.

tranquis_router = APIRouter(prefix="/tranquis/auth/reset-password", tags=["Auth"])

CODE_TTL_MINUTES = 10
CODE_MAX_ATTEMPTS = 5  # wrong guesses before the code is burned
CODE_ERROR = "Incorrect or expired code — check your email or request a new one"


def _hash_code(user_id: uuid.UUID, code: str) -> str:
    return hmac.new(settings.jwt_secret_key.encode(), f"{user_id}:{code}".encode(), hashlib.sha256).hexdigest()


async def _tranquis_user(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(
        select(User).where(User.email == email.lower(), User.app_source == "tranquis")
    )
    return result.scalar_one_or_none()


async def send_reset_code_email(to: str, name: str, code: str) -> bool:
    first = escape_html(name.split()[0] if name.strip() else "there")
    content = (
        h1("Your Tranquis reset code")
        + p(f"Hi {first}, your password reset code is:")
        + p(f'<span style="font-size:32px;font-weight:800;letter-spacing:0.3em;color:#111827">{code}</span>')
        + p(f"It expires in {CODE_TTL_MINUTES} minutes.")
        + p(
            '<span style="font-size:13px;color:#94a3b8">'
            "If you didn't ask to reset your password, you can ignore this email — your account is safe.</span>"
        )
    )
    html = _brand_tranquis(base_template(content, preheader=f"Your password reset code is {code}"))
    return await send_email(to, "Your Tranquis reset code", html, from_name=TRANQUIS_FROM_NAME)


class ResetCodeRequest(BaseModel):
    email: EmailStr


class ResetCodeConfirm(BaseModel):
    email: EmailStr
    code: str
    new_password: str


@tranquis_router.post("/request")
@limiter.limit(RESET_RATE_LIMIT, key_func=client_ip)
async def request_reset_code(
    request: Request,
    req: ResetCodeRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Email a 6-digit reset code. Always succeeds, so it can't be used to find accounts."""
    user = await _tranquis_user(db, req.email)
    if user:
        # Only the newest code works
        await db.execute(
            update(PasswordResetCode)
            .where(PasswordResetCode.user_id == user.id, PasswordResetCode.used == False)  # noqa: E712
            .values(used=True)
        )
        code = f"{secrets.randbelow(900_000) + 100_000}"
        db.add(PasswordResetCode(
            user_id=user.id,
            code_hash=_hash_code(user.id, code),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=CODE_TTL_MINUTES),
        ))
        await db.commit()
        background_tasks.add_task(send_reset_code_email, user.email, user.name, code)

    return {"success": True}


@tranquis_router.post("/confirm")
@limiter.limit("20/hour", key_func=client_ip)
async def confirm_reset_code(
    request: Request,
    req: ResetCodeConfirm,
    db: AsyncSession = Depends(get_db),
):
    """Check the emailed code and set the new password. The app then signs in normally."""
    if len(req.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    user = await _tranquis_user(db, req.email)
    entry = None
    if user:
        result = await db.execute(
            select(PasswordResetCode)
            .where(
                PasswordResetCode.user_id == user.id,
                PasswordResetCode.used == False,  # noqa: E712
                PasswordResetCode.expires_at > datetime.now(timezone.utc),
            )
            .order_by(PasswordResetCode.created_at.desc())
            .limit(1)
        )
        entry = result.scalar_one_or_none()

    # Same answer for unknown email, no code, expired or wrong code
    if not entry:
        raise HTTPException(400, CODE_ERROR)

    if not hmac.compare_digest(entry.code_hash, _hash_code(user.id, req.code.strip())):
        entry.attempts += 1
        if entry.attempts >= CODE_MAX_ATTEMPTS:
            entry.used = True
        await db.commit()
        raise HTTPException(400, CODE_ERROR)

    entry.used = True
    user.hashed_password = hash_password(req.new_password)
    await db.commit()
    return {"success": True}
