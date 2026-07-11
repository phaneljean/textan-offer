# Stripe Payment Setup

## 1. Create Stripe Account

Go to https://stripe.com and create an account.

## 2. Create Your Product & Price

1. In Stripe Dashboard → Products → Create product
   - Name: **TextAnOffer Professional**
   - Description: **Unlimited TREC 20-19 contract generation**
   - Pricing model: **Recurring**
   - Price: **$49 USD / month**
   - Billing period: **Monthly**
   
2. After creating, copy the **Price ID** (starts with `price_...`)

## 3. Get Your API Keys

1. In Stripe Dashboard → Developers → API keys
2. Copy:
   - **Publishable key** (starts with `pk_...`)
   - **Secret key** (starts with `sk_...`)

## 4. Set Environment Variables on Railway

In Railway dashboard → Your project → Variables, add:

```
STRIPE_SECRET_KEY=sk_test_... (or sk_live_... for production)
STRIPE_PRICE_ID=price_...
```

## 5. Set Up Webhook (for production)

1. In Stripe Dashboard → Developers → Webhooks → Add endpoint
2. Endpoint URL: `https://textanoffer-production.up.railway.app/webhook`
3. Events to listen for:
   - `checkout.session.completed`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
4. Copy the **Signing secret** (starts with `whsec_...`)
5. Add to Railway:
   ```
   STRIPE_WEBHOOK_SECRET=whsec_...
   ```

## 6. Test Payment Flow

**Test Mode (use test keys `sk_test_...`):**
1. Visit `/pricing`
2. Click "Get Early Access"
3. Use test card: `4242 4242 4242 4242`
4. Any future expiry date
5. Any CVC

**Live Mode:**
Switch to live keys when ready to accept real payments.

## Current Flow

1. User clicks "Get Early Access" on `/pricing`
2. POST to `/create-checkout-session`
3. Redirects to Stripe Checkout
4. User enters card details
5. On success → `/success` page
6. Webhook notifies your app → `/webhook`

## Next Steps (Optional)

- [ ] Add customer portal for subscription management
- [ ] Store subscriptions in database
- [ ] Check subscription status before generating PDFs
- [ ] Add usage tracking (offers per customer)
- [ ] Implement trial period (7 days free)
