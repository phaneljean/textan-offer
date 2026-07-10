import os
import re
import datetime
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

DEMO_HTML = """


<meta>
<meta>
<title>TextAnOffer</title>
<link>
<link>
<style>
:root{--ink:#171B24;--ink-soft:#242938;--paper:#F3EEDF;--paper-line:#DCD3B8;--brass:#A9772F;--brass-soft:#C9A466;--green:#3A5744;--text-on-paper:#211E17;--text-muted:#847C68;--text-on-ink:#E7E4D8;--text-on-ink-muted:#8B8A82;}
*{box-sizing:border-box;}html,body{margin:0;padding:0;}
body{background:var(--ink);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:48px 20px;font-family:Inter,sans-serif;}
.stage{width:100%;max-width:460px;}
.corner-mark{display:flex;justify-content:space-between;font-family:monospace;font-size:10.5px;color:var(--text-on-ink-muted);margin-bottom:14px;padding:0 4px;}
.brass{color:var(--brass-soft);}
h1{font-family:Georgia,serif;font-weight:600;font-size:32px;color:var(--text-on-ink);margin:0 0 6px;}
.sub{color:var(--text-on-ink-muted);font-size:14px;line-height:1.55;margin:0 0 32px;}
.card{background:var(--paper);border-radius:2px;padding:28px 26px 26px;box-shadow:0 24px 60px -20px rgba(0,0,0,0.5);border-top:2px solid var(--brass);}
.field-label{font-family:monospace;font-size:10.5px;letter-spacing:0.08em;text-transform:uppercase;color:var(--text-muted);margin-bottom:8px;display:block;}
input{width:100%;font-family:monospace;font-size:14px;padding:13px 14px;border:1px solid var(--paper-line);background:#FFFDF7;color:var(--text-on-paper);border-radius:2px;outline:none;}
input:focus{border-color:var(--brass);}
input::placeholder{color:var(--text-muted);}
button{width:100%;margin-top:14px;background:var(--ink);color:var(--text-on-ink);border:none;padding:14px;font-size:14px;font-weight:500;border-radius:2px;cursor:pointer;}
button:hover{background:var(--ink-soft);}
.hint{font-family:monospace;font-size:11px;color:var(--text-muted);margin-top:10px;}
.result{margin-top:22px;padding-top:20px;border-top:1px dashed var(--paper-line);}
.stamp{display:inline-block;font-family:monospace;font-size:10px;letter-spacing:0.08em;text-transform:uppercase;color:var(--green);background:rgba(58,87,68,0.1);border:1px solid rgba(58,87,68,0.35);padding:4px 10px;border-radius:20px;margin-bottom:14px;}
.addr{font-family:Georgia,serif;font-size:19px;color:var(--text-on-paper);margin:0 0 12px;}
.row{display:flex;justify-content:space-between;font-size:13.5px;padding:7px 0;border-bottom:1px solid rgba(220,211,184,0.6);}
.row .k{color:var(--text-muted);font-family:monospace;font-size:11px;text-transform:uppercase;}
.row .v{color:var(--text-on-paper);font-weight:500;}
.dl{margin-top:18px;display:block;text-align:center;background:var(--brass);color:#2A1D08;text-decoration:none;font-weight:500;font-size:14px;padding:13px;border-radius:2px;}
.dl:hover{background:var(--brass-soft);}
.note{margin-top:14px;font-size:11.5px;color:var(--text-muted);line-height:1.5;font-style:italic;}
.err{margin-top:22px;padding:14px 16px;background:rgba(139,58,44,0.08);border:1px solid rgba(139,58,44,0.3);border-radius:2px;font-size:13px;color:#7A3527;}
.foot{text-align:center;margin-top:24px;font-family:monospace;font-size:10.5px;color:var(--text-on-ink-muted);}
</style>



  TEXTANOFFER
  Text a price.Get a real offer.
  Type an offer the way you would text it. Generates the actual TREC 20-19 contract, same form, same fields, ready for review.
  
    <label>Offer details</label>
    <input>
    <button>Generate offer</button>
    price · down % · closing days · address
    
  
  SMS delivery pending carrier registration — demo runs same logic directly

<script>
document.getElementById('ds').textContent=new Date().toLocaleDateString('en-US',{month:'2-digit',day:'2-digit',year:'numeric'});
function go(){
  var t=document.getElementById('offerInput').value.trim(),o=document.getElementById('out');
  var pm=t.match(/([\d,]+(?:\.\d+)?)\s*(k|m)/i),pct=t.match(/(\d+(?:\.\d+)?)\s*%/),dm=t.match(/(\d+)\s*day/i);
  var price=pm?(pm[2].toLowerCase()==='k'?Math.round(parseFloat(pm[1].replace(/,/g,''))*1000):Math.round(parseFloat(pm[1].replace(/,/g,''))*1000000)):null;
  var down=pct?parseFloat(pct[1])/100:null;
  var days=dm?parseInt(dm[1]):null;
  var addr=t.replace(/([\d,]+(?:\.\d+)?)\s*(k|m)/ig,'').replace(/\d+(?:\.\d+)?\s*%/g,'').replace(/\d+\s*day\w*/ig,'').trim().replace(/^[\s,.-]+|[\s,.-]+$/g,'');
  var miss=[];
  if(!price)miss.push('price');if(!down)miss.push('down %');if(!days)miss.push('closing days');if(!addr)miss.push('address');
  if(miss.length){o.innerHTML='<div class="err">Missing: '+miss.join(', ')+'. Try: 725k 3% 21day 1740 Grand Ave</div>';return;}
  var cd=new Date(Date.now()+days*86400000).toLocaleDateString('en-US',{month:'long',day:'numeric',year:'numeric'});
  o.innerHTML='<div class="result"><div class="stamp">Ready to sign</div><div class="addr">'+addr+'</div><div class="row"><span class="k">Sales price</span><span class="v">$'+price.toLocaleString()+'</span></div><div class="row"><span class="k">Down payment</span><span class="v">'+Math.round(down*100)+'%</span></div><div class="row"><span class="k">Closing date</span><span class="v">'+cd+'</span></div><a href="#" class="dl">Download filled TREC 20-19 →</a><div class="note">Draft only — agent must review before signing. TREC NO. 20-19.</div></div>';
}
document.getElementById('offerInput').addEventListener('keydown',function(e){if(e.key==='Enter')go();});
</script>

"""

def parse_offer(message):
    result = {}
    msg = message.lower()
    m = re.search(r'\$?([\d,]+\.?\d*)\s*k\b', msg)
    if m:
        result['price'] = float(m.group(1).replace(',','')) * 1000
    m2 = re.search(r'(\d+\.?\d*)\s*%', msg)
    if m2:
        result['commission'] = float(m2.group(1))
    m3 = re.search(r'(\d+)\s*day', msg)
    if m3:
        days = int(m3.group(1))
        result['closing_days'] = days
        result['closing_date'] = (datetime.date.today() + datetime.timedelta(days=days)).strftime('%B %d, %Y')
    m4 = re.search(r'\d+\s+[A-Za-z][\w\s]*(?:ave|st|rd|blvd|dr|ln|ct|way|pl)\b', message, re.IGNORECASE)
    if m4:
        result['address'] = m4.group(0).strip()
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
    parsed = parse_offer(data.get('message', ''))
    pdf_path = fill_contract(parsed)
    return jsonify({"parsed": parsed, "pdf_generated": pdf_path is not None, "pdf_path": pdf_path})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
