#!/usr/bin/env python3
"""SENECA — Intrinsic Value Oracle v4
  + Persistent server-side watchlist
  + Health Score (deterministic + LLM cross-verification)
  + Empty state landing design with investor quotes
  + Stock models for stocks, ETF models for ETFs only
  + Composite at top, health score below composite
"""

import os, math, io, hashlib, json, pathlib
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, session, send_file

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "seneca-secret-2025")
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
)

STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_PRICE_ID        = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# ── Google Analytics ──────────────────────────────────────────────────────────
# Paste your GA4 Measurement ID here (looks like "G-ABCD1234"). You can also set
# it via the GA_MEASUREMENT_ID environment variable. Leave the placeholder as-is
# to disable analytics (no tracking script is injected when it's unset/placeholder).
GA_MEASUREMENT_ID = os.environ.get("GA_MEASUREMENT_ID", "G-QBNR5XWKVS")
GROQ_API_KEY           = os.environ.get("GROQ_API_KEY", "")

# ── User store (with watchlist) ───────────────────────────────────────────────
USER_FILE = pathlib.Path(os.environ.get("USER_FILE", "/tmp/seneca_users.json"))
import hmac, secrets, time as _time

def load_users():
    try: return json.loads(USER_FILE.read_text()) if USER_FILE.exists() else {}
    except: return {}

def save_users(u):
    try:
        tmp = USER_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(u))
        tmp.replace(USER_FILE)
    except: pass

def hash_pw(pw, salt=None):
    """PBKDF2-HMAC-SHA256, 200k iterations, per-user salt. Returns 'salt$hash'."""
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000)
    return f"{salt}${dk.hex()}"

def verify_pw(pw, stored):
    """Constant-time verify. Supports legacy plain-sha256 with transparent upgrade."""
    try:
        if "$" in stored:
            salt, _ = stored.split("$", 1)
            return hmac.compare_digest(hash_pw(pw, salt), stored)
        # legacy: bare sha256 hex
        legacy = hashlib.sha256(pw.encode()).hexdigest()
        return hmac.compare_digest(legacy, stored)
    except Exception:
        return False

def _valid_email(e):
    return isinstance(e, str) and 3 < len(e) <= 254 and "@" in e and "." in e.split("@")[-1]

def create_user(email, pw):
    email = (email or "").lower().strip()
    if not _valid_email(email): return False, "Enter a valid email"
    if not pw or len(pw) < 8: return False, "Password must be at least 8 characters"
    if len(pw) > 200: return False, "Password too long"
    users = load_users()
    if email in users: return False, "Email already registered"
    users[email] = {"pw": hash_pw(pw), "subscribed": False, "watchlist": []}
    save_users(users); return True, "ok"

def verify_user(email, pw):
    users = load_users(); email = (email or "").lower().strip()
    u = users.get(email)
    if not u:
        # constant-time dummy to avoid leaking which emails exist
        hash_pw(pw or "x"); return False, "Incorrect email or password"
    if not verify_pw(pw or "", u["pw"]):
        return False, "Incorrect email or password"
    # transparent upgrade of legacy hashes
    if "$" not in u["pw"]:
        u["pw"] = hash_pw(pw); users[email] = u; save_users(users)
    return True, u

# ── Brute-force throttle (per-IP, in-memory) ──────────────────────────────────
_login_attempts = {}
def throttle_check(ip):
    now = _time.time()
    rec = _login_attempts.get(ip, [])
    rec = [t for t in rec if now - t < 900]  # 15-min window
    _login_attempts[ip] = rec
    return len(rec) < 8  # max 8 attempts / 15 min
def throttle_hit(ip):
    _login_attempts.setdefault(ip, []).append(_time.time())

# ── Free-lookup tracking (per-IP, persistent across sessions/windows) ─────────
LOOKUP_FILE = pathlib.Path(os.environ.get("LOOKUP_FILE", "/tmp/seneca_lookups.json"))
def _load_lookups():
    try: return json.loads(LOOKUP_FILE.read_text()) if LOOKUP_FILE.exists() else {}
    except: return {}
def _save_lookups(d):
    try:
        tmp = LOOKUP_FILE.with_suffix(".tmp"); tmp.write_text(json.dumps(d)); tmp.replace(LOOKUP_FILE)
    except: pass
def client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    if xff: return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"
def ip_lookup_count(ip):
    return int(_load_lookups().get(ip, 0))
def ip_lookup_inc(ip):
    d = _load_lookups(); d[ip] = int(d.get(ip, 0)) + 1; _save_lookups(d)

def get_user(email):
    return load_users().get(email.lower().strip())

def set_subscribed(email):
    users = load_users(); email = email.lower().strip()
    if email in users: users[email]["subscribed"] = True; save_users(users)

def get_watchlist(email):
    u = get_user(email); return u.get("watchlist", []) if u else []

def save_watchlist(email, wl):
    users = load_users(); email = email.lower().strip()
    if email in users: users[email]["watchlist"] = wl; save_users(users)

@app.after_request
def _security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    if request.is_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp

def seed_master_account():
    """Ensure master test account always exists with full subscription."""
    users = load_users()
    users["korbeark1@aol.com"] = {
        "pw": hash_pw("Jasper1"),
        "subscribed": True,
        "watchlist": []
    }
    save_users(users)

# Seed on startup
seed_master_account()


# ── ETF detection ─────────────────────────────────────────────────────────────
ETF_SET = {"SPY","QQQ","IWM","DIA","VTI","VOO","VEA","VWO","GLD","SLV",
           "TLT","IEF","LQD","HYG","XLF","XLK","XLE","XLV","XLI","XLB",
           "ARKK","IVV","AGG","BND","VIG","SCHD","DGRO","VYM","HDV",
           "EFA","EEM","IEMG","ACWI","VT","MCHI","FXI","EWJ","SQQQ",
           "TQQQ","SPXU","SPXL","UPRO","USO","UNG","BNDX","NOBL"}

def is_fund(ticker, info):
    if ticker.upper() in ETF_SET: return True
    return (info.get("quoteType") or "").upper() in ("ETF","MUTUALFUND","INDEX")

def is_financial(sector, info):
    # Banks, insurers, capital markets — and managed-care plans, which collect
    # premiums up front just like insurers. For all of these, standard FCF is
    # distorted by "float" and must not drive intrinsic value.
    s = (sector or "").lower()
    ind = (info.get("industry") or "").lower()
    if "financial" in s: return True
    if any(k in ind for k in ("insurance","bank","capital market","asset management","financial")):
        return True
    if "healthcare plan" in ind: return True
    return False

def resolve_ticker(q):
    import yfinance as yf; q = q.strip()
    if len(q) <= 6 and q.replace("-","").replace(".","").isalpha(): return q.upper()
    try:
        res = yf.Search(q, max_results=5); quotes = res.quotes
        if quotes:
            for r in quotes:
                if r.get("quoteType","").upper() == "EQUITY": return r["symbol"]
            return quotes[0]["symbol"]
    except: pass
    return q.upper()

# ── Valuation models ──────────────────────────────────────────────────────────
# Hard ceilings that stop the classic screener blow-ups:
#  • TERMINAL_G  — perpetual growth can never approach the discount rate (Gordon
#                  asymptote). Capped at long-run GDP (~2.5%).
#  • GROWTH_CAP  — near-term annual growth fed into 10yr projections, so a 30%/yr
#                  assumption can't compound a fair value into the stratosphere.
TERMINAL_G = 0.025
GROWTH_CAP = 0.12

def gn(e,b): return math.sqrt(22.5*e*b) if e>0 and b>0 else None
def gg(e,g):
    if e<=0 or not g: return None
    g=max(min(g,20.0),-20.0)                       # cap growth% so (8.5+2g) can't explode
    return e*(8.5+2*g)*4.4/4.5
def buf(e,g):
    if e<=0 or not g: return None
    r,d=min(max(g/100,0.0),GROWTH_CAP),.09         # near-term growth capped at GROWTH_CAP
    return sum(e*(1+r)**y/(1+d)**y for y in range(1,11))+(e*(1+r)**10*15)/(1+d)**10
def lyn(e,g):
    if e<=0 or g<=0: return None
    return e*min(g,25.0)                            # PEG=1 fair value, growth% capped
def sim(p,pe,pb,roe,mom):
    if pe<=0 or pb<=0 or roe<=0: return None
    return p*(roe/pe)*(1/pb)*(1+(mom/100)*0.3)*12
def fdcf(f,g):
    if f<=0 or not g: return None
    r,d=min(max(g/100,0.0),GROWTH_CAP),.10
    tg=min(TERMINAL_G,d-0.04)                       # terminal growth always well below discount
    return sum(f*(1+r)**y/(1+d)**y for y in range(1,11))+(f*(1+r)**10*(1+tg)/(d-tg))/(1+d)**10

def capm_r(beta):
    # CAPM: Rf=4.3% (10yr Treasury), market risk premium=5.5%
    b = max(0.3, min(float(beta) if beta and beta > 0 else 1.0, 3.0))
    return 0.043 + b * 0.055

def gordon_ddm(div_ps, div_growth_pct, beta):
    # Gordon Growth Model: P = D1/(r-g); g hard-capped at 3% (≈ GDP) and below r,
    # with a denominator floor so (r-g) can never collapse toward zero.
    if div_ps <= 0: return None
    r = capm_r(beta)
    g = min(div_growth_pct / 100 if div_growth_pct else 0.02, 0.03, r - 0.01)
    if g <= 0: g = 0.02
    denom = max(r - g, 0.02)
    return (div_ps * (1 + g)) / denom

def excess_returns(bvps, roe_pct, beta, g=TERMINAL_G):
    # Residual-income / excess-returns model — the correct intrinsic-value tool for
    # banks & insurers. Justified P/B = (ROE − g)/(r − g); Fair Value = Book × P/B.
    # It rewards a financial for earning ROE above its cost of equity using BOOK
    # VALUE, sidestepping the premium-float distortion that wrecks an FCF model.
    if bvps <= 0 or roe_pct <= 0: return None
    r = capm_r(beta)
    roe = roe_pct / 100.0
    g = min(g, r - 0.01)
    denom = max(r - g, 0.02)
    jpb = (roe - g) / denom
    jpb = max(0.2, min(jpb, 6.0))          # keep the justified multiple in a sane band
    return bvps * jpb

def etf_ddm(price, div_yield_pct, beta):
    # ETF DDM: CAPM-based r, 2.5% long-run growth, denominator floored.
    if div_yield_pct <= 0: return None
    div_ps = price * (div_yield_pct / 100)
    if div_ps <= 0: return None
    r = capm_r(beta)
    g = TERMINAL_G
    denom = max(r - g, 0.03)
    return (div_ps * (1 + g)) / denom

def comp_stock(vals, fin=False):
    # For financials the "dcf" slot holds the Excess Returns value (not FCF), and the
    # weighting leans on book/ROE, dividends, and earnings — where insurers & banks
    # are genuinely valued — giving a deserving insurer a real path to the top.
    if fin:
        w={"gn":.16,"gg":.12,"buf":.16,"lyn":.10,"sim":.05,"dcf":.26,"ddm":.15}
    else:
        w={"gn":.18,"gg":.13,"buf":.22,"lyn":.13,"sim":.09,"dcf":.15,"ddm":.10}
    t=ws=0
    for k,wt in w.items():
        v=vals.get(k)
        if v and v>0: t+=v*wt; ws+=wt
    return t/ws if ws>0 else None

def _sanitize_models(d, price, max_mult=4.0):
    # Drop non-finite, non-positive, or absurd outputs (> max_mult × price). A fair
    # value implying >300% upside is far more likely a units/float/per-share data
    # error than a real opportunity, so it must not pollute the composite.
    for k, v in list(d.items()):
        if v is None: continue
        if (not math.isfinite(v)) or v <= 0 or (price > 0 and v > price * max_mult):
            d[k] = None

def signal(val, price):
    if not val or val<=0: return "na","Insufficient data"
    m=(val-price)/price*100
    if m>=30:  return "up",  f"▲ {m:.0f}% upside · DEEPLY UNDERVALUED"
    if m>=10:  return "up",  f"▲ {m:.0f}% upside · Modestly Undervalued"
    if m<=-30: return "down",f"▼ {abs(m):.0f}% · SIGNIFICANTLY OVERVALUED"
    if m<=-10: return "down",f"▼ {abs(m):.0f}% above fair · Overvalued"
    s="+" if m>=0 else ""
    return "fair",f"≈ {s}{m:.0f}% · Fairly Valued"

# ── Health Score (deterministic) ──────────────────────────────────────────────
def compute_health_score(info, is_etf=False):
    """
    Returns dict: score 0-100, grade A/B/C/D/F, flags list, breakdown dict
    Deterministic formula-based. LLM layer added separately via /api/health-ai
    """
    flags = []
    breakdown = {}

    if is_etf:
        # ETF health: expense ratio, AUM, diversification proxy
        er = float(info.get("annualReportExpenseRatio") or info.get("totalExpenseRatio") or 0)
        aum = float(info.get("totalAssets") or 0)
        score = 70  # base
        if er > 0:
            if er < 0.005:   score += 15; breakdown["Expense Ratio"] = f"{er*100:.2f}% ✦ Excellent"
            elif er < 0.01:  score += 8;  breakdown["Expense Ratio"] = f"{er*100:.2f}% · Good"
            elif er < 0.02:  score += 0;  breakdown["Expense Ratio"] = f"{er*100:.2f}% · Average"
            else:            score -= 10; flags.append("High expense ratio"); breakdown["Expense Ratio"] = f"{er*100:.2f}% ⚠ High"
        if aum > 10e9:  score += 10; breakdown["AUM"] = f"${aum/1e9:.0f}B ✦ Large"
        elif aum > 1e9: score += 5;  breakdown["AUM"] = f"${aum/1e9:.1f}B · Mid"
        elif aum > 0:   score -= 5;  flags.append("Small fund AUM"); breakdown["AUM"] = f"${aum/1e6:.0f}M ⚠ Small"
        score = max(0, min(100, score))
    else:
        def g(k, fb=0.0):
            try: v=info.get(k); f=float(v); return f if math.isfinite(f) else fb
            except: return fb

        score = 0
        weights = 0

        # 1. Profitability (25pts)
        roe = g("returnOnEquity")*100
        roa = g("returnOnAssets")*100
        margins = g("profitMargins")*100
        if roe > 15:   score += 10; breakdown["ROE"] = f"{roe:.1f}% ✦"
        elif roe > 8:  score += 6;  breakdown["ROE"] = f"{roe:.1f}% ·"
        elif roe > 0:  score += 2;  breakdown["ROE"] = f"{roe:.1f}% ·"
        else:          flags.append("Negative ROE"); breakdown["ROE"] = f"{roe:.1f}% ⚠"
        if roa > 8:    score += 8;  breakdown["ROA"] = f"{roa:.1f}% ✦"
        elif roa > 3:  score += 4;  breakdown["ROA"] = f"{roa:.1f}% ·"
        elif roa < 0:  flags.append("Negative ROA"); breakdown["ROA"] = f"{roa:.1f}% ⚠"
        else:          breakdown["ROA"] = f"{roa:.1f}% ·"
        if margins > 20: score += 7; breakdown["Net Margin"] = f"{margins:.1f}% ✦"
        elif margins > 8: score += 4; breakdown["Net Margin"] = f"{margins:.1f}% ·"
        elif margins < 0: flags.append("Negative margins"); breakdown["Net Margin"] = f"{margins:.1f}% ⚠"
        else: breakdown["Net Margin"] = f"{margins:.1f}% ·"

        # 2. Leverage / Debt (25pts)
        de = g("debtToEquity")
        cr = g("currentRatio")
        ic = g("interestCoverage") if "interestCoverage" in info else None
        if de > 0:
            if de < 30:    score += 12; breakdown["Debt/Equity"] = f"{de:.0f}% ✦ Conservative"
            elif de < 80:  score += 7;  breakdown["Debt/Equity"] = f"{de:.0f}% · Moderate"
            elif de < 150: score += 3;  breakdown["Debt/Equity"] = f"{de:.0f}% · Elevated"
            else:          flags.append("High leverage"); breakdown["Debt/Equity"] = f"{de:.0f}% ⚠ High"
        if cr > 0:
            if cr > 2:     score += 8;  breakdown["Current Ratio"] = f"{cr:.1f}× ✦"
            elif cr > 1.2: score += 5;  breakdown["Current Ratio"] = f"{cr:.1f}× ·"
            elif cr > 1:   score += 2;  breakdown["Current Ratio"] = f"{cr:.1f}× ·"
            else:          flags.append("Weak liquidity"); breakdown["Current Ratio"] = f"{cr:.1f}× ⚠"
        if ic and ic > 0:
            if ic > 5:     score += 5; breakdown["Interest Coverage"] = f"{ic:.1f}× ✦"
            elif ic > 2:   score += 2; breakdown["Interest Coverage"] = f"{ic:.1f}× ·"
            else:          flags.append("Low interest coverage"); breakdown["Interest Coverage"] = f"{ic:.1f}× ⚠"

        # 3. Cash Flow (20pts)
        fcf = g("freeCashflow")
        ocf = g("operatingCashflow")
        ni  = g("netIncomeToCommon")
        if fcf > 0:    score += 12; breakdown["Free Cash Flow"] = "Positive ✦"
        elif fcf < 0:  flags.append("Negative FCF"); breakdown["Free Cash Flow"] = "Negative ⚠"
        if ocf > 0 and ni > 0:
            accrual = (ni - ocf) / max(abs(ni),1)
            if abs(accrual) < 0.1: score += 8; breakdown["Cash Quality"] = "High ✦"
            elif abs(accrual) < 0.3: score += 4; breakdown["Cash Quality"] = "Moderate ·"
            else: flags.append("Earnings quality concern"); breakdown["Cash Quality"] = "Low ⚠"
        elif ocf > 0: score += 4; breakdown["Op. Cash Flow"] = "Positive ·"

        # 4. Growth (15pts)
        eg = g("earningsGrowth")*100
        rg = g("revenueGrowth")*100
        if eg > 15:    score += 8; breakdown["Earnings Growth"] = f"{eg:.1f}% ✦"
        elif eg > 5:   score += 5; breakdown["Earnings Growth"] = f"{eg:.1f}% ·"
        elif eg < -10: flags.append("Declining earnings"); breakdown["Earnings Growth"] = f"{eg:.1f}% ⚠"
        else: breakdown["Earnings Growth"] = f"{eg:.1f}% ·"
        if rg > 10:    score += 7; breakdown["Revenue Growth"] = f"{rg:.1f}% ✦"
        elif rg > 3:   score += 4; breakdown["Revenue Growth"] = f"{rg:.1f}% ·"
        elif rg < -5:  flags.append("Declining revenue"); breakdown["Revenue Growth"] = f"{rg:.1f}% ⚠"
        else: breakdown["Revenue Growth"] = f"{rg:.1f}% ·"

        # 5. Valuation sanity (15pts)
        pe = g("trailingPE")
        pb = g("priceToBook")
        if 0 < pe < 15:   score += 8; breakdown["P/E"] = f"{pe:.1f}× ✦ Value"
        elif 0 < pe < 30: score += 5; breakdown["P/E"] = f"{pe:.1f}× · Fair"
        elif pe > 60:     flags.append("Very high P/E"); breakdown["P/E"] = f"{pe:.1f}× ⚠ Stretched"
        elif pe > 0:      breakdown["P/E"] = f"{pe:.1f}× ·"
        if 0 < pb < 1.5:  score += 7; breakdown["P/B"] = f"{pb:.1f}× ✦ Value"
        elif 0 < pb < 4:  score += 4; breakdown["P/B"] = f"{pb:.1f}× · Fair"
        elif pb > 10:     flags.append("Very high P/B"); breakdown["P/B"] = f"{pb:.1f}× ⚠"
        elif pb > 0:      breakdown["P/B"] = f"{pb:.1f}× ·"

        score = max(0, min(100, score))

    # Grade
    if score >= 80: grade = "A"
    elif score >= 65: grade = "B"
    elif score >= 50: grade = "C"
    elif score >= 35: grade = "D"
    else: grade = "F"

    return {"score": score, "grade": grade, "flags": flags, "breakdown": breakdown}

# ── fetch_quote ───────────────────────────────────────────────────────────────
def fetch_quote(query):
    import yfinance as yf
    ticker = resolve_ticker(query)
    t = yf.Ticker(ticker); fi = t.fast_info
    price=float(fi.last_price or 0); prev=float(fi.previous_close or 0)
    lo52=float(fi.year_low or 0); hi52=float(fi.year_high or 0)
    cap=float(fi.market_cap or 0); shares=float(fi.shares or 0)
    if not price:
        # Friendly error messages based on what went wrong
        if len(ticker) > 6:
            raise ValueError(f"Could not find a ticker for '{query}'. Try using the ticker symbol directly (e.g. AAPL, MSFT).")
        raise ValueError(f"No market data found for '{ticker}'. It may be delisted, a private company, or an invalid symbol.")
    info=t.info
    def g(k,fb=0.0):
        try: v=info.get(k); f=float(v); return f if math.isfinite(f) else fb
        except: return fb
    name  = info.get("longName") or info.get("shortName") or ticker
    sector= info.get("sector") or info.get("industry") or info.get("categoryName") or "—"
    eps=g("trailingEps"); bvps=g("bookValue"); pe=g("trailingPE"); pb=g("priceToBook")
    roe=g("returnOnEquity")*100; beta=g("beta")
    # Safe dividend yield: yfinance returns decimal (0.015 = 1.5%), handle None
    _raw_dy = info.get("dividendYield") or info.get("yield") or 0
    try: div_y = float(_raw_dy) * 100 if _raw_dy and math.isfinite(float(_raw_dy)) else 0.0
    except: div_y = 0.0
    # Sanity check: div yield > 25% is almost certainly bad data
    if div_y > 25: div_y = 0.0
    # Dividend per share for DDM
    _div_ps = g("dividendRate") or g("lastDividendValue") or 0
    if _div_ps <= 0 and div_y > 0: _div_ps = price * (div_y / 100)
    # Dividend growth rate: use 5yr avg or earnings growth as proxy
    _div_growth = (g("fiveYearAvgDividendYield") or g("earningsGrowth") or 0) * 100
    if _div_growth < 0: _div_growth = 0
    # Robust shares-outstanding: fast_info.shares is frequently missing, which would
    # turn aggregate FCF into a fake per-share number (the 1,000%-margin bug).
    if not shares or shares < 1000:
        shares = g("sharesOutstanding") or (cap/price if price>0 else 0)
    fcf_ps = g("freeCashflow")/shares if shares else 0
    # Reject implausible FCF/share (>50% FCF yield ⇒ bad data, aggregate value, or float).
    if price>0 and abs(fcf_ps) > price*0.5: fcf_ps = 0
    growth=(g("earningsGrowth") or g("revenueGrowth") or g("earningsQuarterlyGrowth") or 0)*100
    chg=(price-prev)/prev*100 if prev else 0
    mom=(price-lo52)/lo52*100 if lo52 else 0
    ey=(1/pe*100) if pe>0 else 0
    fund = is_fund(ticker, info)
    fin = is_financial(sector, info)
    health = compute_health_score(info, is_etf=fund)

    if fund:
        iv={}
        if ey>0: iv["fed"]=price*(ey/4.3)
        if pe>0: iv["per"]=price*(17.0/pe)
        _etf_ddm_val = etf_ddm(price, div_y, beta)
        if _etf_ddm_val: iv["ddm"] = _etf_ddm_val
        _sanitize_models(iv, price)
        models=[]
        for k,nm,fm,sc,cl in [
            ("fed","FED MODEL",         "Price × (Earnings Yield ÷ Treasury 4.3%)",          "turq","turq"),
            ("per","P/E MEAN REVERSION","Price × (Hist. 17× P/E ÷ Current P/E)",             "gold","gold"),
            ("ddm","GORDON GROWTH DDM", "D1 ÷ (CAPM rate − 2.5% growth)",                   "muted","muted"),
        ]:
            v=iv.get(k); sc2,st2=signal(v,price) if v else ("na","Insufficient data")
            models.append({"name":nm,"formula":fm,"stripe":sc,"cls":cl,"value":v,"sig_cls":sc2,"sig_txt":st2})
        vals=[m["value"] for m in models if m["value"] and m["value"]>0]
        comp=sum(vals)/len(vals) if vals else None
        atype="etf"
    else:
        _ddm_val = gordon_ddm(_div_ps, _div_growth, beta)
        _exr_val = excess_returns(bvps, roe, beta) if fin else None
        vd={"gn":gn(eps,bvps),"gg":gg(eps,growth),"buf":buf(eps,growth),
            "lyn":lyn(eps,growth),"sim":sim(price,pe or 1,pb or 1,roe,mom),
            "dcf":(_exr_val if fin else fdcf(fcf_ps,growth)),  # financials → Excess Returns (no float)
            "ddm":_ddm_val}
        _sanitize_models(vd, price)                            # strip data-error outliers
        comp=comp_stock(vd, fin=fin); models=[]
        for k,nm,fm,sc,cl in [
            ("gn", "GRAHAM NUMBER",      "√( 22.5 × EPS × Book Value )",                 "gold","gold"),
            ("gg", "GRAHAM GROWTH",      "EPS × (8.5+2g) × 4.4/AAA yield",              "gold","gold"),
            ("buf","BUFFETT DCF",        "10yr EPS @ 9% · 15× terminal",                 "turq","turq"),
            ("lyn","PETER LYNCH PEG",    "EPS × growth% (PEG=1)",                        "turq","turq"),
            ("sim","SIMONS QUANT",       "ROE/PE × (1/PB) × momentum",                   "muted","muted"),
            ("dcf","FREE CASH FLOW DCF", "10yr FCF @ 10% · 2.5% terminal",               "muted","muted"),
            ("ddm","GORDON GROWTH DDM",  "D1 ÷ (CAPM rate − div growth%) · dividend-payers only","gold","gold"),
        ]:
            if k=="dcf" and fin:
                nm,fm,sc,cl = "EXCESS RETURNS","Book × (ROE − g) ÷ (r − g) · financials","gold","gold"
            v=vd.get(k); sc2,st2=signal(v,price) if v else ("na","Insufficient data")
            models.append({"name":nm,"formula":fm,"stripe":sc,"cls":cl,"value":v,"sig_cls":sc2,"sig_txt":st2})
        atype="stock"

    vt=vd2=vc=""
    if comp and comp>0:
        m=(comp-price)/price*100
        if   m>=30:  vt,vd2,vc=f"✦ STRONG BUY · {m:.0f}% margin of safety","Deep value. Substantial gap between price and intrinsic worth.","up"
        elif m>=10:  vt,vd2,vc=f"✦ UNDERVALUED · {m:.0f}% upside","Price trades below the model consensus.","up"
        elif m<=-30: vt,vd2,vc=f"✦ AVOID · {abs(m):.0f}% above fair","Significant optimism beyond what fundamentals support.","down"
        elif m<=-10: vt,vd2,vc=f"✦ OVERVALUED · {abs(m):.0f}% premium","Price exceeds what the models suggest.","down"
        else:
            s="+" if m>=0 else ""
            vt,vd2,vc=f"✦ FAIRLY VALUED · {s}{m:.0f}% vs composite","Price is broadly in line with the consensus.","fair"
    else:
        vt,vd2,vc="Insufficient data for composite verdict","","fair"

    return {"ticker":ticker,"name":name,"sector":sector,"asset_type":atype,
            "price":price,"prev":prev,"eps":eps,"bvps":bvps,"pe":pe,"pb":pb,
            "roe":roe,"growth":growth,"fcf":fcf_ps,"lo52":lo52,"hi52":hi52,
            "mom":mom,"chg":chg,"cap":cap,"div_y":div_y,"beta":beta,
            "composite":comp,"models":models,"verdict_text":vt,
            "verdict_detail":vd2,"verdict_cls":vc,"earnings_yield":ey,
            "health":health}

# ── AI ────────────────────────────────────────────────────────────────────────
def get_ai_verdict(data):
    if not GROQ_API_KEY: return None
    try:
        from groq import Groq
        c = Groq(api_key=GROQ_API_KEY)
        comp_str = f"${data['composite']:.2f}" if data['composite'] else 'N/A'
        p = (f"You are SENECA, a stoic value investing oracle. 3 sentences explaining why "
             f"{data['name']} ({data['ticker']}) appears {data['verdict_cls']}:\n"
             f"Price ${data['price']:.2f} | Fair Value {comp_str} | P/E {data['pe']:.1f} | "
             f"Verdict: {data['verdict_text']}\nDirect, wise, no disclaimers, no bullets.")
        msg = c.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=200,
            messages=[{"role":"user","content":p}]
        )
        return msg.choices[0].message.content.strip()
    except: return None

def get_health_ai(data):
    """LLM layer: cross-verify health flags, probe for hidden risks"""
    if not GROQ_API_KEY: return None
    try:
        from groq import Groq
        c = Groq(api_key=GROQ_API_KEY)
        h=data.get("health",{})
        flags=h.get("flags",[])
        score=h.get("score",0)
        grade=h.get("grade","?")
        breakdown=h.get("breakdown",{})
        bd_str="; ".join(f"{k}: {v}" for k,v in list(breakdown.items())[:8])
        p = (f"You are SENECA's financial forensics engine. Analyze {data['name']} ({data['ticker']}) "
             f"for hidden financial risks, accounting irregularities, and off-balance-sheet concerns.\n\n"
             f"Quantitative health score: {score}/100 (Grade {grade})\n"
             f"Key metrics: {bd_str}\n"
             f"Flagged concerns: {', '.join(flags) if flags else 'None detected'}\n"
             f"Sector: {data['sector']} | P/E: {data['pe']:.1f} | D/E: implied from score\n\n"
             f"In exactly 3 sentences: (1) Confirm or challenge the health score with your assessment. "
             f"(2) Identify the single biggest hidden risk an investor might miss. "
             f"(3) Give a plain verdict on financial integrity. "
             f"Be direct. No disclaimers. No bullets.")
        msg = c.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=250,
            messages=[{"role":"user","content":p}]
        )
        return msg.choices[0].message.content.strip()
    except: return None

# ── Members' Leaderboard ──────────────────────────────────────────────────────
import threading

LEADERBOARD_REFRESH = 1800     # seconds between full re-scans (1800 = 30 min)
SCAN_WORKERS        = 8         # parallel fetch threads (modest, avoids rate limits)
LEADERBOARD_SIZE    = 15        # top-N shown

# Curated universe of major NYSE-listed stocks. Scanning the entire ~2,400-name
# NYSE live with this free, no-API-key data source isn't practical — it would
# take hours and get rate limited — so we scan this liquid large-cap set.
# Add or remove tickers freely.
NYSE_UNIVERSE = [
    "JPM","BAC","WFC","C","GS","MS","AXP","BLK","SPGI","V","MA","SCHW","COF","USB","PNC",
    "BRK-B","JNJ","LLY","ABBV","MRK","PFE","ABT","TMO","DHR","BMY","UNH","CVS","MDT","ELV",
    "WMT","PG","KO","PEP","MCD","HD","LOW","TGT","NKE","SBUX","CL","KMB","MDLZ","PM","MO",
    "XOM","CVX","COP","SLB","OXY","EOG","PSX","MPC","VLO","KMI","WMB",
    "CAT","DE","GE","HON","MMM","BA","LMT","RTX","UPS","FDX","UNP","EMR","ETN",
    "DIS","T","VZ","CMCSA","CRM","ORCL","IBM","ACN","NOW","TXN",
    "NEE","SO","DUK","D","AEP","EXC",
    "F","GM","KR","DG","DLTR","ROST","TJX",
]

# Built from sector sublists so the scan always covers the categories you want.
# (NYSE-listed only — NASDAQ names like AAPL/MSFT/NVDA/COST are intentionally excluded.)
_LB_BANKS = [   # 20 banks
    "JPM","BAC","WFC","C","GS","MS","USB","PNC","TFC","BK",
    "STT","COF","CFG","KEY","RF","MTB","ALLY","DFS","CMA","FHN",
]
_LB_INSURANCE = [  # 20 insurers (incl. managed-care health insurers)
    "UNH","ELV","CI","HUM","CNC","CB","TRV","AIG","MET","PRU",
    "AFL","ALL","PGR","MMC","AON","AJG","HIG","MKL","WRB","L",
]
_LB_RETAIL = [  # 12 retailers
    "WMT","HD","LOW","TGT","TJX","DG","KR","BBY","DKS","AZO","WSM","BURL",
]
_LB_TECH = [    # 20 tech / software / IT services
    "ORCL","CRM","IBM","ACN","NOW","UBER","SHOP","SNOW","NET","TWLO",
    "DELL","HPQ","HPE","MSI","GLW","FICO","TYL","EPAM","GPN","CIEN",
]
_LB_OTHER = [   # 48 — payments, energy, industrials, healthcare, staples, utilities, etc.
    "V","MA","AXP","BLK","SPGI","SCHW",                              # payments / asset mgmt
    "XOM","CVX","COP","SLB","OXY","EOG","PSX","KMI","WMB",           # energy
    "CAT","DE","GE","MMM","BA","LMT","RTX","UPS",                    # industrials
    "JNJ","LLY","ABBV","MRK","PFE","ABT","TMO",                      # healthcare / pharma
    "PG","KO","MCD","CL","KMB","PM","MO",                            # consumer staples
    "NEE","SO","DUK","D","AEP",                                      # utilities
    "DIS","NKE","F","GM",                                            # media / discretionary / autos
    "T","VZ",                                                        # telecom
]

NYSE_UNIVERSE = _LB_BANKS + _LB_INSURANCE + _LB_RETAIL + _LB_TECH + _LB_OTHER
NYSE_UNIVERSE = list(dict.fromkeys(NYSE_UNIVERSE))   # ~120 unique tickers

_LB_LOCK = threading.Lock()
LEADERBOARD = {
    "status":  "building",         # "building" | "ready" | "error"
    "updated": None,               # epoch seconds of last completed scan
    "scanned": 0,
    "total":   len(NYSE_UNIVERSE),
    "rows":    [],
}
_lb_thread_started = False

def _lb_scan_one(tk):
    """Fetch one ticker, return slim leaderboard row or None."""
    try:
        d = fetch_quote(tk)
        comp, price = d.get("composite"), d.get("price")
        if comp and comp > 0 and price and price > 0:
            return {
                "ticker": d["ticker"], "name": d["name"], "sector": d["sector"],
                "price": price, "composite": comp,
                "margin": (comp - price) / price * 100,
            }
    except Exception:
        return None
    return None

def _lb_build_loop():
    """Background thread: rebuild the leaderboard on a timer, forever."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    while True:
        with _LB_LOCK:
            LEADERBOARD["status"]  = "building"
            LEADERBOARD["scanned"] = 0
            LEADERBOARD["total"]   = len(NYSE_UNIVERSE)
        rows, done = [], 0
        try:
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
                futs = {ex.submit(_lb_scan_one, tk): tk for tk in NYSE_UNIVERSE}
                for f in as_completed(futs):
                    done += 1
                    with _LB_LOCK:
                        LEADERBOARD["scanned"] = done
                    r = f.result()
                    if r:
                        rows.append(r)
            rows.sort(key=lambda x: x["margin"], reverse=True)
            with _LB_LOCK:
                LEADERBOARD["rows"]    = rows[:LEADERBOARD_SIZE]
                LEADERBOARD["updated"] = _time.time()
                LEADERBOARD["status"]  = "ready"
        except Exception as e:
            with _LB_LOCK:
                LEADERBOARD["status"] = "error"
            print("[leaderboard] scan failed:", e)
        _time.sleep(LEADERBOARD_REFRESH)

def ensure_leaderboard_thread():
    """Start the background scanner exactly once per process (lazy)."""
    global _lb_thread_started
    with _LB_LOCK:
        if _lb_thread_started:
            return
        _lb_thread_started = True
    threading.Thread(target=_lb_build_loop, daemon=True).start()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/leaderboard")
def api_leaderboard():
    ensure_leaderboard_thread()
    email = session.get("email", "")
    sub = session.get("sub", False)
    if email and not sub:
        u = get_user(email)
        if u: sub = u.get("subscribed", False)
    if not sub:
        return jsonify({"error": "PAYWALL"}), 402
    with _LB_LOCK:
        out = {
            "status":  LEADERBOARD["status"],
            "updated": LEADERBOARD["updated"],
            "scanned": LEADERBOARD["scanned"],
            "total":   LEADERBOARD["total"],
            "rows":    LEADERBOARD["rows"],
        }
    return jsonify(out)

@app.route("/")
def index():
    email = session.get("email","")
    sub = False
    if email:
        u = get_user(email)
        sub = u.get("subscribed", False) if u else False
    return render_app(sub=sub, email=email)

@app.route("/success")
def success():
    email = session.get("email","")
    if STRIPE_SECRET_KEY:
        import stripe as sl; sl.api_key = STRIPE_SECRET_KEY
        sid = request.args.get("session_id","")
        try:
            s = sl.checkout.Session.retrieve(sid)
            if s.payment_status == "paid":
                if email: set_subscribed(email)
                session["sub"] = True
        except: pass
    else:
        session["sub"] = True
        if email: set_subscribed(email)
    return render_app(sub=True, email=email, toast="✦ Subscription active! Unlimited access unlocked.", purchase=True, txn=request.args.get("session_id",""))

@app.route("/api/signup", methods=["POST"])
def api_signup():
    d = request.get_json(silent=True) or {}
    email = (d.get("email","")).strip().lower(); pw = d.get("pw","")
    if not email or not pw: return jsonify({"ok":False,"error":"Email and password required"}), 400
    ok, msg = create_user(email, pw)
    if not ok: return jsonify({"ok":False,"error":msg}), 400
    session.permanent = bool(d.get("remember", True))
    session["email"] = email; session["sub"] = False
    return jsonify({"ok":True,"email":email,"sub":False,"watchlist":[]})

@app.route("/api/login", methods=["POST"])
def api_login():
    ip = client_ip()
    if not throttle_check(ip):
        return jsonify({"ok":False,"error":"Too many attempts. Try again in 15 minutes."}), 429
    d = request.get_json(silent=True) or {}
    email = (d.get("email","")).strip().lower(); pw = d.get("pw","")
    if not email or not pw: return jsonify({"ok":False,"error":"Email and password required"}), 400
    ok, result = verify_user(email, pw)
    if not ok:
        throttle_hit(ip)
        return jsonify({"ok":False,"error":result}), 401
    session.permanent = bool(d.get("remember", True))
    session["email"] = email; session["sub"] = result.get("subscribed", False)
    wl = result.get("watchlist", [])
    return jsonify({"ok":True,"email":email,"sub":result.get("subscribed",False),"watchlist":wl})

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear(); return jsonify({"ok":True})

@app.route("/api/watchlist", methods=["GET","POST"])
def api_watchlist():
    email = session.get("email","")
    if not email: return jsonify({"ok":False,"error":"Not logged in"}), 401
    if request.method == "GET":
        return jsonify({"ok":True,"watchlist":get_watchlist(email)})
    d = request.get_json(silent=True) or {}
    wl = d.get("watchlist", [])
    if not isinstance(wl, list): wl = []
    import re as _re
    clean = []
    for t in wl:
        s = str(t).upper().strip()[:10]
        if s and _re.match(r'^[A-Z0-9.\-]{1,10}$', s): clean.append(s)
    wl = clean[:25]
    save_watchlist(email, wl)
    return jsonify({"ok":True,"watchlist":wl})

@app.route("/api/price")
def api_price():
    """Fast batch price endpoint — fetches all tickers in parallel threads."""
    import yfinance as yf
    from concurrent.futures import ThreadPoolExecutor
    raw_tickers = request.args.get("tickers", "").strip()
    if not raw_tickers: return jsonify({"error": "No tickers"}), 400
    tickers = [t.strip().upper() for t in raw_tickers.split(",") if t.strip()][:10]

    # Try yf.download for all at once — single HTTP call, fastest option
    try:
        df = yf.download(tickers, period="2d", interval="1d",
                         auto_adjust=True, progress=False, threads=True)
        results = {}
        # Handle both single-ticker (flat) and multi-ticker (MultiIndex columns)
        if hasattr(df.columns, "levels"):
            close = df["Close"]
        elif "Close" in df.columns:
            close = df[["Close"]].rename(columns={"Close": tickers[0]}) if len(tickers)==1 else df["Close"]
        else:
            close = None
        if close is not None:
            for t in tickers:
                try:
                    col = close[t] if t in close.columns else close.iloc[:,0]
                    vals = col.dropna()
                    if len(vals) >= 2:
                        price, prev = float(vals.iloc[-1]), float(vals.iloc[-2])
                        if price:
                            results[t] = {"price": price, "chg": round((price-prev)/prev*100 if prev else 0, 2)}
                    elif len(vals) == 1:
                        price = float(vals.iloc[-1])
                        if price: results[t] = {"price": price, "chg": 0.0}
                except Exception:
                    pass
        if len(results) == len(tickers):
            return jsonify(results)
    except Exception:
        pass

    # Fallback: parallel fast_info via thread pool (~4× faster than serial)
    def fetch_one(t):
        try:
            fi = yf.Ticker(t).fast_info
            price = float(fi.last_price or 0)
            prev  = float(fi.previous_close or 0)
            if price:
                return t, {"price": price, "chg": round((price-prev)/prev*100 if prev else 0, 2)}
        except Exception:
            pass
        return t, None

    results = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for t, data in ex.map(fetch_one, tickers):
            if data: results[t] = data
    return jsonify(results)


@app.route("/api/quote")
def api_quote():
    q = request.args.get("q","").strip()
    if not q or len(q) > 12: return jsonify({"error":"Invalid ticker"}), 400
    email = session.get("email","")
    sub = session.get("sub", False)
    if email and not sub:
        u = get_user(email)
        if u: sub = u.get("subscribed", False)
    ip = client_ip()
    # Persistent per-IP free lookup (survives new windows / cleared cookies)
    if not sub and ip_lookup_count(ip) >= 1:
        return jsonify({"error":"PAYWALL"}), 402
    try:
        data = fetch_quote(q)
        if not sub:
            ip_lookup_inc(ip)
            session["lookups"] = session.get("lookups", 0) + 1
        return jsonify(data)
    except Exception as e: return jsonify({"error":str(e)}), 500

@app.route("/api/ai")
def api_ai():
    q = request.args.get("q","").strip()
    if not q: return jsonify({"verdict":None})
    try: return jsonify({"verdict": get_ai_verdict(fetch_quote(q))})
    except: return jsonify({"verdict":None})

@app.route("/api/health-ai")
def api_health_ai():
    q = request.args.get("q","").strip()
    if not q: return jsonify({"analysis":None})
    try: return jsonify({"analysis": get_health_ai(fetch_quote(q))})
    except: return jsonify({"analysis":None})

# ── Leadership / Governance ───────────────────────────────────────────────────
def _classify_tier(title):
    t = (title or "").lower()
    if "chief executive" in t or t.strip() in ("ceo",) or "ceo" in t.split():
        return 1, "CEO"
    if "chair" in t and ("board" in t or t.strip() in ("chairman","chairwoman","chairperson","chair")):
        return 0, "CHAIR"
    if "president" in t and "vice" not in t:
        return 1, "PRESIDENT"
    if "chief financial" in t or "cfo" in t.split():
        return 2, "CFO"
    if "chief operating" in t or "coo" in t.split():
        return 2, "COO"
    if "chief technology" in t or "cto" in t.split():
        return 2, "CTO"
    if "chief" in t and "officer" in t:
        return 2, "C-SUITE"
    if "vice president" in t or "evp" in t or "svp" in t or t.startswith("vp"):
        return 3, "VP"
    if "director" in t:
        return 4, "DIRECTOR"
    return 3, "EXEC"

def _initials(name):
    parts = [p for p in (name or "").replace(".","").split() if p and p[0].isalpha()]
    if not parts: return "—"
    if len(parts) == 1: return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()

def get_leadership(query):
    import yfinance as yf
    tk = resolve_ticker(query)
    t = yf.Ticker(tk)
    info = t.info or {}
    officers = info.get("companyOfficers", []) or []
    people = []
    for o in officers:
        name = o.get("name") or ""
        title = o.get("title") or ""
        if not name: continue
        rank, badge = _classify_tier(title)
        age = o.get("age")
        yb = o.get("yearBorn")
        if not age and yb:
            try: age = datetime.now().year - int(yb)
            except: age = None
        pay = o.get("totalPay") or 0
        people.append({
            "name": name, "title": title, "badge": badge, "rank": rank,
            "age": age, "pay": float(pay) if pay else 0,
            "initials": _initials(name),
        })
    people.sort(key=lambda p: (p["rank"], -p["pay"]))
    return {
        "ticker": tk,
        "name": info.get("longName") or info.get("shortName") or tk,
        "sector": info.get("sector") or "—",
        "industry": info.get("industry") or "—",
        "employees": int(info.get("fullTimeEmployees") or 0),
        "country": info.get("country") or "—",
        "city": info.get("city") or "",
        "state": info.get("state") or "",
        "website": info.get("website") or "",
        "summary": (info.get("longBusinessSummary") or "")[:600],
        "people": people,
        "officer_count": len(people),
    }

def get_governance_ai(lead):
    if not GROQ_API_KEY: return None
    try:
        from groq import Groq
        c = Groq(api_key=GROQ_API_KEY)
        names = "; ".join(f"{p['name']} ({p['title']})" for p in lead["people"][:8])
        p = (f"You are SENECA's corporate governance analyst. Assess the leadership of "
             f"{lead['name']} ({lead['ticker']}).\n"
             f"Sector: {lead['sector']} | Employees: {lead['employees']:,}\n"
             f"Key leadership: {names}\n\n"
             f"In exactly 3 sentences: (1) Characterize the leadership structure and its depth. "
             f"(2) Note any governance strength or red flag (e.g. CEO/Chair duality, thin bench, key-person risk). "
             f"(3) Give a plain verdict on management quality and stability. "
             f"Be direct. No disclaimers. No bullets.")
        msg = c.chat.completions.create(model="llama-3.3-70b-versatile", max_tokens=240,
            messages=[{"role":"user","content":p}])
        return msg.choices[0].message.content.strip()
    except: return None

@app.route("/api/leadership")
def api_leadership():
    q = request.args.get("q","").strip()
    if not q or len(q) > 12: return jsonify({"error":"Invalid ticker"}), 400
    email = session.get("email",""); sub = session.get("sub", False)
    if email and not sub:
        u = get_user(email)
        if u: sub = u.get("subscribed", False)
    if not sub and ip_lookup_count(client_ip()) >= 1:
        return jsonify({"error":"PAYWALL"}), 402
    try:
        return jsonify(get_leadership(q))
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/governance-ai")
def api_governance_ai():
    q = request.args.get("q","").strip()
    if not q: return jsonify({"analysis":None})
    try: return jsonify({"analysis": get_governance_ai(get_leadership(q))})
    except: return jsonify({"analysis":None})


@app.route("/api/checkout", methods=["POST"])
def api_checkout():
    if not STRIPE_SECRET_KEY: return jsonify({"url":None}), 200
    import stripe as sl; sl.api_key = STRIPE_SECRET_KEY
    try:
        email = session.get("email","")
        kw = dict(payment_method_types=["card"], mode="subscription",
                  line_items=[{"price":STRIPE_PRICE_ID,"quantity":1}],
                  success_url=request.host_url+"success?session_id={CHECKOUT_SESSION_ID}",
                  cancel_url=request.host_url)
        if email: kw["customer_email"] = email
        s = sl.checkout.Session.create(**kw)
        return jsonify({"url":s.url})
    except Exception as e: return jsonify({"error":str(e)}), 500

@app.route("/webhook", methods=["POST"])
def webhook():
    if not STRIPE_SECRET_KEY: return "",200
    import stripe as sl; sl.api_key = STRIPE_SECRET_KEY
    payload=request.get_data(); sig=request.headers.get("Stripe-Signature","")
    try:
        ev=sl.Webhook.construct_event(payload,sig,STRIPE_WEBHOOK_SECRET)
        if ev["type"]=="checkout.session.completed":
            s=ev["data"]["object"]
            if s.get("payment_status")=="paid":
                em=s.get("customer_email","")
                if em: set_subscribed(em)
    except: pass
    return "",200

@app.route("/api/pdf")
def api_pdf():
    q = request.args.get("q","").strip()
    if not q: return jsonify({"error":"No ticker"}), 400
    sub = session.get("sub", False)
    lookups = session.get("lookups", 0)
    if not sub and lookups < 1: return jsonify({"error":"PAYWALL"}), 402
    try:
        data = fetch_quote(q); ai = get_ai_verdict(data)
        def fp(v): return "N/A" if not v or v<=0 else f"${v:,.2f}"
        rows = "".join(f"<tr><td>{m['name']}</td><td>{m['formula']}</td><td style='text-align:right;font-weight:bold'>{fp(m['value'])}</td><td style='text-align:right'>{m['sig_txt']}</td></tr>" for m in data["models"])
        vc = "#3a8a24" if data["verdict_cls"]=="up" else "#a03020" if data["verdict_cls"]=="down" else "#c88a1a"
        ai_html = (f"<div style='background:#f0f8f5;border:1px solid #1e7a6a;border-radius:8px;padding:14px;margin:12px 0'>"
                   f"<div style='font-size:9px;letter-spacing:3px;color:#1e7a6a;text-transform:uppercase;margin-bottom:8px;font-weight:600'>SENECA AI ANALYSIS</div>"
                   f"<p style='font-size:11px;line-height:1.7;font-style:italic;margin:0'>{ai}</p></div>") if ai else ""
        chg_arrow = "▲" if data["chg"] >= 0 else "▼"
        h = data.get("health",{})
        health_html = (f"<div style='background:#fdf5e0;border:1px solid #c88a1a;border-radius:8px;padding:14px;margin:12px 0'>"
                       f"<div style='font-size:9px;letter-spacing:3px;color:#c88a1a;text-transform:uppercase;margin-bottom:8px;font-weight:600'>HEALTH SCORE</div>"
                       f"<div style='font-size:28px;font-weight:300;color:#c88a1a'>{h.get('score',0)}/100 &nbsp;<span style='font-size:14px'>Grade {h.get('grade','?')}</span></div>"
                       f"<div style='font-size:10px;color:#666;margin-top:6px'>{'; '.join(h.get('flags',[])) or 'No major flags detected'}</div></div>") if h else ""
        html = (f'<!DOCTYPE html><html><head><meta charset="UTF-8"/><style>'
                f'body{{font-family:Georgia,serif;background:#fff;color:#1a1a1a;margin:0;padding:32px;font-size:12px}}'
                f'.header{{border-bottom:3px solid #c88a1a;padding-bottom:16px;margin-bottom:24px;display:flex;justify-content:space-between}}'
                f'table{{width:100%;border-collapse:collapse;margin-bottom:16px}}'
                f'th{{background:#f5ede0;font-size:9px;padding:6px 8px;text-align:left;color:#888;text-transform:uppercase}}'
                f'td{{padding:7px 8px;border-bottom:1px solid #f0e8d8;font-size:11px}}'
                f'tr:nth-child(even) td{{background:#fdf8f2}}'
                f'.footer{{border-top:1px solid #e0d0b0;margin-top:32px;padding-top:10px;font-size:9px;color:#aaa;font-style:italic;text-align:center}}'
                f'</style></head><body>'
                f'<div class="header"><div><div style="font-size:28px;font-weight:300;letter-spacing:6px;color:#c88a1a">SENECA</div>'
                f'<div style="font-size:10px;color:#888;font-style:italic">Intrinsic Value Oracle</div></div>'
                f'<div style="font-size:10px;color:#888;text-align:right">Generated {datetime.now().strftime("%B %d, %Y")}<br/>Educational purposes only</div></div>'
                f'<div style="font-size:22px;font-weight:600">{data["name"]} ({data["ticker"]})</div>'
                f'<div style="font-size:36px;font-weight:300;color:#c88a1a;margin:12px 0">'
                f'${data["price"]:.2f} <span style="font-size:14px;color:#888">{chg_arrow} {abs(data["chg"]):.2f}%</span></div>'
                f'{health_html}'
                f'<table><tr><th>Model</th><th>Formula</th><th style="text-align:right">Fair Value</th><th style="text-align:right">Signal</th></tr>{rows}</table>'
                f'<div style="background:#fdf5e0;border:2px solid #c88a1a;border-radius:10px;padding:16px;display:flex;justify-content:space-between;align-items:center;margin:16px 0">'
                f'<div style="font-size:9px;letter-spacing:3px;color:#888;text-transform:uppercase">Seneca Composite</div>'
                f'<div style="font-size:28px;font-weight:300;color:#c88a1a">{fp(data["composite"])}</div></div>'
                f'<div style="border-left:4px solid {vc};padding:12px 16px;background:#fafafa;border-radius:0 8px 8px 0;margin:12px 0">'
                f'<div style="font-size:14px;font-weight:600;color:{vc};margin-bottom:4px">{data["verdict_text"]}</div>'
                f'<div style="font-size:11px;color:#666;font-style:italic">{data["verdict_detail"]}</div></div>'
                f'{ai_html}'
                f'<div class="footer">SENECA is for educational and research purposes only. Not financial advice.</div>'
                f'</body></html>')
        try:
            from weasyprint import HTML
            buf = io.BytesIO(HTML(string=html).write_pdf()); buf.seek(0)
            return send_file(buf, mimetype='application/pdf', download_name=f'SENECA-{data["ticker"]}-report.pdf', as_attachment=True)
        except ImportError:
            buf = io.BytesIO(html.encode()); buf.seek(0)
            return send_file(buf, mimetype='text/html', download_name=f'SENECA-{data["ticker"]}-report.html', as_attachment=True)
    except Exception as e: return jsonify({"error":str(e)}), 500

# ── HTML ──────────────────────────────────────────────────────────────────────
def render_app(sub=False, email="", toast="", purchase=False, txn=""):
    stripe_pk = STRIPE_PUBLISHABLE_KEY
    safe_toast = toast.replace('"', '&quot;')
    toast_js = f'toast("{safe_toast}");' if toast else ""
    _txn = (txn or "").replace("'", "").replace('"', "")
    purchase_js = (
        "track('purchase',{value:3.99,currency:'USD',transaction_id:'" + _txn + "',"
        "items:[{item_id:'seneca_pro',item_name:'Seneca Pro',price:3.99}]});"
    ) if purchase else ""
    _ga = (GA_MEASUREMENT_ID or "").strip()
    ga = ""
    if _ga and "XXXX" not in _ga.upper():   # inject unless it's the unset placeholder
        ga = (
            '<script async src="https://www.googletagmanager.com/gtag/js?id=' + _ga + '"></script>'
            '<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}'
            'gtag("js",new Date());gtag("config","' + _ga + '");</script>'
        )
    return f"""<!DOCTYPE html>
<html lang="en"><head>{ga}
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,maximum-scale=1"/>
<title>SENECA ◆ Intrinsic Value Oracle</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=JetBrains+Mono:wght@300;400;500;700&family=Chakra+Petch:wght@300;400;500;600&display=swap" rel="stylesheet"/>
{"<script src='https://js.stripe.com/v3/'></script>" if stripe_pk else ""}
<style>
*{{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}}
:root{{
  --void:#000;--obsidian:#060606;--carbon:#0a0a0a;--graphite:#0f0f0f;--slate:#141414;--slate2:#1a1a1a;
  --line:#181818;--line2:#222;--line3:#2c2c2c;
  --gold:#c8901a;--gold2:#e8aa34;--gold3:#f0b840;--gold-dim:#6a4d12;
  --jade:#2ea043;--jade2:#3fbf55;--blood:#c4302b;--blood2:#e0463f;
  --w1:#fff;--w2:#9a9a9a;--w3:#525252;--w4:#2e2e2e;--dim:#525252;
  --mono:'JetBrains Mono','SF Mono',monospace;
  --disp:'Space Grotesk','Chakra Petch',system-ui,sans-serif;
  --num:'Chakra Petch','Space Grotesk',monospace;
  --safe-b:env(safe-area-inset-bottom,0px);--safe-t:env(safe-area-inset-top,0px);
}}
html,body{{min-height:100%;background:var(--void);color:var(--w1);font-family:var(--disp);overscroll-behavior:none;-webkit-font-smoothing:antialiased}}
body{{position:relative}}
body::before{{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(255,255,255,.008) 2px,rgba(255,255,255,.008) 3px);pointer-events:none;z-index:200}}
body::after{{content:'';position:fixed;top:-22%;left:50%;transform:translateX(-50%);width:150%;height:55%;background:radial-gradient(ellipse at center,rgba(200,144,26,.055),transparent 65%);pointer-events:none;z-index:0;animation:auraDrift 14s ease-in-out infinite}}
@keyframes auraDrift{{0%,100%{{opacity:.85}}50%{{opacity:1.0}}}}
::-webkit-scrollbar{{width:0;display:none}}
*{{scrollbar-width:none}}
.hidden{{display:none!important}}
a{{color:var(--gold);cursor:pointer;text-decoration:none}}

/* ── COMMAND BAR ── */
.cmd-bar{{display:flex;align-items:center;justify-content:space-between;padding:12px 18px;padding-top:max(12px,var(--safe-t));border-bottom:0.5px solid var(--line);background:linear-gradient(180deg,var(--carbon),var(--void));position:sticky;top:0;z-index:100}}
.cmd-id{{display:flex;align-items:center;gap:11px;cursor:pointer}}
.cmd-gem{{width:24px;height:24px;background:linear-gradient(135deg,var(--gold),var(--gold3));transform:rotate(45deg);display:flex;align-items:center;justify-content:center;flex-shrink:0;box-shadow:0 0 14px rgba(200,144,26,.35);position:relative;animation:gemPulse 4.5s ease-in-out infinite}}
@keyframes gemPulse{{0%,100%{{box-shadow:0 0 14px rgba(200,144,26,.3)}}50%{{box-shadow:0 0 26px rgba(240,184,64,.55)}}}}
.cmd-gem::after{{content:'◆';transform:rotate(-45deg);font-size:.46rem;color:#000;font-weight:900;position:absolute}}
.cmd-name{{font-size:.86rem;font-weight:300;letter-spacing:.42em;background:linear-gradient(90deg,var(--w1) 0%,var(--w1) 38%,var(--gold3) 50%,var(--w1) 62%,var(--w1) 100%);background-size:250% 100%;-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;color:var(--w1);animation:shimmerText 7s linear infinite}}
@keyframes shimmerText{{0%{{background-position:125% 0}}100%{{background-position:-125% 0}}}}
.cmd-acts{{display:flex;gap:6px;align-items:center}}
.cmd-clock{{font-family:var(--mono);font-size:.5rem;color:var(--w3);letter-spacing:.08em;margin-right:4px}}
.cmd-btn{{height:32px;padding:0 12px;border-radius:8px;font-family:var(--mono);font-size:.54rem;letter-spacing:.07em;display:flex;align-items:center;cursor:pointer;border:0.5px solid var(--line2);background:transparent;color:var(--w2);transition:all .15s}}
.cmd-btn:active{{opacity:.7}}
.cmd-btn-pro{{border-color:var(--gold-dim);color:var(--gold);background:rgba(200,144,26,.06)}}
.cmd-btn-pro:hover{{background:rgba(200,144,26,.12)}}

/* ── INPUT STAGE ── */
.input-stage{{padding:24px 18px 6px}}
.input-eyebrow{{text-align:center;font-family:var(--mono);font-size:.5rem;color:var(--w3);letter-spacing:.24em;margin-bottom:13px}}
.input-field{{background:var(--obsidian);border:0.5px solid var(--line2);border-radius:18px;padding:16px 18px;position:relative;overflow:hidden;transition:border-color .2s}}
.input-field::before{{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,var(--gold-dim),transparent)}}
.input-field:focus-within{{border-color:var(--gold-dim);box-shadow:0 0 0 1px rgba(200,144,26,.18),0 0 36px rgba(200,144,26,.09)}}
.input-field::after{{content:'';position:absolute;top:0;left:-45%;width:38%;height:1px;background:linear-gradient(90deg,transparent,var(--gold3),transparent);opacity:.7;animation:scanLine 5.5s cubic-bezier(.4,0,.2,1) infinite}}
@keyframes scanLine{{0%{{left:-45%}}55%{{left:108%}}100%{{left:108%}}}}
.input-el{{width:100%;background:transparent;border:none;outline:none;font-size:2.1rem;font-weight:300;color:var(--w1);letter-spacing:.2em;text-align:center;line-height:1.1;font-family:var(--disp);text-transform:uppercase;caret-color:var(--gold);-webkit-appearance:none;appearance:none}}
.input-el::placeholder{{color:var(--w4);font-size:1.1rem;letter-spacing:.06em;text-transform:none}}
/* kill browser autofill white background */
.input-el:-webkit-autofill,.input-el:-webkit-autofill:hover,.input-el:-webkit-autofill:focus,.modal-input:-webkit-autofill,.modal-input:-webkit-autofill:hover,.modal-input:-webkit-autofill:focus{{-webkit-text-fill-color:var(--w1);-webkit-box-shadow:0 0 0 1000px var(--obsidian) inset;box-shadow:0 0 0 1000px var(--obsidian) inset;transition:background-color 9999s ease-in-out 0s;caret-color:var(--gold)}}
.input-el:-webkit-autofill{{-webkit-box-shadow:0 0 0 1000px transparent inset;box-shadow:none;background-clip:text}}
.input-go{{margin-top:11px;width:100%;height:50px;background:linear-gradient(135deg,var(--gold),var(--gold3));border:none;border-radius:13px;font-family:var(--mono);font-size:.7rem;font-weight:700;color:#000;letter-spacing:.16em;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px;transition:opacity .15s;position:relative;overflow:hidden}}
.input-go::after,.intro-cta::after{{content:'';position:absolute;top:0;left:-65%;width:42%;height:100%;background:linear-gradient(105deg,transparent,rgba(255,255,255,.38),transparent);transform:skewX(-20deg);animation:btnShine 5s ease-in-out infinite}}
@keyframes btnShine{{0%,55%{{left:-65%}}100%{{left:150%}}}}
.input-go:active{{opacity:.82}}
.input-go:disabled{{background:var(--slate);color:var(--w3);cursor:default}}

/* chips */
.chip-strip{{display:flex;gap:6px;padding:13px 18px 4px;overflow-x:auto}}
.chip{{height:30px;padding:0 12px;background:var(--carbon);border:0.5px solid var(--line);border-radius:8px;font-family:var(--mono);font-size:.54rem;color:var(--w3);white-space:nowrap;letter-spacing:.05em;display:flex;align-items:center;cursor:pointer;flex-shrink:0;gap:5px;transition:all .15s}}
.chip:active,.chip:hover{{border-color:var(--gold-dim);color:var(--gold)}}
.chip-tick{{color:var(--jade);font-size:.4rem}}

/* status line */
.status-line{{font-family:var(--mono);font-size:.52rem;color:var(--w3);text-align:center;padding:8px 18px;letter-spacing:.04em;min-height:26px}}

/* ── SPINNER ── */
.spinner{{text-align:center;padding:46px 0;display:none}}
.spinner.on{{display:block}}
.spin-ring{{width:38px;height:38px;border:2px solid var(--line2);border-top-color:var(--gold);border-radius:50%;display:inline-block;animation:spin 0.9s linear infinite}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
.reveal{{opacity:0;transform:translateY(28px);transition:opacity .7s cubic-bezier(.16,1,.3,1),transform .7s cubic-bezier(.16,1,.3,1)}}
.reveal.shown{{opacity:1;transform:translateY(0)}}
@media (prefers-reduced-motion:reduce){{.reveal{{opacity:1;transform:none;transition:none}}}}
.spin-txt{{font-family:var(--mono);font-size:.56rem;color:var(--w3);letter-spacing:.1em;margin-top:13px}}

/* ── INTRO / LANDING ── */
.intro{{padding:8px 18px 30px;animation:introfade .8s cubic-bezier(.16,1,.3,1)}}
@keyframes introfade{{from{{opacity:0;transform:translateY(16px)}}to{{opacity:1;transform:translateY(0)}}}}
.intro-badge{{display:inline-flex;align-items:center;font-family:var(--mono);font-size:.5rem;color:var(--gold);letter-spacing:.14em;border:0.5px solid var(--gold-dim);background:rgba(200,144,26,.06);border-radius:20px;padding:5px 13px;margin-bottom:18px}}
.intro-headline{{font-family:var(--disp);font-size:2.1rem;font-weight:600;color:var(--w1);line-height:1.12;letter-spacing:.01em;margin-bottom:14px}}
.intro-headline span{{color:var(--gold3);text-shadow:0 0 40px rgba(240,184,64,.18)}}
.intro-sub{{font-family:var(--disp);font-size:.96rem;color:var(--w2);line-height:1.7;margin-bottom:24px;font-weight:400}}

.intro-steps{{display:flex;flex-direction:column;gap:0;margin-bottom:22px;position:relative}}
.intro-step{{display:flex;gap:14px;align-items:flex-start;padding:13px 0;border-top:0.5px solid var(--line);position:relative}}
.intro-step:first-child{{border-top:none}}
.intro-step-num{{font-family:var(--num);font-size:1rem;font-weight:500;color:var(--gold);width:30px;flex-shrink:0;letter-spacing:.02em;padding-top:1px}}
.intro-step-title{{font-family:var(--disp);font-size:.92rem;font-weight:600;color:var(--w1);margin-bottom:3px}}
.intro-step-text{{font-family:var(--disp);font-size:.78rem;color:var(--w2);line-height:1.55}}

.intro-features{{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:22px}}
.intro-feat{{display:flex;align-items:center;gap:9px;background:var(--carbon);border:0.5px solid var(--line);border-radius:11px;padding:11px 13px;font-family:var(--disp);font-size:.76rem;color:var(--w2);font-weight:500}}
.intro-feat-ic{{width:22px;height:22px;border-radius:6px;background:rgba(200,144,26,.1);border:0.5px solid var(--gold-dim);display:flex;align-items:center;justify-content:center;font-size:.62rem;color:var(--gold);flex-shrink:0}}

.intro-cta{{width:100%;height:52px;background:linear-gradient(135deg,var(--gold),var(--gold3));border:none;border-radius:13px;font-family:var(--mono);font-size:.68rem;font-weight:700;color:#000;letter-spacing:.14em;cursor:pointer;margin-bottom:12px;transition:opacity .15s,transform .15s;position:relative;overflow:hidden}}
.intro-cta:active{{opacity:.85;transform:translateY(1px)}}
.intro-foot{{text-align:center;font-family:var(--mono);font-size:.5rem;color:var(--w3);letter-spacing:.04em;line-height:1.6}}

/* ── DIVIDER ── */
.divider{{display:flex;align-items:center;gap:12px;padding:14px 20px 14px}}
.divider-line{{flex:1;height:0.5px;background:linear-gradient(90deg,transparent,var(--line3))}}
.divider-line.r{{background:linear-gradient(90deg,var(--line3),transparent)}}
.divider-txt{{font-family:var(--mono);font-size:.48rem;color:var(--w3);letter-spacing:.2em}}

/* ── VERDICT STAGE (hero) ── */
.verdict-stage{{margin:0 14px;background:radial-gradient(ellipse at top,var(--graphite),var(--obsidian));border:0.5px solid var(--line2);border-radius:24px;overflow:hidden;position:relative}}
.vs-glow{{position:absolute;top:0;left:0;right:0;height:110px;pointer-events:none;animation:glowBreathe 5.5s ease-in-out infinite}}
@keyframes glowBreathe{{0%,100%{{opacity:1}}50%{{opacity:.5}}}}
.vs-glow.up{{background:radial-gradient(ellipse at top,rgba(46,160,67,.14),transparent 70%)}}
.vs-glow.down{{background:radial-gradient(ellipse at top,rgba(196,48,43,.14),transparent 70%)}}
.vs-glow.fair{{background:radial-gradient(ellipse at top,rgba(200,144,26,.12),transparent 70%)}}
.vs-strip{{height:3px}}
.vs-strip.up{{background:linear-gradient(90deg,transparent,var(--jade),transparent)}}
.vs-strip.down{{background:linear-gradient(90deg,transparent,var(--blood),transparent)}}
.vs-strip.fair{{background:linear-gradient(90deg,transparent,var(--gold),transparent)}}
.vs-co{{text-align:center;padding:22px 20px 0;font-family:var(--disp);font-size:.92rem;font-weight:500;color:var(--w1);letter-spacing:.04em}}
.vs-sector{{text-align:center;font-family:var(--mono);font-size:.5rem;color:var(--w3);letter-spacing:.14em;margin-top:6px}}

/* COMPOSITE — the absolute hero */
.vs-comp-label{{text-align:center;font-family:var(--mono);font-size:.52rem;color:var(--gold);letter-spacing:.2em;margin-top:22px}}
.vs-comp{{text-align:center;font-family:var(--num);font-size:3.4rem;font-weight:500;color:var(--gold3);line-height:1.05;letter-spacing:.01em;margin:10px 0 6px;text-shadow:0 0 44px rgba(240,184,64,.25);animation:compReveal 1.5s cubic-bezier(.16,1,.3,1) both}}
@keyframes compReveal{{0%{{opacity:0;transform:scale(.92);text-shadow:0 0 80px rgba(240,184,64,.6)}}100%{{opacity:1;transform:scale(1);text-shadow:0 0 44px rgba(240,184,64,.25)}}}}
.vs-comp-sub{{text-align:center;font-family:var(--mono);font-size:.5rem;color:var(--w3);letter-spacing:.12em;margin-bottom:10px}}
.vs-margin{{text-align:center;font-family:var(--mono);font-size:.78rem;letter-spacing:.06em;margin-bottom:20px;font-weight:700}}
.vs-margin.up{{color:var(--jade)}}.vs-margin.down{{color:var(--blood)}}.vs-margin.fair{{color:var(--gold)}}

/* verdict sentence */
.vs-verdict{{text-align:center;padding:18px 24px;border-top:0.5px solid var(--line);border-bottom:0.5px solid var(--line)}}
.vs-verdict.up{{background:rgba(46,160,67,.05)}}.vs-verdict.down{{background:rgba(196,48,43,.05)}}.vs-verdict.fair{{background:rgba(200,144,26,.05)}}
.vs-verdict-main{{font-family:var(--disp);font-size:1.08rem;font-weight:600;color:var(--w1);letter-spacing:.04em;line-height:1.3;text-transform:uppercase}}
.vs-verdict-main .hl-up{{color:var(--jade2)}}.vs-verdict-main .hl-down{{color:var(--blood2)}}.vs-verdict-main .hl-fair{{color:var(--gold2)}}
.vs-verdict-sub{{font-family:var(--disp);font-size:.8rem;color:var(--w2);font-weight:400;margin-top:8px;line-height:1.6}}

/* current price reference row */
.vs-stats{{display:flex;border-bottom:0.5px solid var(--line)}}
.vs-stat{{flex:1;text-align:center;padding:13px 8px}}
.vs-stat + .vs-stat{{border-left:0.5px solid var(--line)}}
.vs-stat-lbl{{font-family:var(--mono);font-size:.44rem;color:var(--w3);letter-spacing:.1em;margin-bottom:5px}}
.vs-stat-val{{font-size:1.1rem;font-weight:500;color:var(--w1);font-family:var(--num);letter-spacing:.02em}}
.vs-stat-val.up{{color:var(--jade)}}.vs-stat-val.down{{color:var(--blood)}}

/* 52-week range */
.vs-range{{padding:13px 20px 16px;border-bottom:0.5px solid var(--line)}}
.vs-range-ends{{display:flex;justify-content:space-between;font-family:var(--mono);font-size:.5rem;margin-bottom:8px}}
.vs-range-lo{{color:var(--blood)}}.vs-range-hi{{color:var(--jade)}}.vs-range-mid{{color:var(--w3);letter-spacing:.08em}}
.vs-track{{height:2px;background:var(--line3);border-radius:1px;position:relative}}
.vs-fill{{position:absolute;left:0;top:0;height:2px;background:var(--gold);border-radius:1px;transition:width .9s cubic-bezier(.4,0,.2,1)}}
.vs-pin{{position:absolute;top:50%;transform:translate(-50%,-50%);width:9px;height:9px;background:var(--gold);border-radius:50%;border:1.5px solid var(--void);transition:left .9s cubic-bezier(.4,0,.2,1)}}

/* ── MODEL MATRIX ── */
.matrix-head{{display:flex;justify-content:space-between;padding:12px 20px 8px;font-family:var(--mono);font-size:.46rem;color:var(--w3);letter-spacing:.14em}}
.mx-row{{display:flex;align-items:center;padding:10px 20px;border-top:0.5px solid var(--line);position:relative;overflow:hidden}}
.mx-bar{{position:absolute;left:0;top:0;bottom:0;border-left:1.5px solid var(--jade);background:rgba(46,160,67,.05);transform-origin:left;animation:barGrow .9s cubic-bezier(.16,1,.3,1) both}}
@keyframes barGrow{{from{{transform:scaleX(0)}}to{{transform:scaleX(1)}}}}
.mx-bar.down{{border-left-color:var(--blood);background:rgba(196,48,43,.05)}}
.mx-bar.fair{{border-left-color:var(--gold);background:rgba(200,144,26,.05)}}
.mx-bar.na{{border-left-color:var(--w4);background:rgba(120,120,120,.03)}}
.mx-name{{flex:1;font-family:var(--mono);font-size:.55rem;color:var(--w2);letter-spacing:.04em;z-index:1}}
.mx-val{{font-size:1.02rem;font-weight:500;font-family:var(--num);z-index:1;margin-right:11px;letter-spacing:.01em}}
.mx-val.up{{color:var(--jade)}}.mx-val.down{{color:var(--blood)}}.mx-val.fair{{color:var(--gold)}}.mx-val.na{{color:var(--w3)}}
.mx-delta{{font-family:var(--mono);font-size:.5rem;width:52px;text-align:right;z-index:1}}
.mx-delta.up{{color:var(--jade)}}.mx-delta.down{{color:var(--blood)}}.mx-delta.fair{{color:var(--gold)}}.mx-delta.na{{color:var(--w3)}}

/* ── AI CARDS ── */
.ai-block{{margin:13px 14px 0;background:var(--carbon);border:0.5px solid var(--line2);border-radius:18px;overflow:hidden}}
.ai-head{{display:flex;align-items:center;gap:9px;padding:13px 18px 10px;border-bottom:0.5px solid var(--line)}}
.ai-ic{{width:22px;height:22px;border-radius:6px;background:rgba(200,144,26,.12);border:0.5px solid var(--gold-dim);display:flex;align-items:center;justify-content:center;font-size:.6rem;color:var(--gold)}}
.ai-ic.jade{{background:rgba(46,160,67,.1);border-color:rgba(46,160,67,.3);color:var(--jade2)}}
.ai-title{{font-family:var(--mono);font-size:.54rem;color:var(--w2);letter-spacing:.12em}}
.ai-body{{padding:16px 18px;font-family:var(--disp);font-size:.92rem;font-weight:400;color:var(--w2);line-height:1.85;letter-spacing:.01em}}
.ai-loading{{display:flex;align-items:center;gap:7px;padding:14px 18px}}
.dot{{width:5px;height:5px;border-radius:50%;background:var(--gold);animation:pulse 1.2s ease-in-out infinite}}
.dot:nth-child(2){{animation-delay:.2s}}.dot:nth-child(3){{animation-delay:.4s}}
@keyframes pulse{{0%,100%{{opacity:.2}}50%{{opacity:1}}}}
.ai-loading-txt{{font-family:var(--mono);font-size:.52rem;color:var(--w3);letter-spacing:.06em;margin-left:5px}}

/* ── HEALTH CARD ── */
.health-block{{margin:13px 14px 0;background:var(--carbon);border:0.5px solid var(--line2);border-radius:18px;overflow:hidden}}
.health-top{{display:flex;align-items:center;justify-content:space-between;padding:15px 18px 12px}}
.health-title{{font-family:var(--mono);font-size:.54rem;color:var(--w2);letter-spacing:.12em}}
.health-gr{{display:flex;align-items:center;gap:11px}}
.health-score{{font-size:1.7rem;font-weight:500;color:var(--w1);font-family:var(--num)}}
.health-score small{{font-size:.7rem;color:var(--w3)}}
.health-grade{{width:38px;height:38px;border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:1.1rem;font-weight:700;font-family:var(--num)}}
.health-grade.A{{background:rgba(46,160,67,.16);border:0.5px solid rgba(46,160,67,.5);color:var(--jade2)}}
.health-grade.B{{background:rgba(63,191,85,.1);border:0.5px solid rgba(63,191,85,.4);color:var(--jade2)}}
.health-grade.C{{background:rgba(200,144,26,.13);border:0.5px solid rgba(200,144,26,.45);color:var(--gold2)}}
.health-grade.D{{background:rgba(196,48,43,.12);border:0.5px solid rgba(196,48,43,.4);color:var(--blood2)}}
.health-grade.F{{background:rgba(196,48,43,.2);border:0.5px solid rgba(196,48,43,.55);color:var(--blood2)}}
.health-bar{{height:4px;margin:0 18px 13px;background:var(--line3);border-radius:2px;overflow:hidden}}
.health-bar-fill{{height:100%;border-radius:2px;transition:width 1s cubic-bezier(.4,0,.2,1)}}
.health-bar-fill.A,.health-bar-fill.B{{background:linear-gradient(90deg,var(--jade),var(--jade2))}}
.health-bar-fill.C{{background:linear-gradient(90deg,var(--gold),var(--gold2))}}
.health-bar-fill.D,.health-bar-fill.F{{background:linear-gradient(90deg,var(--blood),var(--blood2))}}
.health-grid{{display:grid;grid-template-columns:1fr 1fr;gap:0.5px;background:var(--line);border-top:0.5px solid var(--line)}}
.hg-item{{background:var(--carbon);padding:7px 14px;display:flex;justify-content:space-between;align-items:center;gap:8px}}
.hg-k{{font-family:var(--mono);font-size:.5rem;color:var(--w3);letter-spacing:.03em}}
.hg-v{{font-family:var(--mono);font-size:.54rem;color:var(--w1);text-align:right}}
.health-flags{{display:flex;flex-wrap:wrap;gap:5px;padding:11px 18px}}
.health-flag{{background:rgba(196,48,43,.1);border:0.5px solid rgba(196,48,43,.3);border-radius:20px;padding:3px 10px;font-family:var(--mono);font-size:.48rem;color:var(--blood2);letter-spacing:.03em}}

/* ── LEADERSHIP / GOVERNANCE ── */
.lead-block{{margin:13px 14px 0;background:var(--carbon);border:0.5px solid var(--line2);border-radius:18px;overflow:hidden}}
.lead-head{{display:flex;align-items:center;justify-content:space-between;padding:14px 18px 11px;border-bottom:0.5px solid var(--line)}}
.lead-title{{font-family:var(--mono);font-size:.54rem;color:var(--w2);letter-spacing:.12em;display:flex;align-items:center;gap:8px}}
.lead-ic{{width:22px;height:22px;border-radius:6px;background:rgba(200,144,26,.12);border:0.5px solid var(--gold-dim);display:flex;align-items:center;justify-content:center;font-size:.6rem;color:var(--gold)}}
.lead-meta{{font-family:var(--mono);font-size:.46rem;color:var(--w3);letter-spacing:.06em}}

/* org stats strip */
.lead-stats{{display:flex;border-bottom:0.5px solid var(--line)}}
.lead-stat{{flex:1;text-align:center;padding:11px 6px}}
.lead-stat + .lead-stat{{border-left:0.5px solid var(--line)}}
.lead-stat-v{{font-family:var(--num);font-size:1rem;font-weight:500;color:var(--w1)}}
.lead-stat-l{{font-family:var(--mono);font-size:.42rem;color:var(--w3);letter-spacing:.08em;margin-top:4px}}

/* the tree */
.tree{{padding:16px 16px 18px}}
.tree-tier{{display:flex;flex-direction:column;align-items:center;gap:0}}
.tree-connector{{width:1px;height:16px;background:linear-gradient(180deg,var(--gold-dim),transparent)}}
.tree-connector.up{{background:linear-gradient(180deg,transparent,var(--gold-dim))}}
.tree-row{{display:flex;justify-content:center;flex-wrap:wrap;gap:8px;width:100%;position:relative}}
.tree-row.multi::before{{content:'';position:absolute;top:-8px;left:18%;right:18%;height:1px;background:var(--gold-dim);opacity:.5}}

.exec-node{{background:radial-gradient(ellipse at top,var(--graphite),var(--obsidian));border:0.5px solid var(--line2);border-radius:14px;padding:12px 13px;min-width:140px;max-width:170px;flex:1;position:relative;overflow:hidden;transition:border-color .2s,transform .2s}}
.exec-node:active{{transform:translateY(-1px)}}
.exec-node.ceo{{border-color:var(--gold-dim);box-shadow:0 0 24px rgba(200,144,26,.1)}}
.exec-node.ceo::before{{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--gold),transparent)}}
.exec-node.chair::before{{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--jade),transparent)}}
.exec-top{{display:flex;align-items:center;gap:10px;margin-bottom:8px}}
.exec-avatar{{width:36px;height:36px;border-radius:50%;background:radial-gradient(circle at 35% 30%,var(--slate2),var(--obsidian));border:0.5px solid var(--gold-dim);display:flex;align-items:center;justify-content:center;font-family:var(--num);font-size:.82rem;font-weight:500;color:var(--gold);flex-shrink:0}}
.exec-node.ceo .exec-avatar{{width:44px;height:44px;font-size:.95rem;border-color:var(--gold);box-shadow:0 0 16px rgba(200,144,26,.2)}}
.exec-badge{{font-family:var(--mono);font-size:.44rem;letter-spacing:.1em;color:var(--gold);padding:2px 7px;border:0.5px solid var(--gold-dim);border-radius:10px;background:rgba(200,144,26,.08);align-self:flex-start}}
.exec-badge.chair{{color:var(--jade2);border-color:rgba(46,160,67,.4);background:rgba(46,160,67,.08)}}
.exec-badge.vp,.exec-badge.exec,.exec-badge.director{{color:var(--w2);border-color:var(--line3);background:transparent}}
.exec-name{{font-family:var(--disp);font-size:.82rem;font-weight:600;color:var(--w1);line-height:1.2}}
.exec-title{{font-family:var(--disp);font-size:.62rem;color:var(--w2);line-height:1.35;margin-top:3px}}
.exec-foot{{display:flex;gap:10px;margin-top:9px;padding-top:8px;border-top:0.5px solid var(--line)}}
.exec-metric{{flex:1}}
.exec-metric-l{{font-family:var(--mono);font-size:.4rem;color:var(--w3);letter-spacing:.06em}}
.exec-metric-v{{font-family:var(--num);font-size:.66rem;color:var(--w1);margin-top:2px}}

/* board summary */
.board-block{{border-top:0.5px solid var(--line);padding:13px 18px}}
.board-label{{font-family:var(--mono);font-size:.5rem;color:var(--w3);letter-spacing:.12em;margin-bottom:10px}}
.board-grid{{display:flex;flex-wrap:wrap;gap:6px}}
.board-chip{{display:flex;align-items:center;gap:7px;background:var(--graphite);border:0.5px solid var(--line);border-radius:20px;padding:4px 10px 4px 4px}}
.board-chip-av{{width:22px;height:22px;border-radius:50%;background:var(--slate2);border:0.5px solid var(--line3);display:flex;align-items:center;justify-content:center;font-family:var(--num);font-size:.5rem;color:var(--w2);flex-shrink:0}}
.board-chip-name{{font-family:var(--disp);font-size:.66rem;color:var(--w2)}}

/* action buttons */
.actions{{display:flex;gap:7px;margin:13px 14px 0}}
.act-btn{{flex:1;height:42px;background:var(--carbon);border:0.5px solid var(--line2);border-radius:11px;color:var(--w2);font-family:var(--mono);font-size:.54rem;letter-spacing:.06em;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:6px;transition:all .15s}}
.act-btn:active{{opacity:.7}}
.act-btn.saved{{border-color:var(--gold-dim);color:var(--gold);background:rgba(200,144,26,.06)}}

/* ── SECTION RAILS ── */
.rail-head{{display:flex;align-items:center;justify-content:space-between;padding:22px 20px 11px}}
.rail-title{{font-family:var(--mono);font-size:.5rem;color:var(--w3);letter-spacing:.18em}}
.rail-act{{font-family:var(--mono);font-size:.46rem;color:var(--gold-dim);letter-spacing:.07em;display:flex;align-items:center;gap:4px;cursor:pointer}}
.rail{{display:flex;gap:7px;padding:0 18px 4px;overflow-x:auto}}
.rail-card{{flex-shrink:0;background:var(--carbon);border:0.5px solid var(--line);border-radius:13px;padding:11px 14px;min-width:84px;cursor:pointer;position:relative;overflow:hidden;transition:border-color .15s}}
.rail-card:active{{border-color:var(--gold-dim)}}
.rail-card::after{{content:'';position:absolute;bottom:0;left:0;right:0;height:1.5px;background:var(--line2)}}
.rail-card.up::after{{background:var(--jade)}}.rail-card.dn::after{{background:var(--blood)}}
.rail-sym{{font-family:var(--mono);font-size:.56rem;color:var(--w2);letter-spacing:.06em;margin-bottom:5px}}
.rail-px{{font-size:.98rem;font-weight:500;color:var(--w1);font-family:var(--num);letter-spacing:.01em}}
.rail-chg{{font-family:var(--mono);font-size:.48rem;margin-top:3px}}
.rail-shimmer{{display:inline-block;background:var(--slate);border-radius:3px;animation:shimmer 1.3s ease-in-out infinite}}
@keyframes shimmer{{0%,100%{{opacity:.3}}50%{{opacity:.7}}}}
.up{{color:var(--jade)}}.dn{{color:var(--blood)}}
.rail-rm{{position:absolute;top:6px;right:7px;font-size:.6rem;color:var(--w3);cursor:pointer;line-height:1;padding:2px}}
.rail-rm:hover{{color:var(--blood)}}

/* watchlist analyze hint */
.wl-empty-note{{font-size:.72rem;color:var(--w3);font-style:italic;padding:0 20px 4px;line-height:1.6}}

/* ── MASTERS ── */
.masters{{display:flex;gap:8px;padding:0 18px 4px;overflow-x:auto}}
.master{{flex-shrink:0;width:128px;background:var(--carbon);border:0.5px solid var(--line);border-radius:15px;padding:14px 13px 13px;cursor:pointer;transition:border-color .15s}}
.master:active{{border-color:var(--gold-dim)}}
.master-top{{display:flex;align-items:center;gap:10px;margin-bottom:10px}}
.master-medal{{width:38px;height:38px;border-radius:50%;background:radial-gradient(circle at 35% 30%,var(--slate2),var(--obsidian));border:0.5px solid var(--gold-dim);display:flex;align-items:center;justify-content:center;font-size:.92rem;font-weight:300;color:var(--gold);font-family:var(--disp);flex-shrink:0}}
.master-name{{font-family:var(--disp);font-size:.78rem;color:var(--w1);line-height:1.25;font-weight:600}}
.master-years{{font-family:var(--mono);font-size:.42rem;color:var(--w3);letter-spacing:.05em;margin-top:2px}}
.master-tag{{font-family:var(--mono);font-size:.44rem;color:var(--gold-dim);letter-spacing:.05em;margin-bottom:8px}}
.master-bio{{font-family:var(--disp);font-size:.72rem;color:var(--w2);line-height:1.65;font-weight:400}}
.master-quote{{font-family:var(--disp);font-size:.72rem;color:var(--gold);line-height:1.6;margin-top:10px;padding-top:10px;border-top:0.5px solid var(--line)}}

/* ── ABOUT PANE ── */
.about-pane{{padding:20px 18px 30px}}
.about-gem-wrap{{text-align:center;margin:10px 0 22px}}
.about-gem{{width:56px;height:56px;background:linear-gradient(135deg,var(--gold),var(--gold3));transform:rotate(45deg);display:inline-flex;align-items:center;justify-content:center;box-shadow:0 0 40px rgba(200,144,26,.25);animation:gem-spin 14s linear infinite}}
.about-gem::after{{content:'◆';transform:rotate(-45deg);font-size:.85rem;color:#000;font-weight:900;animation:gem-spin-i 14s linear infinite}}
@keyframes gem-spin{{0%{{transform:rotate(45deg)}}100%{{transform:rotate(405deg)}}}}
@keyframes gem-spin-i{{0%{{transform:rotate(-45deg)}}100%{{transform:rotate(-405deg)}}}}
.about-title{{text-align:center;font-size:1.4rem;font-weight:300;color:var(--gold3);letter-spacing:.3em;margin-bottom:6px}}
.about-tag{{text-align:center;font-family:var(--mono);font-size:.5rem;color:var(--w3);letter-spacing:.12em;margin-bottom:20px}}
.about-body{{font-family:var(--disp);font-size:.92rem;color:var(--w2);line-height:1.85;font-weight:400;margin-bottom:20px;text-align:center}}
.formula-card{{background:var(--carbon);border:0.5px solid var(--line);border-left:2px solid var(--gold-dim);border-radius:0 11px 11px 0;padding:12px 15px;margin-bottom:8px}}
.formula-name{{font-size:.85rem;color:var(--gold2);font-weight:500;margin-bottom:3px;letter-spacing:.03em}}
.formula-eq{{font-family:var(--mono);font-size:.56rem;color:var(--jade2);margin-bottom:4px}}
.formula-desc{{font-size:.74rem;color:var(--w3);font-style:italic;line-height:1.55}}
.about-pricing{{background:radial-gradient(ellipse at top,var(--graphite),var(--obsidian));border:0.5px solid var(--gold-dim);border-radius:18px;padding:22px 20px;text-align:center;margin:22px 0 14px}}
.about-price{{font-size:2.4rem;font-weight:300;color:var(--gold3);line-height:1}}
.about-price small{{font-size:.8rem;color:var(--w3)}}
.about-price-note{{font-family:var(--mono);font-size:.5rem;color:var(--w3);letter-spacing:.08em;margin:8px 0 16px}}
.about-sub-btn{{width:100%;height:48px;background:linear-gradient(135deg,var(--gold),var(--gold3));border:none;border-radius:12px;font-family:var(--mono);font-size:.64rem;font-weight:700;color:#000;letter-spacing:.12em;cursor:pointer}}
.disc{{background:var(--carbon);border:0.5px solid var(--line);border-radius:11px;padding:13px 15px;margin-top:14px}}
.disc p{{font-size:.62rem;color:var(--w3);font-style:italic;line-height:1.7}}

/* ── DOCK ── */
.dock{{position:sticky;bottom:0;display:flex;margin:14px 14px 0;margin-bottom:max(14px,var(--safe-b));background:rgba(10,10,10,.96);backdrop-filter:blur(20px);border:0.5px solid var(--line2);border-radius:16px;padding:6px;gap:4px;z-index:90}}
.dock-item{{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;padding:8px 0;border-radius:11px;cursor:pointer;min-height:50px;justify-content:center;transition:background .15s}}
.dock-item.on{{background:var(--slate);box-shadow:inset 0 0 0 0.5px rgba(200,144,26,.28),0 0 18px rgba(200,144,26,.1)}}
.dock-ic{{font-size:1.05rem;line-height:1;color:var(--w3)}}
.dock-lb{{font-family:var(--mono);font-size:.42rem;color:var(--w3);letter-spacing:.06em}}
.dock-item.on .dock-ic,.dock-item.on .dock-lb{{color:var(--gold)}}

/* panes */
.pane{{min-height:50vh}}

/* ── TOAST ── */
.toast{{position:fixed;left:50%;bottom:90px;transform:translateX(-50%) translateY(20px);background:var(--slate);border:0.5px solid var(--gold-dim);border-radius:11px;padding:11px 18px;font-family:var(--mono);font-size:.58rem;color:var(--gold2);letter-spacing:.04em;opacity:0;pointer-events:none;transition:all .3s;z-index:300;white-space:nowrap;max-width:90vw;overflow:hidden;text-overflow:ellipsis}}
.toast.on{{opacity:1;transform:translateX(-50%) translateY(0)}}

/* ── MODALS ── */
.modal{{position:fixed;inset:0;background:rgba(0,0,0,.8);backdrop-filter:blur(8px);display:none;align-items:center;justify-content:center;z-index:400;padding:20px}}
.modal.on{{display:flex}}
.modal-card{{background:var(--graphite);border:0.5px solid var(--line2);border-radius:20px;padding:24px 22px;width:100%;max-width:340px;position:relative}}
.modal-card::before{{content:'';position:absolute;top:0;left:20px;right:20px;height:1px;background:linear-gradient(90deg,transparent,var(--gold-dim),transparent)}}
.modal-title{{font-size:1.2rem;font-weight:300;color:var(--gold3);letter-spacing:.08em;text-align:center;margin-bottom:5px}}
.modal-sub{{font-family:var(--mono);font-size:.52rem;color:var(--w3);text-align:center;letter-spacing:.06em;margin-bottom:18px}}
.modal-input{{width:100%;height:46px;background:var(--obsidian);border:0.5px solid var(--line2);border-radius:11px;padding:0 15px;color:var(--w1);font-family:var(--mono);font-size:.72rem;outline:none;margin-bottom:10px;transition:border-color .15s}}
.modal-input:focus{{border-color:var(--gold-dim)}}
.modal-input::placeholder{{color:var(--w4)}}
.btn-big{{width:100%;height:48px;background:linear-gradient(135deg,var(--gold),var(--gold3));border:none;border-radius:12px;font-family:var(--mono);font-size:.62rem;font-weight:700;color:#000;letter-spacing:.1em;cursor:pointer;margin-top:6px}}
.btn-ghost-modal{{width:100%;height:42px;background:transparent;border:0.5px solid var(--line2);border-radius:11px;color:var(--w2);font-family:var(--mono);font-size:.56rem;letter-spacing:.06em;cursor:pointer;margin-top:8px}}
.modal-switch{{text-align:center;font-size:.74rem;color:var(--w3);font-style:italic;margin-top:14px}}
.modal-err{{font-family:var(--mono);font-size:.5rem;color:var(--blood2);text-align:center;min-height:16px;margin-bottom:6px;letter-spacing:.03em}}
.remember-row{{display:flex;align-items:center;gap:9px;margin:4px 2px 12px;cursor:pointer;font-family:var(--disp);font-size:.78rem;color:var(--w2)}}
.remember-row input{{width:16px;height:16px;accent-color:var(--gold);cursor:pointer;flex-shrink:0}}
.modal-acct-email{{font-family:var(--mono);font-size:.66rem;color:var(--gold2);text-align:center;letter-spacing:.04em;margin-bottom:6px}}
.modal-acct-status{{font-family:var(--mono);font-size:.5rem;text-align:center;letter-spacing:.08em;margin-bottom:18px}}
.modal-acct-status.pro{{color:var(--jade2)}}.modal-acct-status.free{{color:var(--w3)}}

/* paywall */
.paywall-feats{{margin:14px 0 18px}}
.pf-row{{display:flex;align-items:center;gap:9px;padding:6px 0;font-size:.8rem;color:var(--w2)}}
.pf-check{{color:var(--jade2);font-size:.7rem}}
.modal-price{{text-align:center;margin:8px 0 16px}}
.modal-price-num{{font-size:2.2rem;font-weight:300;color:var(--gold3);line-height:1}}
.modal-price-per{{font-family:var(--mono);font-size:.5rem;color:var(--w3);letter-spacing:.06em;margin-top:5px}}

/* ── LEADERBOARD ── */
.lb-statusbar{{display:flex;justify-content:space-between;align-items:center;font-family:var(--mono);font-size:.46rem;color:var(--w3);letter-spacing:.1em;padding:0 20px 11px}}
.lb-live{{display:flex;align-items:center;gap:6px;color:var(--jade2)}}
.lb-dot{{width:6px;height:6px;border-radius:50%;background:var(--jade2);box-shadow:0 0 8px var(--jade2);animation:lbPulse 1.6s ease-in-out infinite}}
@keyframes lbPulse{{0%,100%{{opacity:.3}}50%{{opacity:1}}}}
.lb-prog{{margin:0 20px 12px}}
.lb-prog-track{{height:4px;border-radius:3px;background:var(--slate);overflow:hidden;border:0.5px solid var(--line2)}}
.lb-prog-fill{{height:100%;background:linear-gradient(90deg,var(--gold),var(--jade2));transition:width .4s ease}}
.lb-prog-txt{{font-family:var(--mono);font-size:.46rem;color:var(--w3);text-align:center;margin-top:6px;letter-spacing:.1em}}
.lb-wrap{{padding:2px 14px 8px}}
.lb-row{{display:flex;align-items:stretch;background:linear-gradient(180deg,var(--graphite),var(--carbon));border:0.5px solid var(--line2);border-radius:14px;margin-bottom:8px;overflow:hidden;position:relative;cursor:pointer;transition:transform .15s,border-color .15s}}
.lb-row:hover{{border-color:var(--gold-dim)}}
.lb-row:hover .lb-go{{color:var(--gold2)}}
.lb-row:active{{transform:scale(.995)}}
.lb-row::before{{content:'';position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--line3)}}
.lb-row.r1::before{{background:linear-gradient(180deg,var(--gold3),var(--gold))}}
.lb-row.r2::before{{background:linear-gradient(180deg,var(--w2),var(--w3))}}
.lb-row.r3::before{{background:linear-gradient(180deg,var(--gold-dim),#5a3f10)}}
.lb-rank{{width:46px;flex-shrink:0;display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:var(--num);background:rgba(255,255,255,.012)}}
.lb-rank b{{font-size:1.18rem;font-weight:500;color:var(--w2);line-height:1}}
.lb-rank.top b{{color:var(--gold3)}}
.lb-rank small{{font-family:var(--mono);font-size:.4rem;color:var(--w4);letter-spacing:.12em;margin-top:3px}}
.lb-mid{{flex:1;min-width:0;padding:11px 10px 11px 6px;display:flex;flex-direction:column;justify-content:center}}
.lb-tk{{font-family:var(--num);font-size:.96rem;font-weight:600;color:var(--w1);letter-spacing:.04em}}
.lb-nm{{font-family:var(--disp);font-size:.72rem;color:var(--w3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px}}
.lb-px{{font-family:var(--mono);font-size:.46rem;color:var(--w3);margin-top:5px;letter-spacing:.05em}}
.lb-val{{flex-shrink:0;padding:11px 10px 11px 8px;display:flex;flex-direction:column;align-items:flex-end;justify-content:center;border-left:0.5px solid var(--line)}}
.lb-go{{display:flex;align-items:center;justify-content:center;padding:0 13px 0 5px;font-size:1.15rem;color:var(--w4);flex-shrink:0;transition:color .15s}}
.lb-margin{{font-family:var(--num);font-size:1.32rem;font-weight:500;color:var(--jade2);line-height:1;white-space:nowrap}}
.lb-tag{{font-family:var(--mono);font-size:.42rem;color:var(--gold);letter-spacing:.1em;margin-top:5px;white-space:nowrap}}
.lb-mos{{font-family:var(--mono);font-size:.42rem;color:var(--w3);letter-spacing:.06em;margin-top:2px}}
/* members lock */
/* members lock — premium blurred teaser */
.lb-locked-head{{display:flex;align-items:center;justify-content:space-between;padding:22px 20px 11px}}
.lb-locked-tag{{font-family:var(--mono);font-size:.46rem;color:var(--gold-dim);letter-spacing:.12em;display:flex;align-items:center;gap:5px}}
.lb-teaser{{position:relative;padding:2px 14px 18px;min-height:478px}}
.lb-teaser-rows{{filter:blur(5px);opacity:.55;pointer-events:none;-webkit-mask-image:linear-gradient(180deg,#000 0%,#000 26%,transparent 90%);mask-image:linear-gradient(180deg,#000 0%,#000 26%,transparent 90%)}}
.lb-ghost{{height:64px;display:flex;align-items:center;background:linear-gradient(180deg,var(--graphite),var(--carbon));border:0.5px solid var(--line2);border-radius:14px;margin-bottom:8px;padding:0 16px;gap:13px;position:relative;overflow:hidden}}
.lb-ghost::before{{content:'';position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--line3)}}
.lb-ghost:nth-child(1)::before{{background:linear-gradient(180deg,var(--gold3),var(--gold))}}
.lb-ghost:nth-child(2)::before{{background:linear-gradient(180deg,var(--w2),var(--w3))}}
.lb-ghost:nth-child(3)::before{{background:linear-gradient(180deg,var(--gold-dim),#5a3f10)}}
.lb-grank{{font-family:var(--num);font-size:1.18rem;color:var(--w3);width:24px;text-align:center;flex-shrink:0}}
.lb-gcol{{flex:1;display:flex;flex-direction:column;gap:7px}}
.lb-bar{{height:9px;border-radius:4px;background:linear-gradient(90deg,var(--slate2),var(--line3),var(--slate2));background-size:200% 100%;animation:lbShine 2.4s linear infinite}}
@keyframes lbShine{{0%{{background-position:200% 0}}100%{{background-position:-200% 0}}}}
.lb-teaser-overlay{{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:flex-start;padding:30px 24px 0}}
.lb-glass{{background:rgba(8,8,8,.66);backdrop-filter:blur(11px);-webkit-backdrop-filter:blur(11px);border:0.5px solid var(--line2);border-radius:22px;padding:26px 22px 24px;max-width:340px;width:100%;text-align:center;position:relative;box-shadow:0 26px 64px rgba(0,0,0,.62),inset 0 0 0 0.5px rgba(200,144,26,.14)}}
.lb-glass::before{{content:'';position:absolute;top:0;left:22px;right:22px;height:1px;background:linear-gradient(90deg,transparent,var(--gold-dim),transparent)}}
.lb-lock-gem{{width:48px;height:48px;margin:2px auto 16px;background:linear-gradient(135deg,var(--gold),var(--gold3));transform:rotate(45deg);display:flex;align-items:center;justify-content:center;box-shadow:0 0 26px rgba(200,144,26,.45);animation:gemPulse 4.5s ease-in-out infinite}}
.lb-lock-gem span{{transform:rotate(-45deg);font-size:1.05rem;color:#000;font-weight:900}}
.lb-lock-title{{font-size:1.34rem;font-weight:300;color:var(--gold3);letter-spacing:.05em;margin-bottom:6px}}
.lb-lock-sub{{font-family:var(--mono);font-size:.48rem;color:var(--w3);letter-spacing:.12em;margin-bottom:16px}}
.lb-glass .paywall-feats{{margin:2px 0 14px;text-align:left}}
.lb-glass-price{{font-family:var(--num);font-size:2.3rem;font-weight:300;color:var(--gold3);line-height:1}}
.lb-glass-price small{{font-size:.8rem;color:var(--w3)}}
.lb-glass-note{{font-family:var(--mono);font-size:.46rem;color:var(--w3);letter-spacing:.08em;margin:7px 0 15px}}
.lb-glass-btn{{width:100%;height:48px;background:linear-gradient(135deg,var(--gold),var(--gold3));border:none;border-radius:12px;font-family:var(--mono);font-size:.62rem;font-weight:700;color:#000;letter-spacing:.12em;cursor:pointer}}
.lb-glass-btn:active{{opacity:.85}}
.lb-glass-foot{{font-family:var(--disp);font-size:.72rem;color:var(--w3);font-style:italic;margin-top:12px}}
</style>
</head>
<body>
<!-- BODY CONTENT INJECTED IN PART 2 -->
<div class="cmd-bar">
  <div class="cmd-id" onclick="goHome()">
    <div class="cmd-gem"></div>
    <div class="cmd-name">SENECA</div>
  </div>
  <div class="cmd-acts">
    <div class="cmd-clock" id="cmd-clock"></div>
    <button class="cmd-btn hidden" id="btn-login" onclick="openModal('login')">SIGN IN</button>
    <button class="cmd-btn hidden" id="btn-acct" onclick="openModal('acct')">◆ ACCT</button>
    <button class="cmd-btn cmd-btn-pro" id="btn-sub" onclick="clickSubscribe('header')">✦ PRO</button>
  </div>
</div>

<div id="pane-oracle" class="pane">
  <div class="input-stage">
    <div class="input-eyebrow">— ENTER SYMBOL TO VALUE —</div>
    <div class="input-field">
      <input id="search" class="input-el" type="text" placeholder="AAPL · MSFT · TSLA" maxlength="12" autocomplete="off" spellcheck="false"/>
    </div>
    <button id="btn-go" class="input-go" onclick="doAnalyze()"><span style="font-size:.62rem">◆</span> CONSULT THE ORACLE</button>
  </div>

  <div class="chip-strip">
    <div class="chip" onclick="setQ('AAPL')"><span class="chip-tick">●</span>AAPL</div>
    <div class="chip" onclick="setQ('NVDA')"><span class="chip-tick">●</span>NVDA</div>
    <div class="chip" onclick="setQ('MSFT')"><span class="chip-tick">●</span>MSFT</div>
    <div class="chip" onclick="setQ('TSLA')"><span class="chip-tick">●</span>TSLA</div>
    <div class="chip" onclick="setQ('GOOGL')"><span class="chip-tick">●</span>GOOGL</div>
    <div class="chip" onclick="setQ('AMZN')"><span class="chip-tick">●</span>AMZN</div>
    <div class="chip" onclick="setQ('KO')"><span class="chip-tick">●</span>KO</div>
    <div class="chip" onclick="setQ('BRK-B')"><span class="chip-tick">●</span>BRK-B</div>
  </div>

  <div class="status-line" id="status">Awaiting symbol · first lookup free</div>

  <div class="spinner" id="spinner">
    <div class="spin-ring"></div>
    <div class="spin-txt">CONSULTING THE ORACLE…</div>
  </div>

  <!-- INTRO / LANDING — shown until first analysis -->
  <div id="intro" class="intro">
    <div class="intro-badge">◆ &nbsp;ONE FREE LOOKUP · NO SIGN-UP</div>
    <div class="intro-headline">What is any company<br/><span>truly worth?</span></div>
    <div class="intro-sub">Seneca runs seven legendary valuation models on any public stock — then hands you a single, decisive verdict the way an institutional terminal would.</div>

    <div class="intro-steps">
      <div class="intro-step">
        <div class="intro-step-num">01</div>
        <div class="intro-step-body">
          <div class="intro-step-title">Enter a ticker</div>
          <div class="intro-step-text">Type a symbol like AAPL or tap a chip above, then hit Consult the Oracle.</div>
        </div>
      </div>
      <div class="intro-step">
        <div class="intro-step-num">02</div>
        <div class="intro-step-body">
          <div class="intro-step-title">Read the Composite</div>
          <div class="intro-step-text">Seven models converge into one Seneca fair-value number — with your margin of safety.</div>
        </div>
      </div>
      <div class="intro-step">
        <div class="intro-step-num">03</div>
        <div class="intro-step-body">
          <div class="intro-step-title">Scroll the full dossier</div>
          <div class="intro-step-text">Financial health, command structure, and an AI final verdict on the business.</div>
        </div>
      </div>
    </div>

    <div class="intro-features">
      <div class="intro-feat"><span class="intro-feat-ic">◆</span> 7 Valuation Models</div>
      <div class="intro-feat"><span class="intro-feat-ic">⚕</span> Forensic Health Score</div>
      <div class="intro-feat"><span class="intro-feat-ic">⊟</span> Leadership Org Tree</div>
      <div class="intro-feat"><span class="intro-feat-ic">⚖</span> AI Verdict</div>
    </div>

    <button class="intro-cta" onclick="document.getElementById('search').focus();document.getElementById('search').scrollIntoView({{behavior:'smooth',block:'center'}})">▲ &nbsp;TRY YOUR FREE LOOKUP</button>
    <div class="intro-foot">No account needed for your first valuation · Unlimited with Pro at $3.99/mo</div>
  </div>

  <div id="results" class="hidden"></div>
</div>

<div id="pane-watch" class="pane hidden">
  <div class="rail-head">
    <div class="rail-title">◈ LIVE WATCHLIST</div>
    <div class="rail-act" id="wl-refresh" onclick="refreshWL()">↻ SYNC</div>
  </div>
  <div class="wl-empty-note" id="wl-note"></div>
  <div class="rail" id="wl-rail" style="flex-wrap:wrap;padding-bottom:8px"></div>

  <div class="rail-head"><div class="rail-title">◈ TRENDING NOW</div></div>
  <div class="rail" id="trend-rail" style="flex-wrap:wrap;padding-bottom:8px"></div>
</div>

<div id="pane-leaders" class="pane hidden">

  <!-- members-only lock (non-subscribers) -->
  <div id="lb-lock" class="hidden">
    <div class="lb-locked-head">
      <div class="rail-title">◈ UNDERVALUATION LEADERBOARD</div>
      <div class="lb-locked-tag">⚿ MEMBERS ONLY</div>
    </div>
    <div class="lb-teaser">
      <div class="lb-teaser-rows">
        <div class="lb-ghost"><div class="lb-grank">1</div><div class="lb-gcol"><div class="lb-bar" style="width:46%"></div><div class="lb-bar" style="width:68%;height:7px;opacity:.6"></div></div><div class="lb-bar" style="width:58px;height:18px"></div></div>
        <div class="lb-ghost"><div class="lb-grank">2</div><div class="lb-gcol"><div class="lb-bar" style="width:38%"></div><div class="lb-bar" style="width:60%;height:7px;opacity:.6"></div></div><div class="lb-bar" style="width:52px;height:18px"></div></div>
        <div class="lb-ghost"><div class="lb-grank">3</div><div class="lb-gcol"><div class="lb-bar" style="width:52%"></div><div class="lb-bar" style="width:72%;height:7px;opacity:.6"></div></div><div class="lb-bar" style="width:60px;height:18px"></div></div>
        <div class="lb-ghost"><div class="lb-grank">4</div><div class="lb-gcol"><div class="lb-bar" style="width:42%"></div><div class="lb-bar" style="width:64%;height:7px;opacity:.6"></div></div><div class="lb-bar" style="width:50px;height:18px"></div></div>
        <div class="lb-ghost"><div class="lb-grank">5</div><div class="lb-gcol"><div class="lb-bar" style="width:48%"></div><div class="lb-bar" style="width:56%;height:7px;opacity:.6"></div></div><div class="lb-bar" style="width:54px;height:18px"></div></div>
        <div class="lb-ghost"><div class="lb-grank">6</div><div class="lb-gcol"><div class="lb-bar" style="width:40%"></div><div class="lb-bar" style="width:66%;height:7px;opacity:.6"></div></div><div class="lb-bar" style="width:48px;height:18px"></div></div>
      </div>
      <div class="lb-teaser-overlay">
        <div class="lb-glass">
          <div class="lb-lock-gem"><span>⚿</span></div>
          <div class="lb-lock-title">Members Only</div>
          <div class="lb-lock-sub">LIVE NYSE OPPORTUNITY INDEX</div>
          <div class="paywall-feats">
            <div class="pf-row"><span class="pf-check">✓</span> Top 15 most undervalued NYSE names</div>
            <div class="pf-row"><span class="pf-check">✓</span> Ranked by Seneca Composite margin of safety</div>
            <div class="pf-row"><span class="pf-check">✓</span> Auto-scanned &amp; refreshed continuously</div>
            <div class="pf-row"><span class="pf-check">✓</span> Plus everything in Seneca Pro</div>
          </div>
          <div class="lb-glass-price">$3.99<small>/mo</small></div>
          <div class="lb-glass-note">UNLIMITED ACCESS · CANCEL ANYTIME</div>
          <button class="lb-glass-btn" onclick="clickSubscribe('leaderboard_paywall')">✦ UNLOCK SENECA PRO</button>
          <div class="lb-glass-foot">Already a member? <a onclick="openModal('login')">Sign in</a></div>
        </div>
      </div>
    </div>
  </div>

  <!-- leaderboard content (subscribers) -->
  <div id="lb-content" class="hidden">
    <div class="rail-head">
      <div class="rail-title">◈ UNDERVALUATION LEADERBOARD</div>
      <div class="rail-act" id="lb-sync" onclick="loadLeaderboard(true)">↻ SYNC</div>
    </div>
    <div class="lb-statusbar">
      <span class="lb-live"><span class="lb-dot"></span><span id="lb-livetxt">CONNECTING…</span></span>
      <span>TOP 15 · NYSE · BY MARGIN OF SAFETY</span>
    </div>
    <div class="lb-prog hidden" id="lb-prog">
      <div class="lb-prog-track"><div class="lb-prog-fill" id="lb-progfill" style="width:0%"></div></div>
      <div class="lb-prog-txt" id="lb-progtxt">Scanning…</div>
    </div>
    <div class="lb-wrap" id="lb-rows"></div>
  </div>

</div>

<div id="pane-minds" class="pane hidden">
  <div class="rail-head"><div class="rail-title">◈ MINDS BEHIND THE MODELS</div></div>
  <div id="minds-list" style="padding:0 14px 8px"></div>
</div>

<div id="pane-about" class="pane hidden">
  <div class="about-pane">
    <div class="about-gem-wrap"><div class="about-gem"></div></div>
    <div class="about-title">SENECA</div>
    <div class="about-tag">INTRINSIC VALUE ORACLE</div>
    <div class="about-body">Named for the Stoic philosopher and the Seneca Nation — keepers of wisdom. This oracle applies seven time-tested valuation frameworks to reveal what a company is truly worth beneath the market's noise.</div>

    <div class="formula-card"><div class="formula-name">Graham Number</div><div class="formula-eq">√( 22.5 × EPS × Book Value )</div><div class="formula-desc">Ben Graham's bedrock — the geometric mean of earnings and asset value.</div></div>
    <div class="formula-card"><div class="formula-name">Graham Growth</div><div class="formula-eq">EPS × (8.5 + 2g) × 4.4 / AAA yield</div><div class="formula-desc">Extends Graham for growth relative to bond yields.</div></div>
    <div class="formula-card"><div class="formula-name">Buffett DCF</div><div class="formula-eq">10yr EPS @ 9% · 15× terminal</div><div class="formula-desc">Discounts a decade of projected earnings to present value.</div></div>
    <div class="formula-card"><div class="formula-name">Peter Lynch PEG</div><div class="formula-eq">EPS × growth% (PEG = 1)</div><div class="formula-desc">A fair P/E equals the earnings growth rate.</div></div>
    <div class="formula-card"><div class="formula-name">Simons Quant</div><div class="formula-eq">ROE/PE × (1/PB) × momentum</div><div class="formula-desc">Renaissance-style multi-factor quality + momentum signal.</div></div>
    <div class="formula-card"><div class="formula-name">Free Cash Flow DCF</div><div class="formula-eq">10yr FCF @ 10% · 2.5% terminal</div><div class="formula-desc">Pure cash generation discounted to today.</div></div>
    <div class="formula-card"><div class="formula-name">Gordon Growth DDM</div><div class="formula-eq">D1 ÷ (CAPM rate − div growth)</div><div class="formula-desc">For dividend-payers — values the future dividend stream.</div></div>

    <div class="about-pricing">
      <div class="about-price">$3.99<small>/mo</small></div>
      <div class="about-price-note">UNLIMITED LOOKUPS · CANCEL ANYTIME</div>
      <button class="about-sub-btn" onclick="clickSubscribe('about_page')">✦ UNLOCK SENECA PRO</button>
    </div>
    <div class="disc"><p>✦ Seneca is for educational and research purposes only. Nothing here constitutes financial advice. Always conduct your own due diligence.</p></div>
  </div>
</div>

<div class="dock">
  <div class="dock-item on" id="dock-oracle" onclick="switchPane('oracle')"><i class="dock-ic">◆</i><div class="dock-lb">ORACLE</div></div>
  <div class="dock-item" id="dock-watch" onclick="switchPane('watch')"><i class="dock-ic">◈</i><div class="dock-lb">WATCH</div></div>
  <div class="dock-item" id="dock-leaders" onclick="switchPane('leaders')"><i class="dock-ic">▲</i><div class="dock-lb">LEADERS</div></div>
  <div class="dock-item" id="dock-minds" onclick="switchPane('minds')"><i class="dock-ic">✦</i><div class="dock-lb">MINDS</div></div>
  <div class="dock-item" id="dock-about" onclick="switchPane('about')"><i class="dock-ic">◇</i><div class="dock-lb">ABOUT</div></div>
</div>

<div class="toast" id="toast"></div>

<div class="modal" id="modal-pay">
  <div class="modal-card">
    <div class="modal-title">✦ Unlock Seneca Pro</div>
    <div class="modal-sub">YOUR FREE LOOKUP IS USED</div>
    <div class="paywall-feats">
      <div class="pf-row"><span class="pf-check">✓</span> Unlimited valuations</div>
      <div class="pf-row"><span class="pf-check">✓</span> AI analysis & financial forensics</div>
      <div class="pf-row"><span class="pf-check">✓</span> Live watchlist with real-time prices</div>
      <div class="pf-row"><span class="pf-check">✓</span> All seven valuation models</div>
    </div>
    <div class="modal-price"><div class="modal-price-num">$3.99</div><div class="modal-price-per">PER MONTH · CANCEL ANYTIME</div></div>
    <button class="btn-big" id="pay-btn" onclick="launchStripe()">✦ SUBSCRIBE NOW</button>
    <button class="btn-ghost-modal" onclick="closeModal('modal-pay')">Maybe later</button>
  </div>
</div>

<div class="modal" id="modal-signup">
  <div class="modal-card">
    <div class="modal-title">Create Account</div>
    <div class="modal-sub">SECURE YOUR ORACLE ACCESS</div>
    <div class="modal-err" id="signup-err"></div>
    <input class="modal-input" id="signup-email" type="email" placeholder="Email" autocomplete="email"/>
    <input class="modal-input" id="signup-pw" type="password" placeholder="Password" autocomplete="new-password"/>
    <button class="btn-big" onclick="doSignup()">CREATE ACCOUNT & SUBSCRIBE</button>
    <button class="btn-ghost-modal" onclick="closeModal('modal-signup')">Cancel</button>
    <div class="modal-switch">Already have an account? <a onclick="switchModal('modal-signup','modal-login')">Sign in</a></div>
  </div>
</div>

<div class="modal" id="modal-login">
  <div class="modal-card">
    <div class="modal-title">Welcome Back</div>
    <div class="modal-sub">SIGN IN TO YOUR ORACLE</div>
    <div class="modal-err" id="login-err"></div>
    <input class="modal-input" id="login-email" type="email" placeholder="Email" autocomplete="email"/>
    <input class="modal-input" id="login-pw" type="password" placeholder="Password" autocomplete="current-password"/>
    <label class="remember-row"><input type="checkbox" id="login-remember" checked/><span>Keep me signed in for 30 days</span></label>
    <button class="btn-big" onclick="doLogin()">SIGN IN</button>
    <button class="btn-ghost-modal" onclick="closeModal('modal-login')">Cancel</button>
    <div class="modal-switch">No account yet? <a onclick="switchModal('modal-login','modal-signup')">Create one</a></div>
  </div>
</div>

<div class="modal" id="modal-acct">
  <div class="modal-card">
    <div class="modal-title">Account</div>
    <div class="modal-acct-email" id="acct-email"></div>
    <div class="modal-acct-status" id="acct-status"></div>
    <button class="btn-big" id="acct-sub-btn" onclick="closeModal('modal-acct');launchStripe()" style="display:none">✦ SUBSCRIBE NOW — $3.99/mo</button>
    <button class="btn-ghost-modal" onclick="doLogout()">Sign Out</button>
    <button class="btn-ghost-modal" onclick="closeModal('modal-acct')">Close</button>
  </div>
</div>

<script>
let userEmail = {json.dumps(email)};
let userSub   = {'true' if sub else 'false'};
let watchlist = [];
let lastTicker = '';

const DEFAULT_INDEXES = [
  {{ticker:'SPY', name:'S&P 500'}},
  {{ticker:'QQQ', name:'Nasdaq 100'}},
  {{ticker:'DIA', name:'Dow Jones'}},
  {{ticker:'IWM', name:'Russell 2000'}},
  {{ticker:'GLD', name:'Gold'}},
  {{ticker:'TLT', name:'Treasuries'}},
];
const TRENDING = [
  {{ticker:'NVDA', name:'Nvidia'}},
  {{ticker:'AAPL', name:'Apple'}},
  {{ticker:'TSLA', name:'Tesla'}},
  {{ticker:'MSFT', name:'Microsoft'}},
  {{ticker:'AMZN', name:'Amazon'}},
  {{ticker:'META', name:'Meta'}},
  {{ticker:'GOOGL',name:'Alphabet'}},
  {{ticker:'AMD',  name:'AMD'}},
  {{ticker:'PLTR', name:'Palantir'}},
  {{ticker:'COIN', name:'Coinbase'}},
  {{ticker:'NFLX', name:'Netflix'}},
  {{ticker:'BRK-B',name:'Berkshire'}},
];

const INVESTORS = [
  {{initials:'BG',name:'Benjamin Graham',years:'1894–1976',tag:'FATHER OF VALUE',bio:'Wrote the bible of investing. Invented intrinsic value and margin of safety. Mentored Warren Buffett.',quote:'The investor\u2019s chief problem is likely to be himself.'}},
  {{initials:'WB',name:'Warren Buffett',years:'1930–',tag:'ORACLE OF OMAHA',bio:'Built Berkshire from a failing mill into a $900B empire averaging ~20% annual returns for 60 years.',quote:'Price is what you pay. Value is what you get.'}},
  {{initials:'PL',name:'Peter Lynch',years:'1944–',tag:'PEG PIONEER',bio:'Ran Fidelity Magellan to 29% annual returns. Believed ordinary people had an edge over Wall Street.',quote:'Know what you own, and know why you own it.'}},
  {{initials:'JT',name:'John Templeton',years:'1912–2008',tag:'GLOBAL PIONEER',bio:'Bought 100 shares of every NYSE stock under $1 in 1939 and made a fortune. Built the first global fund.',quote:'The time of maximum pessimism is the best time to buy.'}},
  {{initials:'JS',name:'Jim Simons',years:'1938–2024',tag:'THE QUANT KING',bio:'Renaissance\u2019s Medallion Fund averaged 66% annually for decades using pure mathematics and signals.',quote:'We don\u2019t override the models. The models know.'}},
  {{initials:'CM',name:'Charlie Munger',years:'1924–2023',tag:'MENTAL MODELS',bio:'Buffett\u2019s partner for 45 years. Shifted Berkshire toward quality businesses at fair prices.',quote:'Invert, always invert.'}},
];

const priceCache = {{}};

// ── clock ──
(function tick(){{
  const el=document.getElementById('cmd-clock');
  if(el) el.textContent=new Date().toLocaleTimeString([],{{hour:'2-digit',minute:'2-digit'}});
  setTimeout(tick,1000);
}})();

// ── init ──
window.addEventListener('DOMContentLoaded',()=>{{
  if(userEmail){{
    fetch('/api/watchlist').then(r=>r.json()).then(d=>{{ if(d.ok){{watchlist=d.watchlist||[];renderWLBar();}} }}).catch(()=>{{}});
  }} else {{
    try{{ watchlist=JSON.parse(sessionStorage.getItem('wl')||'[]'); }}catch(e){{ watchlist=[]; }}
  }}
  syncHeader();
  renderMinds();
  {toast_js}
  {purchase_js}
}});

// ── pane switching ──
function switchPane(p){{
  ['oracle','watch','leaders','minds','about'].forEach(x=>{{
    document.getElementById('pane-'+x).classList.toggle('hidden',x!==p);
    document.getElementById('dock-'+x).classList.toggle('on',x===p);
  }});
  if(p==='watch') renderWLPage();
  else if(p==='leaders') enterLeaders();
  else stopLbPoll();
  window.scrollTo({{top:0,behavior:'smooth'}});
}}
function goHome(){{ switchPane('oracle'); }}

// ── members' leaderboard ──
let lbTimer=null;
const LB_FAST=3000, LB_SLOW=60000;

function enterLeaders(){{
  const lock=document.getElementById('lb-lock');
  const content=document.getElementById('lb-content');
  if(!userSub){{
    track('leaderboard_locked_view');
    lock.classList.remove('hidden');
    content.classList.add('hidden');
    stopLbPoll();
    return;
  }}
  track('leaderboard_view');
  lock.classList.add('hidden');
  content.classList.remove('hidden');
  loadLeaderboard(true);
}}

function stopLbPoll(){{ if(lbTimer){{ clearTimeout(lbTimer); lbTimer=null; }} }}

function scheduleLb(status){{
  stopLbPoll();
  lbTimer=setTimeout(()=>loadLeaderboard(false), status==='ready'?LB_SLOW:LB_FAST);
}}

async function loadLeaderboard(spin){{
  if(!userSub) return;
  const btn=document.getElementById('lb-sync');
  if(spin&&btn) btn.textContent='↻ SYNCING';
  try{{
    const r=await fetch('/api/leaderboard');
    if(r.status===402){{ userSub=false; syncHeader(); enterLeaders(); return; }}
    if(!r.ok) throw new Error('err');
    const d=await r.json();
    paintLeaders(d);
    scheduleLb(d.status);
  }}catch(e){{
    scheduleLb('building');
  }}finally{{
    if(btn) btn.textContent='↻ SYNC';
  }}
}}

function lbAgo(ts){{
  if(!ts) return '—';
  const s=Math.max(0,Math.floor(Date.now()/1000-ts));
  if(s<60) return s+'s ago';
  if(s<3600) return Math.floor(s/60)+'m ago';
  return Math.floor(s/3600)+'h ago';
}}

function paintLeaders(d){{
  const live=document.getElementById('lb-livetxt');
  const prog=document.getElementById('lb-prog');
  if(d.status==='building'){{
    prog.classList.remove('hidden');
    const pct=d.total?Math.round(d.scanned/d.total*100):0;
    document.getElementById('lb-progfill').style.width=pct+'%';
    document.getElementById('lb-progtxt').textContent='SCANNING NYSE UNIVERSE · '+d.scanned+' / '+d.total;
    live.textContent='BUILDING INDEX';
  }} else if(d.status==='error'){{
    prog.classList.add('hidden'); live.textContent='SCAN ERROR · RETRYING';
  }} else {{
    prog.classList.add('hidden'); live.textContent='LIVE · UPDATED '+lbAgo(d.updated);
  }}
  const wrap=document.getElementById('lb-rows');
  if(!d.rows||!d.rows.length){{
    if(d.status!=='building') wrap.innerHTML='<div class="lb-prog-txt" style="padding:26px 0">No undervalued names in range right now. Check back after the next sync.</div>';
    return;
  }}
  wrap.innerHTML=d.rows.map((row,i)=>{{
    const rank=i+1;
    const rc=rank<=3?('r'+rank):'';
    const tc=rank<=3?'top':'';
    const tag=rank===1?'DEEPEST VALUE':(row.margin>=30?'DEEP VALUE':'UNDERVALUED');
    return `<div class="lb-row ${{rc}}" onclick="lbOpen('${{row.ticker}}')" title="Open ${{row.ticker}} in the Oracle">
      <div class="lb-rank ${{tc}}"><b>${{rank}}</b><small>RANK</small></div>
      <div class="lb-mid">
        <div class="lb-tk">${{row.ticker}}</div>
        <div class="lb-nm">${{row.name}}</div>
        <div class="lb-px">PRICE ${{fp(row.price)}} · FAIR ${{fp(row.composite)}}</div>
      </div>
      <div class="lb-val">
        <div class="lb-margin">▲ ${{row.margin.toFixed(0)}}%</div>
        <div class="lb-tag">✦ ${{tag}}</div>
        <div class="lb-mos">margin of safety</div>
      </div>
      <div class="lb-go">›</div>
    </div>`;
  }}).join('');
}}

// ── header sync ──
function syncHeader(){{
  const login=document.getElementById('btn-login');
  const acct=document.getElementById('btn-acct');
  const sub=document.getElementById('btn-sub');
  if(userEmail){{
    login.classList.add('hidden'); acct.classList.remove('hidden');
    sub.style.display=userSub?'none':'';
  }} else {{
    login.classList.remove('hidden'); acct.classList.add('hidden');
    sub.style.display='';
  }}
}}

// ── input ──
function track(n,p){{ try{{ if(window.gtag) gtag('event', n, p||{{}}); }}catch(e){{}} }}
function lbOpen(t){{ track('leaderboard_row_click',{{ticker:t}}); setQ(t); }}
function setQ(v){{ document.getElementById('search').value=v; doAnalyze(); }}
document.getElementById('search').addEventListener('keydown',e=>{{ if(e.key==='Enter'){{e.preventDefault();doAnalyze();}} }});
document.getElementById('search').addEventListener('input',e=>{{ e.target.value=e.target.value.toUpperCase(); }});

// ── ANALYZE ──
async function doAnalyze(){{
  const t=document.getElementById('search').value.trim().toUpperCase();
  if(!t) return;
  switchPane('oracle');
  lastTicker=t;
  document.getElementById('btn-go').disabled=true;
  document.getElementById('results').classList.add('hidden');
  const _intro=document.getElementById('intro'); if(_intro) _intro.classList.add('hidden');
  document.getElementById('spinner').classList.add('on');
  setStatus('Consulting the oracle for '+t+'…','var(--gold)');
  try{{
    const r=await fetch('/api/quote?q='+encodeURIComponent(t));
    if(r.status===402){{ document.getElementById('spinner').classList.remove('on'); document.getElementById('btn-go').disabled=false; setStatus('Free lookup used','var(--w3)'); track('paywall_hit',{{source:'oracle_lookup',ticker:t}}); openModal('pay'); return; }}
    if(!r.ok){{ const e=await r.json(); throw new Error(e.error||'Server error'); }}
    const d=await r.json();
    render(d);
    setStatus('Analysis complete · '+d.ticker+' · '+new Date().toLocaleTimeString(),'var(--jade)');
    track('consult_oracle',{{ticker:d.ticker,asset_type:d.asset_type||'stock'}});
    loadAI(d.ticker);
    loadHealthAI(d.ticker);
    loadLeadership(d.ticker);
  }}catch(e){{
    setStatus('⚠ '+e.message,'var(--blood2)');
    document.getElementById('results').innerHTML='<div class="ai-block" style="border-color:var(--blood)"><div class="ai-body" style="color:var(--blood2);font-style:normal">⚠ '+e.message+'</div></div>';
    document.getElementById('results').classList.remove('hidden');
  }}finally{{
    document.getElementById('spinner').classList.remove('on');
    document.getElementById('btn-go').disabled=false;
  }}
}}

function render(d){{
  const p=d.price;
  const cls=d.verdict_cls==='up'?'up':d.verdict_cls==='down'?'down':'fair';
  let comp=d.composite;
  let marginPct=(comp&&comp>0&&p)?((comp-p)/p*100):null;
  const pct=(d.hi52>d.lo52&&d.lo52>0)?Math.min(Math.max((p-d.lo52)/(d.hi52-d.lo52),0),1)*100:null;

  // verdict highlight word
  let vmain=d.verdict_text||'Insufficient data';
  let vword='', vrest=vmain;
  const vm=vmain.replace('✦','').trim();
  let html='';

  // ── HERO: VERDICT STAGE ──
  html+=`<div class="divider"><div class="divider-line"></div><div class="divider-txt">ORACLE VERDICT</div><div class="divider-line r"></div></div>`;
  html+=`<div class="verdict-stage">
    <div class="vs-glow ${{cls}}"></div>
    <div class="vs-strip ${{cls}}"></div>
    <div class="vs-co">${{d.name}} · ${{d.ticker}}</div>
    <div class="vs-sector">${{(d.sector||'—').toUpperCase()}} · ${{(d.asset_type||'stock').toUpperCase()}}</div>
    <div class="vs-comp-label">◆ SENECA COMPOSITE FAIR VALUE</div>
    <div class="vs-comp">${{comp&&comp>0?fp(comp):'N/A'}}</div>
    <div class="vs-comp-sub">WEIGHTED SYNTHESIS OF SEVEN MODELS</div>`;
  if(marginPct!==null){{
    const ms=marginPct>=0?'▲ '+marginPct.toFixed(1)+'% MARGIN OF SAFETY':'▼ '+Math.abs(marginPct).toFixed(1)+'% ABOVE FAIR VALUE';
    html+=`<div class="vs-margin ${{cls}}">${{ms}}</div>`;
  }} else {{ html+=`<div class="vs-margin fair">—</div>`; }}

  html+=`<div class="vs-verdict ${{cls}}">
    <div class="vs-verdict-main"><span class="hl-${{cls}}">${{vm}}</span></div>
    <div class="vs-verdict-sub">${{d.verdict_detail||'Six-model consensus vs. current price.'}}</div>
  </div>`;

  // current stats
  html+=`<div class="vs-stats">
    <div class="vs-stat"><div class="vs-stat-lbl">MARKET PRICE</div><div class="vs-stat-val">${{fp(p)}}</div></div>
    <div class="vs-stat"><div class="vs-stat-lbl">TODAY</div><div class="vs-stat-val ${{d.chg>=0?'up':'down'}}">${{d.chg>=0?'▲':'▼'}} ${{Math.abs(d.chg).toFixed(2)}}%</div></div>
    <div class="vs-stat"><div class="vs-stat-lbl">52W POS</div><div class="vs-stat-val">${{pct!==null?pct.toFixed(0)+'%':'—'}}</div></div>
  </div>`;

  // range
  html+=`<div class="vs-range">
    <div class="vs-range-ends">
      <span class="vs-range-lo">${{d.lo52>0?'$'+d.lo52.toFixed(2):'—'}}</span>
      <span class="vs-range-mid">52-WEEK RANGE</span>
      <span class="vs-range-hi">${{d.hi52>0?'$'+d.hi52.toFixed(2):'—'}}</span>
    </div>
    <div class="vs-track"><div class="vs-fill" style="width:${{pct!==null?pct:0}}%"></div><div class="vs-pin" style="left:${{pct!==null?pct:0}}%"></div></div>
  </div>`;

  // ── MODEL MATRIX ──
  html+=`<div class="matrix-head"><span>VALUATION MODEL</span><span>FAIR VALUE · Δ</span></div>`;
  html+=d.models.map((m,mi)=>{{
    const v=m.value;
    const sc=m.sig_cls==='up'?'up':m.sig_cls==='down'?'down':(m.sig_cls==='na'?'na':'fair');
    let delta='—', barW=15;
    if(v&&v>0&&p){{ const dm=(v-p)/p*100; delta=(dm>=0?'+':'')+dm.toFixed(0)+'%'; barW=Math.min(Math.max(50+dm,8),98); }}
    return `<div class="mx-row">
      <div class="mx-bar ${{sc}}" style="width:${{barW}}%;animation-delay:${{180+mi*85}}ms"></div>
      <div class="mx-name">${{m.name}}</div>
      <div class="mx-val ${{sc}}">${{v&&v>0?fp(v):'N/A'}}</div>
      <div class="mx-delta ${{sc}}">${{delta}}</div>
    </div>`;
  }}).join('');
  html+=`</div>`; // close verdict-stage

  // ── HEALTH CARD ──
  if(d.health){{
    const h=d.health;
    const g=h.grade||'C';
    html+=`<div class="health-block reveal">
      <div class="health-top">
        <div class="health-title">◆ FINANCIAL HEALTH</div>
        <div class="health-gr">
          <div class="health-score">${{h.score}}<small>/100</small></div>
          <div class="health-grade ${{g}}">${{g}}</div>
        </div>
      </div>
      <div class="health-bar"><div class="health-bar-fill ${{g}}" style="width:${{h.score}}%"></div></div>`;
    const bd=h.breakdown||{{}};
    const keys=Object.keys(bd);
    if(keys.length){{
      html+=`<div class="health-grid">`+keys.slice(0,8).map(k=>`<div class="hg-item"><span class="hg-k">${{k}}</span><span class="hg-v">${{bd[k]}}</span></div>`).join('')+`</div>`;
    }}
    if(h.flags&&h.flags.length){{
      html+=`<div class="health-flags">`+h.flags.map(f=>`<span class="health-flag">⚠ ${{f}}</span>`).join('')+`</div>`;
    }}
    // health AI placeholder
    html+=`<div id="health-ai" style="border-top:0.5px solid var(--line)">
      <div class="ai-head"><div class="ai-ic jade">⚕</div><div class="ai-title">SENECA FORENSIC ANALYSIS</div></div>
      <div class="ai-loading" id="health-ai-load"><div class="dot"></div><div class="dot"></div><div class="dot"></div><span class="ai-loading-txt">Probing for hidden risks…</span></div>
    </div>`;
    html+=`</div>`;
  }}

  // ── LEADERSHIP / GOVERNANCE (lazy) — comes BEFORE final verdict ──
  html+=`<div id="lead-mount" class="reveal"><div class="lead-block"><div class="lead-head"><div class="lead-title"><div class="lead-ic">⊟</div>COMMAND STRUCTURE</div></div><div class="ai-loading"><div class="dot"></div><div class="dot"></div><div class="dot"></div><span class="ai-loading-txt">Mapping the org chart…</span></div></div></div>`;

  // ── AI VERDICT CARD — the closing word ──
  html+=`<div class="ai-block reveal">
    <div class="ai-head"><div class="ai-ic">◆</div><div class="ai-title">SENECA AI · FINAL VERDICT</div></div>
    <div id="ai-verdict"><div class="ai-loading"><div class="dot"></div><div class="dot"></div><div class="dot"></div><span class="ai-loading-txt">The oracle is contemplating…</span></div></div>
  </div>`;

  // ── ACTIONS ──
  const inWL=watchlist.includes(d.ticker);
  html+=`<div class="actions reveal">
    <button class="act-btn ${{inWL?'saved':''}}" id="wl-toggle" onclick="toggleWL('${{d.ticker}}')">${{inWL?'★ SAVED':'☆ ADD TO WATCHLIST'}}</button>
    <button class="act-btn" onclick="downloadReport('${{d.ticker}}')">⬇ REPORT</button>
  </div>`;

  document.getElementById('results').innerHTML=html;
  document.getElementById('results').classList.remove('hidden');
  if(comp&&comp>0) countUp(document.querySelector('.vs-comp'), comp);
  initReveal();
}}

// ── Composite count-up — the number resolves like a revelation ──
function countUp(el, target){{
  if(!el||!target||target<=0) return;
  if(window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const dur=1100, t0=performance.now();
  const fmt=v=>'$'+v.toLocaleString('en-US',{{minimumFractionDigits:2,maximumFractionDigits:2}});
  function step(now){{
    const p=Math.min((now-t0)/dur,1);
    const e=1-Math.pow(1-p,3);
    el.textContent=fmt(target*e);
    if(p<1) requestAnimationFrame(step); else el.textContent=fmt(target);
  }}
  requestAnimationFrame(step);
}}

// ── AI loaders ──
async function loadAI(t){{
  try{{
    const r=await fetch('/api/ai?q='+encodeURIComponent(t));
    const d=await r.json();
    const el=document.getElementById('ai-verdict');
    if(el) el.innerHTML=d.verdict?`<div class="ai-body">${{d.verdict}}</div>`:`<div class="ai-body" style="color:var(--w3)">AI analysis unavailable. The seven models above remain your guide.</div>`;
  }}catch(e){{
    const el=document.getElementById('ai-verdict');
    if(el) el.innerHTML=`<div class="ai-body" style="color:var(--w3)">AI analysis unavailable.</div>`;
  }}
}}
async function loadHealthAI(t){{
  try{{
    const r=await fetch('/api/health-ai?q='+encodeURIComponent(t));
    const d=await r.json();
    const el=document.getElementById('health-ai-load');
    if(el) el.outerHTML=d.analysis?`<div class="ai-body">${{d.analysis}}</div>`:`<div class="ai-body" style="color:var(--w3)">Forensic analysis unavailable.</div>`;
  }}catch(e){{
    const el=document.getElementById('health-ai-load');
    if(el) el.outerHTML=`<div class="ai-body" style="color:var(--w3)">Forensic analysis unavailable.</div>`;
  }}
}}

// ── LEADERSHIP ──
function fmtPay(v){{ if(!v||v<=0) return '—'; if(v>=1e9) return '$'+(v/1e9).toFixed(1)+'B'; if(v>=1e6) return '$'+(v/1e6).toFixed(1)+'M'; if(v>=1e3) return '$'+(v/1e3).toFixed(0)+'K'; return '$'+v.toFixed(0); }}
function fmtEmp(v){{ if(!v||v<=0) return '—'; if(v>=1e6) return (v/1e6).toFixed(1)+'M'; if(v>=1e3) return (v/1e3).toFixed(0)+'K'; return ''+v; }}

function execNode(p,isCeo){{
  const bcls=p.badge==='CHAIR'?'chair':(['VP','EXEC','DIRECTOR'].includes(p.badge)?p.badge.toLowerCase():'');
  const ncls=isCeo?'ceo':(p.badge==='CHAIR'?'chair':'');
  return `<div class="exec-node ${{ncls}}">
    <div class="exec-top">
      <div class="exec-avatar">${{p.initials}}</div>
      <div class="exec-badge ${{bcls}}">${{p.badge}}</div>
    </div>
    <div class="exec-name">${{p.name}}</div>
    <div class="exec-title">${{p.title}}</div>
    <div class="exec-foot">
      <div class="exec-metric"><div class="exec-metric-l">AGE</div><div class="exec-metric-v">${{p.age||'—'}}</div></div>
      <div class="exec-metric"><div class="exec-metric-l">COMP</div><div class="exec-metric-v">${{fmtPay(p.pay)}}</div></div>
    </div>
  </div>`;
}}

async function loadLeadership(t){{
  let d;
  try{{
    const r=await fetch('/api/leadership?q='+encodeURIComponent(t));
    if(!r.ok) throw new Error('x');
    d=await r.json();
    if(d.error) throw new Error(d.error);
  }}catch(e){{
    const m=document.getElementById('lead-mount');
    if(m) m.innerHTML=`<div class="lead-block"><div class="lead-head"><div class="lead-title"><div class="lead-ic">⊟</div>COMMAND STRUCTURE</div></div><div class="ai-body" style="color:var(--w3)">Leadership data unavailable for this symbol.</div></div>`;
    return;
  }}
  const ppl=d.people||[];
  if(!ppl.length){{
    const m=document.getElementById('lead-mount');
    if(m) m.innerHTML=`<div class="lead-block"><div class="lead-head"><div class="lead-title"><div class="lead-ic">⊟</div>COMMAND STRUCTURE</div></div><div class="ai-body" style="color:var(--w3)">No executive disclosures available for this symbol.</div></div>`;
    return;
  }}
  // tier groups
  const chair=ppl.filter(p=>p.rank===0);
  const ceo=ppl.filter(p=>p.rank===1);
  const csuite=ppl.filter(p=>p.rank===2);
  const board=ppl.filter(p=>p.rank>=3);

  let tree='';
  if(chair.length){{ tree+=`<div class="tree-row ${{chair.length>1?'multi':''}}">${{chair.map(p=>execNode(p,false)).join('')}}</div><div class="tree-connector"></div>`; }}
  if(ceo.length){{ tree+=`<div class="tree-row ${{ceo.length>1?'multi':''}}">${{ceo.map(p=>execNode(p,true)).join('')}}</div>`; if(csuite.length) tree+=`<div class="tree-connector"></div>`; }}
  if(csuite.length){{ tree+=`<div class="tree-row ${{csuite.length>1?'multi':''}}">${{csuite.map(p=>execNode(p,false)).join('')}}</div>`; }}

  let boardHtml='';
  if(board.length){{
    boardHtml=`<div class="board-block"><div class="board-label">◈ EXTENDED LEADERSHIP & DIRECTORS · ${{board.length}}</div><div class="board-grid">`+
      board.map(p=>`<div class="board-chip"><div class="board-chip-av">${{p.initials}}</div><div class="board-chip-name">${{p.name}} · <span style="color:var(--w3)">${{p.badge}}</span></div></div>`).join('')+`</div></div>`;
  }}

  const loc=[d.city,d.state].filter(Boolean).join(', ');
  const m=document.getElementById('lead-mount');
  m.innerHTML=`<div class="lead-block">
    <div class="lead-head">
      <div class="lead-title"><div class="lead-ic">⊟</div>COMMAND STRUCTURE</div>
      <div class="lead-meta">${{loc||d.country||''}}</div>
    </div>
    <div class="lead-stats">
      <div class="lead-stat"><div class="lead-stat-v">${{d.officer_count}}</div><div class="lead-stat-l">DISCLOSED EXECS</div></div>
      <div class="lead-stat"><div class="lead-stat-v">${{fmtEmp(d.employees)}}</div><div class="lead-stat-l">EMPLOYEES</div></div>
      <div class="lead-stat"><div class="lead-stat-v">${{d.sector!=='—'?d.sector.split(' ')[0].slice(0,9):'—'}}</div><div class="lead-stat-l">SECTOR</div></div>
    </div>
    <div class="tree">${{tree}}</div>
    ${{boardHtml}}
    <div id="gov-ai" style="border-top:0.5px solid var(--line)">
      <div class="ai-head"><div class="ai-ic jade">⚖</div><div class="ai-title">GOVERNANCE ASSESSMENT</div></div>
      <div class="ai-loading" id="gov-ai-load"><div class="dot"></div><div class="dot"></div><div class="dot"></div><span class="ai-loading-txt">Evaluating management quality…</span></div>
    </div>
  </div>`;
  loadGovernanceAI(t);
}}

async function loadGovernanceAI(t){{
  try{{
    const r=await fetch('/api/governance-ai?q='+encodeURIComponent(t));
    const d=await r.json();
    const el=document.getElementById('gov-ai-load');
    if(el) el.outerHTML=d.analysis?`<div class="ai-body">${{d.analysis}}</div>`:`<div class="ai-body" style="color:var(--w3)">Governance assessment unavailable.</div>`;
  }}catch(e){{
    const el=document.getElementById('gov-ai-load');
    if(el) el.outerHTML=`<div class="ai-body" style="color:var(--w3)">Governance assessment unavailable.</div>`;
  }}
}}


function downloadReport(t){{ window.open('/api/pdf?q='+encodeURIComponent(t),'_blank'); }}

// ── Scroll reveal — sections fade up as they enter view ──
let _revealObs=null;
function initReveal(){{
  if(_revealObs) _revealObs.disconnect();
  const els=document.querySelectorAll('#results .reveal');
  if(!('IntersectionObserver' in window)){{ els.forEach(e=>e.classList.add('shown')); return; }}
  _revealObs=new IntersectionObserver((entries)=>{{
    entries.forEach(en=>{{ if(en.isIntersecting){{ en.target.classList.add('shown'); _revealObs.unobserve(en.target); }} }});
  }},{{root:null,rootMargin:'0px 0px -8% 0px',threshold:0.12}});
  els.forEach((e,i)=>{{
    // first block (already in view) reveals immediately with a tiny stagger
    if(i===0){{ setTimeout(()=>e.classList.add('shown'),60); _revealObs.observe(e); }}
    else _revealObs.observe(e);
  }});
}}

// ── WATCHLIST ──
function saveWL(){{
  if(userEmail) fetch('/api/watchlist',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{watchlist}})}}).catch(()=>{{}});
  else sessionStorage.setItem('wl',JSON.stringify(watchlist));
}}
function renderWLBar(){{ if(!document.getElementById('pane-watch').classList.contains('hidden')) renderWLPage(); }}
function toggleWL(t){{
  if(watchlist.includes(t)){{ watchlist=watchlist.filter(x=>x!==t); toast('Removed '+t); }}
  else {{ watchlist.push(t); toast('★ '+t+' added to watchlist'); }}
  saveWL();
  const btn=document.getElementById('wl-toggle');
  if(btn){{ const on=watchlist.includes(t); btn.classList.toggle('saved',on); btn.textContent=on?'★ SAVED':'☆ ADD TO WATCHLIST'; }}
}}
function removeWL(t){{ watchlist=watchlist.filter(x=>x!==t); saveWL(); renderWLPage(); }}

function buildWLCard(ticker,name,removable){{
  const c=priceCache[ticker];
  let inner;
  if(c){{
    const arrow=c.chg>=0?'▲':'▼', cl=c.chg>=0?'up':'dn';
    inner=`<div class="rail-px">$${{c.price.toLocaleString('en-US',{{minimumFractionDigits:2,maximumFractionDigits:2}})}}</div><div class="rail-chg ${{cl}}">${{arrow}} ${{Math.abs(c.chg).toFixed(2)}}%</div>`;
  }} else {{
    inner=`<div class="rail-px"><span class="rail-shimmer" style="width:46px;height:14px">&nbsp;</span></div><div class="rail-chg" style="margin-top:4px"><span class="rail-shimmer" style="width:32px;height:8px">&nbsp;</span></div>`;
  }}
  const cardCls=c?(c.chg>=0?'up':'dn'):'';
  const rm=removable?`<span class="rail-rm" onclick="event.stopPropagation();removeWL('${{ticker}}')">✕</span>`:'';
  return `<div class="rail-card ${{cardCls}}" onclick="setQ('${{ticker}}')" style="min-width:96px">${{rm}}<div class="rail-sym">${{ticker}}</div>${{inner}}</div>`;
}}

function renderWLPage(){{
  const rail=document.getElementById('wl-rail');
  const note=document.getElementById('wl-note');
  let tickers;
  if(watchlist.length){{
    note.textContent='Your saved symbols · tap any to analyze.';
    tickers=[...watchlist];
    rail.innerHTML=watchlist.map(t=>buildWLCard(t,'',true)).join('');
  }} else {{
    note.textContent='No saved symbols yet. Here are the market benchmarks — analyze a stock and tap ☆ to build your list.';
    tickers=DEFAULT_INDEXES.map(x=>x.ticker);
    rail.innerHTML=DEFAULT_INDEXES.map(x=>buildWLCard(x.ticker,x.name,false)).join('');
  }}
  // trending row always shown
  const trail=document.getElementById('trend-rail');
  if(trail) trail.innerHTML=TRENDING.map(x=>buildWLCard(x.ticker,x.name,false)).join('');
  const allT=[...new Set([...tickers, ...TRENDING.map(x=>x.ticker)])];
  fetchWLPrices(allT);
}}

async function fetchWLPrices(tickers){{
  const btn=document.getElementById('wl-refresh');
  if(btn) btn.textContent='↻ SYNCING';
  try{{
    const r=await fetch('/api/price?tickers='+tickers.join(','));
    if(r.ok){{
      const data=await r.json();
      Object.entries(data).forEach(([t,d])=>{{ priceCache[t]=d; }});
      renderWLCardsInPlace();
    }}
  }}catch(e){{}}
  if(btn) btn.textContent='↻ SYNC';
}}
function renderWLCardsInPlace(){{
  const rail=document.getElementById('wl-rail');
  if(rail){{
    if(watchlist.length) rail.innerHTML=watchlist.map(t=>buildWLCard(t,'',true)).join('');
    else rail.innerHTML=DEFAULT_INDEXES.map(x=>buildWLCard(x.ticker,x.name,false)).join('');
  }}
  const trail=document.getElementById('trend-rail');
  if(trail) trail.innerHTML=TRENDING.map(x=>buildWLCard(x.ticker,x.name,false)).join('');
}}
function refreshWL(){{
  const base=watchlist.length?[...watchlist]:DEFAULT_INDEXES.map(x=>x.ticker);
  const allT=[...new Set([...base, ...TRENDING.map(x=>x.ticker)])];
  allT.forEach(t=>delete priceCache[t]);
  renderWLPage();
}}

// ── MINDS ──
function renderMinds(){{
  const el=document.getElementById('minds-list');
  el.innerHTML=INVESTORS.map(inv=>`
    <div class="master" style="width:auto;margin-bottom:9px">
      <div class="master-top">
        <div class="master-medal">${{inv.initials}}</div>
        <div><div class="master-name">${{inv.name}}</div><div class="master-years">${{inv.years}}</div></div>
      </div>
      <div class="master-tag">${{inv.tag}}</div>
      <div class="master-bio">${{inv.bio}}</div>
      <div class="master-quote">❝ ${{inv.quote}}</div>
    </div>`).join('');
}}

// ── MODALS ──
function openModal(name){{
  if(name==='acct'){{
    document.getElementById('acct-email').textContent='◆ '+userEmail;
    const st=document.getElementById('acct-status');
    if(userSub){{ st.textContent='✦ PRO · UNLIMITED ACCESS'; st.className='modal-acct-status pro'; document.getElementById('acct-sub-btn').style.display='none'; }}
    else {{ st.textContent='FREE TIER'; st.className='modal-acct-status free'; document.getElementById('acct-sub-btn').style.display=''; }}
  }}
  document.getElementById('modal-'+name).classList.add('on');
}}
function closeModal(id){{ document.getElementById(id).classList.remove('on'); }}
function switchModal(from,to){{ closeModal(from); openModal(to.replace('modal-','')); }}
['modal-pay','modal-signup','modal-login','modal-acct'].forEach(id=>{{
  const m=document.getElementById(id);
  if(m) m.addEventListener('click',e=>{{ if(e.target===e.currentTarget) closeModal(id); }});
}});

// ── SUBSCRIBE ──
function clickSubscribe(source){{
  track('subscribe_click',{{source:source||'header',logged_in:!!userEmail}});
  if(userSub){{ toast('✦ You already have full access!'); return; }}
  if(userEmail){{ launchStripe(); }}
  else {{ openModal('signup'); }}
}}
async function launchStripe(){{
  const btn=document.getElementById('pay-btn');
  if(btn) btn.textContent='Loading…';
  try{{
    const r=await fetch('/api/checkout',{{method:'POST'}});
    const d=await r.json();
    if(d.url){{ window.location.href=d.url; }}
    else {{ userSub=true; syncHeader(); closeModal('modal-pay'); toast('✦ Demo mode: full access!'); }}
  }}catch(e){{ userSub=true; syncHeader(); closeModal('modal-pay'); toast('✦ Demo mode!'); }}
  if(btn) btn.textContent='✦ SUBSCRIBE NOW';
}}

// ── AUTH ──
async function doSignup(){{
  const email=document.getElementById('signup-email').value.trim();
  const pw=document.getElementById('signup-pw').value;
  const err=document.getElementById('signup-err');
  if(!email||!pw){{ err.textContent='Email and password required'; return; }}
  err.textContent='';
  try{{
    const r=await fetch('/api/signup',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email,pw}})}});
    const d=await r.json();
    if(!d.ok){{ err.textContent=d.error||'Signup failed'; return; }}
    userEmail=d.email; userSub=d.sub;
    syncHeader(); closeModal('modal-signup'); toast('◆ Account created.');
    setTimeout(launchStripe,500);
  }}catch(e){{ err.textContent='Network error'; }}
}}
async function doLogin(){{
  const email=document.getElementById('login-email').value.trim();
  const pw=document.getElementById('login-pw').value;
  const remember=document.getElementById('login-remember').checked;
  const err=document.getElementById('login-err');
  if(!email||!pw){{ err.textContent='Email and password required'; return; }}
  err.textContent='';
  try{{
    const r=await fetch('/api/login',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email,pw,remember}})}});
    const d=await r.json();
    if(!d.ok){{ err.textContent=d.error||'Login failed'; return; }}
    userEmail=d.email; userSub=d.sub;
    fetch('/api/watchlist').then(rr=>rr.json()).then(dd=>{{ if(dd.ok){{watchlist=dd.watchlist||[];renderWLBar();}} }}).catch(()=>{{}});
    syncHeader(); closeModal('modal-login');
    toast(userSub?'✦ Welcome back! Pro active.':'◆ Signed in.');
  }}catch(e){{ err.textContent='Network error'; }}
}}
async function doLogout(){{
  await fetch('/api/logout',{{method:'POST'}});
  userEmail=''; userSub=false; watchlist=[];
  syncHeader(); closeModal('modal-acct'); toast('Signed out.');
}}

// ── helpers ──
function toast(msg){{ const t=document.getElementById('toast'); t.textContent=msg; t.classList.add('on'); setTimeout(()=>t.classList.remove('on'),3500); }}
function setStatus(msg,col){{ const e=document.getElementById('status'); e.textContent=msg; e.style.color=col||'var(--w3)'; }}
function fp(v){{ if(!v||v<=0) return 'N/A'; return '$'+v.toLocaleString('en-US',{{minimumFractionDigits:2,maximumFractionDigits:2}}); }}
function fc(v){{ if(!v||v<=0) return '—'; if(v>1e12) return '$'+(v/1e12).toFixed(2)+'T'; if(v>1e9) return '$'+(v/1e9).toFixed(1)+'B'; if(v>1e6) return '$'+(v/1e6).toFixed(0)+'M'; return '—'; }}
</script>

</body></html>"""


if __name__ == "__main__":
    app.run(debug=True, port=5678)
