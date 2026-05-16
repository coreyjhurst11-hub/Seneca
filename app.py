#!/usr/bin/env python3
"""
SENECA — Intrinsic Value Oracle
Flask web app with Stripe paywall (1 free lookup, then subscription)
"""

import os, math, json
from flask import Flask, request, jsonify, session, render_template_string
import yfinance as yf

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_PRICE_ID        = os.environ.get("STRIPE_PRICE_ID", "")       # monthly sub price ID
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# ── Valuation math ──────────────────────────────────────────────────────────
def graham_number(e, b):
    return math.sqrt(22.5 * e * b) if e > 0 and b > 0 else None

def graham_growth(e, g):
    return e * (8.5 + 2 * g) * 4.4 / 4.5 if e > 0 and g else None

def buffett_dcf(e, g):
    if e <= 0 or not g: return None
    r, d = min(g / 100, 0.25), 0.09
    return sum(e*(1+r)**y/(1+d)**y for y in range(1,11)) + (e*(1+r)**10*15)/(1+d)**10

def lynch_peg(e, g):
    return e * g if e > 0 and g > 0 else None

def simons_quant(p, pe, pb, roe, mom):
    if pe <= 0 or pb <= 0 or roe <= 0: return None
    return p * (roe/pe) * (1/pb) * (1 + (mom/100)*0.3) * 12

def fcf_dcf(f, g):
    if f <= 0 or not g: return None
    r, d, tg = min(g/100, 0.30), 0.10, 0.025
    pv = sum(f*(1+r)**y/(1+d)**y for y in range(1,11))
    return pv + (f*(1+r)**10*(1+tg)/(d-tg))/(1+d)**10

def composite(vals):
    w = {'gn':.20,'gg':.15,'buf':.25,'lyn':.15,'sim':.10,'dcf':.15}
    t = ws = 0
    for k, wt in w.items():
        v = vals.get(k)
        if v and v > 0: t += v*wt; ws += wt
    return t/ws if ws > 0 else None

def get_signal(val, price):
    if not val or val <= 0: return "na", "Insufficient data"
    m = (val - price) / price * 100
    if m >= 30:  return "up",   f"▲ {m:.0f}% upside · DEEPLY UNDERVALUED"
    if m >= 10:  return "up",   f"▲ {m:.0f}% upside · Modestly Undervalued"
    if m <=-30:  return "down", f"▼ {abs(m):.0f}% premium · SIGNIFICANTLY OVERVALUED"
    if m <=-10:  return "down", f"▼ {abs(m):.0f}% above fair · Overvalued"
    s = "+" if m >= 0 else ""
    return "fair", f"≈ {s}{m:.0f}% · Fairly Valued"

def fetch_quote(ticker):
    ticker = ticker.upper().strip()
    t = yf.Ticker(ticker)

    fi = t.fast_info
    price  = float(fi.last_price or 0)
    prev   = float(fi.previous_close or 0)
    lo52   = float(fi.year_low  or 0)
    hi52   = float(fi.year_high or 0)
    cap    = float(fi.market_cap  or 0)
    shares = float(fi.shares or 1)

    if not price:
        raise ValueError(f"No price data for '{ticker}'. Check the symbol.")

    info = t.info

    def g(key, fb=0.0):
        try:
            v = info.get(key)
            f = float(v)
            return f if math.isfinite(f) else fb
        except: return fb

    name   = info.get("longName") or info.get("shortName") or ticker
    sector = info.get("sector") or info.get("industry") or "—"
    eps    = g("trailingEps")
    bvps   = g("bookValue")
    pe     = g("trailingPE")
    pb     = g("priceToBook")
    roe    = g("returnOnEquity") * 100
    div_y  = g("dividendYield") * 100
    beta   = g("beta")
    fcf    = g("freeCashflow") / shares if shares else 0
    growth = (g("earningsGrowth") or g("revenueGrowth") or g("earningsQuarterlyGrowth") or 0) * 100
    chg    = (price - prev) / prev * 100 if prev else 0
    mom    = (price - lo52) / lo52 * 100 if lo52 else 0

    vals = {
        "gn":  graham_number(eps, bvps),
        "gg":  graham_growth(eps, growth),
        "buf": buffett_dcf(eps, growth),
        "lyn": lynch_peg(eps, growth),
        "sim": simons_quant(price, pe or 1, pb or 1, roe, mom),
        "dcf": fcf_dcf(fcf, growth),
    }
    comp = composite(vals)

    models_out = []
    for key, mname, formula, stripe_color, cls in [
        ("gn",  "GRAHAM NUMBER",      "√( 22.5 × EPS × Book Value )",          "gold",  "gold"),
        ("gg",  "GRAHAM GROWTH",      "EPS × (8.5 + 2g) × 4.4 / AAA yield",   "gold",  "gold"),
        ("buf", "BUFFETT DCF",        "10yr EPS @ 9% discount · 15× terminal", "turq",  "turq"),
        ("lyn", "PETER LYNCH PEG",    "EPS × growth%  (PEG = 1)",              "turq",  "turq"),
        ("sim", "SIMONS QUANT",       "ROE/PE × (1/PB) × momentum",            "muted", "muted"),
        ("dcf", "FREE CASH FLOW DCF", "10yr FCF @ 10% · 2.5% terminal",        "muted", "muted"),
    ]:
        v = vals.get(key)
        sig_cls, sig_txt = get_signal(v, price) if (v and v > 0) else ("na", "Insufficient data")
        models_out.append({
            "name": mname, "formula": formula,
            "stripe": stripe_color, "cls": cls,
            "value": v, "sig_cls": sig_cls, "sig_txt": sig_txt,
        })

    verdict_text = verdict_detail = verdict_cls = ""
    if comp and comp > 0:
        m = (comp - price) / price * 100
        if   m >= 30:  verdict_text, verdict_detail, verdict_cls = f"✦ STRONG BUY · {m:.0f}% margin of safety",    "Deep value. Substantial gap between price and intrinsic worth.", "up"
        elif m >= 10:  verdict_text, verdict_detail, verdict_cls = f"✦ UNDERVALUED · {m:.0f}% upside",             "Price trades below the six-model consensus.", "up"
        elif m <=-30:  verdict_text, verdict_detail, verdict_cls = f"✦ AVOID · {abs(m):.0f}% above fair value",    "Significant optimism priced in beyond what fundamentals support.", "down"
        elif m <=-10:  verdict_text, verdict_detail, verdict_cls = f"✦ OVERVALUED · {abs(m):.0f}% premium",        "Price exceeds what the six models suggest is warranted.", "down"
        else:
            s = "+" if m >= 0 else ""
            verdict_text, verdict_detail, verdict_cls = f"✦ FAIRLY VALUED · {s}{m:.0f}% vs composite", "Price is broadly in line with the consensus.", "fair"
    else:
        verdict_text, verdict_detail, verdict_cls = "Insufficient data for composite verdict", "", "fair"

    return {
        "ticker": ticker, "name": name, "sector": sector,
        "price": price, "prev": prev, "eps": eps, "bvps": bvps,
        "pe": pe, "pb": pb, "roe": roe, "growth": growth, "fcf": fcf,
        "lo52": lo52, "hi52": hi52, "mom": mom, "chg": chg,
        "cap": cap, "div_y": div_y, "beta": beta,
        "composite": comp,
        "models": models_out,
        "verdict_text": verdict_text,
        "verdict_detail": verdict_detail,
        "verdict_cls": verdict_cls,
    }

# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(open(os.path.join(os.path.dirname(__file__), "templates/index.html")).read(),
                                  stripe_pk=STRIPE_PUBLISHABLE_KEY)

@app.route("/api/quote")
def api_quote():
    ticker = request.args.get("ticker", "").strip().upper()
    if not ticker:
        return jsonify({"error": "No ticker provided"}), 400

    # Session-based free lookup tracking
    lookups = session.get("lookups", 0)
    is_subscribed = session.get("subscribed", False)  # set by webhook

    if not is_subscribed and lookups >= 1:
        return jsonify({"error": "PAYWALL", "lookups_used": lookups}), 402

    try:
        data = fetch_quote(ticker)
        session["lookups"] = lookups + 1
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/create-checkout-session", methods=["POST"])
def create_checkout():
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe not configured"}), 500
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY
    try:
        checkout = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=request.host_url + "success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.host_url,
        )
        return jsonify({"url": checkout.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/success")
def success():
    if not STRIPE_SECRET_KEY:
        session["subscribed"] = True  # dev mode: auto-grant
    else:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        sid = request.args.get("session_id", "")
        try:
            s = stripe.checkout.Session.retrieve(sid)
            if s.payment_status == "paid":
                session["subscribed"] = True
        except: pass
    return render_template_string(open(os.path.join(os.path.dirname(__file__), "templates/index.html")).read(),
                                  stripe_pk=STRIPE_PUBLISHABLE_KEY,
                                  success_flash=True)

@app.route("/webhook", methods=["POST"])
def webhook():
    if not STRIPE_SECRET_KEY:
        return "", 200
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        if event["type"] == "customer.subscription.deleted":
            # In production: look up user by customer ID and revoke access
            pass
    except Exception as e:
        return str(e), 400
    return "", 200

if __name__ == "__main__":
    app.run(debug=True, port=5678)
