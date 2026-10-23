"""
first_screen.py -- the copy and the source matching for the first screen of
the homepage and /tc-check (2026-10-23, from txo-product-strategist's
homepage-first-screen audit, copy reviewed by txo-trec-qa the same day).

Why: about 80% of tagged traffic lands on /tc-check, whose first screen on a
phone was "TC File Check" plus an empty upload box -- nothing to see if you
had no PDF at hand. The homepage's report card (the proof) sat below the fold
on a phone. So both pages now show sample findings and a no-file sample
button first, and /tc-check matches the line to the link's ?src= tag.

Every visible string the 2026-10-23 change added lives in COPY below, so a
wording change from review is a one-line edit here. app.py's templates use
"__FS_<KEY>__" tokens; fill_tokens() swaps them in. Strings are HTML-ready
(entities, like the rest of the templates).
"""
import re

COPY = {
    # --- homepage hero -------------------------------------------------------
    "home_eyebrow": "For Texas &amp; Colorado transaction coordinators and managing brokers",
    # Texas only (trec-qa): Effective Date blank, initials and 3A+3B=3C are
    # all live tc_audit rules. The Colorado subhead is unchanged in app.py.
    "home_sub_tx": "Drop a filled TREC 20-19. In seconds you see the blank Effective Date, the missing initial, the price that doesn&rsquo;t add up, before title sends it back.",
    "home_sample_btn": "See a sample report (no file needed) &rarr;",
    # Texas only: /tc-check/bulk's copy is TREC-only. "Up to 20" is
    # FREE_BULK_LIMIT (tc_bulk.py); not "last 20" -- they pick the files.
    "home_broker_line": "Managing broker? Check up to 20 of your agents&rsquo; closed contracts free&nbsp;&rarr;",

    # --- /tc-check -----------------------------------------------------------
    "tc_eyebrow": "TC File Check",
    "tc_h1": "Find the blank date or missing initial before title calls you back.",
    "tc_sub_tx": "Free check for a filled TREC 20-19. Built for Texas TCs, brokers and their agents.",
    "tc_sub_co": "Free check for a filled Colorado CBS1. Built for Colorado TCs, brokers and their agents.",
    "tc_sample_btn": "See a sample report &mdash; no file needed &rarr;",
    "tc_strip_label_tx": "Sample contract, 123 Main St, Austin, TX 78704, with planted mistakes. Not a client file.",
    "tc_strip_label_co": "Sample CBS1, 123 Main St, Denver, with planted mistakes. Not a client file.",

    # --- /tc-check, src ti_* / title_* (RESPA Section 8, RULES.md #3) --------
    # One generic line for every title company: no company name or logo,
    # nothing gated, no link to the sponsorship page, and the copied link is
    # the same for everyone (TITLE_SHARE_URL), so nothing is per-company.
    "title_line": "For title teams: TC Check is free for anyone. Share this link with the agents and TCs you work with.",
    "title_copy_btn": "Copy link",
    "title_copied": "Copied",
}

# What the title line's "Copy link" puts on the clipboard. Identical for every
# title company (trec-qa condition); its src is excluded from the title match
# below so the agents who open it see the normal page.
TITLE_SHARE_SRC = "title_share"
TITLE_SHARE_URL = "https://txtanoffer.com/tc-check?src=" + TITLE_SHARE_SRC

# The sample strip on /tc-check. Each message is the real display text of
# tc_audit / co_audit on static/sample_trec_20-19.pdf / sample_cbs1.pdf --
# the same verbatim (or verbatim leading clause) text as the homepage hero
# card. tests/test_first_screen.py fails if either drifts from the live
# output. (key, tag class, tag, message)
SAMPLE_ROWS = {
    "tx": [
        ("price", "b", "MUST FIX", "Paragraph 3: Cash portion (3A) $85,000 + financing (3B) $340,000 = $425,000, but the Sales Price (3C) says $435,000"),
        ("november31", "b", "MUST FIX", "Paragraph 9A: Closing date &ldquo;November 31&rdquo; is not a real date"),
        ("email", "w", "REVIEW", "Buyer&rsquo;s notice email (Paragraph 21) &ldquo;john.doe@gmailcom&rdquo; is not a valid email address"),
    ],
    "co": [
        ("price", "b", "MUST FIX", "&sect;4.1: Earnest Money + loan + Cash at Closing add up to $490,000, not the $500,000 Purchase Price"),
        ("loan", "w", "REVIEW", "&sect;3.1 item 15: New Loan Availability Deadline is N/A with a New Loan in &sect;4.1"),
        ("holiday", "w", "REVIEW", "&sect;3.1 item 39: Closing Date (11/11/2026) is Veterans Day"),
    ],
}


def source_variant(src):
    """Which source-matched first screen a ?src= tag gets on /tc-check:
    "title" for the title-company cold emails (ti_keydfw, title_reach_oct,
    co_ti_...), "november31" for the November 31 ads (ad_november31_fbtcg,
    ad_november31_facebook...), else None. Only the link's own src counts,
    not the ta_src cookie, so the line matches the message just clicked."""
    s = (src or "").lower()
    if s == TITLE_SHARE_SRC:
        return None
    if re.match(r"^(ti_|title_|co_ti_)", s):
        return "title"
    if "november31" in s:
        return "november31"
    return None


def sample_strip_rows(state, lead=None):
    """The strip's <li> rows for one state; `lead` moves that row first
    (the November 31 ad's hook)."""
    rows = list(SAMPLE_ROWS[state])
    rows.sort(key=lambda r: r[0] != lead)  # stable: the rest keep their order
    return "".join(
        f'<li data-row="{key}"><span class="ss-tag {cls}">{tag}</span><span class="ss-msg">{msg}</span></li>'
        for key, cls, tag, msg in rows
    )


def title_line_html():
    return (
        '<div class="title-line" id="titleLine"><p>' + COPY["title_line"] + '</p>'
        '<div class="title-link"><input type="text" readonly id="titleLinkUrl" value="' + TITLE_SHARE_URL + '" aria-label="Link to share">'
        '<button type="button" id="titleCopyBtn" data-evt="tc_title_copy_link" data-done="' + COPY["title_copied"] + '">'
        + COPY["title_copy_btn"] + '</button></div></div>'
    )


def fill_tokens(html, src=None):
    """Swap every __FS_<KEY>__ token for its COPY string, plus the
    src-dependent ones (/tc-check only; absent tokens are a no-op)."""
    variant = source_variant(src)
    html = (html.replace("__FS_TITLE_LINE__", title_line_html() if variant == "title" else "")
                .replace("__FS_STRIP_TX__", sample_strip_rows("tx", "november31" if variant == "november31" else None))
                .replace("__FS_STRIP_CO__", sample_strip_rows("co")))
    for key, value in COPY.items():
        html = html.replace("__FS_" + key.upper() + "__", value)
    return html
