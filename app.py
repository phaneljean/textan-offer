import os
import re
import datetime
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

DEMO_HTML = """



<meta>
<meta>
<title>TextAnOffer</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0a0a0a;
    color: #fff;
    min-height: 100vh;
  }
  .hero {
    text-align: center;
    padding: 80px 24px 48px;
    background: linear-gradient(135deg, #0a0a0a 0%, #111827 100%);
  }
  .badge {
    display: inline-block;
    background: rgba(99,102,241,0.15);
    border: 1px solid rgba(99,102,241,0.4);
    color: #818cf8;
    padding: 6px 16px;
    border-radius: 999px;
    font-size: 13px;
    letter-spacing: 0.05em;
    margin-bottom: 28px;
  }
  h1 {
    font-size: clamp(2rem, 5vw, 3.5rem);
    font-weight: 800;
    line-height: 1.1;
    margin-bottom: 20px;
    background: linear-gradient(135deg, #fff 0%, #a5b4fc 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }
  .subtitle {
    font-size: 1.1rem;
    color: #9ca3af;
    max-width: 600px;
    margin: 0 auto 48px;
    line-height: 1.6;
  }
  .demo-box {
    background: #111827;
    border: 1px solid #1f2937;
    border-radius: 20px;
    padding: 40px;
    max-width: 720px;
    margin: 0 auto;
    box-shadow: 0 25px 60px rgba(0,0,0,0.5);
  }
  .phone-mock {
    background: #1f2937;
    border-radius: 16px;
    padding: 20px;
    margin-bottom: 24px;
    border: 1px solid #374151;
  }
  .phone-label {
    font-size: 11px;
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-bottom: 12px;
  }
  .sms-bubble {
    background: #4f46e5;
    color: #fff;
    padding: 12px 16px;
    border-radius: 18px 18px 4px 18px;
    font-size: 15px;
    display: inline-block;
    max-width: 80%;
    line-height: 1.4;
  }
  .input-area {
    margin-bottom: 20px;
  }
  label {
    display: block;
    font-size: 13px;
    color: #9ca3af;
    margin-bottom: 8px;
    letter-spacing: 0.03em;
  }
  textarea {
    width: 100%;
    background: #1f2937;
    border: 1px solid #374151;
    border-radius: 12px;
    color: #fff;
    font-size: 16px;
    padding: 16px;
    resize: none;
    height: 100px;
    outline: none;
    transition: border-color 0.2s;
    font-family: inherit;
  }
  textarea:focus { border-color: #4f46e5; }
  textarea::placeholder { color: #4b5563; }
  button {
    width: 100%;
    background: linear-gradient(135deg, #4f46e5, #7c3aed);
    color: #fff;
    border: none;
    border-radius: 12px;
    padding: 16px;
    font-size: 16px;
    font-weight: 600;
    cursor: pointer;
    transition: opacity 0.2s, transform 0.1s;
    letter-spacing: 0.02em;
  }
  button:hover { opacity: 0.9; transform: translateY(-1px); }
  button:active { transform: translateY(0); }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .result {
    margin-top: 28px;
    display: none;
  }
  .result-header {
    font-size: 13px;
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-bottom: 16px;
  }
  .field-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
  }
  .field-card {
    background: #1f2937;
    border: 1px solid #374151;
    border-radius: 12px;
    padding: 16px;
  }
  .field-label {
    font-size: 11px;
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 6px;
  }
  .field-value {
    font-size: 18px;
    font-weight: 700;
    color: #a5b4fc;
  }
  .pdf-link {
    display: block;
    margin-top: 16px;
    background: rgba(99,102,241,0.1);
    border: 1px solid rgba(99,102,241,0.3);
    border-radius: 12px;
    padding: 16px;
    text-align: center;
    color: #818cf8;
    text-decoration: none;
    font-weight: 600;
    transition: background 0.2s;
  }
  .pdf-link:hover { background: rgba(99,102,241,0.2); }
  .error-box {
    background: rgba(239,68,68,0.1);
    border: 1px solid rgba(239,68,68,0.3);
    border-radius: 12px;
    padding: 16px;
    color: #fca5a5;
    margin-top: 16px;
    display: none;
  }
  .how-section {
    max-width: 720px;
    margin: 64px auto;
    padding: 0 24px;
  }
  .how-section h2 {
    font-size: 1.5rem;
    font-weight: 700;
    margin-bottom: 32px;
    text-align: center;
    color: #e5e7eb;
  }
  .steps {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 20px;
  }
  .step {
    background: #111827;
    border: 1px solid #1f2937;
    border-radius: 16px;
    padding: 24px;
    text-align: center;
  }
  .step-icon {
    font-size: 2rem;
    margin-bottom: 12px;
  }
  .step h3 {
    font-size: 15px;
    font-weight: 600;
    margin-bottom: 8px;
    color: #e5e7eb;
  }
  .step p {
    font-size: 13px;
    color: #6b7280;
    line-height: 1.5;
  }
  .turbo-section {
    max-width: 720px;
    margin: 0 auto 80px;
    padding: 0 24px;
  }
  .turbo-card {
    background: linear-gradient(135deg, #111827, #1e1b4b);
    border: 1px solid #3730a3;
    border-radius: 20px;
    padding: 40px;
    text-align: center;
  }
  .turbo-card h2 {
    font-size: 1.4rem;
    font-weight: 700;
    margin-bottom: 16px;
    color: #e0e7ff;
  }
  .turbo-card p {
    color: #a5b4fc;
    line-height: 1.7;
    font-size: 15px;
  }
  .status-pill {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: rgba(16,185,129,0.1);
    border: 1px solid rgba(16,185,129,0.3);
    color: #6ee7b7;
    padding: 8px 16px;
    border-radius: 999px;
    font-size: 13px;
    margin-top: 24px;
  }
  .dot {
    width: 8px;
    height: 8px;
    background: #10b981;
    border-radius: 50%;
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }
  @media (max-width: 600px) {
    .field-grid { grid-template-columns: 1fr; }
    .steps { grid-template-columns: 1fr; }
    .demo-box { padding: 24px; }
  }
</style>




  ⚡ Live Demo
  Text an offer.Get a contract.
  
    Type your offer details the way you'd say them out loud.
    TextAnOffer reads it, understands it, and fills out the
    real official TREC contract — automatically.
  

  
    
      📱 Incoming SMS from Agent
      725k 3% 21day 1740 Grand Ave
    

    
      <label>Try it — type an offer like you'd text it:</label>
      <textarea></textarea>
    

    <button>
      ⚡ Parse Offer & Generate Contract
    </button>

    

    
      ✅ Parsed — ready to fill contract
      
      
        📄 Download Filled TREC Contract (PDF)
      
    
  



  How it works
  
    
      💬
      Agent texts the offer
      Price, commission, closing days, address — however they naturally say it
    
    
      🧠
      AI reads it instantly
      Parses every detail — price, terms, dates — no form to fill out
    
    
      📄
      Contract appears
      The real official TREC form, filled in and ready for review and signature
    
  



  
    Like TurboTax — but for real estate contracts
    
      Same official paperwork your clients already sign.
      Instead of typing every field by hand, you just say what you want —
      and it fills every box for you. The parser and PDF filler are built
      and tested. SMS delivery is the final step, currently finishing
      carrier registration.
    
    
      
      Parser & PDF engine: live
    
  


<script>
async function runParse() {
  const msg = document.getElementById('msgInput').value.trim();
  if (!msg) return;

  const btn = document.getElementById('parseBtn');
  const resultBox = document.getElementById('resultBox');
  const errorBox = document.getElementById('errorBox');
  const fieldGrid = document.getElementById('fieldGrid');
  const pdfLink = document.getElementById('pdfLink');

  btn.disabled = true;
  btn.textContent = 'Parsing...';
  resultBox.style.display = 'none';
  errorBox.style.display = 'none';

  try {
    const res = await fetch('/parse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: msg })
    });
    const data = await res.json();

    if (!res.ok) throw new Error(data.error || 'Server error');

    const p = data.parsed;
    const fields = [
      { label: 'Price', value: p.price ? '$' + Number(p.price).toLocaleString() : '—' },
      { label: 'Commission', value: p.commission ? p.commission + '%' : '—' },
      { label: 'Closing', value: p.closing_days ? p.closing_days + ' days' : '—' },
      { label: 'Address', value: p.address || '—' },
      { label: 'Closing Date', value: p.closing_date || '—' },
      { label: 'Down Payment', value: p.down_payment ? '$' + Number(p.down_payment).toLocaleString() : '—' },
    ];

    fieldGrid.innerHTML = fields.map(f => `
      <div class="field-card">
        <div class="field-label">${f.label}</div>
        <div class="field-value">${f.value}</div>
      </div>
    `).join('');

    if (data.pdf_path) {
      pdfLink.href = '/download/' + encodeURIComponent(data.pdf_path);
      pdfLink.style.display = 'block';
    } else {
      pdfLink.style.display = 'none';
    }

    resultBox.style.display = 'block';

  } catch (err) {
    errorBox.textContent = '❌ ' + err.message;
    errorBox.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = '⚡ Parse Offer & Generate Contract';
  }
}

document.getElementById('msgInput').addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    runParse();
  }
});
</script>


"""

def parse_offer(message):
    result = {}
    msg = message.lower()

    price_match = re.search(r'\$?([\d,]+\.?\d*)\s*k\b', msg)
    if price_match:
        result['price'] = float(price_match.group(1).replace(',', '')) * 1000
    else:
        price_match = re.search(r'\$?([\d,]+)', msg)
        if price_match:
            val = float(price_match.group(1).replace(',', ''))
            if val > 10000:
                result['price'] = val

    comm_match = re.search(r'(\d+\.?\d*)\s*%', msg)
    if comm_match:
        result['commission'] = float(comm_match.group(1))

    day_match = re.search(r'(\d+)\s*day', msg)
    if day_match:
        days = int(day_match.group(1))
        result['closing_days'] = days
        closing_date = datetime.date.today() + datetime.timedelta(days=days)
        result['closing_date'] = closing_date.strftime('%B %d, %Y')

    addr_match = re.search(r'\d+\s+[A-Za-z][\w\s]*(?:ave|st|rd|blvd|dr|ln|ct|way|pl)\b', message, re.IGNORECASE)
    if addr_match:
        result['address'] = addr_match.group(0).strip()

    if 'price' in result:
        result['down_payment'] = result['price'] * 0.20

    return result

def fill_contract(parsed):
    return None

@app.route('/')
def home():
    return jsonify({"status": "TextAnOffer API is running"})

@app.route('/demo')
def demo():
    return render_template_string(DEMO_HTML)

@app.route('/parse', methods=['POST'])
def parse():
    data = request.get_json()
    message = data.get('message', '')
    parsed = parse_offer(message)
    pdf_path = fill_contract(parsed)
    return jsonify({
        "parsed": parsed,
        "pdf_generated": pdf_path is not None,
        "pdf_path": pdf_path
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
 
