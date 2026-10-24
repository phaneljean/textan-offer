"""
text_offer_page.py -- /text-an-offer, the "Learn more" page behind the
homepage's Text an offer section (2026-10-24, Phanel's ask): what to put in
the text, what the reply looks like, what gets filled and what doesn't.
Same look as the homepage (its variables, nav and footer); app.py stitches
in _SITE_CHROME_CSS / _site_nav / _SITE_FOOTER.

Every statement here is what the code does, as of 2026-10-24. Sources:
- accepted formats + limits: parser.py (_parse_price, _parse_pct,
  _parse_days, FINANCING_RE, INSPECTION_RE, HOA_RE, COUNTY_RE,
  parse_offer_sms range checks, FIELD_HINTS order)
- address + state checks: app.py validate_address(), build_offer_draft(),
  format_tx_confirmation() / TX_NEEDS_STATE_MESSAGE, other_state_block_message()
- YES / NO / corrections / AMEND / keywords: app.py sms_reply(),
  drafts.DRAFT_TTL_MINUTES (30)
- what comes back: app.py finalize_offer_sms() (forms list), pdf_filler.py
  fill_offer_pdf() (fields, cover + certification pages), financing_addendum.py,
  hoa_addendum.py, water_disclosure.py, iabs.py, agent_profiles.py defaults
  (1% earnest, $250 option fee)
- the three worked examples: real build_offer_draft() + fill_offer_pdf()
  output; tests/test_text_offer_page.py re-runs them so a parser or money-math
  change fails the suite instead of leaving this page wrong.
- free offers / price: subscriptions.FREE_OFFER_LIMIT (3) and can_generate_offer(),
  /pricing Agent card ($40/mo)

Not said on purpose: "close in 21 days" and "21 day close" are parsed, but
when the street number follows them directly ("close in 21 days 123 Main
St") parser._parse_address() strips the street number too, so the page only
teaches "21day" / "21 days" / "close Dec 15". Property lookup (lookup_mls)
depends on APIFY_API_TOKEN, so city/county are only promised when typed.
"""

CSS = """
  /* ---------- /text-an-offer (text_offer_page.py) ---------- */
  .to-hero{background:var(--green-deep);color:#fff;padding:96px 22px 88px}
  .to-hero .eyebrow{color:#9fd4c9}
  .to-h1{font-size:56px;line-height:1.07}
  .to-hero .sub{color:#b9d3ce}
  .to-hero .btn.solid{background:#fff;color:var(--green-deep);border-color:#fff}
  .to-hero .btn.ghost{color:#fff;border-color:rgba(255,255,255,.55)}
  .to-hero .fine{color:#8fb1aa;max-width:560px;margin-left:auto;margin-right:auto}
  .to-hero .fine a{text-decoration:underline}
  .to-sec{padding:88px 22px 0;text-align:left;max-width:900px;margin:0 auto}
  .to-sec h2{font-size:40px;text-align:left}
  .to-sec .lead{font-size:19px;line-height:1.5;color:var(--mute);margin-top:14px;max-width:720px}
  .to-sec p,.to-sec li{font-size:17px;line-height:1.55}
  .to-sec a{color:var(--green);text-decoration:underline;text-underline-offset:2px}
  .to-sms{display:inline-block;font-family:"SF Mono",Menlo,monospace;font-size:15px;background:var(--gray);border:1px solid var(--line);border-radius:8px;padding:2px 8px;white-space:nowrap}
  .to-big{display:block;font-family:"SF Mono",Menlo,monospace;font-size:20px;font-weight:600;background:var(--green);color:#fff;border-radius:18px 18px 6px 18px;padding:14px 18px;margin:28px 0 0 auto;max-width:520px;overflow-wrap:anywhere}
  .to-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px;margin-top:28px}
  .to-card{border:1px solid var(--line);border-radius:18px;padding:22px 22px 20px;background:#fff}
  .to-card h3{font-size:19px;font-weight:700;letter-spacing:-.01em}
  .to-card .req{font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--green);display:block;margin-bottom:6px}
  .to-card .opt{color:var(--mute)}
  .to-card p{font-size:16px;color:var(--mute);margin-top:8px}
  .to-card .to-sms{margin:6px 6px 0 0;font-size:14px}
  .to-ex{border:1px solid var(--line);border-radius:18px;margin-top:18px;overflow:hidden}
  .to-ex .in{background:var(--gray);padding:14px 20px;font-family:"SF Mono",Menlo,monospace;font-size:16px;font-weight:600;overflow-wrap:anywhere}
  .to-ex table{width:100%;border-collapse:collapse}
  .to-ex td{padding:10px 20px;border-top:1px solid var(--line);font-size:16px;vertical-align:top}
  .to-ex td:first-child{color:var(--mute);width:46%}
  .to-ex td:last-child{font-weight:600}
  .to-steps{list-style:none;counter-reset:s;margin-top:28px}
  .to-steps li{counter-increment:s;position:relative;padding:0 0 26px 52px}
  .to-steps li:before{content:counter(s);position:absolute;left:0;top:0;width:34px;height:34px;border-radius:50%;background:var(--green);color:#fff;font-weight:700;display:grid;place-items:center;font-size:15px}
  .to-steps b{display:block;font-size:19px;margin-bottom:4px}
  .to-steps p{color:var(--mute)}
  .to-reply{font-family:"SF Mono",Menlo,monospace;font-size:14px;line-height:1.5;background:#e9e9eb;border-radius:18px 18px 18px 6px;padding:12px 16px;margin-top:10px;max-width:440px;white-space:pre-line;color:var(--ink)}
  .to-two{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:28px}
  .to-two ul{padding-left:20px;margin-top:10px}
  .to-two li{font-size:16px;margin-bottom:8px;color:var(--ink)}
  .to-two li span{color:var(--mute)}
  .to-note{background:#fff8e6;border:1px solid #f2dfa8;border-radius:14px;padding:16px 18px;margin-top:22px;font-size:16px;color:#5c4a12;line-height:1.5}
  .to-cmds{width:100%;border-collapse:collapse;margin-top:18px}
  .to-cmds td{padding:10px 0;border-top:1px solid var(--line);font-size:16px;vertical-align:top}
  .to-cmds td:first-child{width:42%;padding-right:14px}
  .to-end{text-align:center;padding:88px 22px 96px}
  @media (max-width:734px){
    .to-hero{padding:64px 22px 64px}
    .to-h1{font-size:38px}
    .to-sec{padding-top:64px}
    .to-sec h2{font-size:30px}
    .to-sec .lead{font-size:17px}
    .to-big{font-size:16px}
    .to-grid,.to-two{grid-template-columns:1fr}
    .to-ex td{display:block;width:auto!important;padding:4px 16px}
    .to-ex td:first-child{padding-top:10px}
    .to-ex td:last-child{border-top:none;padding-bottom:10px}
    .to-ex .in{font-size:14px;padding:12px 16px}
    .to-cmds td{display:block;width:auto!important;padding:3px 0}
    .to-cmds td:first-child{padding-top:10px}
    .to-cmds td:last-child{border-top:none;padding-bottom:10px}
    .to-end{padding:64px 22px 72px}
  }
"""

# The consent line is the homepage section's own, word for word.
SMS_CONSENT = ('By texting, you agree to receive SMS replies and reminders about your offers, plus up to 2 tips '
               'on the free trial. Reply STOP to opt out, HELP for help. Msg frequency varies. Msg &amp; data '
               'rates may apply. <a href="/privacy">Privacy</a> &middot; <a href="/terms">Terms</a>')

# (text, [(what it becomes, value)]) -- checked against the real code by
# tests/test_text_offer_page.py.
EXAMPLES = [
    ("725k 3% 21day 123 Main St, Austin, TX", [
        ("2A Property, City of", "123 Main St, Austin"),
        ("3A Cash portion", "$21,750"),
        ("3B Sum of all financing", "$703,250 (Third Party Financing box checked)"),
        ("3C Sales Price", "$725,000"),
        ("5A Earnest money / option fee", "$7,250 / $250 (your defaults)"),
        ("9A Closing date", "21 days from today"),
        ("Forms", "20-19, 40-11 (Conventional, $703,250), 61-0, IABS"),
    ]),
    ("725k cash 21day 123 Main St, Austin, TX", [
        ("3A Cash portion", "$725,000"),
        ("3B Sum of all financing", "left blank, no financing box"),
        ("3C Sales Price", "$725,000"),
        ("Forms", "20-19, 61-0, IABS (no 40-11)"),
    ]),
    ("1.2m 20% FHA 45 days 10-day option HOA 123 Main St, Dallas, TX 75201", [
        ("3A / 3B / 3C", "$240,000 / $960,000 / $1,200,000"),
        ("5B Option period", "10 days"),
        ("9A Closing date", "45 days from today"),
        ("40-11", "FHA box checked, $960,000"),
        ("HOA", "36-10 HOA addendum attached, its box in Paragraph 22 checked"),
    ]),
]


def _example_html(text, rows):
    trs = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows)
    return f'<div class="to-ex"><div class="in">{text}</div><table>{trs}</table></div>'


def body() -> str:
    examples = "".join(_example_html(t, r) for t, r in EXAMPLES)
    return f"""
<!-- 1. HERO: same buttons and consent line as the homepage section -->
<section class="to-hero">
  <div class="eyebrow">Text an offer</div>
  <h1 class="to-h1">From one text<br>to a TREC 20&#8209;19 draft.</h1>
  <p class="sub">No laptop, no retyping the price math. Text the price, down payment, closing days and address. Reply YES and the 20&#8209;19 comes back with those terms filled in, plus the forms that go with it, ready for you to review.</p>
  <div class="ctas">
    <a class="btn solid" href="sms:+18338970333" data-evt="workflow_offer_sms">Text (833) 897-0333</a>
    <a class="btn ghost" href="/pricing">See pricing</a>
  </div>
  <p class="fine">{SMS_CONSENT}</p>
</section>

<!-- 2. WHAT TO TEXT -->
<section class="to-sec" id="what-to-text">
  <h2>What to put in the text</h2>
  <p class="lead">Four things, in one message. It reads them with fixed rules, not AI, so shorthand works as long as it&rsquo;s one of the forms below.</p>
  <span class="to-big">725k 3% 21day 123 Main St, Austin, TX</span>
  <div class="to-grid">
    <div class="to-card"><span class="req">Required</span><h3>Price</h3>
      <span class="to-sms">725k</span><span class="to-sms">725,000</span><span class="to-sms">725000</span><span class="to-sms">1.2m</span>
      <p>Up to $50 million.</p></div>
    <div class="to-card"><span class="req">Required</span><h3>Down payment</h3>
      <span class="to-sms">3%</span><span class="to-sms">3 percent</span><span class="to-sms">3 pct</span><span class="to-sms">3 down</span><span class="to-sms">cash</span>
      <p>More than 0% and up to 50%. &ldquo;cash&rdquo; with no percent means an all-cash offer.</p></div>
    <div class="to-card"><span class="req">Required</span><h3>Closing</h3>
      <span class="to-sms">21day</span><span class="to-sms">21 days</span><span class="to-sms">close Dec 15</span>
      <p>Counted from today. Between 7 and 365 days out. A date that has already passed this year means next year.</p></div>
    <div class="to-card"><span class="req">Required</span><h3>Address</h3>
      <span class="to-sms">123 Main St, Austin, TX</span>
      <p>Street number, street name and type (St, Ave, Dr, Ln, Rd, Blvd and others). Add the city and TX, or a Texas ZIP, so it can confirm the property is in Texas.</p></div>
    <div class="to-card"><span class="req opt">Optional</span><h3>Financing type</h3>
      <span class="to-sms">conventional</span><span class="to-sms">FHA</span><span class="to-sms">VA</span><span class="to-sms">USDA</span><span class="to-sms">Texas Veterans</span><span class="to-sms">reverse mortgage</span>
      <p>Checks that box on the 40&#8209;11. Leave it out and the 40&#8209;11 starts as Conventional.</p></div>
    <div class="to-card"><span class="req opt">Optional</span><h3>Option period, HOA, county</h3>
      <span class="to-sms">10-day option</span><span class="to-sms">10 day inspection</span><span class="to-sms">HOA</span><span class="to-sms">Travis</span>
      <p>Option days go in 5B. &ldquo;HOA&rdquo; attaches the 36&#8209;10 addendum. A county name it knows (Travis, Harris, Bexar, Dallas, Tarrant, Collin, Denton, Williamson, Fort Bend, Montgomery, El Paso, Hidalgo, Cameron) goes in 2A.</p></div>
  </div>
</section>

<!-- 3. REAL EXAMPLES -->
<section class="to-sec" id="examples">
  <h2>Three texts and what they become</h2>
  <p class="lead">Run through the real code. Earnest money starts at 1% of the price and the option fee at $250; you can change both in your profile.</p>
  {examples}
</section>

<!-- 4. THE CONVERSATION -->
<section class="to-sec" id="how-it-works">
  <h2>What happens after you send it</h2>
  <ol class="to-steps">
    <li><b>It texts back a summary first</b>
      <p>Nothing is created yet. You see the address, price, down payment, loan, earnest money, option fee and closing date, so a typo is caught before it&rsquo;s on a contract.</p>
      <div class="to-reply">TxtAnOffer: got it.

123 Main St, Austin, TX
Price $725,000, 3% down ($21,750)
Loan $703,250
Earnest $7,250, option fee $250
Closing (date) (21 days)

Reply YES to create the TREC draft, or text a correction (ex: "make it 820k").</div></li>
    <li><b>Reply YES, NO or a correction</b>
      <p>YES creates the draft. NO cancels it. Or text just the change, like <span class="to-sms">make it 820k</span>, <span class="to-sms">close in 25 days</span> or <span class="to-sms">FHA instead</span>, and you get a new summary. A summary waits 30 minutes for your answer.</p></li>
    <li><b>Open the link</b>
      <p>The reply has a private link to review and email the draft. The link is signed and expires. The PDF has a one-page summary on top, then the TREC 20&#8209;19, the 40&#8209;11 Third Party Financing Addendum when there&rsquo;s a loan, the 36&#8209;10 HOA addendum if you texted &ldquo;HOA&rdquo;, the 61&#8209;0 water disclosure, the IABS, and a certification page for you to sign at the end.</p></li>
  </ol>
  <h3 style="font-size:22px;margin-top:8px">When something is missing or unclear</h3>
  <ul style="padding-left:20px;margin-top:12px">
    <li style="margin-bottom:8px"><b>A required piece is missing.</b> It names the first one (price, then down payment, then closing, then address) and repeats what it did catch, e.g. &ldquo;Couldn&rsquo;t find a down payment %. Try something like 3%.&rdquo;</li>
    <li style="margin-bottom:8px"><b>The address has no street number or street type.</b> It asks you to send the full address.</li>
    <li style="margin-bottom:8px"><b>It can&rsquo;t tell the property is in Texas.</b> It asks you to resend with the city and state. YES won&rsquo;t create a TREC draft until it can.</li>
    <li style="margin-bottom:8px"><b>A number is out of range.</b> Under 7 or over 365 closing days, a down payment over 50%, or a price over $50 million gets a reply saying so.</li>
  </ul>
</section>

<!-- 5. FILLED VS. YOURS -->
<section class="to-sec" id="what-it-fills">
  <h2>What it fills, and what stays yours</h2>
  <div class="to-two">
    <div class="to-card"><h3>Filled in for you</h3><ul>
      <li>2A address, plus city and county <span>when your text names one it recognizes</span></li>
      <li>&ldquo;Address of Property&rdquo; at the top of each page</li>
      <li>3A, 3B and 3C, and the Third Party Financing boxes when there&rsquo;s a loan</li>
      <li>5A earnest money and option fee <span>(your defaults)</span></li>
      <li>5B option days <span>if you texted them</span></li>
      <li>9A closing date</li>
      <li>7D &ldquo;As Is&rdquo; and 10A possession at closing and funding <span>(starting defaults, check them)</span></li>
      <li>Your name, license, brokerage, phone, email and office address, and your title company as escrow agent and title insurer <span>(from your saved profile)</span></li>
      <li>40&#8209;11: financing type and loan amount, and &ldquo;subject to Buyer Approval&rdquo; checked</li>
      <li>61&#8209;0 and 36&#8209;10: the property address</li>
    </ul></div>
    <div class="to-card"><h3>Left for you</h3><ul>
      <li>Buyer and seller names</li>
      <li>Interest rate, loan term and origination limits on the 40&#8209;11</li>
      <li>Every answer on the 61&#8209;0 and 36&#8209;10</li>
      <li>Any other term you and your buyer agree on, and changing any filled-in default that doesn&rsquo;t fit this deal</li>
      <li>Initials and signatures</li>
    </ul></div>
  </div>
  <div class="to-note"><b>It&rsquo;s a draft for a licensed agent to review.</b> TxtAnOffer fills in the forms from what you text. It doesn&rsquo;t pick terms for you or give legal advice, and it doesn&rsquo;t send the offer to the other side; you do that from the review page. Review every page with your buyer before anyone signs.</div>
</section>

<!-- 6. STATES + COMMANDS -->
<section class="to-sec" id="states">
  <h2>Texas, and Colorado</h2>
  <p class="lead">Texas addresses get the TREC 20&#8209;19 and the forms above. Put CO or Colorado in the address and you get a Colorado CBS1 draft instead, with its own summary first. For an address in any other state, it stops and doesn&rsquo;t create a contract; reply WAITLIST to hear when that state is added. A TREC form is never created for a property outside Texas.</p>
  <h3 style="font-size:22px;margin-top:40px">Other things you can text</h3>
  <table class="to-cmds">
    <tr><td><span class="to-sms">AMEND 123 Main St price 730k</span></td><td>A TREC 39&#8209;11 amendment with the new sales price</td></tr>
    <tr><td><span class="to-sms">AMEND 123 Main St close +10</span></td><td>A 39&#8209;11 pushing closing back 10 days</td></tr>
    <tr><td><span class="to-sms">PROFILE</span></td><td>A link to save your name, license, brokerage, title company and defaults, so they fill in every time</td></tr>
    <tr><td><span class="to-sms">DASHBOARD</span></td><td>A link to every offer you&rsquo;ve made</td></tr>
    <tr><td><span class="to-sms">STATUS</span></td><td>Your plan and how many free offers are left</td></tr>
    <tr><td><span class="to-sms">HELP</span></td><td>This list, by text</td></tr>
  </table>
</section>

<!-- 7. PRICE + PRIVACY -->
<section class="to-sec" id="price">
  <h2>What it costs</h2>
  <p class="lead">Your first 3 offers are free. After that it&rsquo;s the Agent plan, $40/mo for unlimited offers, and agents on a Brokerage plan aren&rsquo;t limited. <a href="/pricing">See pricing</a>.</p>
  <p class="lead">What we keep and for how long is in the <a href="/privacy">Privacy Policy</a> and the <a href="/trust">Trust Center</a>. Use of the service is covered by the <a href="/terms">Terms</a>.</p>
</section>

<section class="to-end">
  <h2 style="font-size:40px">Try it on 123 Main St.</h2>
  <p class="sub" style="font-size:19px">Text <b>725k 3% 21day 123 Main St, Austin, TX</b> and see the summary come back.</p>
  <div class="ctas">
    <a class="btn solid" href="sms:+18338970333?body=725k%203%25%2021day%20123%20Main%20St%2C%20Austin%2C%20TX" data-evt="workflow_offer_sms">Text (833) 897-0333</a>
    <a class="btn ghost" href="/pricing">See pricing</a>
  </div>
  <p class="fine" style="max-width:560px;margin-left:auto;margin-right:auto">{SMS_CONSENT}</p>
</section>
"""
