# TextAnOffer

Twilio SMS webhook for real estate offer generation. Agents text structured offer data, the service parses it and fills a TREC 20-19 PDF form.

## Usage

Agent texts: `725k 3% 21day 1740 Grand Ave`

Service responds with:
- Parsed offer summary
- Link to generated PDF for review/signing

## Setup

### Environment Variables

- `TWILIO_AUTH_TOKEN` (optional, recommended for prod) — Twilio auth token for request validation
- `TREC_TEMPLATE_PATH` (default: `20-19_2.pdf`) — Path to the clean TREC form template
- `OFFER_OUTPUT_DIR` (default: `generated_offers`) — Directory to store generated PDFs

### Files Required

- `20-19_2.pdf` — Clean, final TREC 20-19 form (the one with 281 real AcroForm fields)

## Deployment

Deploy to Railway with:
```bash
git push origin main
```

The service will:
1. Install dependencies from `requirements.txt`
2. Start Gunicorn on `$PORT`
3. Serve SMS webhook at `/sms`
4. Serve generated PDFs at `/offers/<filename>`

## Development

```bash
pip install -r requirements.txt
python app.py
```

Test the parser:
```bash
python parser.py
```

Inspect PDF fields:
```bash
python -c "from pdf_filler import inspect_fields; inspect_fields()"
```

