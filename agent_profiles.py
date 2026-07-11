"""
agent_profiles.py — Store agent-specific defaults (name, license, title company, etc)
so their info auto-fills every offer. In production, this should be a database.
For now, use a simple dict keyed by phone number.
"""

# In production: replace with database lookup
AGENT_PROFILES = {
    # Example profile
    "demo-web": {
        "name": "Demo Agent",
        "license": "TX-123456",
        "phone": "(512) 555-0100",
        "email": "agent@example.com",
        "brokerage": "Example Realty",
        "title_company": "Texas Title Co.",
        "default_earnest_pct": 0.01,  # 1% of price
        "default_option_fee": 250,
    },
}

def get_agent_profile(source_id: str) -> dict:
    """
    Returns agent profile defaults, or generic defaults if agent not found.
    source_id is either phone number (+15125550100) or 'demo-web'
    """
    return AGENT_PROFILES.get(source_id, {
        "name": "",
        "license": "",
        "phone": source_id if source_id.startswith("+") else "",
        "email": "",
        "brokerage": "",
        "title_company": "",
        "default_earnest_pct": 0.01,
        "default_option_fee": 250,
    })

def register_agent(source_id: str, profile: dict):
    """
    Save agent profile. In production, write to database.
    For now, just updates the in-memory dict.
    """
    AGENT_PROFILES[source_id] = profile
