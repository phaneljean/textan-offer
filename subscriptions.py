"""
subscriptions.py — Track usage and subscription status per phone number
"""
import sqlite3
import os
from datetime import datetime

DB_PATH = os.environ.get("DATABASE_PATH", "subscriptions.db")
FREE_OFFER_LIMIT = 3

# Comma-separated phone numbers (e.g. "+15622570392,+13105057120") that get
# unlimited offers without a real Stripe subscription -- for testing on
# production. Env-var driven rather than hardcoded so a real phone number
# never lands in the public repo.
ADMIN_PHONES = {p.strip() for p in os.environ.get("ADMIN_PHONES", "").split(",") if p.strip()}


def is_admin_phone(phone: str) -> bool:
    return phone in ADMIN_PHONES

def init_db():
    """Create tables if they don't exist"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            phone TEXT PRIMARY KEY,
            offer_count INTEGER DEFAULT 0,
            is_subscribed INTEGER DEFAULT 0,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    # Table may already exist from before plan tracking was added.
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'starter'")
    except sqlite3.OperationalError:
        pass  # column already exists

    conn.commit()
    conn.close()

# Plans that include DocuSign send + Webhook/Zapier automation, in ascending order.
PROFESSIONAL_PLANS = {"professional", "brokerage"}

def get_user(phone: str) -> dict:
    """Get user data by phone number"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT phone, offer_count, is_subscribed, stripe_customer_id, stripe_subscription_id, plan
        FROM users WHERE phone = ?
    """, (phone,))

    row = cursor.fetchone()
    conn.close()

    if row:
        return {
            "phone": row[0],
            "offer_count": row[1],
            "is_subscribed": bool(row[2]),
            "stripe_customer_id": row[3],
            "stripe_subscription_id": row[4],
            "plan": row[5] or "starter",
        }
    return None


def has_professional_access(phone: str) -> bool:
    """DocuSign send + Webhook/Zapier automation are Professional-plan features."""
    if is_admin_phone(phone):
        return True
    user = get_user(phone)
    return bool(user) and user["is_subscribed"] and user["plan"] in PROFESSIONAL_PLANS

def create_user(phone: str) -> dict:
    """Create new user record"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()

    cursor.execute("""
        INSERT INTO users (phone, offer_count, is_subscribed, created_at, updated_at)
        VALUES (?, 0, 0, ?, ?)
    """, (phone, now, now))

    conn.commit()
    conn.close()

    return {
        "phone": phone,
        "offer_count": 0,
        "is_subscribed": False,
        "stripe_customer_id": None,
        "stripe_subscription_id": None,
    }

def increment_offer_count(phone: str) -> int:
    """Increment offer count and return new count"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()

    cursor.execute("""
        UPDATE users
        SET offer_count = offer_count + 1, updated_at = ?
        WHERE phone = ?
    """, (now, phone))

    cursor.execute("SELECT offer_count FROM users WHERE phone = ?", (phone,))
    new_count = cursor.fetchone()[0]

    conn.commit()
    conn.close()

    return new_count

def activate_subscription(phone: str, stripe_customer_id: str, stripe_subscription_id: str, plan: str = "starter"):
    """Mark user as subscribed on the given plan (starter/professional/brokerage)."""
    if plan not in ("starter", "professional", "brokerage"):
        plan = "starter"
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()

    cursor.execute("""
        UPDATE users
        SET is_subscribed = 1,
            stripe_customer_id = ?,
            stripe_subscription_id = ?,
            plan = ?,
            updated_at = ?
        WHERE phone = ?
    """, (stripe_customer_id, stripe_subscription_id, plan, now, phone))

    conn.commit()
    conn.close()

def deactivate_subscription(stripe_subscription_id: str):
    """Mark user as unsubscribed"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()

    cursor.execute("""
        UPDATE users
        SET is_subscribed = 0, updated_at = ?
        WHERE stripe_subscription_id = ?
    """, (now, stripe_subscription_id))

    conn.commit()
    conn.close()

def can_generate_offer(phone: str) -> tuple[bool, str, dict]:
    """
    Check if user can generate an offer.
    Returns: (can_generate, reason, user_data)
    """
    user = get_user(phone)

    if not user:
        user = create_user(phone)

    # Admin/test numbers have unlimited access without a real subscription
    if is_admin_phone(phone):
        return True, "admin", user

    # Subscribed users have unlimited access
    if user["is_subscribed"]:
        return True, "subscribed", user

    # Free users get 3 offers
    if user["offer_count"] < FREE_OFFER_LIMIT:
        return True, "free_trial", user

    # Over limit
    return False, "limit_reached", user

init_db()
