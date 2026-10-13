"""
trust_center.py -- body of the Trust Center (/trust). The page shell (head,
base styles, nav) is the Privacy Policy's own; app.py stitches this in.

Every statement here must stay true to the Privacy Policy, the Terms and the
code. Sources, as of 2026-10-13:
- file handling + retention: Privacy Policy §5 (temp file, deleted after the
  check; TC Check records ~90 days; archive 5 years; drafts 30 days free /
  5 years paid)
- subprocessors: Privacy Policy §4
- security: Privacy Policy §6 (HTTPS, Stripe PCI DSS L1, founder-only access),
  signed expiring PDF links (sign_pdf_url), rate_limit.py
- "no AI reads your contract": no model/LLM dependency anywhere in the repo;
  TC Check is rule code (tc_audit.py, co_audit.py, co_forms.py)
- rights: Privacy Policy §7
Update this page whenever any of those change.
"""

EXTRA_CSS = """
<style>
  .container.trust {max-width:960px;}
  .trust-hero p {font-size:1rem;color:var(--text-muted);max-width:640px;margin-top:0.5rem;line-height:1.6;}
  .trust-cards {display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin:2rem 0 2.5rem;}
  .trust-card {background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-sm);padding:1.25rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);display:flex;flex-direction:column;}
  .trust-card h3 {font-size:0.95rem;font-weight:700;margin-bottom:0.4rem;}
  .trust-card p {font-size:0.82rem;color:var(--text-muted);line-height:1.55;margin-bottom:0.8rem;}
  .trust-card a {margin-top:auto;font-size:0.82rem;font-weight:600;color:var(--accent-dark);text-decoration:underline;text-underline-offset:2px;}
  .trust-section {background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:2rem;margin-bottom:1.25rem;box-shadow:0 1px 3px rgba(15,31,47,0.05);}
  .trust-section h2 {font-size:1.2rem;font-weight:800;letter-spacing:-0.01em;margin-bottom:0.35rem;}
  .trust-section .sub {font-size:0.88rem;color:var(--text-muted);margin-bottom:1.25rem;line-height:1.6;}
  .trust-section p, .trust-section li {font-size:0.86rem;color:var(--text-muted);line-height:1.7;}
  .trust-section ul {padding-left:1.2rem;margin:0.4rem 0 0.6rem;}
  .trust-section li {list-style:disc;margin-bottom:0.3rem;}
  .trust-section strong {color:var(--text);}
  .trust-section a {color:var(--accent-dark);text-decoration:underline;text-underline-offset:2px;}
  .flow {display:grid;grid-template-columns:repeat(4,1fr);gap:0.75rem;margin:0.5rem 0 1rem;}
  .flow div {background:var(--accent-tint);border-radius:var(--radius-sm);padding:0.9rem;font-size:0.82rem;color:var(--text);line-height:1.5;}
  .flow b {display:block;font-size:0.7rem;letter-spacing:0.08em;text-transform:uppercase;color:var(--accent-dark);margin-bottom:0.25rem;}
  .note {background:#fff8e6;border:1px solid #f2dfa8;border-radius:var(--radius-sm);padding:0.8rem 1rem;font-size:0.83rem;color:#5c4a12;margin-top:0.75rem;}
  .vendors {width:100%;border-collapse:collapse;margin-top:0.5rem;}
  .vendors td {font-size:0.84rem;color:var(--text-muted);padding:0.55rem 0.4rem;border-top:1px solid var(--border);vertical-align:top;}
  .vendors td:first-child {font-weight:700;color:var(--text);white-space:nowrap;width:110px;}
  .report-grid {display:grid;grid-template-columns:repeat(2,1fr);gap:0.75rem;margin-top:0.5rem;}
  .report-btn {display:block;border:1px solid var(--border);border-radius:var(--radius-sm);padding:0.9rem 1rem;text-decoration:none !important;background:#fff;}
  .report-btn b {display:block;font-size:0.9rem;color:var(--text);margin-bottom:0.2rem;}
  .report-btn span {font-size:0.8rem;color:var(--text-muted);}
  .report-btn:hover {border-color:var(--accent);}
  @media(max-width:820px){ .trust-cards{grid-template-columns:1fr 1fr;} .flow{grid-template-columns:1fr 1fr;} }
  @media(max-width:520px){ .trust-cards,.flow,.report-grid{grid-template-columns:1fr;} .trust-section{padding:1.4rem 1.1rem;} }
</style>
"""


def _mail(subject: str) -> str:
    return "mailto:support@txtanoffer.com?subject=" + subject.replace(" ", "%20")


def body() -> str:
    return f"""
<div class="container trust">
  <div class="page-header trust-hero">
    <h1>Trust Center</h1>
    <div class="updated">Last reviewed October 13, 2026</div>
    <p>How TxtAnOffer handles your contracts, your data and your trust &mdash; in plain language. Everything here matches our <a href="/privacy" style="color:var(--accent-dark);text-decoration:underline;">Privacy Policy</a> and <a href="/terms" style="color:var(--accent-dark);text-decoration:underline;">Terms</a>; if anything ever differs, those documents control.</p>
  </div>

  <div class="trust-cards">
    <div class="trust-card"><h3>Your contracts</h3><p>What happens to a file you check: read, checked, deleted.</p><a href="#contracts">How files are handled &darr;</a></div>
    <div class="trust-card"><h3>How TC Check works</h3><p>Rule-based checks tied to the official forms. No AI reads your contract.</p><a href="#how-it-works">What it checks &darr;</a></div>
    <div class="trust-card"><h3>Your data &amp; rights</h3><p>We don&rsquo;t sell data. Who we share with, how long we keep it, and how to ask for it back.</p><a href="#data">Data &amp; rights &darr;</a></div>
    <div class="trust-card"><h3>Report a problem</h3><p>A wrong finding, a privacy request, a security issue or an unwanted message.</p><a href="#report">Report it &darr;</a></div>
  </div>

  <div class="trust-section" id="contracts">
    <h2>Your contracts</h2>
    <p class="sub">A contract is a confidential document. Here is exactly what happens when you upload one or forward it to tc@check.txtanoffer.com.</p>
    <div class="flow">
      <div><b>1 &middot; Receive</b>Your PDF is written to a temporary file.</div>
      <div><b>2 &middot; Check</b>Rule code reads the form fields and builds your report.</div>
      <div><b>3 &middot; Delete</b>The file is deleted, normally within seconds (bulk uploads: when the batch finishes).</div>
      <div><b>4 &middot; Record</b>We keep a short record &mdash; your email if you gave one and issue codes &mdash; not the file, the property address or its contents.</div>
    </div>
    <ul>
      <li><strong>Check records</strong> older than 90 days are removed by an automatic cleanup that doesn&rsquo;t run on a fixed schedule, so some may last a little longer.</li>
      <li><strong>Brokerage archive (opt-out):</strong> on the Brokerage plan, executed contracts a brokerage forwards from a linked email or uploads while signed in are kept for 5 years, visible only to that brokerage&rsquo;s signed-in users and exportable anytime. The brokerage can turn this off.</li>
      <li><strong>Drafts you generate by text</strong> are kept 30 days on the free trial and 5 years on a paid plan, in your dashboard.</li>
      <li><strong>Links to your PDFs</strong> are signed and expire &mdash; they can&rsquo;t be guessed from the file name.</li>
    </ul>
    <div class="note">Our retention is not your record-keeping. Brokers have their own legal duty to keep transaction records (in Texas, TREC Rule &sect;535.2) &mdash; keep your own copies.</div>
  </div>

  <div class="trust-section" id="how-it-works">
    <h2>How TC Check works</h2>
    <p class="sub">Predictable, explainable checks &mdash; not a black box.</p>
    <ul>
      <li><strong>No AI reads your contract.</strong> TC Check is rule code: each rule checks specific fields of the official form and names the paragraph it&rsquo;s about.</li>
      <li><strong>Built on the official forms.</strong> Texas checks map to TREC&rsquo;s published 20-19 and its addenda; Colorado checks map to the Colorado Real Estate Commission&rsquo;s CBS1 and related forms. Field positions are verified by rendering the real form, because field names on these PDFs are sometimes wrong.</li>
      <li><strong>We publish what it checks &mdash; and what it doesn&rsquo;t.</strong> See the lists on <a href="/tc-check">TC Check</a> (Texas) and <a href="/tc-check?state=co#colorado">TC Check &middot; Colorado</a>. It doesn&rsquo;t verify signatures or read scanned or flattened PDFs.</li>
      <li><strong>Likely issues, not legal advice.</strong> A clean report means none of the checked items were found blank or inconsistent &mdash; not that the contract is correct. Your own review and your broker&rsquo;s still matter.</li>
      <li><strong>Severity is honest.</strong> Only things that are clearly wrong are marked as blockers; choices that can be intentional (like deleting a contingency) are flagged as warnings for you to confirm.</li>
    </ul>
    <p>Think a finding is wrong? <a href="{_mail('TC Check - finding looks wrong')}">Tell us</a> &mdash; false flags get fixed in the rules, for everyone.</p>
  </div>

  <div class="trust-section" id="data">
    <h2>Your data &amp; rights</h2>
    <p class="sub">We do not sell, rent or trade your personal information. We share it only with the services that run TxtAnOffer:</p>
    <table class="vendors">
      <tr><td>Twilio</td><td>Text messages &mdash; your phone number and message content</td></tr>
      <tr><td>Stripe</td><td>Payments &mdash; billing details. Card numbers never touch our servers (Stripe is PCI DSS Level 1).</td></tr>
      <tr><td>Railway</td><td>Hosting &mdash; our app server and database run there</td></tr>
      <tr><td>SendGrid</td><td>Inbound email &mdash; receives contracts forwarded to tc@check.txtanoffer.com; backup outbound email</td></tr>
      <tr><td>Resend</td><td>Outbound email &mdash; TC Check reports, offer emails and account notices</td></tr>
      <tr><td>DocuSign</td><td>Only when you send an offer for signature: the PDF and the signers&rsquo; names and emails</td></tr>
      <tr><td>Your webhook</td><td>Only if you set one up (e.g. Zapier): offer data goes to the URL you choose</td></tr>
    </table>
    <ul style="margin-top:1rem;">
      <li><strong>Your rights:</strong> ask for a copy of your data, a correction or deletion &mdash; email <a href="{_mail('Privacy request')}">support@txtanoffer.com</a>.</li>
      <li><strong>Text messages:</strong> reply STOP to any message to opt out, START to opt back in, HELP for help.</li>
      <li><strong>Email:</strong> every outreach or marketing email says how to opt out &mdash; reply &ldquo;no&rdquo; or &ldquo;unsubscribe&rdquo; and we stop.</li>
      <li><strong>Legal requests:</strong> we may disclose information if required by law or legal process, as our Privacy Policy explains.</li>
    </ul>
  </div>

  <div class="trust-section" id="security">
    <h2>Security</h2>
    <p class="sub">Reasonable, plain measures &mdash; described honestly.</p>
    <ul>
      <li>The site is served over HTTPS.</li>
      <li>Payment card data is handled entirely by Stripe.</li>
      <li>PDF links are signed and expire.</li>
      <li>Request limits guard against floods and abuse.</li>
      <li>Processing is automated; TxtAnOffer&rsquo;s founder can access stored data for support and debugging, and no one else.</li>
    </ul>
    <p>No method of transmission over the internet is 100% secure, and we can&rsquo;t promise absolute security. Found a vulnerability? <a href="{_mail('Security issue')}">Report it privately</a> &mdash; please don&rsquo;t post it publicly first.</p>
  </div>

  <div class="trust-section" id="independence">
    <h2>Independence</h2>
    <ul>
      <li>TxtAnOffer is an independent tool. It is <strong>not affiliated with, endorsed by, or partnered with</strong> the Texas Real Estate Commission (TREC), the Colorado Real Estate Commission or the Colorado Division of Real Estate.</li>
      <li>Commission forms are used as published; the Service fills in their blanks and does not change their printed text.</li>
      <li>Form versions are tracked: <a href="/trec-changes">Texas 2026 changes</a> &middot; <a href="/tc-hub/colorado#changes">Colorado 2026 changes</a>.</li>
    </ul>
  </div>

  <div class="trust-section" id="report">
    <h2>Report a problem</h2>
    <p class="sub">Every report goes to the person who built TxtAnOffer.</p>
    <div class="report-grid">
      <a class="report-btn" href="{_mail('TC Check - finding looks wrong')}"><b>A finding looks wrong</b><span>A false flag, or something TC Check missed</span></a>
      <a class="report-btn" href="{_mail('Privacy request')}"><b>Privacy or data request</b><span>Access, correction or deletion</span></a>
      <a class="report-btn" href="{_mail('Security issue')}"><b>Security issue</b><span>Report a vulnerability privately</span></a>
      <a class="report-btn" href="{_mail('Stop contacting me')}"><b>Unwanted message</b><span>Stop emails or texts from us</span></a>
    </div>
  </div>

  <div class="trust-section">
    <h2>Who runs TxtAnOffer</h2>
    <p>TxtAnOffer is operated by Phanel Jean-Baptiste, 807 W 112th St, Los Angeles, CA 90044 &middot; <a href="mailto:support@txtanoffer.com">support@txtanoffer.com</a>. Read the full <a href="/privacy">Privacy Policy</a>, <a href="/terms">Terms of Service</a> and <a href="/privacy#sms-messaging">SMS Terms</a>.</p>
  </div>

  <p class="foot"><a href="/">&larr; Back to home</a> &middot; <a href="/privacy">Privacy</a> &middot; <a href="/terms">Terms</a> &middot; <a href="/faq">FAQ</a></p>
</div>
</body>
</html>"""
