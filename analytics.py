"""
analytics.py — Track key conversion metrics
"""
import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.environ.get("DATABASE_PATH", "subscriptions.db")

def init_analytics_tables():
    """Create analytics tables"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            phone TEXT,
            metadata TEXT,
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()

def track_event(event_type: str, phone: str = None, metadata: dict = None):
    """Track an analytics event"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()

    import json
    metadata_json = json.dumps(metadata) if metadata else None

    cursor.execute("""
        INSERT INTO events (event_type, phone, metadata, created_at)
        VALUES (?, ?, ?, ?)
    """, (event_type, phone, metadata_json, now))

    conn.commit()
    conn.close()

def get_conversion_metrics(days: int = 30) -> dict:
    """Get conversion funnel metrics for last N days"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

    # Total signups
    cursor.execute("""
        SELECT COUNT(DISTINCT phone)
        FROM events
        WHERE event_type = 'offer_generated'
        AND created_at > ?
    """, (cutoff,))
    signups = cursor.fetchone()[0]

    # Trial completions
    cursor.execute("""
        SELECT COUNT(DISTINCT phone)
        FROM events
        WHERE event_type = 'trial_completed'
        AND created_at > ?
    """, (cutoff,))
    trial_completions = cursor.fetchone()[0]

    # Conversions
    cursor.execute("""
        SELECT COUNT(DISTINCT phone)
        FROM events
        WHERE event_type = 'subscription_created'
        AND created_at > ?
    """, (cutoff,))
    conversions = cursor.fetchone()[0]

    # Total offers
    cursor.execute("""
        SELECT COUNT(*)
        FROM events
        WHERE event_type = 'offer_generated'
        AND created_at > ?
    """, (cutoff,))
    total_offers = cursor.fetchone()[0]

    # Hit paywall
    cursor.execute("""
        SELECT COUNT(DISTINCT phone)
        FROM events
        WHERE event_type = 'limit_reached'
        AND created_at > ?
    """, (cutoff,))
    hit_paywall = cursor.fetchone()[0]

    conn.close()

    # Calculate rates
    trial_activation_rate = (trial_completions / signups * 100) if signups > 0 else 0
    paywall_to_paid = (conversions / hit_paywall * 100) if hit_paywall > 0 else 0
    overall_conversion = (conversions / signups * 100) if signups > 0 else 0

    return {
        "period_days": days,
        "signups": signups,
        "trial_completions": trial_completions,
        "hit_paywall": hit_paywall,
        "conversions": conversions,
        "total_offers": total_offers,
        "trial_activation_rate": round(trial_activation_rate, 1),
        "paywall_to_paid_rate": round(paywall_to_paid, 1),
        "overall_conversion_rate": round(overall_conversion, 1),
        "avg_offers_per_user": round(total_offers / signups, 1) if signups > 0 else 0,
    }

def get_revenue_metrics() -> dict:
    """Calculate revenue metrics"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COUNT(*)
        FROM users
        WHERE is_subscribed = 1
    """)
    active_subs = cursor.fetchone()[0]

    conn.close()

    mrr = active_subs * 49
    arr = mrr * 12

    return {
        "active_subscribers": active_subs,
        "mrr": mrr,
        "arr": arr,
    }

init_analytics_tables()
