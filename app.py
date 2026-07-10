import os
import re
import datetime
from flask import Flask, request, jsonify, send_file, render_template_string
from fillpdf import fillpdfs

app = Flask(__name__)

# ─── CONFIG ───
TEMPLATE_PDF = "Arizona_Residential_Purchase_Contract.pdf"
OUTPUT_DIR = "generated_contracts"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ─── PARSER ───
def parse_offer(text):
    offer = {}

    # Price
    price_match = re.search(r'(\d+\.?\d*)\s*k', text, re.IGNORECASE)
    if price_match:
        offer['price'] = int(float(price_match.group(1)) * 1000)

    # Down payment
    down_match = re.search(r'(\d+\.?\d*)\s*%', text)
    if down_match:
        offer['down_percent'] = float(down_match.group(1))
        if 'price' in offer:
            offer['down_amount'] = int(offer['price'] * offer['down_percent'] / 100)
            offer['loan_amount'] = offer['price'] - offer['down_amount']

    # Close days
    close_match = re.search(r'(\d+)\s*day', text, re.IGNORECASE)
    if close_match:
        days = int(close_match.group(1))
        offer['close_days'] = days
        offer['close_date'] = (datetime.date.today() + datetime.timedelta(days=days)).strftime('%m/%d/%Y')

    # Address
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
    else:
        return None

# ─── DEMO PAGE ───
DEMO_HTML = """



    <meta>
    <meta>
    <title>TextAnOffer</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0a0a0f;
            color: #e0e0e0;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }

        .container {
            width: 100%;
            max-width: 520px;
        }

        .logo {
            text-align: center;
            margin-bottom: 40px;
        }

        .logo h1 {
            font-size: 2.2rem;
            font-weight: 700;
            background: linear-gradient(135deg, #667eea, #764ba2);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            letter-spacing: -0.5px;
        }

        .logo p {
            color: #888;
            font-size: 0.95rem;
            margin-top: 8px;
        }

        .card {
            background: #16161f;
            border: 1px solid #2a2a3a;
            border-radius: 16px;
            padding: 32px;
        }

        .input-group {
            margin-bottom: 20px;
        }

        .input-group label {
            display: block;
            font-size: 0.85rem;
            color: #999;
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .input-group input {
            width: 100%;
            padding: 14px 16px;
            background: #0d0d14;
            border: 1px solid #2a2a3a;
            border-radius: 10px;
            color: #fff;
            font-size: 1.1rem;
            font-family: 'SF Mono', 'Fira Code', monospace;
            transition: border-color 0.2s;
        }

        .input-group input:focus {
            outline: none;
            border-color: #667eea;
        }

        .input-group input::placeholder {
            color: #444;
        }

        .example {
            background: #0d0d14;
            border: 1px solid #1e1e2e;
            border-radius: 8px;
            padding: 12px 16px;
            margin-bottom: 24px;
            font-size: 0.82rem;
            color: #666;
        }

        .example code {
            color: #667eea;
            font-family: 'SF Mono', 'Fira Code', monospace;
        }

        button {
            width: 100%;
            padding: 16px;
            background: linear-gradient(135deg, #667eea, #764ba2);
            border: none;
            border-radius: 10px;
            color: #fff;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: opacity 0.2s, transform 0.1s;
        }

        button:hover { opacity: 0.9; }
        button:active { transform: scale(0.98); }

        .result {
            margin-top: 24px;
            padding: 20px;
            background: #0d0d14;
            border: 1px solid #2a2a3a;
            border-radius: 10px;
            display: none;
        }

        .result.show { display: block; }

        .result h3 {
            font-size: 0.85rem;
            color: #999;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 12px;
        }

        .result .parsed-item {
            display: flex;
            justify-content: space-between;
            padding: 8px 0;
            border-bottom: 1px solid #1a1a2a;
            font-size: 0.95rem;
        }

        .result .parsed-item:last-child { border-bottom: none; }
        .result .parsed-item .label { color: #888; }
        .result .parsed-item .value { color: #667eea; font-weight: 600; }

        .status {
            text-align: center;
            margin-top: 16px;
            font-size: 0.85rem;
            color: #4caf50;
            display: none;
        }

        .status.show { display: block; }

        .footer {
            text-align: center;
            margin-top: 32px;
            font-size: 0.8rem;
            color: #444;
        }
    </style>


    
        
            TextAnOffer
            Text an offer. Get a contract. Done.
        

        
            
                <label>Your Offer</label>
                <input>
            

            
                Try: 725k 3% 21day 1740 Grand Ave
                Or: 550k 10% 30day 422 Oak Blvd
            

            <button>Generate Offer →</button>

            
                Parsed Offer
                
            

            ✓ Contract PDF generated successfully
        

        
            SMS integration coming soon — same engine, just text it.
        
    

    <script>
        async function submitOffer() {
            const input = document.getElementById('offerInput').value.trim();
            if (!input) return;

            const res = await fetch('/parse', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: input })
            });

            const data = await res.json();
            const fields = document.getElementById('parsedFields');
            const result = document.getElementById('result');
            const status = document.getElementById('status');

            fields.innerHTML = '';

            const displayMap = {
                'price': 'Purchase Price',
                'down_percent': 'Down Payment',
                'down_amount': 'Down Amount',
                'loan_amount': 'Loan Amount',
                'close_days': 'Days to Close',
                'close_date': 'Close Date',
                'address': 'Property Address'
            };

            const formatValue = (key, val) => {
                if (key === 'price' || key === 'down_amount' || key === 'loan_amount')
                    return '$' + Number(val).toLocaleString();
                if (key === 'down_percent') return val + '%';
                if (key === 'close_days') return val + ' days';
                return val;
            };

            for (const [key, label] of Object.entries(displayMap)) {
                if (data.parsed && data.parsed[key]) {
                    fields.innerHTML += `
                        <div class="parsed-item">
                            <span class="label">${label}</span>
                            <span class="value">${formatValue(key, data.parsed[key])}</span>
                        </div>`;
                }
            }

            result.classList.add('show');
            status.classList.add('show');
        }

        // Submit on Enter key
        document.getElementById('offerInput').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') submitOffer();
        });
    </script>


"""

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

# force rebuild
