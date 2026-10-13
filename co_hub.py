"""
co_hub.py -- body of the Colorado TC Hub (/tc-hub/colorado). The page shell
(head, styles, nav) is the Texas hub's own, so both hubs look identical;
app.py stitches this body into it.

Everything here is sourced: form names and dates are read from the
Commission's PDFs themselves (adoption / mandatory-use lines, checked
2026-10-12/13), checklists mirror what co_audit.py and co_forms.py actually
check, and HB26-1426 is described only as far as the Division of Real
Estate's own summary and advisory say. No Colorado mistake statistics are
shown until real (non-demo) Colorado files exist.
"""
import json

FORMS = [
    ("CBS1", "Contract to Buy and Sell Real Estate (Residential)",
     "The main contract for a Colorado residential resale. Its &sect;3.1 Dates and Deadlines table drives the whole file &mdash; and under &sect;3.2 a deadline left blank or marked N/A deletes that provision.",
     "Adopted August 5, 2025 &middot; mandatory January 1, 2026"),
    ("AE41", "Agreement to Amend/Extend Contract",
     "Changes dates or terms on an executed contract. Each &sect;3.1 row gets a new date, No Change, or Deleted.",
     "Mandatory August 2024"),
    ("CP40", "Counterproposal",
     "Answers an offer with changed terms &mdash; its own &sect;3.1 deadline table and &sect;4 price table.",
     "Mandatory August 2024"),
    ("NTC43", "Inspection Objection Notice",
     "Buyer's written list of unsatisfactory conditions, due on or before the Inspection Objection Deadline (&sect;10.3.2).",
     "Adopted August 5, 2025 &middot; mandatory January 1, 2026"),
    ("BC60", "Exclusive Right-to-Buy Listing Contract",
     "The buyer's written agreement with their brokerage. Since August 12, 2026 (HB26-1426) a written agreement is required before a buyer's agent or transaction-broker performs licensed work for them.",
     "Adopted October 7, 2025 &middot; mandatory January 1, 2026"),
    ("SWA35", "Source of Water Addendum",
     "Discloses the property's source of potable water &mdash; well, water district or other provider.",
     "Mandatory January 2011"),
]

CHECKLISTS = [
    ("Before Sending an Offer", [
        "Street, city and county (&sect;2.4) are complete, and the legal description is filled in",
        "Buyer and Seller names are filled in (&sect;2.1, &sect;2.3)",
        "&sect;4.1: Earnest Money + New Loan + Cash at Closing = Purchase Price, and both TOTAL lines match",
        "Earnest Money Holder is named (&sect;4.3)",
        "A loan type is checked (&sect;4.5.3) when there's a New Loan",
        "Every &sect;3.1 deadline you mean to keep is filled &mdash; blank or N/A deletes it (&sect;3.2)",
        "Closing Date isn't on a weekend or a federal or Colorado holiday",
        "Acceptance Deadline is set, and before the Closing Date (&sect;27)",
        "Buyer's agent or transaction-broker? A signed Exclusive Right-to-Buy is on file (HB26-1426)",
    ]),
    ("After Execution", [
        "&sect;3.1 deadlines run in order: delivery &rarr; objection &rarr; resolution",
        "No &sect;3.1 deadline falls after the Closing Date",
        "Possession Date and Time are filled in &mdash; and not before closing unless agreed",
        "Lead-based paint deadlines are filled in (homes built before 1978) or marked N/A",
        "A Source of Water Addendum listed in &sect;31 is actually attached",
        "All required signatures are present",
    ]),
    ("Amend/Extend &amp; Counterproposal Review", [
        "Each changed row has only one of: new date, No Change, Deleted",
        "New dates still run in order and before the (amended) Closing Date",
        "&ldquo;Deleted&rdquo; on a loan, appraisal or inspection row removes that protection &mdash; intended?",
        "It names the right property and original contract date",
        "A Counterproposal's &sect;4 price table still adds up",
    ]),
    ("Inspection Objection", [
        "Delivered on or before the Inspection Objection Deadline &mdash; as amended (&sect;10.3.2)",
        "Describes what is unsatisfactory",
        "Inspection Resolution Deadline is tracked (&sect;10.3.3)",
    ]),
]


def _li(items):
    return "".join(f"<li>{i}</li>" for i in items)


def body() -> str:
    forms = "".join(
        f'<div class="form-card"><div class="form-num">{num}</div><h3>{name}</h3><p>{desc}</p>'
        f'<div class="form-meta">{dates}</div>'
        f'<a class="form-link" href="https://dre.colorado.gov/real-estate-broker-contracts-and-forms" target="_blank" rel="noopener">Official Division of Real Estate source &rarr;</a></div>'
        for num, name, desc, dates in FORMS)
    checklists = "".join(f'<div class="checklist-card"><h3>{t}</h3><ul>{_li(items)}</ul></div>' for t, items in CHECKLISTS)
    return f"""
<div class="container">
  <div class="page-header">
    <h1>Colorado TC Hub</h1>
    <p>Practical transaction-coordination resources for Colorado -- Colorado Real Estate Commission form references, file-review checklists built around the &sect;3.1 deadline table, and the 2026 changes.</p>
    <p style="font-size:0.82rem;margin-top:0.5rem;">Working in Texas? <a href="/tc-hub" style="color:var(--accent-dark);text-decoration:underline;">Texas TC Hub &rarr;</a></p>
  </div>

  <div class="hub-nav">
    <a href="#forms">Colorado Forms</a>
    <a href="#checklists">File-Review Checklists</a>
    <a href="#mistakes">Common TC Mistakes</a>
    <a href="#changes">2026 Changes</a>
    <a href="#checklist-download">Free Checklist</a>
  </div>

  <div class="section" id="forms">
    <h2>Colorado Forms</h2>
    <p class="section-sub">The Commission-approved forms a Colorado residential file runs through, with the adoption and mandatory-use dates printed on each form -- from the Division of Real Estate's own forms page, not a mirror.</p>
    <div class="form-grid">{forms}</div>
  </div>

  <div class="section" id="checklists">
    <h2>File-Review Checklists</h2>
    <p class="section-sub">Organized by the point in the transaction where each check matters -- built from the exact checks TC Check runs on a CBS1 and the forms attached to it, not a generic industry list.</p>
    <div class="checklist-grid">{checklists}</div>
  </div>

  <div class="section" id="mistakes">
    <h2>Common TC Mistakes</h2>
    <p class="section-sub">What shows up blank or inconsistent on real Colorado files, ranked by how often TC Check finds it.</p>
    <div class="mistakes-card">
      <p style="font-size:0.85rem;color:var(--text-muted);">No data yet -- check back after real Colorado files have been run through TC Check.</p>
      <p class="mistakes-note">Not enough real-world CBS1 files have been audited yet to publish an honest breakdown. TC Check is built to catch blank &sect;3.1 deadlines that delete a contingency, &sect;4.1 price math, and deadlines out of order or after closing -- run your own file at <a href="/tc-check?state=co" style="color:var(--accent-dark);text-decoration:underline;">/tc-check</a> to see where yours stands.</p>
    </div>
  </div>

  <div class="section" id="changes">
    <h2>2026 Changes</h2>
    <div class="changes-pointer">
        <p style="margin-bottom:0.6rem;"><b>New CBS1 and Inspection Objection Notice.</b> Adopted August 5, 2025; mandatory since January 1, 2026.</p>
        <p style="margin-bottom:0.6rem;"><b>New Exclusive Right-to-Buy (BC60).</b> Adopted October 7, 2025; mandatory since January 1, 2026.</p>
        <p style="margin-bottom:0.6rem;"><b>HB26-1426 (effective August 12, 2026).</b> A broker must have a written agreement with the buyer or seller -- for transaction-brokerage as well as agency -- before performing licensed duties for them. The Division says only a listing contract establishes the relationship; the buyer's broker compensation agreement on the disclosure form isn't enough. <a href="https://dre.colorado.gov/hb26-1426-summary" target="_blank" rel="noopener" style="color:var(--accent-dark);text-decoration:underline;">Division summary &rarr;</a></p>
    </div>
  </div>

  <div class="section" id="checklist-download" style="margin-bottom:2rem;">
    <div class="cta-card">
      <div class="cta-kicker">Colorado TC Checklist &mdash; Free</div>
      <h3>Before submitting a transaction, check these 15 items.</h3>
      <p>The combined "Before Sending" + "After Execution" checklist above, as a plain-text file you can keep on hand.</p>
      <div class="cta-buttons">
        <button class="cta-primary" onclick="downloadCoChecklist()">Download checklist</button>
        <a class="cta-secondary" href="/tc-check?state=co">Want the computer to check it instead? Try TC Check &rarr;</a>
      </div>
    </div>
  </div>

  <div class="sources-box">
    <h3>Verify this yourself</h3>
    <p>This page is a courtesy summary, not the authoritative text. The Commission's forms, rules and the statute are the source.</p>
    <ul>
      <li><a href="https://dre.colorado.gov/real-estate-broker-contracts-and-forms" target="_blank" rel="noopener">Real Estate Broker Contracts and Forms</a> -- the Division of Real Estate's forms library.</li>
      <li><a href="https://dre.colorado.gov/program-rules-notices-of-rule-making-hearings" target="_blank" rel="noopener">Division of Real Estate rules</a> -- including the Rules Regarding Real Estate Brokers (4 CCR 725-1).</li>
      <li><a href="https://dre.colorado.gov/division-notifications/real-estate-broker-advisory-signed-listing-agreements-required-by-new-law" target="_blank" rel="noopener">Broker advisory: signed listing agreements required by new law</a> -- the Division's HB26-1426 advisory.</li>
    </ul>
  </div>

  <div class="disclaimer-box">
    <strong>This page is educational information only -- it is not legal advice and is not a substitute for advice from a licensed Colorado real estate attorney or your employing broker.</strong> TxtAnOffer is an independent, third-party tool and is NOT affiliated with, endorsed by, or partnered with the Colorado Real Estate Commission or the Colorado Division of Real Estate. Form numbers referenced above are the Commission's designations. Checklists reflect what TC Check verifies on a file; they are not a complete substitute for your own professional judgment or your broker's review requirements. See our <a href="/terms" style="color:var(--text);text-decoration:underline;">Terms of Service</a> for the full disclaimer.
  </div>

  <p class="foot">Questions? Email <a href="mailto:support@txtanoffer.com">support@txtanoffer.com</a>.<br><a href="/?state=co">&larr; Back to home</a> &middot; <a href="/tc-hub">Texas TC Hub</a> &middot; <a href="/faq">FAQ</a> &middot; <a href="/terms">Terms</a></p>
</div>

<script>
function downloadCoChecklist(){{
  var lines = ['Colorado TC Checklist -- TxtAnOffer', 'txtanoffer.com/tc-hub/colorado', ''];
  var groups = {json.dumps([[t, items] for t, items in CHECKLISTS[:2]])};
  groups.forEach(function(g){{
    lines.push(g[0].toUpperCase());
    g[1].forEach(function(i){{ lines.push('[ ] ' + i); }});
    lines.push('');
  }});
  lines.push('Educational reference only -- not legal advice. See txtanoffer.com/tc-hub/colorado for sourcing and disclaimer.');
  var txt = lines.join('\\n').replace(/&sect;/g,'Sec. ').replace(/&rarr;/g,'->').replace(/&mdash;/g,'--').replace(/&amp;/g,'&');
  var blob = new Blob([txt], {{type: 'text/plain'}});
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url; a.download = 'colorado-tc-checklist.txt';
  document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
}}
</script>
</body>
</html>"""
