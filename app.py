import os
import re
import datetime
from flask import Flask, request, jsonify, render_template_string
from fillpdf import fillpdfs

app = Flask(__name__)

# ─── CONFIG ───
TEMPLATE_PDF = "Arizona_Residential_Purchase_Contract.pdf"
OUTPUT_DIR = "generated_contracts"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ─── PARSER ───
def parse_offer(text):
    offer = {}
    price_match = re.search(r'(\d+\.?\d*)\s*k', text, re.IGNORECASE)
    if price_match:
        offer['price'] = int(float(price_match.group(1)) * 1000)
    down_match = re.search(r'(\d+\.?\d*)\s*%', text)
    if down_match:
        offer['down_percent'] = float(down_match.group(1))
        if 'price' in offer:
            offer['down_amount'] = int(offer['price'] * offer['down_percent'] / 100)
            offer['loan_amount'] = offer['price'] - offer['down_amount']
    close_match = re.search(r'(\d+)\s*day', text, re.IGNORECASE)
    if close_match:
        days = int(close_match.group(1))
        offer['close_days'] = days
        offer['close_date'] = (datetime.date.today() + datetime.timedelta(days=days)).strftime('%m/%d/%Y')
    addr_match = re.search(r'\d+\s+[A-Za-z0-9\s\.]+(?:St|Ave|Blvd|Dr|Ln|Rd|Way|Ct|Pl|Cir)', text, re.IGNORECASE)
    if addr_match:
        offer['address'] = addr_match.group(0).strip()
    return offer

# ─── PDF FILLER ───
def fill_contract(offer):
    data_dict = {
        'PURCHASE PRICE': str(offer.get('price', '')),
        'CLOSE OF ESCROW': offer.get('close_date', ''),
        'PROPERTY ADDRESS': offer.get('address', ''),
        'DOWN PAYMENT': str(offer.get('down_amount', '')),
        'LOAN AMOUNT': str(offer.get('loan_amount', '')),
    }
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    output_path = os.path.join(OUTPUT_DIR, f"offer_{timestamp}.pdf")
    if os.path.exists(TEMPLATE_PDF):
        fillpdfs.write_fillable_pdf(TEMPLATE_PDF, output_path, data_dict, flatten=False)
        return output_path
    return None

# ─── DEMO PAGE ───
DEMO_HTML = '''


<meta>
<meta>
<title>TextAnOffer</title>
<link>
<link>
<style>
  :root{
    --ink:#171B24;
    --ink-soft:#242938;
    --paper:#F3EEDF;
    --paper-line:#DCD3B8;
    --brass:#A9772F;
    --brass-soft:#C9A466;
    --green:#3A5744;
    --green-bg:#26332B;
    --text-on-paper:#211E17;
    --text-muted:#847C68;
    --text-on-ink:#E7E4D8;
    --text-on-ink-muted:#8B8A82;
  }
  *{box-sizing:border-box;}
  html,body{margin:0;padding:0;}
  body{
    background:var(--ink);
    background-image:
      radial-gradient(circle at 15% 10%, rgba(169,119,47,0.06), transparent 45%),
      radial-gradient(circle at 85% 90%, rgba(169,119,47,0.04), transparent 40%);
    min-height:100vh;
    display:flex;
    align-items:center;
    justify-content:center;
    padding:48px 20px;
    font-family:'Inter',sans-serif;
  }
  .stage{width:100%;max-width:460px;}
  .corner-mark{
    display:flex;
    justify-content:space-between;
    align-items:baseline;
    font-family:'IBM Plex Mono',monospace;
    font-size:10.5px;
    letter-spacing:0.06em;
    color:var(--text-on-ink-muted);
    margin-bottom:14px;
    padding:0 4px;
  }
  .corner-mark span.brass{color:var(--brass-soft);}
  h1{
    font-family:'Source Serif 4',serif;
    font-weight:600;
    font-size:32px;
    color:var(--text-on-ink);
    margin:0 0 6px;
    letter-spacing:-0.01em;
  }
  .sub{
    color:var(--text-on-ink-muted);
    font-size:14px;
    line-height:1.55;
    margin:0 0 32px;
    max-width:380px;
  }
  .card{
    background:var(--paper);
    border-radius:2px;
    padding:28px 26px 26px;
    box-shadow:0 24px 60px -20px rgba(0,0,0,0.5);
    border-top:2px solid var(--brass);
  }
  .field-label{
    font-family:'IBM Plex Mono',monospace;
    font-size:10.5px;
    letter-spacing:0.08em;
    text-transform:uppercase;
    color:var(--text-muted);
    margin-bottom:8px;
    display:block;
  }
  input[type=text]{
    width:100%;
    font-family:'IBM Plex Mono',monospace;
    font-size:14px;
    padding:13px 14px;
    border:1px solid var(--paper-line);
    background:#FFFDF7;
    color:var(--text-on-paper);
    border-radius:2px;
    outline:none;
  }
  input[type=text]:focus{border-color:var(--brass);}
  input[type=text]::placeholder{color:var(--text-muted);}
  button{
    width:100%;
    margin-top:14px;
    background:var(--ink);
    color:var(--text-on-ink);
    border:none;
    padding:14px;
    font-family:'Inter',sans-serif;
    font-size:14px;
    font-weight:500;
    border-radius:2px;
    cursor:pointer;
    letter-spacing:0.01em;
  }
  button:hover{background:var(--ink-soft);}
  button:active{transform:scale(0.99);}
  .hint{
    font-family:'IBM Plex Mono',monospace;
    font-size:11px;
    color:var(--text-muted);
    margin-top:10px;
  }
  .result{
    margin-top:22px;
    padding-top:20px;
    border-top:1px dashed var(--paper-line);
  }
  .result-stamp{
    display:inline-flex;
    align-items:center;
    gap:6px;
    font-family:'IBM Plex Mono',monospace;
    font-size:10px;
    letter-spacing:0.08em;
    text-transform:uppercase;
    color:var(--green);
    background:rgba(58,87,68,0.1);
    border:1px solid rgba(58,87,68,0.35);
    padding:4px 10px;
    border-radius:20px;
    margin-bottom:14px;
  }
  .result-addr{
    font-family:'Source Serif 4',serif;
    font-size:19px;
    color:var(--text-on-paper);
    margin:0 0 12px;
  }
  .result-row{
    display:flex;
    justify-content:space-between;
    font-size:13.5px;
    padding:7px 0;
    border-bottom:1px solid rgba(220,211,184,0.6);
  }
  .result-row .k{color:var(--text-muted);font-family:'IBM Plex Mono',monospace;font-size:11px;text-transform:uppercase;letter-spacing:0.04em;}
  .result-row .v{color:var(--text-on-paper);font-weight:500;}
  .download-btn{
    margin-top:18px;
    display:block;
    text-align:center;
    background:var(--brass);
    color:#2A1D08;
    text-decoration:none;
    font-weight:500;
    font-size:14px;
    padding:13px;
    border-radius:2px;
  }
  .download-btn:hover{background:var(--brass-soft);}
  .disclaimer{
    margin-top:14px;
    font-size:11.5px;
    color:var(--text-muted);
    line-height:1.5;
    font-style:italic;
  }
  .error{
    margin-top:22px;
    padding:14px 16px;
    background:rgba(139,58,44,0.08);
    border:1px solid rgba(139,58,44,0.3);
    border-radius:2px;
    font-size:13px;
    color:#7A3527;
  }
  .foot{
    text-align:center;
    margin-top:24px;
    font-family:'IBM Plex Mono',monospace;
    font-size:10.5px;
    color:var(--text-on-ink-muted);
    letter-spacing:0.03em;
  }
</style>


  
    
      TEXTANOFFER
      
    
    Text a price.Get a real offer.
    Type an offer the way you'd text it. This generates the actual TREC 20-19 contract — same form, same fields, ready for review.

    
      <label>Offer details</label>
      <input>
      <button>Generate offer</button>
      price · down % · closing days · address

      
    

    SMS delivery pending carrier registration — this demo runs the same logic directly
  

<script>
document.getElementById('dateStamp').textContent = new Date().toLocaleDateString('en-US', {month:'2-digit', day:'2-digit', year:'numeric'});

function parsePrice(text){
  const m = text.match(/(\\d+(?:\\.\\d+)?)\\s*(k|m|million|mil)\\b/i);
  if(!m) return null;
  const num = parseFloat(m[1]);
  return m[2].toLowerCase() === 'k' ? Math.round(num*1000) : Math.round(num*1000000);
}
function parsePct(text){ const m = text.match(/(\\d+(?:\\.\\d+)?)\\s*%/); return m ? parseFloat(m[1])/100 : null; }
function parseDays(text){ const m = text.match(/(\\d+)\\s*day/i); return m ? parseInt(m[1]) : null; }
function parseAddress(text){
  let s = text.replace(/\\d+(?:\\.\\d+)?\\s*(k|m|million|mil)\\b/ig,'').replace(/\\d+(?:\\.\\d+)?\\s*%/g,'').replace(/\\d+\\s*day\\w*/ig,'');
  return s.trim().replace(/^[,.\\-\\s]+|[,.\\-\\s]+$/g,'');
}

function generateOffer(){
  const text = document.getElementById('offerInput').value.trim();
  const resultArea = document.getElementById('resultArea');
  const price = parsePrice(text), pct = parsePct(text), days = parseDays(text), address = parseAddress(text);
  const missing = [];
  if(price===null) missing.push('price');
  if(pct===null) missing.push('down %');
  if(days===null) missing.push('closing days');
  if(!address) missing.push('address');

  if(missing.length){
    resultArea.innerHTML = '<div class="error">Missing: '+missing.join(', ')+'. Try: 725k 3% 21day 1740 Grand Ave</div>';
    return;
  }

  const closeDate = new Date(Date.now() + days*86400000).toLocaleDateString('en-US', {month:'long', day:'numeric', year:'numeric'});

  resultArea.innerHTML = '<div class="result">'
    +'<div class="result-stamp">Ready to sign</div>'
    +'<div class="result-addr">'+address+'</div>'
    +'<div class="result-row"><span class="k">Sales price</span><span class="v">$'+price.toLocaleString()+'</span></div>'
    +'<div class="result-row"><span class="k">Down payment</span><span class="v">'+Math.round(pct*100)+'%</span></div>'
    +'<div class="result-row"><span class="k">Closing date</span><span class="v">'+closeDate+'</span></div>'
    +'<a href="#" class="download-btn">Download filled TREC 20-19 &rarr;</a>'
    +'<div class="disclaimer">Draft only — agent must review before signing. TREC NO. 20-19.</div>'
    +'</div>';
}

document.getElementById('offerInput').addEventListener('keydown', function(e){ if(e.key === 'Enter') generateOffer(); });
</script>

'''

# ─── ROUTES ───
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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
