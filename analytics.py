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

def get_recent_sms(limit: int = 50) -> list:
    """Get recent SMS activity"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT phone, metadata, created_at
        FROM events
        WHERE event_type = 'sms_received'
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    import json
    results = []
    for row in rows:
        metadata = json.loads(row[1]) if row[1] else {}
        results.append({
            "phone": row[0],
            "body": metadata.get("body", ""),
            "created_at": row[2]
        })

    return results

def get_recent_sms_failures(limit: int = 20) -> list:
    """Get recent outbound SMS send failures (e.g. Twilio A2P 10DLC blocks)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT phone, metadata, created_at
        FROM events
        WHERE event_type = 'sms_send_failed'
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    import json
    results = []
    for row in rows:
        metadata = json.loads(row[1]) if row[1] else {}
        results.append({
            "phone": row[0],
            "error": metadata.get("error", ""),
            "body": metadata.get("body", ""),
            "created_at": row[2]
        })

    return results

def get_last_blocked_state(phone: str, hours: int = 72):
    """Most recent state a phone's offer was blocked for (see
    other_state_block_message in app.py), so a WAITLIST reply can be
    attributed to a specific state instead of just a bare signup. Only
    looks back `hours` so a reply days later after trying a real Texas
    address isn't mis-attributed to a stale block."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    cursor.execute("""
        SELECT metadata FROM events
        WHERE event_type = 'blocked_other_state' AND phone = ? AND created_at > ?
        ORDER BY created_at DESC LIMIT 1
    """, (phone, cutoff))
    row = cursor.fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    import json
    return json.loads(row[0]).get("state")

def get_landing_visits_by_source(days: int = 30) -> list:
    """Raw homepage-visit counts grouped by ?src= attribution, regardless of
    whether the visitor ever signed up. Signups-by-source alone can't tell
    "nobody opened the link" apart from "people opened it and left" -- both
    look like silence. This answers that: a nonzero count here with zero
    matching signups means the link IS being clicked, just not converting;
    a zero count means the messages aren't being opened/clicked at all."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor.execute("""
        SELECT metadata FROM events
        WHERE event_type = 'landing_visit' AND created_at > ?
    """, (cutoff,))
    rows = cursor.fetchall()
    conn.close()

    import json
    counts = {}
    for row in rows:
        metadata = json.loads(row[0]) if row[0] else {}
        source = metadata.get("source") or "direct"
        counts[source] = counts.get(source, 0) + 1

    return sorted(
        [{"source": source, "count": count} for source, count in counts.items()],
        key=lambda r: -r["count"]
    )

def get_signups_by_source(days: int = 30) -> list:
    """Signup counts grouped by ?src= attribution (Direct Reach, BiggerPockets,
    LinkedIn, etc.), most recent-heavy channels first. 'direct' covers anyone
    who signed up without an src param (e.g. typed the URL directly)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor.execute("""
        SELECT metadata FROM events
        WHERE event_type = 'signup' AND created_at > ?
    """, (cutoff,))
    rows = cursor.fetchall()
    conn.close()

    import json
    counts = {}
    for row in rows:
        metadata = json.loads(row[0]) if row[0] else {}
        source = metadata.get("source") or "direct"
        counts[source] = counts.get(source, 0) + 1

    return sorted(
        [{"source": source, "count": count} for source, count in counts.items()],
        key=lambda r: -r["count"]
    )

def get_waitlist_signups(limit: int = 200) -> list:
    """All waitlist signups, most recent first -- grouped by state on
    /analytics so demand for a specific state is visible at a glance."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT phone, metadata, created_at FROM events
        WHERE event_type = 'waitlist_joined'
        ORDER BY created_at DESC LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    import json
    results = []
    for row in rows:
        metadata = json.loads(row[1]) if row[1] else {}
        results.append({
            "phone": row[0],
            "state": metadata.get("state") or "Unknown",
            "created_at": row[2],
        })
    return results

init_analytics_tables()
