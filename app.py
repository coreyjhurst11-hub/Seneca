#!/usr/bin/env python3
"""
SENECA — Intrinsic Value Oracle
v3 — All features:
  1. Mobile-friendly search bar (no overlap)
  2. Company name search (type "Apple" → finds AAPL)
  3. ETF / Index fund support + Index Valuation Model
  4. Login / password system (persistent subscription across devices)
  5. AI Verdict, Comparison, Watchlist, PDF Export
"""

import os, math, io, hashlib, secrets
from datetime import datetime
from flask import Flask, request, jsonify, session, send_file, redirect, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_PRICE_ID        = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
ANTHROPIC_API_KEY      = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Simple file-based user store (no DB needed for Railway) ──────────────────
import json, pathlib

USER_FILE = pathlib.Path("/tmp/seneca_users.json")

def load_users():
    try:
        return json.loads(USER_FILE.read_text()) if USER_FILE.exists() else {}
    except: return {}

def save_users(users):
    try: USER_FILE.write_text(json.dumps(users))
    except: pass

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def create_user(email, pw):
    users = load_users()
    email = email.lower().strip()
    if email in users: return False, "Email already registered"
    users[email] = {
        "pw": hash_pw(pw), "subscribed": False,
        "stripe_customer": "", "created": datetime.now().isoformat(),
        "watchlist": []
    }
    save_users(users)
    return True, "ok"

def verify_user(email, pw):
    users = load_users()
    email = email.lower().strip()
    u = users.get(email)
    if not u: return False, "Email not found"
    if u["pw"] != hash_pw(pw): return False, "Incorrect password"
    return True, u

def get_user(email):
    users = load_users()
    return users.get(email.lower().strip())

def update_user(email, **kwargs):
    users = load_users()
    email = email.lower().strip()
    if email in users:
        users[email].update(kwargs)
        save_users(users)

# ── ETF / Index detection ────────────────────────────────────────────────────
ETF_TICKERS = {
    "SPY","QQQ","IWM","DIA","VTI","VOO","VEA","VWO","GLD","SLV",
    "TLT","IEF","LQD","HYG","XLF","XLK","XLE","XLV","XLI","XLB",
    "ARKK","ARKG","ARKW","ARKF","ARKQ","IVV","AGG","BND","BNDX",
    "VIG","SCHD","DGRO","NOBL","SDY","DVY","VYM","HDV","SPHD",
    "SPYG","SPYV","IWF","IWD","VBK","VBR","VO","VB","VV",
    "EFA","EEM","IEMG","ACWI","VT","URTH","MCHI","FXI","EWJ",
    "PDBC","USO","UNG","UVXY","VXX","SQQQ","TQQQ","SPXU","SPXL","UPRO",
}

def is_etf_or_index(ticker, info):
    if ticker.upper() in ETF_TICKERS: return True
    qt = (info.get("quoteType") or "").upper()
    return qt in ("ETF", "MUTUALFUND", "INDEX", "FUTURE")

# ── Index / ETF valuation model ──────────────────────────────────────────────
def index_fair_value(price, div_y_pct, earnings_yield_pct, pe, hist_pe=17.0):
    results = {}
    treasury = 4.3
    if earnings_yield_pct > 0:
        results["fed_model"] = price * (earnings_yield_pct / treasury)
    if pe > 0:
        results["pe_reversion"] = price * (hist_pe / pe)
    div = price * (div_y_pct / 100)
    if div > 0:
        results["ddm"] = div / (0.08 - 0.03)
    return results

# ── Company name → ticker search ─────────────────────────────────────────────
def resolve_ticker(query):
    import yfinance as yf
    query = query.strip()
    if len(query) <= 5 and query.replace("-","").replace(".","").isalpha():
        return query.upper()
    try:
        results = yf.Search(query, max_results=5)
        quotes = results.quotes
        if quotes:
            for q in quotes:
                if q.get("quoteType","").upper() == "EQUITY":
                    return q["symbol"]
            return quotes[0]["symbol"]
    except Exception:
        pass
    return query.upper()

# ── Valuation math ───────────────────────────────────────────────────────────
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

def fetch_quote(query):
    import yfinance as yf
    ticker = resolve_ticker(query)
    t = yf.Ticker(ticker)
    fi = t.fast_info
    price  = float(fi.last_price or 0)
    prev   = float(fi.previous_close or 0)
    lo52   = float(fi.year_low  or 0)
    hi52   = float(fi.year_high or 0)
    cap    = float(fi.market_cap  or 0)
    shares = float(fi.shares or 1)
    if not price:
        raise ValueError(f"No data for '{query}'. Try the ticker symbol directly.")
    info = t.info
    def g(key, fb=0.0):
        try:
            v = info.get(key); f = float(v)
            return f if math.isfinite(f) else fb
        except: return fb
    name   = info.get("longName") or info.get("shortName") or ticker
    sector = info.get("sector") or info.get("industry") or info.get("categoryName") or "—"
    eps    = g("trailingEps"); bvps = g("bookValue")
    pe     = g("trailingPE");  pb   = g("priceToBook")
    roe    = g("returnOnEquity") * 100
    div_y  = g("dividendYield") * 100
    beta   = g("beta")
    fcf    = g("freeCashflow") / shares if shares else 0
    growth = (g("earningsGrowth") or g("revenueGrowth") or g("earningsQuarterlyGrowth") or 0) * 100
    chg    = (price - prev) / prev * 100 if prev else 0
    mom    = (price - lo52) / lo52 * 100 if lo52 else 0
    earnings_yield = (1/pe*100) if pe > 0 else 0
    is_fund = is_etf_or_index(ticker, info)

    if is_fund:
        idx_vals = index_fair_value(price, div_y, earnings_yield, pe)
        models_out = []
        for key, mname, formula, sc, cls in [
            ("fed_model",    "FED MODEL",          "Price × (Earnings Yield ÷ 10yr Treasury 4.3%)",    "turq", "turq"),
            ("pe_reversion", "P/E MEAN REVERSION", "Price × (Hist. Avg P/E 17× ÷ Current P/E)",        "gold", "gold"),
            ("ddm",          "DIVIDEND DISCOUNT",  "Annual Dividend ÷ (8% required return − 3% growth)","muted","muted"),
        ]:
            v = idx_vals.get(key)
            sig_cls, sig_txt = get_signal(v, price) if (v and v > 0) else ("na", "Insufficient data")
            models_out.append({"name": mname, "formula": formula, "stripe": sc, "cls": cls,
                               "value": v, "sig_cls": sig_cls, "sig_txt": sig_txt})
        valid_vals = [m["value"] for m in models_out if m["value"] and m["value"] > 0]
        comp = sum(valid_vals)/len(valid_vals) if valid_vals else None
        asset_type = "etf"
    else:
        vals = {
            "gn":  graham_number(eps, bvps), "gg":  graham_growth(eps, growth),
            "buf": buffett_dcf(eps, growth),  "lyn": lynch_peg(eps, growth),
            "sim": simons_quant(price, pe or 1, pb or 1, roe, mom),
            "dcf": fcf_dcf(fcf, growth),
        }
        comp = composite(vals)
        models_out = []
        for key, mname, formula, sc, cls in [
            ("gn",  "GRAHAM NUMBER",      "√( 22.5 × EPS × Book Value )",          "gold",  "gold"),
            ("gg",  "GRAHAM GROWTH",      "EPS × (8.5 + 2g) × 4.4 / AAA yield",   "gold",  "gold"),
            ("buf", "BUFFETT DCF",        "10yr EPS @ 9% discount · 15× terminal", "turq",  "turq"),
            ("lyn", "PETER LYNCH PEG",    "EPS × growth%  (PEG = 1)",              "turq",  "turq"),
            ("sim", "SIMONS QUANT",       "ROE/PE × (1/PB) × momentum",            "muted", "muted"),
            ("dcf", "FREE CASH FLOW DCF", "10yr FCF @ 10% · 2.5% terminal",        "muted", "muted"),
        ]:
            v = vals.get(key)
            sig_cls, sig_txt = get_signal(v, price) if (v and v > 0) else ("na", "Insufficient data")
            models_out.append({"name": mname, "formula": formula, "stripe": sc, "cls": cls,
                               "value": v, "sig_cls": sig_cls, "sig_txt": sig_txt})
        asset_type = "stock"

    verdict_text = verdict_detail = verdict_cls = ""
    if comp and comp > 0:
        m = (comp - price) / price * 100
        if   m >= 30:  verdict_text, verdict_detail, verdict_cls = f"✦ STRONG BUY · {m:.0f}% margin of safety", "Deep value. Substantial gap between price and intrinsic worth.", "up"
        elif m >= 10:  verdict_text, verdict_detail, verdict_cls = f"✦ UNDERVALUED · {m:.0f}% upside", "Price trades below the model consensus.", "up"
        elif m <=-30:  verdict_text, verdict_detail, verdict_cls = f"✦ AVOID · {abs(m):.0f}% above fair value", "Significant optimism priced in beyond what fundamentals support.", "down"
        elif m <=-10:  verdict_text, verdict_detail, verdict_cls = f"✦ OVERVALUED · {abs(m):.0f}% premium", "Price exceeds what the models suggest is warranted.", "down"
        else:
            s = "+" if m >= 0 else ""
            verdict_text, verdict_detail, verdict_cls = f"✦ FAIRLY VALUED · {s}{m:.0f}% vs composite", "Price is broadly in line with the consensus.", "fair"
    else:
        verdict_text, verdict_detail, verdict_cls = "Insufficient data for composite verdict", "", "fair"

    return {
        "ticker": ticker, "name": name, "sector": sector, "asset_type": asset_type,
        "price": price, "prev": prev, "eps": eps, "bvps": bvps,
        "pe": pe, "pb": pb, "roe": roe, "growth": growth, "fcf": fcf,
        "lo52": lo52, "hi52": hi52, "mom": mom, "chg": chg,
        "cap": cap, "div_y": div_y, "beta": beta, "composite": comp,
        "models": models_out, "verdict_text": verdict_text,
        "verdict_detail": verdict_detail, "verdict_cls": verdict_cls,
        "earnings_yield": earnings_yield,
    }

# ── AI Verdict ───────────────────────────────────────────────────────────────
def get_ai_verdict(data):
    if not ANTHROPIC_API_KEY: return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        asset_note = "ETF/index fund" if data.get("asset_type") == "etf" else "stock"
        prompt = f"""You are SENECA, a stoic value investing oracle. Write a 3-sentence plain-English explanation of why {data['name']} ({data['ticker']}) — a {asset_note} — appears {data['verdict_cls']} based on these fundamentals:

Price: ${data['price']:.2f} | Composite Fair Value: ${f"{data['composite']:.2f}" if data['composite'] else 'N/A'}
P/E: {data['pe']:.1f} | Dividend Yield: {data['div_y']:.2f}% | Beta: {data['beta']:.2f}
Verdict: {data['verdict_text']}

Be direct, insightful, speak like a wise investor. No disclaimers. No bullets. Just 3 flowing sentences."""
        msg = client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
    except: return None

# ── PDF ───────────────────────────────────────────────────────────────────────
def build_pdf_html(data, ai_text=None):
    def fp(v):
        if not v or v <= 0: return "N/A"
        return f"${v:,.2f}"
    models_rows = ""
    for m in data["models"]:
        models_rows += f"<tr><td>{m['name']}</td><td style='font-family:monospace;font-size:10px'>{m['formula']}</td><td style='text-align:right;font-weight:bold'>{fp(m['value'])}</td><td style='text-align:right'>{m['sig_txt']}</td></tr>"
    ai_section = f"<div class='ai-box'><div class='ai-title'>◆ SENECA AI ANALYSIS</div><p>{ai_text}</p></div>" if ai_text else ""
    vc = "#3a8a24" if data["verdict_cls"]=="up" else ("#a03020" if data["verdict_cls"]=="down" else "#c88a1a")
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"/>
<style>
body{{font-family:Georgia,serif;background:#fff;color:#1a1a1a;margin:0;padding:32px;font-size:12px}}
.header{{border-bottom:3px solid #c88a1a;padding-bottom:16px;margin-bottom:24px;display:flex;justify-content:space-between}}
.logo{{font-size:28px;font-weight:300;letter-spacing:6px;color:#c88a1a}}
.logo-sub{{font-size:10px;color:#888;font-style:italic}}
.section-title{{font-size:9px;letter-spacing:3px;color:#888;border-bottom:1px solid #e0d0b0;padding-bottom:4px;margin:20px 0 10px;text-transform:uppercase}}
table{{width:100%;border-collapse:collapse;margin-bottom:16px}}
th{{background:#f5ede0;font-size:9px;padding:6px 8px;text-align:left;color:#888;text-transform:uppercase;letter-spacing:1px}}
td{{padding:7px 8px;border-bottom:1px solid #f0e8d8;font-size:11px}}
tr:nth-child(even) td{{background:#fdf8f2}}
.funds-grid{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:16px}}
.fund-item{{background:#fdf8f2;border:1px solid #e8d8b8;border-radius:6px;padding:8px 10px}}
.fund-label{{font-size:9px;color:#888;letter-spacing:1px;text-transform:uppercase}}
.fund-val{{font-size:14px;font-weight:600;color:#1a1a1a;margin-top:2px}}
.composite-box{{background:#fdf5e0;border:2px solid #c88a1a;border-radius:10px;padding:16px 20px;display:flex;justify-content:space-between;align-items:center;margin:16px 0}}
.composite-val{{font-size:28px;font-weight:300;color:#c88a1a}}
.verdict-box{{border-left:4px solid {vc};padding:12px 16px;background:#fafafa;border-radius:0 8px 8px 0;margin:12px 0}}
.verdict-text{{font-size:14px;font-weight:600;color:{vc};margin-bottom:4px}}
.ai-box{{background:#f0f8f5;border:1px solid #1e7a6a;border-radius:8px;padding:14px 16px;margin:12px 0}}
.ai-title{{font-size:9px;letter-spacing:3px;color:#1e7a6a;text-transform:uppercase;margin-bottom:8px;font-weight:600}}
.ai-box p{{font-size:11px;line-height:1.7;color:#1a1a1a;font-style:italic;margin:0}}
.footer{{border-top:1px solid #e0d0b0;margin-top:32px;padding-top:10px;font-size:9px;color:#aaa;font-style:italic;text-align:center}}
</style></head><body>
<div class="header">
  <div><div class="logo">SENECA</div><div class="logo-sub">Intrinsic Value Oracle</div></div>
  <div style="font-size:10px;color:#888;text-align:right">Generated {datetime.now().strftime('%B %d, %Y at %I:%M %p')}<br/>For educational purposes only</div>
</div>
<div style="font-size:22px;font-weight:600">{data['name']} ({data['ticker']})</div>
<div style="font-size:11px;color:#666;margin-top:4px">{data['sector']} · {"ETF/Index" if data.get("asset_type")=="etf" else "Stock"}</div>
<div style="font-size:36px;font-weight:300;color:#c88a1a;margin:12px 0">${data['price']:.2f} <span style="font-size:14px;color:#888">{'▲' if data['chg']>=0 else '▼'} {abs(data['chg']):.2f}%</span></div>
<div class="section-title">Fundamentals</div>
<div class="funds-grid">
  <div class="fund-item"><div class="fund-label">P/E Ratio</div><div class="fund-val">{data['pe']:.1f}×</div></div>
  <div class="fund-item"><div class="fund-label">Dividend Yield</div><div class="fund-val">{data['div_y']:.2f}%</div></div>
  <div class="fund-item"><div class="fund-label">Beta</div><div class="fund-val">{data['beta']:.2f}</div></div>
  <div class="fund-item"><div class="fund-label">EPS</div><div class="fund-val">${data['eps']:.2f}</div></div>
  <div class="fund-item"><div class="fund-label">52wk Momentum</div><div class="fund-val">{data['mom']:.1f}%</div></div>
  <div class="fund-item"><div class="fund-label">Growth Est.</div><div class="fund-val">{data['growth']:.1f}%</div></div>
</div>
<div class="section-title">Valuation Models</div>
<table><tr><th>Model</th><th>Formula</th><th style="text-align:right">Fair Value</th><th style="text-align:right">Signal</th></tr>{models_rows}</table>
<div class="composite-box">
  <div><div style="font-size:9px;letter-spacing:3px;color:#888;text-transform:uppercase">Seneca Composite</div></div>
  <div class="composite-val">{fp(data['composite'])}</div>
</div>
<div class="verdict-box">
  <div class="verdict-text">{data['verdict_text']}</div>
  <div style="font-size:11px;color:#666;font-style:italic">{data['verdict_detail']}</div>
</div>
{ai_section}
<div class="footer">SENECA is for educational and research purposes only. Not financial advice.</div>
</body></html>"""

# ── HTML ──────────────────────────────────────────────────────────────────────
def build_html(stripe_pk="", success_flash=False, user_email="", is_subscribed=False):
    success_js = "showToast('✦ Subscription active! Unlimited access unlocked.');" if success_flash else ""
    user_js = f"currentUser='{user_email}';isSubscribed={'true' if is_subscribed else 'false'};" if user_email else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"/>
<meta name="google-site-verification" content="dZDX1AMHsuaZcDjFD8CGt6EVQepwkUk4fre82eWuiHM" />
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<title>SENECA ◆ Intrinsic Value Oracle</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400&family=Share+Tech+Mono&display=swap" rel="stylesheet"/>
<script src="https://js.stripe.com/v3/"></script>
<style>
:root{{
  --bg:#0d0a04;--panel:#140d05;--card:#1e1308;--card2:#271908;--card3:#301f0c;
  --border:#4e3010;--border2:#6e4514;
  --gold:#c88a1a;--gold2:#e8aa34;--gold3:#f5cc60;
  --turq:#1e7a6a;--turq2:#28a892;--turq3:#48c4ae;
  --green:#3a8a24;--red:#a03020;--muted:#6a4e2c;
  --text:#ede4c8;--sub:#b88a4c;--dim:#4a3418;
  --safe:env(safe-area-inset-bottom,0px);
}}
*{{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}}
html,body{{min-height:100%;background:var(--bg);color:var(--text);font-family:'Cormorant Garamond',Georgia,serif;overscroll-behavior:none}}
::-webkit-scrollbar{{width:4px}}::-webkit-scrollbar-track{{background:var(--panel)}}::-webkit-scrollbar-thumb{{background:var(--border2);border-radius:2px}}
.hero{{min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px 24px;text-align:center;position:relative;overflow:hidden}}
.hero-bg{{position:absolute;inset:0;background:radial-gradient(ellipse 60% 50% at 50% 0%,rgba(200,138,26,.08) 0%,transparent 70%),radial-gradient(ellipse 40% 40% at 80% 80%,rgba(30,122,106,.06) 0%,transparent 60%),repeating-linear-gradient(0deg,transparent,transparent 79px,rgba(78,48,16,.15) 80px),repeating-linear-gradient(90deg,transparent,transparent 79px,rgba(78,48,16,.15) 80px)}}
.hero-diamond{{width:72px;height:72px;background:linear-gradient(135deg,var(--gold2),var(--gold3));transform:rotate(45deg);display:flex;align-items:center;justify-content:center;margin-bottom:28px;box-shadow:0 0 0 1px var(--gold3),0 0 60px rgba(232,170,52,.25)}}
.hero-diamond span{{transform:rotate(-45deg);font-size:1.3rem;color:var(--bg)}}
.hero-title{{font-size:clamp(2.8rem,8vw,5rem);font-weight:300;color:var(--gold3);letter-spacing:.35em;margin-bottom:6px;position:relative;z-index:1}}
.hero-sub{{font-size:1rem;color:var(--sub);font-style:italic;letter-spacing:.15em;margin-bottom:40px;position:relative;z-index:1}}
.hero-rule{{width:180px;height:1px;background:linear-gradient(90deg,transparent,var(--gold),transparent);margin:0 auto 40px}}
.hero-pitch{{max-width:520px;font-size:1.1rem;color:var(--sub);line-height:1.9;font-style:italic;margin-bottom:48px;position:relative;z-index:1}}
.hero-pitch strong{{color:var(--text);font-style:normal}}
.hero-cta{{display:flex;gap:14px;justify-content:center;flex-wrap:wrap;margin-bottom:48px;position:relative;z-index:1}}
.btn-primary{{background:linear-gradient(135deg,var(--turq),var(--turq2));color:var(--bg);border:none;border-radius:14px;padding:15px 32px;font-family:'Cormorant Garamond',serif;font-size:1.05rem;font-weight:600;letter-spacing:.1em;cursor:pointer;transition:all .25s}}
.btn-primary:hover{{transform:translateY(-2px)}}
.btn-ghost{{background:transparent;color:var(--gold2);border:1px solid var(--border2);border-radius:14px;padding:15px 32px;font-family:'Cormorant Garamond',serif;font-size:1.05rem;letter-spacing:.1em;cursor:pointer;transition:all .25s}}
.btn-ghost:hover{{border-color:var(--gold);color:var(--gold3);transform:translateY(-2px)}}
.hero-models{{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;max-width:500px;position:relative;z-index:1}}
.hm-badge{{background:var(--card2);border:1px solid var(--border2);border-radius:20px;padding:5px 14px;font-family:'Share Tech Mono',monospace;font-size:.58rem;color:var(--sub);letter-spacing:.06em}}
.hero-free-note{{font-size:.75rem;color:var(--dim);font-style:italic;margin-top:16px;position:relative;z-index:1}}
.app-shell{{display:none}}.app-shell.visible{{display:flex;flex-direction:column;min-height:100vh}}
header{{background:var(--panel);border-bottom:1px solid var(--border2);padding:14px 16px;padding-top:max(14px,env(safe-area-inset-top,14px));position:sticky;top:0;z-index:100}}
.hdr{{display:flex;align-items:center;justify-content:space-between;gap:8px}}
.logo-wrap{{display:flex;align-items:center;gap:10px;flex-shrink:0}}
.diamond{{width:32px;height:32px;background:var(--gold2);transform:rotate(45deg);display:flex;align-items:center;justify-content:center;flex-shrink:0}}
.diamond span{{transform:rotate(-45deg);font-size:.6rem;color:var(--bg);font-weight:600}}
.logo-name{{font-size:1.2rem;font-weight:600;color:var(--gold3);letter-spacing:.2em;line-height:1}}
.logo-tag{{font-size:.6rem;color:var(--sub);font-style:italic;letter-spacing:.05em;margin-top:2px}}
.hdr-right{{display:flex;align-items:center;gap:8px;flex-shrink:0}}
.clock{{font-family:'Share Tech Mono',monospace;font-size:.55rem;color:var(--dim);display:none}}
@media(min-width:480px){{.clock{{display:block}}}}
.btn-upgrade{{background:linear-gradient(135deg,var(--gold),var(--gold2));color:var(--bg);border:none;border-radius:10px;padding:6px 12px;font-family:'Cormorant Garamond',serif;font-size:.78rem;font-weight:600;cursor:pointer;white-space:nowrap}}
.btn-user{{background:var(--card2);color:var(--gold2);border:1px solid var(--border2);border-radius:10px;padding:6px 12px;font-family:'Share Tech Mono',monospace;font-size:.58rem;cursor:pointer;white-space:nowrap}}
/* ── MOBILE SEARCH FIX — stacked layout so button never overlaps ── */
.search-outer{{background:var(--panel);padding:12px 14px 0;border-bottom:1px solid var(--border)}}
.search-box{{background:var(--card);border:1px solid var(--border2);border-radius:14px;padding:10px 14px}}
.search-input-row{{display:flex;align-items:center;gap:8px;margin-bottom:6px}}
.search-box input{{flex:1;background:transparent;border:none;outline:none;color:var(--gold3);font-family:'Cormorant Garamond',serif;font-size:1.3rem;font-weight:600;text-align:center;text-transform:uppercase;letter-spacing:.12em;caret-color:var(--turq3);min-width:0;width:0}}
.search-box input::placeholder{{color:var(--dim);font-size:.85rem;letter-spacing:.03em;font-weight:400;text-transform:none}}
.btn-go{{background:var(--turq);color:var(--bg);border:none;border-radius:10px;font-family:'Cormorant Garamond',serif;font-size:.95rem;font-weight:600;padding:10px 16px;cursor:pointer;white-space:nowrap;flex-shrink:0;transition:all .2s}}
.btn-go:hover{{background:var(--turq2)}}.btn-go:disabled{{background:var(--dim);cursor:default}}
.search-hint{{font-size:.6rem;color:var(--dim);font-style:italic;text-align:center;padding-bottom:2px}}
.chips{{display:flex;flex-wrap:wrap;gap:6px;padding:8px 14px 10px}}
.chip{{background:var(--card2);border:1px solid var(--border2);border-radius:20px;padding:4px 12px;font-family:'Share Tech Mono',monospace;font-size:.6rem;color:var(--sub);cursor:pointer;transition:all .15s}}
.chip:hover{{background:var(--turq);border-color:var(--turq2);color:var(--bg)}}
.status{{font-family:'Share Tech Mono',monospace;font-size:.62rem;color:var(--dim);text-align:center;padding:6px 14px;background:var(--panel);border-bottom:1px solid var(--border);min-height:22px}}
.tabs{{display:flex;background:var(--panel);border-bottom:1px solid var(--border)}}
.tab{{flex:1;background:none;border:none;font-family:'Cormorant Garamond',serif;font-size:.92rem;color:var(--muted);padding:10px 8px;cursor:pointer;border-bottom:2px solid transparent;transition:all .2s;letter-spacing:.05em;position:relative;top:1px}}
.tab.active{{color:var(--gold2);border-bottom-color:var(--turq2)}}
main{{overflow-y:auto;padding:14px 13px;padding-bottom:calc(24px + var(--safe));flex:1}}
.div{{border:none;height:1px;background:linear-gradient(90deg,transparent,var(--border2),transparent);margin:10px 0}}
.sec{{display:flex;align-items:center;gap:10px;font-family:'Share Tech Mono',monospace;font-size:.58rem;color:var(--muted);letter-spacing:.14em;margin:16px 0 8px}}
.sec::before{{content:'';flex:1;height:1px;background:linear-gradient(90deg,transparent,var(--border))}}
.sec::after{{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--border),transparent)}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:16px;margin-bottom:12px;overflow:hidden}}
.card-t{{border-top:2px solid var(--turq2)}}.card-g{{border-top:2px solid var(--gold)}}
.ci{{padding:14px 15px}}
.co-name{{font-size:.78rem;color:var(--sub);font-style:italic;margin-bottom:4px}}
.price-row{{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:3px}}
.price{{font-size:2.6rem;font-weight:300;color:var(--gold3);line-height:1}}
.chg{{font-size:.95rem;font-weight:600}}.up{{color:var(--green)}}.down{{color:var(--red)}}
.meta{{font-family:'Share Tech Mono',monospace;font-size:.6rem;color:var(--muted);line-height:1.85;margin-top:6px}}
.etf-badge{{display:inline-block;background:rgba(30,122,106,.2);border:1px solid var(--turq2);border-radius:6px;padding:2px 8px;font-family:'Share Tech Mono',monospace;font-size:.55rem;color:var(--turq3);letter-spacing:.08em;margin-bottom:6px}}
.range-wrap{{margin-top:12px}}
.range-labels{{display:flex;justify-content:space-between;font-family:'Share Tech Mono',monospace;font-size:.58rem;margin-bottom:5px}}
.r-lo{{color:var(--red)}}.r-hi{{color:var(--green)}}.r-mid{{color:var(--dim)}}
.range-track{{height:7px;border-radius:4px;background:var(--card3);border:1px solid var(--border);position:relative}}
.range-fill{{height:100%;border-radius:4px;background:linear-gradient(90deg,var(--turq),var(--turq2));transition:width .9s cubic-bezier(.4,0,.2,1)}}
.range-thumb{{position:absolute;top:50%;transform:translate(-50%,-50%);width:16px;height:16px;background:var(--gold2);border:2px solid var(--gold3);border-radius:50%;transition:left .9s cubic-bezier(.4,0,.2,1)}}
.range-pct{{text-align:center;font-family:'Share Tech Mono',monospace;font-size:.56rem;color:var(--dim);margin-top:4px}}
.card-title{{font-size:.72rem;color:var(--gold);font-weight:600;letter-spacing:.1em;margin-bottom:10px;display:flex;align-items:center;gap:8px}}
.card-title::after{{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--border),transparent)}}
.fund-grid{{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--border)}}
.fc{{background:var(--card);padding:8px 10px;display:flex;justify-content:space-between;align-items:center}}
.fc:nth-child(4n+1),.fc:nth-child(4n+2){{background:var(--card2)}}
.fl{{font-family:'Share Tech Mono',monospace;font-size:.56rem;color:var(--muted)}}
.fv{{font-family:'Share Tech Mono',monospace;font-size:.68rem;color:var(--text);font-weight:bold}}
.mc{{background:var(--card);border:1px solid var(--border);border-radius:12px;margin-bottom:8px;display:flex;overflow:hidden}}
.ms{{width:4px;flex-shrink:0}}
.ms.gold{{background:linear-gradient(180deg,var(--gold3),var(--gold))}}
.ms.turq{{background:linear-gradient(180deg,var(--turq3),var(--turq))}}
.ms.muted{{background:linear-gradient(180deg,var(--muted),var(--dim))}}
.mb{{padding:10px 14px;flex:1;min-width:0}}
.mt{{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:2px}}
.mn{{font-size:.65rem;font-weight:600;letter-spacing:.07em}}
.mn.gold{{color:var(--gold2)}}.mn.turq{{color:var(--turq3)}}.mn.muted{{color:var(--sub)}}
.mv{{font-size:1.15rem;font-weight:300;white-space:nowrap}}
.mv.up{{color:var(--green)}}.mv.down{{color:var(--red)}}.mv.fair{{color:var(--gold2)}}.mv.na{{color:var(--dim)}}
.msig{{font-size:.6rem;font-style:italic;text-align:right;margin-bottom:2px}}
.msig.up{{color:var(--green)}}.msig.down{{color:var(--red)}}.msig.fair{{color:var(--gold)}}.msig.na{{color:var(--dim)}}
.mf{{font-family:'Share Tech Mono',monospace;font-size:.52rem;color:var(--dim);margin-top:1px}}
.comp{{border-radius:16px;margin-bottom:12px;overflow:hidden;border:1px solid var(--gold);background:var(--panel)}}
.band{{height:3px;background:linear-gradient(90deg,var(--gold),var(--turq2),var(--gold3),var(--turq),var(--gold))}}
.comp-inner{{padding:16px 17px;display:flex;justify-content:space-between;align-items:center}}
.comp-lbl{{font-size:.7rem;color:var(--gold);font-weight:600;letter-spacing:.1em;margin-bottom:3px}}
.comp-sub{{font-size:.58rem;color:var(--dim);font-style:italic}}
.comp-val{{font-size:2rem;font-weight:300;color:var(--gold3)}}
.verdict{{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:15px 17px;margin-bottom:12px;position:relative;overflow:hidden}}
.verdict::before{{content:'';position:absolute;left:0;top:0;bottom:0;width:3px}}
.verdict.up::before{{background:var(--green)}}.verdict.down::before{{background:var(--red)}}.verdict.fair::before{{background:var(--gold)}}
.vt{{font-size:1.05rem;font-weight:600;margin-bottom:5px}}
.vd{{font-size:.8rem;font-style:italic;color:var(--sub);line-height:1.6}}
.spin{{text-align:center;padding:52px 0;display:none}}.spin.on{{display:block}}
.ring{{width:42px;height:42px;border:2px solid var(--border2);border-top-color:var(--turq2);border-radius:50%;display:inline-block;animation:spin 1s linear infinite}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
.spin-txt{{font-size:.76rem;color:var(--sub);font-style:italic;margin-top:10px}}
.about-body{{font-size:.85rem;color:var(--sub);line-height:1.8;font-style:italic;margin-bottom:14px}}
.form-card{{background:var(--card);border:1px solid var(--border);border-left:3px solid var(--turq2);border-radius:0 10px 10px 0;padding:11px 14px;margin-bottom:8px}}
.form-name{{font-size:.78rem;color:var(--gold2);font-weight:600;margin-bottom:3px;letter-spacing:.04em}}
.form-eq{{font-family:'Share Tech Mono',monospace;font-size:.62rem;color:var(--turq3);margin-bottom:3px}}
.form-desc{{font-size:.7rem;color:var(--muted);font-style:italic;line-height:1.6}}
.disc{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px 14px;margin-top:12px}}
.disc p{{font-size:.64rem;color:var(--dim);font-style:italic;line-height:1.8}}
.hidden{{display:none!important}}
.err-card{{background:var(--card);border:1px solid var(--red);border-radius:14px;padding:16px 18px;margin-bottom:12px;border-left:4px solid var(--red)}}
.err-title{{font-size:.95rem;font-weight:600;color:var(--red);margin-bottom:6px}}
.err-body{{font-size:.82rem;color:var(--sub);line-height:1.7}}
.modal-overlay{{position:fixed;inset:0;background:rgba(0,0,0,.88);backdrop-filter:blur(6px);z-index:1000;display:none;align-items:center;justify-content:center;padding:16px}}
.modal-overlay.open{{display:flex}}
.modal{{background:var(--panel);border:1px solid var(--gold);border-radius:22px;max-width:400px;width:100%;overflow:hidden;animation:slideUp .35s cubic-bezier(.22,1,.36,1);max-height:92vh;overflow-y:auto}}
@keyframes slideUp{{from{{opacity:0;transform:translateY(30px)}}to{{opacity:1;transform:translateY(0)}}}}
.modal-band{{height:3px;background:linear-gradient(90deg,var(--gold),var(--turq2),var(--gold3))}}
.modal-body{{padding:24px 22px 26px}}
.modal-diamond{{width:46px;height:46px;background:linear-gradient(135deg,var(--gold2),var(--gold3));transform:rotate(45deg);display:flex;align-items:center;justify-content:center;margin:0 auto 16px}}
.modal-diamond span{{transform:rotate(-45deg);font-size:.88rem;color:var(--bg)}}
.modal-title{{text-align:center;font-size:1.4rem;font-weight:300;color:var(--gold3);letter-spacing:.15em;margin-bottom:6px}}
.modal-sub{{text-align:center;font-size:.78rem;color:var(--sub);font-style:italic;line-height:1.7;margin-bottom:20px}}
.modal-price{{text-align:center;margin-bottom:18px}}
.modal-price-num{{font-size:2.4rem;font-weight:300;color:var(--gold3)}}
.modal-price-per{{font-size:.86rem;color:var(--sub);font-style:italic}}
.modal-features{{list-style:none;margin-bottom:20px;display:flex;flex-direction:column;gap:7px}}
.modal-features li{{display:flex;align-items:center;gap:9px;font-size:.82rem;color:var(--sub);font-style:italic}}
.modal-features li::before{{content:'◆';color:var(--gold2);font-size:.5rem;flex-shrink:0}}
.btn-subscribe{{width:100%;background:linear-gradient(135deg,var(--turq),var(--turq2));color:var(--bg);border:none;border-radius:14px;padding:14px;font-family:'Cormorant Garamond',serif;font-size:1rem;font-weight:600;letter-spacing:.08em;cursor:pointer;transition:all .25s;margin-bottom:8px}}
.btn-subscribe:hover{{transform:translateY(-1px)}}
.btn-subscribe:disabled{{background:var(--dim);cursor:default;transform:none}}
.btn-cancel-modal{{width:100%;background:transparent;color:var(--dim);border:none;font-family:'Cormorant Garamond',serif;font-size:.78rem;font-style:italic;cursor:pointer;padding:5px}}
.btn-cancel-modal:hover{{color:var(--sub)}}
.auth-field{{margin-bottom:13px}}
.auth-label{{font-family:'Share Tech Mono',monospace;font-size:.56rem;color:var(--muted);letter-spacing:.1em;margin-bottom:5px;display:block}}
.auth-input{{width:100%;background:var(--card);border:1px solid var(--border2);border-radius:10px;padding:10px 13px;color:var(--text);font-family:'Cormorant Garamond',serif;font-size:1rem;outline:none;transition:border-color .2s}}
.auth-input:focus{{border-color:var(--turq2)}}
.auth-error{{font-size:.74rem;color:var(--red);font-style:italic;text-align:center;margin-bottom:9px;min-height:18px}}
.auth-switch{{text-align:center;font-size:.76rem;color:var(--dim);font-style:italic;margin-top:9px}}
.auth-switch a{{color:var(--gold2);cursor:pointer}}
.toast{{position:fixed;bottom:28px;left:50%;transform:translateX(-50%) translateY(80px);background:var(--green);color:#fff;border-radius:12px;padding:11px 20px;font-size:.82rem;font-style:italic;letter-spacing:.05em;z-index:2000;opacity:0;transition:all .4s cubic-bezier(.22,1,.36,1);white-space:nowrap;max-width:90vw;text-align:center}}
.toast.show{{opacity:1;transform:translateX(-50%) translateY(0)}}
.pricing-card{{background:var(--card);border:1px solid var(--gold);border-radius:18px;overflow:hidden;margin-bottom:12px}}
.pricing-band{{height:2px;background:linear-gradient(90deg,var(--gold),var(--turq2),var(--gold3))}}
.pricing-inner{{padding:20px 18px}}
.pricing-title{{font-size:.7rem;color:var(--gold);font-weight:600;letter-spacing:.12em;margin-bottom:12px}}
.pricing-amount{{font-size:2.2rem;font-weight:300;color:var(--gold3);margin-bottom:3px}}
.pricing-note{{font-size:.7rem;color:var(--sub);font-style:italic;margin-bottom:16px}}
.ai-card{{background:var(--card);border:1px solid var(--turq);border-radius:16px;padding:14px 16px;margin-bottom:12px;position:relative;overflow:hidden}}
.ai-card::before{{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:linear-gradient(180deg,var(--turq3),var(--turq))}}
.ai-title{{font-family:'Share Tech Mono',monospace;font-size:.58rem;color:var(--turq3);letter-spacing:.14em;margin-bottom:8px}}
.ai-text{{font-size:.86rem;color:var(--sub);line-height:1.8;font-style:italic}}
.ai-loading{{display:flex;align-items:center;gap:7px}}
.ai-dot{{width:5px;height:5px;border-radius:50%;background:var(--turq2);animation:pulse 1.2s ease-in-out infinite}}
.ai-dot:nth-child(2){{animation-delay:.2s}}.ai-dot:nth-child(3){{animation-delay:.4s}}
@keyframes pulse{{0%,100%{{opacity:.3}}50%{{opacity:1}}}}
.compare-outer{{background:var(--panel);padding:12px 14px 0;border-bottom:1px solid var(--border);display:none}}
.compare-outer.visible{{display:block}}
.compare-inputs{{display:flex;gap:7px;align-items:center}}
.compare-inputs input{{flex:1;background:var(--card);border:1px solid var(--border2);border-radius:10px;padding:8px 10px;color:var(--gold3);font-family:'Cormorant Garamond',serif;font-size:1.05rem;font-weight:600;text-align:center;text-transform:uppercase;letter-spacing:.1em;outline:none;min-width:0}}
.compare-inputs input::placeholder{{color:var(--dim);font-size:.78rem;font-weight:400;letter-spacing:0}}
.btn-compare{{background:var(--gold);color:var(--bg);border:none;border-radius:10px;font-family:'Cormorant Garamond',serif;font-size:.88rem;font-weight:600;padding:9px 14px;cursor:pointer;white-space:nowrap;flex-shrink:0}}
.btn-compare:hover{{background:var(--gold2)}}
.compare-note{{font-size:.58rem;color:var(--dim);font-style:italic;padding:5px 0 10px;text-align:center}}
.compare-grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px}}
.compare-col{{background:var(--card);border:1px solid var(--border);border-radius:14px;overflow:hidden}}
.compare-col-header{{padding:10px 12px;border-bottom:1px solid var(--border)}}
.compare-ticker{{font-size:1.2rem;font-weight:600;color:var(--gold3);letter-spacing:.1em}}
.compare-name{{font-size:.6rem;color:var(--sub);font-style:italic;margin-top:2px}}
.compare-price{{font-size:1.3rem;font-weight:300;color:var(--gold3);margin:3px 0 2px}}
.compare-row{{display:flex;justify-content:space-between;padding:6px 12px;border-bottom:1px solid var(--border)}}
.compare-lbl{{color:var(--muted);font-family:'Share Tech Mono',monospace;font-size:.54rem}}
.compare-val{{color:var(--text);font-family:'Share Tech Mono',monospace;font-size:.63rem;font-weight:bold}}
.compare-winner{{background:rgba(30,122,106,.12);border-color:var(--turq2)}}
.compare-verdict-row{{padding:9px 12px}}
.compare-verdict-txt{{font-size:.74rem;font-weight:600}}
.compare-verdict-txt.up{{color:var(--green)}}.compare-verdict-txt.down{{color:var(--red)}}.compare-verdict-txt.fair{{color:var(--gold2)}}
.watchlist-bar{{background:var(--panel);border-bottom:1px solid var(--border);padding:9px 14px;display:none}}
.watchlist-bar.visible{{display:block}}
.watchlist-title{{font-family:'Share Tech Mono',monospace;font-size:.56rem;color:var(--muted);letter-spacing:.12em;margin-bottom:6px}}
.watchlist-items{{display:flex;flex-wrap:wrap;gap:5px}}
.wl-item{{background:var(--card2);border:1px solid var(--border2);border-radius:20px;padding:3px 9px 3px 11px;display:flex;align-items:center;gap:5px;font-family:'Share Tech Mono',monospace;font-size:.6rem;color:var(--sub);cursor:pointer}}
.wl-item:hover{{border-color:var(--turq2);color:var(--text)}}
.wl-remove{{background:none;border:none;cursor:pointer;color:var(--muted);font-size:.68rem;padding:0 2px;line-height:1}}
.wl-remove:hover{{color:var(--red)}}
.action-row{{display:flex;gap:7px;margin-bottom:12px;flex-wrap:wrap}}
.btn-action{{background:var(--card2);border:1px solid var(--border2);color:var(--sub);border-radius:10px;padding:7px 13px;font-family:'Cormorant Garamond',serif;font-size:.8rem;cursor:pointer;transition:all .2s}}
.btn-action:hover{{border-color:var(--gold);color:var(--gold2)}}
.btn-action.active{{border-color:var(--turq2);color:var(--turq3);background:rgba(30,122,106,.1)}}
</style>
</head>
<body>

<div class="hero" id="hero">
  <div class="hero-bg"></div>
  <div class="hero-diamond"><span>◆</span></div>
  <div class="hero-title">SENECA</div>
  <div class="hero-sub">Intrinsic Value Oracle</div>
  <div class="hero-rule"></div>
  <div class="hero-pitch">Six <strong>classical valuation models</strong> — Graham, Buffett, Lynch, and more — synthesised into one honest verdict. Stocks, ETFs &amp; indexes. No noise. No hype. Just <strong>what it's actually worth.</strong></div>
  <div class="hero-cta">
    <button class="btn-primary" onclick="enterApp()">◈ &nbsp;Try Free Lookup</button>
    <button class="btn-ghost" onclick="showAuthModal('signup')">✦ &nbsp;Create Account — $9/mo</button>
  </div>
  <div class="hero-models">
    <span class="hm-badge">Graham · Buffett · Lynch</span>
    <span class="hm-badge">Fed Model · ETF Support</span>
    <span class="hm-badge">AI Verdict</span>
    <span class="hm-badge">Login · Any Device</span>
    <span class="hm-badge">PDF Export</span>
  </div>
  <div class="hero-free-note">First lookup free · <a href="#" onclick="showAuthModal('login');return false;" style="color:var(--gold2);text-decoration:none">Sign in to your account</a></div>
</div>

<div class="app-shell" id="app">
<header>
<div class="hdr">
  <div class="logo-wrap">
    <div class="diamond"><span>◆</span></div>
    <div><div class="logo-name">SENECA</div><div class="logo-tag">Intrinsic Value Oracle</div></div>
  </div>
  <div class="hdr-right">
    <div class="clock" id="clock"></div>
    <button class="btn-user hidden" id="user-btn" onclick="showUserMenu()">◆ Account</button>
    <button class="btn-upgrade" id="upgrade-btn" onclick="showAuthModal('signup')">✦ $9/mo</button>
  </div>
</div>
</header>

<div class="watchlist-bar" id="wl-bar">
  <div class="watchlist-title">◈ &nbsp;WATCHLIST</div>
  <div class="watchlist-items" id="wl-items"></div>
</div>

<div class="search-outer">
  <div class="search-box">
    <div class="search-input-row">
      <input id="ti" type="text" placeholder="Apple · AAPL · S&amp;P 500 · SPY" maxlength="60" autocomplete="off" spellcheck="false"/>
      <button class="btn-go" id="btn" onclick="go()">◈ Analyze</button>
    </div>
    <div class="search-hint">Ticker symbol OR company name OR ETF name</div>
  </div>
  <div class="chips">
    <span class="chip" onclick="pick('AAPL')">AAPL</span>
    <span class="chip" onclick="pick('MSFT')">MSFT</span>
    <span class="chip" onclick="pick('TSLA')">TSLA</span>
    <span class="chip" onclick="pick('NVDA')">NVDA</span>
    <span class="chip" onclick="pick('SPY')">SPY</span>
    <span class="chip" onclick="pick('QQQ')">QQQ</span>
    <span class="chip" onclick="pick('VOO')">VOO</span>
    <span class="chip" onclick="pick('KO')">KO</span>
    <span class="chip" onclick="pick('AMZN')">AMZN</span>
    <span class="chip" onclick="pick('BRK-B')">BRK-B</span>
  </div>
</div>

<div class="compare-outer" id="compare-outer">
  <div class="compare-inputs">
    <input id="cmp1" type="text" placeholder="AAPL or Apple" maxlength="60" autocomplete="off" spellcheck="false"/>
    <span style="color:var(--dim);font-size:1rem;flex-shrink:0">vs</span>
    <input id="cmp2" type="text" placeholder="MSFT or Microsoft" maxlength="60" autocomplete="off" spellcheck="false"/>
    <button class="btn-compare" onclick="doCompare()">Go</button>
  </div>
  <div class="compare-note">Tickers or company names both work</div>
</div>

<div class="status" id="st">Enter a ticker, company name, or ETF to begin</div>
<div class="tabs">
  <button class="tab active" id="nav-a" onclick="tabSwitch('a')">◈ &nbsp;Analyze</button>
  <button class="tab" id="nav-b" onclick="tabSwitch('b')">✦ &nbsp;About</button>
</div>
<main id="scroll">
  <div id="pane-a">
    <div class="spin" id="spin"><div class="ring"></div><div class="spin-txt">Consulting the oracle…</div></div>
    <div id="res" class="hidden"></div>
    <div id="cmp-res" class="hidden"></div>
  </div>
  <div id="pane-b" class="hidden">
    <div class="card card-g"><div class="ci">
      <div class="card-title">✦ &nbsp;ABOUT SENECA</div>
      <div class="about-body">Named for the Stoic philosopher and the Seneca Nation. Six time-tested valuation frameworks reveal what a stock, ETF, or index is truly worth. Create an account to access your subscription from any device.</div>
    </div></div>
    <div class="pricing-card"><div class="pricing-band"></div><div class="pricing-inner">
      <div class="pricing-title">◆ &nbsp;FULL ACCESS</div>
      <div class="pricing-amount">$9<span style="font-size:1.1rem;color:var(--sub)">/mo</span></div>
      <div class="pricing-note">Unlimited lookups · AI Verdicts · ETFs &amp; Indexes · Any device · Cancel anytime</div>
      <button class="btn-subscribe" onclick="showAuthModal('signup')" style="max-width:240px">✦ &nbsp;Create Account</button>
    </div></div>
    <div class="form-card"><div class="form-name">Graham Number</div><div class="form-eq">√( 22.5 × EPS × Book Value )</div><div class="form-desc">Ben Graham's bedrock formula for stocks.</div></div>
    <div class="form-card"><div class="form-name">Buffett DCF</div><div class="form-eq">10yr EPS @ 9% discount · 15× terminal</div><div class="form-desc">Discounts projected earnings to present value.</div></div>
    <div class="form-card"><div class="form-name">Peter Lynch PEG</div><div class="form-eq">EPS × growth% (PEG = 1)</div><div class="form-desc">A fair stock has a P/E equal to its growth rate.</div></div>
    <div class="form-card"><div class="form-name">Fed Model (ETFs/Indexes)</div><div class="form-eq">Price × (Earnings Yield ÷ 10yr Treasury)</div><div class="form-desc">Compares earnings yield to the risk-free rate.</div></div>
    <div class="form-card"><div class="form-name">P/E Mean Reversion (ETFs)</div><div class="form-eq">Price × (Historical 17× P/E ÷ Current P/E)</div><div class="form-desc">Fair value at a historically normal valuation.</div></div>
    <div class="form-card"><div class="form-name">Dividend Discount (ETFs)</div><div class="form-eq">Annual Dividend ÷ (8% return − 3% growth)</div><div class="form-desc">Intrinsic value based purely on dividend income.</div></div>
    <div class="disc"><p>✦ Seneca is for educational and research purposes only. Not financial advice. Always conduct your own due diligence.</p></div>
  </div>
</main>
</div>

<!-- Paywall Modal -->
<div class="modal-overlay" id="modal-paywall">
  <div class="modal"><div class="modal-band"></div><div class="modal-body">
    <div class="modal-diamond"><span>◆</span></div>
    <div class="modal-title">UNLOCK SENECA</div>
    <div class="modal-sub">You've used your free lookup.<br/>Create an account to subscribe for unlimited access on any device.</div>
    <div class="modal-price"><div class="modal-price-num">$9</div><div class="modal-price-per">per month · cancel anytime</div></div>
    <ul class="modal-features">
      <li>Unlimited lookups on any device</li>
      <li>Stocks, ETFs &amp; indexes</li>
      <li>AI-powered verdict explanations</li>
      <li>Persistent watchlist &amp; PDF export</li>
    </ul>
    <button class="btn-subscribe" onclick="closePaywall();showAuthModal('signup')">✦ &nbsp;Create Account &amp; Subscribe</button>
    <button class="btn-cancel-modal" onclick="closePaywall()">Maybe later</button>
  </div></div>
</div>

<!-- Auth Modal -->
<div class="modal-overlay" id="modal-auth">
  <div class="modal"><div class="modal-band"></div><div class="modal-body">
    <div class="modal-diamond"><span>◆</span></div>
    <div class="modal-title" id="auth-title">CREATE ACCOUNT</div>
    <div class="modal-sub" id="auth-sub">Subscribe once, access from any device.</div>
    <div class="auth-error" id="auth-error"></div>
    <div class="auth-field">
      <label class="auth-label">EMAIL ADDRESS</label>
      <input class="auth-input" id="auth-email" type="email" placeholder="you@example.com" autocomplete="email"/>
    </div>
    <div class="auth-field">
      <label class="auth-label">PASSWORD</label>
      <input class="auth-input" id="auth-pw" type="password" placeholder="••••••••" autocomplete="current-password"/>
    </div>
    <button class="btn-subscribe" id="auth-btn" onclick="doAuth()">✦ &nbsp;Continue</button>
    <button class="btn-cancel-modal" onclick="closeAuthModal()">Cancel</button>
    <div class="auth-switch" id="auth-switch">Already have an account? <a onclick="toggleAuthMode()">Sign in</a></div>
  </div></div>
</div>

<!-- User Menu Modal -->
<div class="modal-overlay" id="modal-user">
  <div class="modal"><div class="modal-band"></div><div class="modal-body">
    <div class="modal-title">◆ ACCOUNT</div>
    <div style="font-family:'Share Tech Mono',monospace;font-size:.58rem;color:var(--dim);text-align:center;margin-bottom:14px" id="user-info-text"></div>
    <div id="user-sub-status" style="text-align:center;margin-bottom:18px;font-size:.82rem;color:var(--sub);font-style:italic"></div>
    <button class="btn-subscribe" id="user-sub-btn" onclick="doSubscribeFromAccount()" style="display:none">✦ &nbsp;Subscribe — $9/mo</button>
    <button class="btn-action" style="width:100%;justify-content:center;margin-bottom:10px;display:block;text-align:center" onclick="doLogout()">Sign Out</button>
    <button class="btn-cancel-modal" onclick="closeUserMenu()">Close</button>
  </div></div>
</div>

<div class="toast" id="toast"></div>

<script>
const STRIPE_PK = "{stripe_pk}";
const stripe = STRIPE_PK ? Stripe(STRIPE_PK) : null;
let currentUser = '';
let isSubscribed = false;
let authMode = 'signup';
let watchlist = [];
let lastData = null;
let compareMode = false;

window.addEventListener('DOMContentLoaded', () => {{
  {success_js}
  {user_js}
  if (currentUser) {{
    watchlist = JSON.parse(localStorage.getItem('seneca_wl_' + currentUser) || '[]');
    if (isSubscribed) {{
      document.getElementById('upgrade-btn').style.display = 'none';
      document.getElementById('user-btn').classList.remove('hidden');
    }} else {{
      document.getElementById('user-btn').classList.remove('hidden');
    }}
  }} else {{
    watchlist = JSON.parse(sessionStorage.getItem('seneca_wl') || '[]');
  }}
  renderWatchlist();
}});

(function tick() {{
  const el = document.getElementById('clock');
  if (el) el.textContent = new Date().toLocaleTimeString([],{{hour:'2-digit',minute:'2-digit',second:'2-digit'}});
  setTimeout(tick, 1000);
}})();

function enterApp() {{
  document.getElementById('hero').style.display = 'none';
  document.getElementById('app').classList.add('visible');
}}
function tabSwitch(t) {{
  ['a','b'].forEach(x => {{
    document.getElementById('pane-'+x).classList.toggle('hidden', x!==t);
    document.getElementById('nav-'+x).classList.toggle('active', x===t);
  }});
}}
function pick(s) {{ document.getElementById('ti').value = s; go(); }}
document.getElementById('ti').addEventListener('keydown', e => {{ if(e.key==='Enter'){{e.preventDefault();go();}} }});

// ── Auth ──────────────────────────────────────────────────────────────────
function showAuthModal(mode) {{
  authMode = mode || 'signup';
  updateAuthUI();
  document.getElementById('modal-auth').classList.add('open');
  setTimeout(() => document.getElementById('auth-email').focus(), 300);
}}
function closeAuthModal() {{
  document.getElementById('modal-auth').classList.remove('open');
  document.getElementById('auth-error').textContent = '';
}}
function toggleAuthMode() {{
  authMode = authMode === 'signup' ? 'login' : 'signup';
  updateAuthUI();
}}
function updateAuthUI() {{
  const s = authMode === 'signup';
  document.getElementById('auth-title').textContent = s ? 'CREATE ACCOUNT' : 'SIGN IN';
  document.getElementById('auth-sub').textContent = s ? 'Subscribe once, access from any device.' : 'Welcome back to SENECA.';
  document.getElementById('auth-btn').textContent = s ? '✦  Create Account' : '✦  Sign In';
  document.getElementById('auth-switch').innerHTML = s
    ? 'Already have an account? <a onclick="toggleAuthMode()">Sign in</a>'
    : "Don't have an account? <a onclick=\"toggleAuthMode()\">Sign up</a>";
  document.getElementById('auth-error').textContent = '';
}}
async function doAuth() {{
  const email = document.getElementById('auth-email').value.trim();
  const pw = document.getElementById('auth-pw').value;
  const btn = document.getElementById('auth-btn');
  if (!email || !pw) {{ document.getElementById('auth-error').textContent = 'Please enter email and password.'; return; }}
  btn.disabled = true; btn.textContent = 'Please wait…';
  try {{
    const r = await fetch('/api/auth', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{mode: authMode, email, pw}})
    }});
    const d = await r.json();
    if (!r.ok) {{ document.getElementById('auth-error').textContent = d.error || 'Error'; return; }}
    currentUser = d.email; isSubscribed = d.subscribed;
    watchlist = JSON.parse(localStorage.getItem('seneca_wl_' + currentUser) || '[]');
    renderWatchlist();
    closeAuthModal();
    document.getElementById('user-btn').classList.remove('hidden');
    if (isSubscribed) {{
      document.getElementById('upgrade-btn').style.display = 'none';
      showToast('✦ Welcome back! Subscription active.');
    }} else {{
      showToast('◆ Account ready! Subscribe for unlimited access.');
      if (authMode === 'signup') setTimeout(openPaywall, 600);
    }}
    enterApp();
  }} catch(e) {{
    document.getElementById('auth-error').textContent = 'Network error. Try again.';
  }} finally {{
    btn.disabled = false; updateAuthUI();
    document.getElementById('auth-btn').textContent = authMode==='signup' ? '✦  Create Account' : '✦  Sign In';
  }}
}}

// ── User menu ─────────────────────────────────────────────────────────────
function showUserMenu() {{
  document.getElementById('user-info-text').textContent = '◆ ' + currentUser;
  const ss = document.getElementById('user-sub-status');
  const sb = document.getElementById('user-sub-btn');
  if (isSubscribed) {{ ss.textContent='✦ Active subscription'; ss.style.color='var(--green)'; sb.style.display='none'; }}
  else {{ ss.textContent='No active subscription'; ss.style.color='var(--dim)'; sb.style.display='block'; }}
  document.getElementById('modal-user').classList.add('open');
}}
function closeUserMenu() {{ document.getElementById('modal-user').classList.remove('open'); }}
async function doLogout() {{
  await fetch('/api/logout',{{method:'POST'}});
  currentUser=''; isSubscribed=false; watchlist=[];
  document.getElementById('upgrade-btn').style.display='';
  document.getElementById('user-btn').classList.add('hidden');
  closeUserMenu(); renderWatchlist(); showToast('Signed out.');
}}
async function doSubscribeFromAccount() {{ closeUserMenu(); await startSubscribe(); }}

// ── Paywall ───────────────────────────────────────────────────────────────
function openPaywall() {{ document.getElementById('modal-paywall').classList.add('open'); }}
function closePaywall() {{ document.getElementById('modal-paywall').classList.remove('open'); }}
['modal-paywall','modal-auth','modal-user'].forEach(id => {{
  document.getElementById(id).addEventListener('click', e => {{ if(e.target===e.currentTarget) document.getElementById(id).classList.remove('open'); }});
}});

async function startSubscribe() {{
  try {{
    const r = await fetch('/api/create-checkout-session',{{method:'POST'}});
    const d = await r.json();
    if (d.url) {{ window.location.href = d.url; }}
    else {{ isSubscribed=true; document.getElementById('upgrade-btn').style.display='none'; showToast('✦ Demo mode!'); }}
  }} catch(e) {{
    isSubscribed=true; document.getElementById('upgrade-btn').style.display='none'; showToast('✦ Demo mode!');
  }}
}}

// ── Watchlist ─────────────────────────────────────────────────────────────
function renderWatchlist() {{
  const bar = document.getElementById('wl-bar');
  const items = document.getElementById('wl-items');
  if (!watchlist.length) {{ bar.classList.remove('visible'); return; }}
  bar.classList.add('visible');
  items.innerHTML = watchlist.map(t => `<span class="wl-item" onclick="pick('${{t}}')">${{t}}<button class="wl-remove" onclick="event.stopPropagation();removeFromWatchlist('${{t}}')">✕</button></span>`).join('');
}}
function addToWatchlist(ticker) {{
  if (!ticker || watchlist.includes(ticker)) return;
  watchlist.push(ticker);
  if (currentUser) localStorage.setItem('seneca_wl_'+currentUser, JSON.stringify(watchlist));
  else sessionStorage.setItem('seneca_wl', JSON.stringify(watchlist));
  renderWatchlist(); showToast('◆ '+ticker+' added to watchlist');
}}
function removeFromWatchlist(ticker) {{
  watchlist = watchlist.filter(t => t!==ticker);
  if (currentUser) localStorage.setItem('seneca_wl_'+currentUser, JSON.stringify(watchlist));
  else sessionStorage.setItem('seneca_wl', JSON.stringify(watchlist));
  renderWatchlist();
}}

// ── Compare ───────────────────────────────────────────────────────────────
function toggleCompare() {{
  compareMode = !compareMode;
  document.getElementById('compare-outer').classList.toggle('visible', compareMode);
  document.getElementById('compare-btn').classList.toggle('active', compareMode);
  if (compareMode) document.getElementById('cmp1').focus();
}}
async function doCompare() {{
  const t1 = document.getElementById('cmp1').value.trim();
  const t2 = document.getElementById('cmp2').value.trim();
  if (!t1||!t2) {{ showToast('Enter two tickers or company names'); return; }}
  tabSwitch('a');
  document.getElementById('spin').classList.add('on');
  document.getElementById('res').classList.add('hidden');
  document.getElementById('cmp-res').classList.add('hidden');
  st('Comparing '+t1+' vs '+t2+'…','var(--turq3)');
  try {{
    const [r1,r2] = await Promise.all([
      fetch('/api/quote?ticker='+encodeURIComponent(t1)),
      fetch('/api/quote?ticker='+encodeURIComponent(t2))
    ]);
    if (r1.status===402||r2.status===402) {{ st('Subscribe for comparisons','var(--gold2)'); openPaywall(); return; }}
    if (!r1.ok||!r2.ok) throw new Error('Could not fetch one or both');
    const [d1,d2] = await Promise.all([r1.json(),r2.json()]);
    renderCompare(d1,d2);
    st('Comparison: '+d1.ticker+' vs '+d2.ticker,'var(--green)');
  }} catch(e) {{ st('⚠ '+e.message,'var(--red)'); }}
  finally {{ document.getElementById('spin').classList.remove('on'); }}
}}
function renderCompare(d1,d2) {{
  function fp(v) {{ if(!v||v<=0) return 'N/A'; return '$'+v.toLocaleString('en-US',{{minimumFractionDigits:2,maximumFractionDigits:2}}); }}
  const c1win = (d1.composite&&d2.composite) ? d1.composite>d2.composite : false;
  function col(d,win) {{
    return `<div class="compare-col ${{win?'compare-winner':''}}">
      <div class="compare-col-header">
        <div class="compare-ticker">${{d.ticker}}${{win?' ✦':''}}</div>
        <div class="compare-name">${{d.name}}</div>
        <div class="compare-price">${{fp(d.price)}}</div>
      </div>
      <div class="compare-row"><span class="compare-lbl">Composite FV</span><span class="compare-val">${{fp(d.composite)}}</span></div>
      <div class="compare-row"><span class="compare-lbl">P/E</span><span class="compare-val">${{d.pe?d.pe.toFixed(1)+'×':'—'}}</span></div>
      <div class="compare-row"><span class="compare-lbl">Div Yield</span><span class="compare-val">${{d.div_y?d.div_y.toFixed(2)+'%':'—'}}</span></div>
      <div class="compare-row"><span class="compare-lbl">Beta</span><span class="compare-val">${{d.beta?d.beta.toFixed(2):'—'}}</span></div>
      <div class="compare-row"><span class="compare-lbl">Type</span><span class="compare-val">${{d.asset_type==='etf'?'ETF/Index':'Stock'}}</span></div>
      <div class="compare-verdict-row"><div class="compare-verdict-txt ${{d.verdict_cls}}">${{d.verdict_text}}</div></div>
    </div>`;
  }}
  document.getElementById('cmp-res').innerHTML = `<div class="sec">◈ &nbsp;COMPARISON</div><div class="compare-grid">${{col(d1,c1win)}}${{col(d2,!c1win)}}</div>`;
  document.getElementById('cmp-res').classList.remove('hidden');
  document.getElementById('res').classList.add('hidden');
}}

// ── PDF ───────────────────────────────────────────────────────────────────
async function exportPDF() {{
  if (!lastData) {{ showToast('Analyze a stock first'); return; }}
  showToast('◆ Generating report…');
  try {{
    const r = await fetch('/api/export-pdf?ticker='+encodeURIComponent(lastData.ticker));
    if (r.status===402) {{ openPaywall(); return; }}
    if (!r.ok) throw new Error('Report error');
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href=url; a.download='SENECA-'+lastData.ticker+'-report.pdf';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast('◆ Report downloaded!');
  }} catch(e) {{ showToast('⚠ '+e.message); }}
}}

// ── AI Verdict ────────────────────────────────────────────────────────────
async function loadAiVerdict(ticker) {{
  const el = document.getElementById('ai-verdict-text');
  if (!el) return;
  try {{
    const r = await fetch('/api/ai-verdict?ticker='+encodeURIComponent(ticker));
    if (r.ok) {{
      const j = await r.json();
      el.innerHTML = j.verdict
        ? '<span class="ai-text">'+j.verdict+'</span>'
        : '<span style="color:var(--dim);font-size:.7rem;font-style:italic">Add ANTHROPIC_API_KEY in Railway to enable</span>';
    }}
  }} catch(e) {{ el.innerHTML='<span style="color:var(--dim);font-size:.7rem;font-style:italic">AI analysis unavailable</span>'; }}
}}

// ── Main go ───────────────────────────────────────────────────────────────
async function go() {{
  const t = document.getElementById('ti').value.trim();
  if (!t) return;
  tabSwitch('a');
  document.getElementById('btn').disabled = true;
  document.getElementById('res').classList.add('hidden');
  document.getElementById('cmp-res').classList.add('hidden');
  document.getElementById('spin').classList.add('on');
  st('Consulting the oracle…','var(--turq3)');
  try {{
    const r = await fetch('/api/quote?ticker='+encodeURIComponent(t));
    if (r.status===402) {{ st('Free lookup used — subscribe for unlimited access','var(--gold2)'); openPaywall(); return; }}
    if (!r.ok) {{ const e=await r.json(); throw new Error(e.error||'Server error'); }}
    const d = await r.json();
    lastData = d;
    render(d);
    st('Analysis complete · '+d.ticker+' · '+new Date().toLocaleTimeString(),'var(--green)');
    loadAiVerdict(d.ticker);
  }} catch(e) {{
    st('⚠ '+e.message,'var(--red)');
    document.getElementById('res').innerHTML=`<div class="err-card"><div class="err-title">⚠ Could not fetch data</div><div class="err-body">${{e.message}}</div></div>`;
    document.getElementById('res').classList.remove('hidden');
  }} finally {{
    document.getElementById('spin').classList.remove('on');
    document.getElementById('btn').disabled = false;
  }}
}}

// ── Render ────────────────────────────────────────────────────────────────
function render(d) {{
  const p = d.price; let html = '';
  const pct = (d.hi52>d.lo52&&d.lo52>0) ? Math.min(Math.max((p-d.lo52)/(d.hi52-d.lo52),0),1)*100 : null;
  const isEtf = d.asset_type === 'etf';
  html += `<div class="card card-t"><div class="ci">
    ${{isEtf?'<div class="etf-badge">◈ ETF / INDEX FUND</div>':''}}
    <div class="co-name">${{d.name}} &nbsp;(${{d.ticker}})</div>
    <div class="price-row">
      <div class="price">${{fp(p)}}</div>
      <div class="chg ${{d.chg>=0?'up':'down'}}">${{d.chg>=0?'▲':'▼'}} ${{Math.abs(d.chg).toFixed(2)}}%</div>
    </div>
    <div class="meta">${{d.sector}} · ${{fc(d.cap)}} · Prev $${{d.prev.toFixed(2)}}</div>
    <hr class="div"/>
    <div class="range-wrap">
      <div class="range-labels">
        <span class="r-lo">${{d.lo52>0?'$'+d.lo52.toFixed(2):'—'}}</span>
        <span class="r-mid">52 · W E E K</span>
        <span class="r-hi">${{d.hi52>0?'$'+d.hi52.toFixed(2):'—'}}</span>
      </div>
      <div class="range-track">
        <div class="range-fill" style="width:${{pct!==null?pct:0}}%"></div>
        <div class="range-thumb" style="left:${{pct!==null?pct:0}}%"></div>
      </div>
      <div class="range-pct">${{pct!==null?pct.toFixed(0)+'th percentile':'unavailable'}}</div>
    </div>
  </div></div>`;

  const funds = isEtf ? [
    ['P/E Ratio',d.pe?d.pe.toFixed(1)+'×':'—'],['Dividend Yield',d.div_y?d.div_y.toFixed(2)+'%':'—'],
    ['Beta',d.beta?d.beta.toFixed(2):'—'],['52wk Momentum',d.mom.toFixed(1)+'%'],
    ['Earnings Yield',d.earnings_yield?d.earnings_yield.toFixed(2)+'%':'—'],['Growth Est.',d.growth?d.growth.toFixed(1)+'%':'—'],
  ] : [
    ['EPS (TTM)',d.eps?'$'+d.eps.toFixed(2):'—'],['Book Val/Shr',d.bvps?'$'+d.bvps.toFixed(2):'—'],
    ['P/E Ratio',d.pe?d.pe.toFixed(1)+'×':'—'],['P/B Ratio',d.pb?d.pb.toFixed(2)+'×':'—'],
    ['Ret. on Equity',d.roe?d.roe.toFixed(1)+'%':'—'],['Growth Est.',d.growth?d.growth.toFixed(1)+'%':'—'],
    ['FCF/Share',d.fcf?'$'+d.fcf.toFixed(2):'—'],['52wk Momentum',d.mom.toFixed(1)+'%'],
    ['Dividend Yield',d.div_y?d.div_y.toFixed(2)+'%':'—'],['Beta',d.beta?d.beta.toFixed(2):'—'],
  ];
  html += `<div class="card card-g"><div class="ci"><div class="card-title">◆ &nbsp;FUNDAMENTALS</div>
    <div class="fund-grid">${{funds.map(([l,v])=>`<div class="fc"><span class="fl">${{l}}</span><span class="fv">${{v}}</span></div>`).join('')}}</div>
  </div></div>`;

  html += `<div class="action-row">
    <button class="btn-action" id="compare-btn" onclick="toggleCompare()">⇄ Compare</button>
    <button class="btn-action" onclick="addToWatchlist('${{d.ticker}}')">◈ Watchlist</button>
    <button class="btn-action" onclick="exportPDF()">↓ PDF</button>
  </div>`;

  html += `<div class="sec">◈ &nbsp;${{isEtf?'INDEX VALUATION MODELS':'VALUATION MODELS'}}</div>`;
  html += d.models.map(m => {{
    const vs = m.value&&m.value>0 ? fp(m.value) : 'N/A';
    return `<div class="mc"><div class="ms ${{m.stripe}}"></div><div class="mb">
      <div class="mt"><span class="mn ${{m.cls}}">◆ ${{m.name}}</span><span class="mv ${{m.sig_cls}}">${{vs}}</span></div>
      <div class="msig ${{m.sig_cls}}">${{m.sig_txt}}</div>
      <div class="mf">${{m.formula}}</div>
    </div></div>`;
  }}).join('');

  const compLabel = isEtf ? 'INDEX COMPOSITE' : 'SENECA COMPOSITE';
  const compSub   = isEtf ? 'Average of Fed Model, P/E Reversion &amp; DDM' : 'Weighted synthesis of six classical formulae';
  html += `<div class="comp"><div class="band"></div>
    <div class="comp-inner">
      <div><div class="comp-lbl">◈ &nbsp;${{compLabel}}</div><div class="comp-sub">${{compSub}}</div></div>
      <div class="comp-val">${{d.composite&&d.composite>0?fp(d.composite):'N/A'}}</div>
    </div><div class="band"></div></div>`;

  const vcol = d.verdict_cls==='up'?'var(--green)':d.verdict_cls==='down'?'var(--red)':'var(--gold2)';
  html += `<div class="verdict ${{d.verdict_cls}}">
    <div class="vt" style="color:${{vcol}}">${{d.verdict_text}}</div>
    <div class="vd">${{d.verdict_detail}}</div>
  </div>`;

  html += `<div class="ai-card">
    <div class="ai-title">◆ &nbsp;SENECA AI ANALYSIS</div>
    <div id="ai-verdict-text"><div class="ai-loading">
      <div class="ai-dot"></div><div class="ai-dot"></div><div class="ai-dot"></div>
      <span style="color:var(--dim);font-size:.7rem;font-style:italic;margin-left:5px">Oracle is thinking…</span>
    </div></div>
  </div>`;

  document.getElementById('res').innerHTML = html;
  document.getElementById('res').classList.remove('hidden');
  document.getElementById('cmp-res').classList.add('hidden');
  document.getElementById('scroll').scrollTo({{top:0,behavior:'smooth'}});
}}

function showToast(msg) {{
  const t=document.getElementById('toast'); t.textContent=msg; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),4000);
}}
function st(msg,col) {{ const e=document.getElementById('st'); e.textContent=msg; e.style.color=col||'var(--dim)'; }}
function fp(v) {{ if(!v||v<=0) return 'N/A'; return '$'+v.toLocaleString('en-US',{{minimumFractionDigits:2,maximumFractionDigits:2}}); }}
function fc(v) {{ if(!v||v<=0) return '—'; if(v>1e12) return '$'+(v/1e12).toFixed(2)+'T'; if(v>1e9) return '$'+(v/1e9).toFixed(1)+'B'; if(v>1e6) return '$'+(v/1e6).toFixed(0)+'M'; return '—'; }}
</script>
</body></html>"""

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    email = session.get("user_email","")
    sub = False
    if email:
        u = get_user(email)
        sub = u.get("subscribed",False) if u else False
    return build_html(stripe_pk=STRIPE_PUBLISHABLE_KEY, user_email=email, is_subscribed=sub)

@app.route("/success")
def success():
    email = session.get("user_email","")
    if STRIPE_SECRET_KEY and email:
        import stripe as sl; sl.api_key = STRIPE_SECRET_KEY
        sid = request.args.get("session_id","")
        try:
            s = sl.checkout.Session.retrieve(sid)
            if s.payment_status == "paid":
                update_user(email, subscribed=True); session["subscribed"]=True
        except: pass
    elif not STRIPE_SECRET_KEY:
        session["subscribed"]=True
        if email: update_user(email, subscribed=True)
    sub = bool(session.get("subscribed"))
    return build_html(stripe_pk=STRIPE_PUBLISHABLE_KEY, success_flash=True, user_email=email, is_subscribed=sub)

@app.route("/api/auth", methods=["POST"])
def api_auth():
    data = request.get_json()
    mode = data.get("mode","login")
    email = data.get("email","").strip().lower()
    pw = data.get("pw","")
    if not email or not pw: return jsonify({"error":"Email and password required"}),400
    if mode == "signup":
        ok,msg = create_user(email,pw)
        if not ok: return jsonify({"error":msg}),400
        session["user_email"]=email; session["subscribed"]=False
        return jsonify({"email":email,"subscribed":False})
    else:
        ok,result = verify_user(email,pw)
        if not ok: return jsonify({"error":result}),401
        session["user_email"]=email
        sub = result.get("subscribed",False); session["subscribed"]=sub
        return jsonify({"email":email,"subscribed":sub})

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear(); return jsonify({"ok":True})

@app.route("/api/quote")
def api_quote():
    query = request.args.get("ticker","").strip()
    if not query: return jsonify({"error":"No ticker provided"}),400
    email = session.get("user_email","")
    is_subscribed = session.get("subscribed",False)
    if email and not is_subscribed:
        u = get_user(email)
        if u: is_subscribed = u.get("subscribed",False)
    lookups = session.get("lookups",0)
    if not is_subscribed and lookups >= 1:
        return jsonify({"error":"PAYWALL"}),402
    try:
        data = fetch_quote(query)
        session["lookups"] = lookups+1
        return jsonify(data)
    except Exception as e:
        return jsonify({"error":str(e)}),500

@app.route("/api/ai-verdict")
def api_ai_verdict():
    ticker = request.args.get("ticker","").strip().upper()
    if not ticker: return jsonify({"error":"No ticker"}),400
    try:
        data = fetch_quote(ticker)
        return jsonify({"verdict": get_ai_verdict(data)})
    except Exception as e:
        return jsonify({"error":str(e)}),500

@app.route("/api/export-pdf")
def api_export_pdf():
    ticker = request.args.get("ticker","").strip().upper()
    if not ticker: return jsonify({"error":"No ticker"}),400
    if not session.get("subscribed") and session.get("lookups",0)<1:
        return jsonify({"error":"PAYWALL"}),402
    try:
        data = fetch_quote(ticker)
        ai_text = get_ai_verdict(data)
        html_content = build_pdf_html(data, ai_text)
        try:
            from weasyprint import HTML
            pdf_bytes = HTML(string=html_content).write_pdf()
            buf = io.BytesIO(pdf_bytes); buf.seek(0)
            return send_file(buf,mimetype='application/pdf',download_name=f'SENECA-{ticker}-report.pdf',as_attachment=True)
        except ImportError:
            buf = io.BytesIO(html_content.encode()); buf.seek(0)
            return send_file(buf,mimetype='text/html',download_name=f'SENECA-{ticker}-report.html',as_attachment=True)
    except Exception as e:
        return jsonify({"error":str(e)}),500

@app.route("/api/create-checkout-session", methods=["POST"])
def create_checkout():
    if not STRIPE_SECRET_KEY: return jsonify({"error":"Stripe not configured"}),500
    import stripe as sl; sl.api_key = STRIPE_SECRET_KEY
    try:
        email = session.get("user_email","")
        kwargs = dict(
            payment_method_types=["card"], mode="subscription",
            line_items=[{"price":STRIPE_PRICE_ID,"quantity":1}],
            success_url=request.host_url+"success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.host_url,
        )
        if email: kwargs["customer_email"]=email
        checkout = sl.checkout.Session.create(**kwargs)
        return jsonify({"url":checkout.url})
    except Exception as e:
        return jsonify({"error":str(e)}),500

@app.route("/webhook", methods=["POST"])
def webhook():
    if not STRIPE_SECRET_KEY: return "",200
    import stripe as sl; sl.api_key = STRIPE_SECRET_KEY
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature","")
    try:
        event = sl.Webhook.construct_event(payload,sig,STRIPE_WEBHOOK_SECRET)
        if event["type"]=="checkout.session.completed":
            s = event["data"]["object"]
            if s.get("payment_status")=="paid":
                cust_email = s.get("customer_email","")
                if cust_email: update_user(cust_email,subscribed=True)
    except Exception as e:
        return str(e),400
    return "",200

if __name__ == "__main__":
    app.run(debug=True, port=5678)
