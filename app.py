#!/usr/bin/env python3
"""SENECA — Intrinsic Value Oracle v4
  + Persistent server-side watchlist
  + Health Score (deterministic + LLM cross-verification)
  + Empty state landing design with investor quotes
  + Stock models for stocks, ETF models for ETFs only
  + Composite at top, health score below composite
"""

import os, math, io, hashlib, json, pathlib
from datetime import datetime
from flask import Flask, request, jsonify, session, send_file

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "seneca-secret-2025")

STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_PRICE_ID        = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
GROQ_API_KEY           = os.environ.get("GROQ_API_KEY", "")

# ── User store (with watchlist) ───────────────────────────────────────────────
USER_FILE = pathlib.Path("/tmp/seneca_users.json")

def load_users():
    try: return json.loads(USER_FILE.read_text()) if USER_FILE.exists() else {}
    except: return {}

def save_users(u):
    try: USER_FILE.write_text(json.dumps(u))
    except: pass

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

def create_user(email, pw):
    users = load_users(); email = email.lower().strip()
    if email in users: return False, "Email already registered"
    users[email] = {"pw": hash_pw(pw), "subscribed": False, "watchlist": []}
    save_users(users); return True, "ok"

def verify_user(email, pw):
    users = load_users(); email = email.lower().strip()
    u = users.get(email)
    if not u: return False, "Email not found"
    if u["pw"] != hash_pw(pw): return False, "Incorrect password"
    return True, u

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
def gn(e,b): return math.sqrt(22.5*e*b) if e>0 and b>0 else None
def gg(e,g): return e*(8.5+2*g)*4.4/4.5 if e>0 and g else None
def buf(e,g):
    if e<=0 or not g: return None
    r,d=min(g/100,.25),.09
    return sum(e*(1+r)**y/(1+d)**y for y in range(1,11))+(e*(1+r)**10*15)/(1+d)**10
def lyn(e,g): return e*g if e>0 and g>0 else None
def sim(p,pe,pb,roe,mom):
    if pe<=0 or pb<=0 or roe<=0: return None
    return p*(roe/pe)*(1/pb)*(1+(mom/100)*0.3)*12
def fdcf(f,g):
    if f<=0 or not g: return None
    r,d,tg=min(g/100,.30),.10,.025
    return sum(f*(1+r)**y/(1+d)**y for y in range(1,11))+(f*(1+r)**10*(1+tg)/(d-tg))/(1+d)**10

def capm_r(beta):
    # CAPM: Rf=4.3% (10yr Treasury), market risk premium=5.5%
    b = max(0.3, min(float(beta) if beta and beta > 0 else 1.0, 3.0))
    return 0.043 + b * 0.055

def gordon_ddm(div_ps, div_growth_pct, beta):
    # Gordon Growth Model: P = D1/(r-g)
    # D1 = D0*(1+g), r = CAPM rate, g capped at 3.5% and must be < r
    if div_ps <= 0: return None
    r = capm_r(beta)
    g = min(div_growth_pct / 100 if div_growth_pct else 0.02, 0.035, r - 0.01)
    if g <= 0: g = 0.02
    if r <= g: return None
    return (div_ps * (1 + g)) / (r - g)

def etf_ddm(price, div_yield_pct, beta):
    # ETF DDM: CAPM-based r, 2.5% long-run growth
    if div_yield_pct <= 0: return None
    div_ps = price * (div_yield_pct / 100)
    if div_ps <= 0: return None
    r = capm_r(beta)
    g = 0.025
    if r <= g: r = g + 0.03
    return (div_ps * (1 + g)) / (r - g)

def comp_stock(vals):
    w={"gn":.18,"gg":.13,"buf":.22,"lyn":.13,"sim":.09,"dcf":.15,"ddm":.10}; t=ws=0
    for k,wt in w.items():
        v=vals.get(k)
        if v and v>0: t+=v*wt; ws+=wt
    return t/ws if ws>0 else None

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
    cap=float(fi.market_cap or 0); shares=float(fi.shares or 1)
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
    fcf_ps=g("freeCashflow")/shares if shares else 0
    growth=(g("earningsGrowth") or g("revenueGrowth") or g("earningsQuarterlyGrowth") or 0)*100
    chg=(price-prev)/prev*100 if prev else 0
    mom=(price-lo52)/lo52*100 if lo52 else 0
    ey=(1/pe*100) if pe>0 else 0
    fund = is_fund(ticker, info)
    health = compute_health_score(info, is_etf=fund)

    if fund:
        iv={}
        if ey>0: iv["fed"]=price*(ey/4.3)
        if pe>0: iv["per"]=price*(17.0/pe)
        _etf_ddm_val = etf_ddm(price, div_y, beta)
        if _etf_ddm_val: iv["ddm"] = _etf_ddm_val
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
        vd={"gn":gn(eps,bvps),"gg":gg(eps,growth),"buf":buf(eps,growth),
            "lyn":lyn(eps,growth),"sim":sim(price,pe or 1,pb or 1,roe,mom),"dcf":fdcf(fcf_ps,growth),
            "ddm":_ddm_val}
        comp=comp_stock(vd); models=[]
        for k,nm,fm,sc,cl in [
            ("gn", "GRAHAM NUMBER",      "√( 22.5 × EPS × Book Value )",                 "gold","gold"),
            ("gg", "GRAHAM GROWTH",      "EPS × (8.5+2g) × 4.4/AAA yield",              "gold","gold"),
            ("buf","BUFFETT DCF",        "10yr EPS @ 9% · 15× terminal",                 "turq","turq"),
            ("lyn","PETER LYNCH PEG",    "EPS × growth% (PEG=1)",                        "turq","turq"),
            ("sim","SIMONS QUANT",       "ROE/PE × (1/PB) × momentum",                   "muted","muted"),
            ("dcf","FREE CASH FLOW DCF", "10yr FCF @ 10% · 2.5% terminal",               "muted","muted"),
            ("ddm","GORDON GROWTH DDM",  "D1 ÷ (CAPM rate − div growth%) · dividend-payers only","gold","gold"),
        ]:
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

# ── Routes ────────────────────────────────────────────────────────────────────

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
    return render_app(sub=True, email=email, toast="✦ Subscription active! Unlimited access unlocked.")

@app.route("/api/signup", methods=["POST"])
def api_signup():
    d = request.get_json()
    email = (d.get("email","")).strip().lower(); pw = d.get("pw","")
    if not email or not pw: return jsonify({"ok":False,"error":"Email and password required"}), 400
    ok, msg = create_user(email, pw)
    if not ok: return jsonify({"ok":False,"error":msg}), 400
    session["email"] = email; session["sub"] = False
    return jsonify({"ok":True,"email":email,"sub":False,"watchlist":[]})

@app.route("/api/login", methods=["POST"])
def api_login():
    d = request.get_json()
    email = (d.get("email","")).strip().lower(); pw = d.get("pw","")
    if not email or not pw: return jsonify({"ok":False,"error":"Email and password required"}), 400
    ok, result = verify_user(email, pw)
    if not ok: return jsonify({"ok":False,"error":result}), 401
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
    d = request.get_json()
    wl = d.get("watchlist", [])
    # Keep max 20 items, only valid tickers
    wl = [str(t).upper().strip()[:10] for t in wl if t][:20]
    save_watchlist(email, wl)
    return jsonify({"ok":True,"watchlist":wl})

@app.route("/api/quote")
def api_quote():
    q = request.args.get("q","").strip()
    if not q: return jsonify({"error":"No ticker"}), 400
    email = session.get("email","")
    sub = session.get("sub", False)
    if email and not sub:
        u = get_user(email)
        if u: sub = u.get("subscribed", False)
    lookups = session.get("lookups", 0)
    if not sub and lookups >= 1: return jsonify({"error":"PAYWALL"}), 402
    try:
        data = fetch_quote(q); session["lookups"] = lookups + 1
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
def render_app(sub=False, email="", toast=""):
    stripe_pk = STRIPE_PUBLISHABLE_KEY
    safe_toast = toast.replace('"', '&quot;')
    toast_js = f'toast("{safe_toast}");' if toast else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"/>
<meta name="google-site-verification" content="dZDX1AMHsuaZcDjFD8CGt6EVQepwkUk4fre82eWuiHM"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<title>SENECA ◆ Intrinsic Value Oracle</title>
<!-- Google tag (gtag.js) -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-QBNR5XWKVS"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){{dataLayer.push(arguments);}}
  gtag('js', new Date());
  gtag('config', 'G-QBNR5XWKVS');
</script>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400&family=Share+Tech+Mono&display=swap" rel="stylesheet"/>
{"<script src='https://js.stripe.com/v3/'></script>" if stripe_pk else ""}
<style>
:root{{--bg:#0d0a04;--panel:#140d05;--card:#1e1308;--card2:#271908;--card3:#301f0c;
  --border:#4e3010;--b2:#6e4514;--gold:#c88a1a;--g2:#e8aa34;--g3:#f5cc60;
  --turq:#1e7a6a;--t2:#28a892;--t3:#48c4ae;--green:#3a8a24;--red:#a03020;
  --muted:#6a4e2c;--text:#ede4c8;--sub:#b88a4c;--dim:#4a3418;}}
*{{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}}
html,body{{min-height:100%;background:var(--bg);color:var(--text);font-family:'Cormorant Garamond',serif;}}
::-webkit-scrollbar{{width:3px}}::-webkit-scrollbar-thumb{{background:var(--b2);border-radius:2px}}

/* HERO */
.hero{{min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px 20px;text-align:center;position:relative}}
.hero-bg{{position:absolute;inset:0;background:radial-gradient(ellipse 60% 50% at 50% 0%,rgba(200,138,26,.08),transparent 70%),repeating-linear-gradient(0deg,transparent,transparent 79px,rgba(78,48,16,.12) 80px),repeating-linear-gradient(90deg,transparent,transparent 79px,rgba(78,48,16,.12) 80px);pointer-events:none}}
.hero-gem{{width:70px;height:70px;background:linear-gradient(135deg,var(--g2),var(--g3));transform:rotate(45deg);display:flex;align-items:center;justify-content:center;margin-bottom:28px;box-shadow:0 0 60px rgba(232,170,52,.25)}}
.hero-gem span{{transform:rotate(-45deg);font-size:1.3rem;color:var(--bg)}}
.hero-title{{font-size:clamp(3rem,9vw,5.5rem);font-weight:300;color:var(--g3);letter-spacing:.35em;position:relative;z-index:1}}
.hero-sub{{font-size:1rem;color:var(--sub);font-style:italic;letter-spacing:.15em;margin:8px 0 36px;position:relative;z-index:1}}
.hero-rule{{width:160px;height:1px;background:linear-gradient(90deg,transparent,var(--gold),transparent);margin:0 auto 36px}}
.hero-pitch{{max-width:500px;font-size:1.08rem;color:var(--sub);line-height:1.9;font-style:italic;margin-bottom:40px;position:relative;z-index:1}}
.hero-pitch strong{{color:var(--text);font-style:normal}}
.hero-btns{{display:flex;gap:14px;justify-content:center;flex-wrap:wrap;margin-bottom:36px;position:relative;z-index:1}}
.btn-teal{{background:linear-gradient(135deg,var(--turq),var(--t2));color:var(--bg);border:none;border-radius:14px;padding:14px 30px;font-family:'Cormorant Garamond',serif;font-size:1rem;font-weight:600;letter-spacing:.1em;cursor:pointer;transition:all .2s}}
.btn-teal:hover{{transform:translateY(-2px);opacity:.9}}
.btn-outline{{background:transparent;color:var(--g2);border:1px solid var(--b2);border-radius:14px;padding:14px 30px;font-family:'Cormorant Garamond',serif;font-size:1rem;letter-spacing:.1em;cursor:pointer;transition:all .2s}}
.btn-outline:hover{{border-color:var(--gold);color:var(--g3);transform:translateY(-2px)}}
.hero-badges{{display:flex;flex-wrap:wrap;gap:7px;justify-content:center;position:relative;z-index:1;margin-bottom:16px}}
.badge{{background:var(--card2);border:1px solid var(--b2);border-radius:20px;padding:4px 13px;font-family:'Share Tech Mono',monospace;font-size:.56rem;color:var(--sub)}}
.hero-note{{font-size:.72rem;color:var(--dim);font-style:italic;position:relative;z-index:1}}
.hero-note a{{color:var(--g2);text-decoration:none;cursor:pointer}}

/* APP SHELL */
.shell{{display:none;flex-direction:column;min-height:100vh}}.shell.on{{display:flex}}

/* HEADER */
.hdr{{background:var(--panel);border-bottom:1px solid var(--b2);padding:0 20px;padding-top:env(safe-area-inset-top,0px);position:sticky;top:0;z-index:100;display:flex;align-items:stretch;justify-content:space-between;gap:0;box-shadow:0 2px 24px rgba(0,0,0,.4)}}
/* Gold accent line across top of header */
.hdr::before{{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--gold),var(--g2),var(--gold),transparent)}}
/* Logo / home link — left side */
.hdr-logo{{display:flex;align-items:center;gap:14px;padding:12px 0;cursor:pointer;text-decoration:none;flex-shrink:0;transition:opacity .2s;position:relative}}
.hdr-logo:hover{{opacity:.85}}
.hdr-logo:hover .logo-home-hint{{opacity:1}}
.logo-home-hint{{position:absolute;bottom:-1px;left:50%;transform:translateX(-50%);font-family:'Share Tech Mono',monospace;font-size:.42rem;color:var(--dim);letter-spacing:.1em;opacity:0;transition:opacity .2s;white-space:nowrap}}
/* Diamond gem */
.hdr-gem{{width:38px;height:38px;background:linear-gradient(135deg,var(--g2),var(--g3));transform:rotate(45deg);display:flex;align-items:center;justify-content:center;flex-shrink:0;box-shadow:0 0 0 1px rgba(232,170,52,.3),0 0 16px rgba(232,170,52,.15)}}
.hdr-gem span{{transform:rotate(-45deg);font-size:.7rem;color:var(--bg);font-weight:700}}
/* Logo text */
.hdr-logo-text{{display:flex;flex-direction:column;gap:0}}
.hdr-logo-name{{font-size:1.35rem;font-weight:600;color:var(--g3);letter-spacing:.28em;line-height:1;font-family:'Cormorant Garamond',serif}}
.hdr-logo-tag{{font-size:.52rem;color:var(--sub);font-style:italic;letter-spacing:.08em;margin-top:2px;font-family:'Share Tech Mono',monospace}}
/* Center rule */
.hdr-rule{{flex:1;height:1px;background:linear-gradient(90deg,transparent,var(--border),transparent);align-self:center;margin:0 20px}}
/* Right side actions */
.hdr-right{{display:flex;align-items:center;gap:10px;flex-shrink:0;padding:12px 0}}
.btn-sub{{background:linear-gradient(135deg,var(--gold),var(--g2));color:var(--bg);border:none;border-radius:10px;padding:8px 16px;font-family:'Cormorant Garamond',serif;font-size:.82rem;font-weight:600;cursor:pointer;white-space:nowrap;letter-spacing:.05em;box-shadow:0 2px 12px rgba(200,138,26,.25);transition:all .2s}}
.btn-sub:hover{{transform:translateY(-1px);box-shadow:0 4px 18px rgba(200,138,26,.35)}}
.btn-acct{{background:transparent;color:var(--g2);border:1px solid var(--b2);border-radius:10px;padding:7px 14px;font-family:'Share Tech Mono',monospace;font-size:.56rem;cursor:pointer;white-space:nowrap;letter-spacing:.06em;transition:all .2s}}
.btn-acct:hover{{border-color:var(--gold);color:var(--g3)}}

/* SEARCH */
.search-wrap{{background:var(--panel);padding:12px 14px 0;border-bottom:1px solid var(--border)}}
.search-box{{background:var(--card);border:1px solid var(--b2);border-radius:14px;padding:10px 13px}}
.search-row{{display:flex;align-items:center;gap:8px;margin-bottom:6px}}
.search-input{{flex:1;min-width:0;width:0;background:transparent;border:none;outline:none;color:var(--g3);font-family:'Cormorant Garamond',serif;font-size:1.25rem;font-weight:600;text-align:center;letter-spacing:.1em;caret-color:var(--t3)}}
.search-input::placeholder{{color:var(--dim);font-size:.85rem;font-weight:400;letter-spacing:.02em}}
.btn-analyze{{background:var(--turq);color:var(--bg);border:none;border-radius:9px;padding:10px 15px;font-family:'Cormorant Garamond',serif;font-size:.9rem;font-weight:600;cursor:pointer;flex-shrink:0;white-space:nowrap;transition:all .2s}}
.btn-analyze:hover{{background:var(--t2)}}.btn-analyze:disabled{{background:var(--muted);cursor:default}}
.search-hint{{font-size:.59rem;color:var(--dim);font-style:italic;text-align:center;padding-bottom:2px}}
.chips{{display:flex;flex-wrap:wrap;gap:6px;padding:8px 14px 10px}}
.chip{{background:var(--card2);border:1px solid var(--b2);border-radius:20px;padding:3px 11px;font-family:'Share Tech Mono',monospace;font-size:.58rem;color:var(--sub);cursor:pointer;transition:all .15s}}
.chip:hover{{background:var(--turq);border-color:var(--t2);color:var(--bg)}}

/* WATCHLIST BAR */
.wl-bar{{background:var(--panel);border-bottom:1px solid var(--border);padding:8px 14px;display:none}}
.wl-bar.on{{display:block}}
.wl-title{{font-family:'Share Tech Mono',monospace;font-size:.54rem;color:var(--muted);letter-spacing:.1em;margin-bottom:5px}}
.wl-items{{display:flex;flex-wrap:wrap;gap:5px}}
.wl-chip{{background:var(--card2);border:1px solid var(--b2);border-radius:18px;padding:3px 8px 3px 11px;display:flex;align-items:center;gap:4px;font-family:'Share Tech Mono',monospace;font-size:.58rem;color:var(--sub);cursor:pointer;transition:all .15s}}
.wl-chip:hover{{border-color:var(--g2);color:var(--text)}}
.wl-rm{{background:none;border:none;cursor:pointer;color:var(--muted);font-size:.66rem;line-height:1;padding:0 2px}}
.wl-rm:hover{{color:var(--red)}}

/* STATUS + TABS */
.status-bar{{font-family:'Share Tech Mono',monospace;font-size:.6rem;color:var(--dim);text-align:center;padding:6px 14px;background:var(--panel);border-bottom:1px solid var(--border);min-height:21px}}
.tabs{{display:flex;background:var(--panel);border-bottom:1px solid var(--border)}}
.tab{{flex:1;background:none;border:none;border-bottom:2px solid transparent;font-family:'Cormorant Garamond',serif;font-size:.9rem;color:var(--muted);padding:10px 8px;cursor:pointer;transition:all .2s;letter-spacing:.05em;position:relative;top:1px}}
.tab.on{{color:var(--g2);border-bottom-color:var(--t2)}}
main{{flex:1;overflow-y:auto;padding:0;padding-bottom:calc(20px + env(safe-area-inset-bottom,0px))}}

/* ── EMPTY STATE ── */
.empty-state{{padding:24px 16px 32px;display:flex;flex-direction:column;gap:0}}
.empty-quote-carousel{{position:relative;overflow:hidden;margin-bottom:0}}
.eq-slide{{display:none;animation:fadeIn .6s ease}}
.eq-slide.active{{display:block}}
@keyframes fadeIn{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:translateY(0)}}}}
.eq-card{{background:linear-gradient(135deg,var(--card2),var(--card));border:1px solid var(--b2);border-radius:18px;padding:22px 22px 18px;margin-bottom:16px;position:relative;overflow:hidden}}
.eq-card::before{{content:'❝';position:absolute;top:10px;left:16px;font-size:3rem;color:var(--b2);font-family:Georgia,serif;line-height:1}}
.eq-text{{font-size:1.05rem;color:var(--text);line-height:1.75;font-style:italic;padding-left:24px;margin-bottom:12px}}
.eq-author{{font-family:'Share Tech Mono',monospace;font-size:.58rem;color:var(--g2);letter-spacing:.1em;padding-left:24px}}
.eq-dots{{display:flex;justify-content:center;gap:6px;margin-bottom:18px}}
.eq-dot{{width:5px;height:5px;border-radius:50%;background:var(--border);cursor:pointer;transition:all .2s}}
.eq-dot.active{{background:var(--g2);transform:scale(1.3)}}
.empty-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}}
.empty-tile{{background:linear-gradient(135deg,var(--card2),var(--card));border:1px solid var(--border);border-radius:14px;padding:14px 14px 12px;text-align:center}}
.empty-tile-icon{{font-size:1.4rem;margin-bottom:6px}}
.empty-tile-title{{font-size:.72rem;color:var(--g2);font-weight:600;letter-spacing:.08em;margin-bottom:4px}}
.empty-tile-body{{font-size:.65rem;color:var(--sub);font-style:italic;line-height:1.5}}
.empty-cta{{background:linear-gradient(135deg,var(--card2),var(--card));border:1px solid var(--b2);border-radius:14px;padding:16px;text-align:center}}
.empty-cta-title{{font-size:.62rem;color:var(--muted);font-family:'Share Tech Mono',monospace;letter-spacing:.12em;margin-bottom:10px}}
.market-ticker{{display:flex;justify-content:center;gap:20px;flex-wrap:wrap}}
.mt-item{{text-align:center}}
.mt-sym{{font-family:'Share Tech Mono',monospace;font-size:.6rem;color:var(--dim);letter-spacing:.08em}}
.mt-tap{{font-size:.78rem;color:var(--sub);font-style:italic;margin-top:8px}}

/* RESULTS area */
.results-wrap{{padding:13px 12px}}

/* ── LUXURY HERO CARD ── */
.hero-card{{background:linear-gradient(160deg,var(--card2) 0%,var(--card) 100%);border:1px solid var(--b2);border-radius:20px;margin-bottom:13px;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,.4),inset 0 1px 0 rgba(232,170,52,.08)}}
.hero-card-band{{height:3px}}
.hero-card-band.up{{background:linear-gradient(90deg,var(--green),#5ab83a,var(--green))}}
.hero-card-band.dn{{background:linear-gradient(90deg,var(--red),#c84030,var(--red))}}
.hero-card-band.fair{{background:linear-gradient(90deg,var(--gold),var(--g2),var(--gold))}}
.hero-card-inner{{display:flex;align-items:stretch;padding:16px 18px 14px;gap:0}}
.hero-left{{flex:1;min-width:0;padding-right:16px;border-right:1px solid var(--border)}}
.hero-name{{font-size:.95rem;font-weight:600;color:var(--text);letter-spacing:.02em;margin-bottom:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.hero-ticker{{font-family:'Share Tech Mono',monospace;font-size:.72rem;color:var(--g2);letter-spacing:.12em;margin-bottom:4px}}
.hero-sector{{font-family:'Share Tech Mono',monospace;font-size:.55rem;color:var(--dim);letter-spacing:.04em}}
.hero-right{{display:flex;align-items:center;padding-left:16px;flex-shrink:0}}
.hero-price-block,.hero-comp-block{{text-align:center;padding:0 12px}}
.hero-price-label{{font-family:'Share Tech Mono',monospace;font-size:.5rem;color:var(--dim);letter-spacing:.12em;text-transform:uppercase;margin-bottom:4px}}
.hero-price{{font-size:1.9rem;font-weight:300;color:var(--g3);line-height:1;margin-bottom:3px}}
.hero-comp-val{{font-size:1.9rem;font-weight:300;color:var(--gold);line-height:1;margin-bottom:3px}}
.hero-chg{{font-size:.78rem;font-weight:600}}
.hero-chg.up{{color:var(--green)}}.hero-chg.dn{{color:var(--red)}}
.hero-margin{{font-size:.7rem;font-weight:600;font-family:'Share Tech Mono',monospace}}
.hero-margin.up{{color:var(--green)}}.hero-margin.dn{{color:var(--red)}}.hero-margin.fair{{color:var(--g2)}}
.hero-divider-v{{width:1px;background:var(--border);align-self:stretch;margin:0 4px}}
.hero-verdict{{padding:11px 18px;font-size:.88rem;font-weight:600;letter-spacing:.04em;border-top:1px solid var(--border)}}
.hero-verdict.up{{color:var(--green);background:rgba(58,138,36,.06)}}
.hero-verdict.dn{{color:var(--red);background:rgba(160,48,32,.06)}}
.hero-verdict.fair{{color:var(--g2);background:rgba(200,138,26,.06)}}
.hero-range{{padding:10px 18px 14px}}
.rng-labels{{display:flex;justify-content:space-between;font-family:'Share Tech Mono',monospace;font-size:.56rem;margin-bottom:4px}}
.rlo{{color:var(--red)}}.rhi{{color:var(--green)}}.rmid{{color:var(--dim)}}
.rng-track{{height:7px;border-radius:4px;background:var(--card3);border:1px solid var(--border);position:relative}}
.rng-fill{{height:100%;border-radius:4px;background:linear-gradient(90deg,var(--turq),var(--t2));transition:width .8s cubic-bezier(.4,0,.2,1)}}
.rng-thumb{{position:absolute;top:50%;transform:translate(-50%,-50%);width:15px;height:15px;background:var(--g2);border:2px solid var(--g3);border-radius:50%;transition:left .8s cubic-bezier(.4,0,.2,1)}}
.rng-pct{{text-align:center;font-family:'Share Tech Mono',monospace;font-size:.54rem;color:var(--dim);margin-top:4px}}

/* ── HEALTH SCORE ── */
.health-card{{background:linear-gradient(160deg,var(--card2),var(--card));border:1px solid var(--b2);border-radius:16px;margin-bottom:13px;overflow:hidden}}
.health-header{{display:flex;align-items:center;justify-content:space-between;padding:14px 16px 10px}}
.health-title{{font-family:'Share Tech Mono',monospace;font-size:.62rem;color:var(--g2);letter-spacing:.14em}}
.health-grade-wrap{{display:flex;align-items:center;gap:10px}}
.health-score{{font-size:1.6rem;font-weight:300;color:var(--g3)}}
.health-grade{{width:36px;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:1.1rem;font-weight:600;flex-shrink:0}}
.health-grade.A{{background:rgba(58,138,36,.2);border:1px solid var(--green);color:var(--green)}}
.health-grade.B{{background:rgba(72,196,174,.15);border:1px solid var(--t3);color:var(--t3)}}
.health-grade.C{{background:rgba(200,138,26,.15);border:1px solid var(--gold);color:var(--g2)}}
.health-grade.D{{background:rgba(160,48,32,.15);border:1px solid var(--red);color:#e08070}}
.health-grade.F{{background:rgba(160,48,32,.25);border:1px solid var(--red);color:var(--red)}}
.health-bar-wrap{{padding:0 16px 12px}}
.health-bar-track{{height:6px;border-radius:3px;background:var(--card3);border:1px solid var(--border);overflow:hidden}}
.health-bar-fill{{height:100%;border-radius:3px;transition:width 1s cubic-bezier(.4,0,.2,1)}}
.health-bar-fill.A{{background:linear-gradient(90deg,var(--green),#5ab83a)}}
.health-bar-fill.B{{background:linear-gradient(90deg,var(--turq),var(--t2))}}
.health-bar-fill.C{{background:linear-gradient(90deg,var(--gold),var(--g2))}}
.health-bar-fill.D{{background:linear-gradient(90deg,#c84030,var(--red))}}
.health-bar-fill.F{{background:linear-gradient(90deg,#8a1010,var(--red))}}
.health-breakdown{{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--border);border-top:1px solid var(--border)}}
.hb-item{{background:var(--card);padding:6px 10px;display:flex;justify-content:space-between;align-items:center}}
.hb-item:nth-child(4n+1),.hb-item:nth-child(4n+2){{background:var(--card2)}}
.hb-key{{font-family:'Share Tech Mono',monospace;font-size:.52rem;color:var(--muted)}}
.hb-val{{font-family:'Share Tech Mono',monospace;font-size:.58rem;color:var(--text);text-align:right;max-width:55%}}
.health-flags{{padding:8px 16px 12px;display:flex;flex-wrap:wrap;gap:5px}}
.health-flag{{background:rgba(160,48,32,.12);border:1px solid rgba(160,48,32,.3);border-radius:20px;padding:2px 9px;font-family:'Share Tech Mono',monospace;font-size:.52rem;color:#e08070}}
.health-ai-wrap{{padding:0 16px 14px}}
.health-ai-label{{font-family:'Share Tech Mono',monospace;font-size:.54rem;color:var(--t3);letter-spacing:.1em;margin-bottom:6px}}
.health-ai-text{{font-size:.82rem;color:var(--sub);line-height:1.75;font-style:italic}}
.health-ai-loading{{display:flex;align-items:center;gap:6px}}

/* SECTION TITLE */
.lux-section-title{{font-family:'Share Tech Mono',monospace;font-size:.62rem;color:var(--g2);letter-spacing:.14em;margin-bottom:9px;display:flex;align-items:center;gap:8px}}
.lux-section-title::after{{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--b2),transparent)}}

/* FUND GRID */
.lux-section{{background:linear-gradient(160deg,var(--card2),var(--card));border:1px solid var(--border);border-radius:16px;margin-bottom:11px;padding:13px 14px}}
.fgrid{{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--border)}}
.fc{{background:var(--card);padding:7px 9px;display:flex;justify-content:space-between;align-items:center}}
.fc:nth-child(4n+1),.fc:nth-child(4n+2){{background:var(--card2)}}
.fl{{font-family:'Share Tech Mono',monospace;font-size:.54rem;color:var(--muted)}}
.fv{{font-family:'Share Tech Mono',monospace;font-size:.66rem;color:var(--text);font-weight:700}}

/* MODEL CARDS */
.mc{{background:linear-gradient(160deg,var(--card2),var(--card));border:1px solid var(--border);border-radius:11px;margin-bottom:7px;display:flex;overflow:hidden;transition:transform .15s}}
.mc:hover{{transform:translateY(-1px)}}
.mbar{{width:4px;flex-shrink:0}}
.mbar.gold{{background:linear-gradient(180deg,var(--g3),var(--gold))}}
.mbar.turq{{background:linear-gradient(180deg,var(--t3),var(--turq))}}
.mbar.muted{{background:linear-gradient(180deg,var(--muted),var(--dim))}}
.mbody{{padding:9px 13px;flex:1;min-width:0}}
.mrow{{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:2px}}
.mname{{font-size:.63rem;font-weight:600;letter-spacing:.06em}}
.mname.gold{{color:var(--g2)}}.mname.turq{{color:var(--t3)}}.mname.muted{{color:var(--sub)}}
.mval{{font-size:1.1rem;font-weight:300;white-space:nowrap}}
.mval.up{{color:var(--green)}}.mval.dn{{color:var(--red)}}.mval.fair{{color:var(--g2)}}.mval.na{{color:var(--dim)}}
.msig{{font-size:.58rem;font-style:italic;text-align:right;margin-bottom:1px}}
.msig.up{{color:var(--green)}}.msig.dn{{color:var(--red)}}.msig.fair{{color:var(--gold)}}.msig.na{{color:var(--dim)}}
.mfm{{font-family:'Share Tech Mono',monospace;font-size:.5rem;color:var(--dim);margin-top:1px}}

/* VERDICT DETAIL */
.verdict-detail-card{{display:flex;align-items:flex-start;gap:14px;background:linear-gradient(160deg,var(--card2),var(--card));border:1px solid var(--border);border-radius:15px;padding:14px 16px;margin-bottom:11px;position:relative;overflow:hidden}}
.verdict-detail-card::before{{content:'';position:absolute;left:0;top:0;bottom:0;width:3px}}
.verdict-detail-card.up::before{{background:linear-gradient(180deg,var(--green),#5ab83a)}}
.verdict-detail-card.dn::before{{background:linear-gradient(180deg,var(--red),#c84030)}}
.verdict-detail-card.fair::before{{background:linear-gradient(180deg,var(--gold),var(--g2))}}
.vd-icon{{font-size:1.4rem;opacity:.4;flex-shrink:0;margin-top:2px}}
.vd-title{{font-size:.95rem;font-weight:600;margin-bottom:5px}}
.vd-body{{font-size:.78rem;font-style:italic;color:var(--sub);line-height:1.65}}

/* AI CARD */
.ai-card{{background:linear-gradient(160deg,var(--card2),var(--card));border:1px solid var(--turq);border-radius:15px;padding:13px 15px;margin-bottom:11px;position:relative;overflow:hidden}}
.ai-card::before{{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:linear-gradient(180deg,var(--t3),var(--turq))}}
.ai-label{{font-family:'Share Tech Mono',monospace;font-size:.56rem;color:var(--t3);letter-spacing:.13em;margin-bottom:7px}}
.ai-text{{font-size:.84rem;color:var(--sub);line-height:1.8;font-style:italic}}
.dots{{display:flex;gap:5px;align-items:center}}
.dot{{width:5px;height:5px;border-radius:50%;background:var(--t2);animation:pulse 1.2s ease-in-out infinite}}
.dot:nth-child(2){{animation-delay:.2s}}.dot:nth-child(3){{animation-delay:.4s}}
@keyframes pulse{{0%,100%{{opacity:.25}}50%{{opacity:1}}}}

/* ACTIONS */
.actions{{display:flex;gap:7px;margin-bottom:11px;flex-wrap:wrap}}
.btn-sm{{background:var(--card2);border:1px solid var(--b2);color:var(--sub);border-radius:9px;padding:7px 12px;font-family:'Cormorant Garamond',serif;font-size:.78rem;cursor:pointer;transition:all .2s}}
.btn-sm:hover{{border-color:var(--gold);color:var(--g2)}}
.btn-sm.active{{border-color:var(--t2);color:var(--t3);background:rgba(30,122,106,.1)}}

/* COMPARE */
.cmp-bar{{background:var(--panel);padding:11px 13px 0;border-bottom:1px solid var(--border);display:none}}
.cmp-bar.on{{display:block}}
.cmp-row{{display:flex;gap:7px;align-items:center}}
.cmp-input{{flex:1;min-width:0;background:var(--card);border:1px solid var(--b2);border-radius:9px;padding:8px 10px;color:var(--g3);font-family:'Cormorant Garamond',serif;font-size:1rem;font-weight:600;text-align:center;letter-spacing:.1em;outline:none}}
.cmp-input::placeholder{{color:var(--dim);font-size:.78rem;font-weight:400;letter-spacing:0}}
.btn-cmp{{background:var(--gold);color:var(--bg);border:none;border-radius:9px;font-family:'Cormorant Garamond',serif;font-size:.86rem;font-weight:600;padding:9px 13px;cursor:pointer;white-space:nowrap;flex-shrink:0}}
.btn-cmp:hover{{background:var(--g2)}}
.cmp-hint{{font-size:.57rem;color:var(--dim);font-style:italic;padding:4px 0 9px;text-align:center}}
.cmp-grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:11px}}
.cmp-col{{background:linear-gradient(160deg,var(--card2),var(--card));border:1px solid var(--border);border-radius:13px;overflow:hidden}}
.cmp-col.win{{background:rgba(30,122,106,.1);border-color:var(--t2)}}
.cmp-hdr{{padding:9px 11px;border-bottom:1px solid var(--border)}}
.cmp-tkr{{font-size:1.15rem;font-weight:600;color:var(--g3);letter-spacing:.1em}}
.cmp-nm{{font-size:.58rem;color:var(--sub);font-style:italic;margin-top:1px}}
.cmp-px{{font-size:1.2rem;font-weight:300;color:var(--g3);margin:3px 0}}
.cmp-r{{display:flex;justify-content:space-between;padding:5px 11px;border-bottom:1px solid var(--border)}}
.cmp-r:last-child{{border:none}}
.cl{{color:var(--muted);font-family:'Share Tech Mono',monospace;font-size:.52rem}}
.cv{{color:var(--text);font-family:'Share Tech Mono',monospace;font-size:.61rem;font-weight:700}}
.cmp-vd{{padding:8px 11px;font-size:.72rem;font-weight:600}}
.cmp-vd.up{{color:var(--green)}}.cmp-vd.dn{{color:var(--red)}}.cmp-vd.fair{{color:var(--g2)}}

/* SEC DIVIDER */
.sec{{display:flex;align-items:center;gap:9px;font-family:'Share Tech Mono',monospace;font-size:.56rem;color:var(--muted);letter-spacing:.13em;margin:15px 0 7px}}
.sec::before,.sec::after{{content:'';flex:1;height:1px}}
.sec::before{{background:linear-gradient(90deg,transparent,var(--border))}}
.sec::after{{background:linear-gradient(90deg,var(--border),transparent)}}

/* SPINNER */
.spinner{{text-align:center;padding:50px 0;display:none}}.spinner.on{{display:block}}
.ring{{width:40px;height:40px;border:2px solid var(--b2);border-top-color:var(--t2);border-radius:50%;display:inline-block;animation:spin 1s linear infinite}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
.spin-txt{{font-size:.74rem;color:var(--sub);font-style:italic;margin-top:9px}}

/* ERR */
.err{{background:var(--card);border:1px solid var(--red);border-left:4px solid var(--red);border-radius:13px;padding:15px 17px;margin-bottom:11px}}
.err-title{{font-size:.92rem;font-weight:600;color:var(--red);margin-bottom:5px}}
.err-body{{font-size:.8rem;color:var(--sub);line-height:1.7}}
.etf-tag{{display:inline-block;background:rgba(30,122,106,.2);border:1px solid var(--t2);border-radius:5px;padding:1px 7px;font-family:'Share Tech Mono',monospace;font-size:.53rem;color:var(--t3);letter-spacing:.07em;margin-bottom:5px}}
.hidden{{display:none!important}}

/* MODALS */
.overlay{{position:fixed;inset:0;background:rgba(0,0,0,.88);backdrop-filter:blur(6px);z-index:1000;display:none;align-items:center;justify-content:center;padding:16px}}
.overlay.on{{display:flex}}
.modal{{background:var(--panel);border:1px solid var(--gold);border-radius:20px;max-width:390px;width:100%;overflow:hidden;animation:slideUp .3s cubic-bezier(.22,1,.36,1);max-height:92vh;overflow-y:auto}}
@keyframes slideUp{{from{{opacity:0;transform:translateY(28px)}}to{{opacity:1;transform:translateY(0)}}}}
.mband{{height:3px;background:linear-gradient(90deg,var(--gold),var(--t2),var(--g3))}}
.mbody{{padding:22px 20px 24px}}
.mgem{{width:44px;height:44px;background:linear-gradient(135deg,var(--g2),var(--g3));transform:rotate(45deg);display:flex;align-items:center;justify-content:center;margin:0 auto 14px}}
.mgem span{{transform:rotate(-45deg);font-size:.85rem;color:var(--bg)}}
.mtitle{{text-align:center;font-size:1.35rem;font-weight:300;color:var(--g3);letter-spacing:.13em;margin-bottom:5px}}
.msub{{text-align:center;font-size:.76rem;color:var(--sub);font-style:italic;line-height:1.7;margin-bottom:18px}}
.mprice{{text-align:center;margin-bottom:16px}}
.mprice-num{{font-size:2.3rem;font-weight:300;color:var(--g3)}}
.mprice-per{{font-size:.84rem;color:var(--sub);font-style:italic}}
.mfeatures{{list-style:none;margin-bottom:18px;display:flex;flex-direction:column;gap:6px}}
.mfeatures li{{display:flex;align-items:center;gap:8px;font-size:.8rem;color:var(--sub);font-style:italic}}
.mfeatures li::before{{content:'◆';color:var(--g2);font-size:.48rem;flex-shrink:0}}
.btn-big{{width:100%;background:linear-gradient(135deg,var(--turq),var(--t2));color:var(--bg);border:none;border-radius:13px;padding:13px;font-family:'Cormorant Garamond',serif;font-size:.97rem;font-weight:600;letter-spacing:.07em;cursor:pointer;transition:all .22s;margin-bottom:7px;display:block;text-align:center}}
.btn-big:hover{{transform:translateY(-1px);opacity:.92}}
.btn-big:disabled{{background:var(--muted);cursor:default;transform:none}}
.btn-ghost-modal{{width:100%;background:transparent;color:var(--dim);border:none;font-family:'Cormorant Garamond',serif;font-size:.76rem;font-style:italic;cursor:pointer;padding:4px}}
.btn-ghost-modal:hover{{color:var(--sub)}}
.afield{{margin-bottom:12px}}
.albl{{font-family:'Share Tech Mono',monospace;font-size:.54rem;color:var(--muted);letter-spacing:.09em;margin-bottom:4px;display:block}}
.ainput{{width:100%;background:var(--card);border:1px solid var(--b2);border-radius:9px;padding:10px 12px;color:var(--text);font-family:'Cormorant Garamond',serif;font-size:.97rem;outline:none;transition:border-color .2s}}
.ainput:focus{{border-color:var(--t2)}}
.aerror{{font-size:.72rem;color:var(--red);font-style:italic;text-align:center;margin-bottom:8px;min-height:17px}}
.aswitch{{text-align:center;font-size:.74rem;color:var(--dim);font-style:italic;margin-top:8px}}
.aswitch a{{color:var(--g2);cursor:pointer}}
.acct-email{{font-family:'Share Tech Mono',monospace;font-size:.56rem;color:var(--dim);text-align:center;margin-bottom:12px}}
.toast{{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(70px);background:var(--green);color:#fff;border-radius:11px;padding:10px 18px;font-size:.8rem;font-style:italic;z-index:2000;opacity:0;transition:all .35s cubic-bezier(.22,1,.36,1);white-space:nowrap;max-width:88vw;text-align:center}}
.toast.on{{opacity:1;transform:translateX(-50%) translateY(0)}}
.price-card{{background:var(--card);border:1px solid var(--gold);border-radius:16px;overflow:hidden;margin-bottom:11px}}
.price-band{{height:2px;background:linear-gradient(90deg,var(--gold),var(--t2),var(--g3))}}
.price-inner{{padding:18px 16px}}
.price-title{{font-size:.68rem;color:var(--gold);font-weight:600;letter-spacing:.11em;margin-bottom:10px}}
.price-amt{{font-size:2.1rem;font-weight:300;color:var(--g3);margin-bottom:2px}}
.price-note{{font-size:.68rem;color:var(--sub);font-style:italic;margin-bottom:14px}}
/* about */
.form-card{{background:var(--card);border:1px solid var(--border);border-left:3px solid var(--t2);border-radius:0 9px 9px 0;padding:10px 13px;margin-bottom:7px}}
.fn{{font-size:.76rem;color:var(--g2);font-weight:600;margin-bottom:2px;letter-spacing:.04em}}
.feq{{font-family:'Share Tech Mono',monospace;font-size:.6rem;color:var(--t3);margin-bottom:2px}}
.fdesc{{font-size:.68rem;color:var(--muted);font-style:italic;line-height:1.6}}
.disc-box{{background:var(--card);border:1px solid var(--border);border-radius:11px;padding:11px 13px;margin-top:11px}}
.disc-box p{{font-size:.62rem;color:var(--dim);font-style:italic;line-height:1.8}}
.about-page{{padding:0 0 40px}}
.about-hero{{background:linear-gradient(180deg,var(--card2) 0%,var(--bg) 100%);border-bottom:1px solid var(--border);padding:36px 20px 32px;text-align:center;position:relative;overflow:hidden}}
.about-hero::before{{content:'';position:absolute;inset:0;background:radial-gradient(ellipse 80% 60% at 50% 0%,rgba(200,138,26,.07),transparent 70%);pointer-events:none}}
.about-hero-gem{{width:56px;height:56px;background:linear-gradient(135deg,var(--g2),var(--g3));transform:rotate(45deg);display:flex;align-items:center;justify-content:center;margin:0 auto 20px;box-shadow:0 0 40px rgba(232,170,52,.2),0 0 0 1px rgba(232,170,52,.2)}}
.about-hero-gem span{{transform:rotate(-45deg);font-size:1rem;color:var(--bg);font-weight:700}}
.about-hero-title{{font-size:2.6rem;font-weight:300;color:var(--g3);letter-spacing:.35em;margin-bottom:4px;position:relative;z-index:1}}
.about-hero-sub{{font-size:.78rem;color:var(--sub);font-style:italic;letter-spacing:.18em;margin-bottom:22px;position:relative;z-index:1;font-family:'Share Tech Mono',monospace}}
.about-hero-rule{{width:120px;height:1px;background:linear-gradient(90deg,transparent,var(--gold),transparent);margin:0 auto 22px}}
.about-hero-pitch{{max-width:480px;margin:0 auto;font-size:.9rem;color:var(--sub);line-height:1.85;font-style:italic;position:relative;z-index:1}}
.about-section-label{{font-family:'Share Tech Mono',monospace;font-size:.58rem;color:var(--muted);letter-spacing:.18em;padding:22px 18px 10px;display:flex;align-items:center;gap:10px}}
.about-section-label::after{{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--border),transparent)}}
.about-features{{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:0 14px 4px}}
.af-card{{background:linear-gradient(160deg,var(--card2),var(--card));border:1px solid var(--border);border-radius:16px;padding:16px 14px 14px;position:relative;overflow:hidden;transition:transform .2s,box-shadow .2s}}
.af-card:hover{{transform:translateY(-2px);box-shadow:0 8px 32px rgba(0,0,0,.3)}}
.af-card::before{{content:'';position:absolute;top:0;left:0;right:0;height:2px}}
.af-card.af-gold::before{{background:linear-gradient(90deg,var(--gold),var(--g2))}}
.af-card.af-turq::before{{background:linear-gradient(90deg,var(--turq),var(--t2))}}
.af-card.af-muted::before{{background:linear-gradient(90deg,var(--muted),var(--dim))}}
.af-icon{{font-size:1.3rem;margin-bottom:8px;opacity:.85}}
.af-title{{font-family:'Share Tech Mono',monospace;font-size:.58rem;font-weight:700;letter-spacing:.1em;margin-bottom:7px}}
.af-card.af-gold .af-title{{color:var(--g2)}}
.af-card.af-turq .af-title{{color:var(--t3)}}
.af-card.af-muted .af-title{{color:var(--sub)}}
.af-body{{font-size:.72rem;color:var(--sub);line-height:1.7;margin-bottom:10px;font-style:italic}}
.af-formula{{font-family:'Share Tech Mono',monospace;font-size:.5rem;color:var(--dim);border-top:1px solid var(--border);padding-top:8px;letter-spacing:.04em}}
.about-model-card{{display:flex;margin:0 14px 8px;background:linear-gradient(160deg,var(--card2),var(--card));border:1px solid var(--border);border-radius:13px;overflow:hidden;transition:transform .15s}}
.about-model-card:hover{{transform:translateX(2px)}}
.amc-left{{width:3px;flex-shrink:0}}
.amc-left.gold{{background:linear-gradient(180deg,var(--g3),var(--gold))}}
.amc-left.turq{{background:linear-gradient(180deg,var(--t3),var(--turq))}}
.amc-left.muted{{background:linear-gradient(180deg,var(--muted),var(--dim))}}
.amc-body{{padding:12px 14px;flex:1}}
.amc-name{{font-size:.78rem;font-weight:600;color:var(--g2);letter-spacing:.05em;margin-bottom:3px}}
.amc-eq{{font-family:'Share Tech Mono',monospace;font-size:.58rem;color:var(--t3);margin-bottom:6px}}
.amc-desc{{font-size:.7rem;color:var(--sub);font-style:italic;line-height:1.65}}
.about-pricing{{margin:0 14px 14px;background:linear-gradient(160deg,var(--card2),var(--card));border:1px solid var(--gold);border-radius:16px;overflow:hidden;display:flex;align-items:stretch}}
.ap-left{{padding:22px 20px;text-align:center;flex-shrink:0;display:flex;flex-direction:column;justify-content:center;background:rgba(200,138,26,.06)}}
.ap-price{{font-size:2.4rem;font-weight:300;color:var(--g3);line-height:1}}
.ap-per{{font-size:.9rem;color:var(--sub)}}
.ap-cancel{{font-size:.58rem;color:var(--dim);font-style:italic;margin-top:4px;font-family:'Share Tech Mono',monospace;letter-spacing:.04em}}
.ap-divider{{width:1px;background:var(--border);flex-shrink:0}}
.ap-features{{padding:16px 18px;display:flex;flex-direction:column;gap:7px;justify-content:center}}
.ap-feat{{font-size:.76rem;color:var(--sub);font-style:italic}}
.about-subscribe-btn{{display:block;width:calc(100% - 28px);margin:0 14px 20px;background:linear-gradient(135deg,var(--turq),var(--t2));color:var(--bg);border:none;border-radius:13px;padding:15px;font-family:'Cormorant Garamond',serif;font-size:1.05rem;font-weight:600;letter-spacing:.08em;cursor:pointer;transition:all .22s;box-shadow:0 4px 20px rgba(30,122,106,.25)}}
.about-subscribe-btn:hover{{transform:translateY(-1px);box-shadow:0 8px 28px rgba(30,122,106,.35)}}
.about-disc{{margin:0 14px;font-size:.62rem;color:var(--dim);font-style:italic;line-height:1.8;text-align:center;padding:14px;border-top:1px solid var(--border)}}
.tabs{{box-shadow:0 2px 12px rgba(0,0,0,.2)}}
.mc{{box-shadow:0 2px 8px rgba(0,0,0,.2)}}
.health-card{{box-shadow:0 4px 20px rgba(0,0,0,.25)}}
.hero-card{{box-shadow:0 12px 48px rgba(0,0,0,.5),inset 0 1px 0 rgba(232,170,52,.1)}}
.verdict-detail-card{{box-shadow:0 4px 18px rgba(0,0,0,.2)}}
.ai-card{{box-shadow:0 4px 18px rgba(0,0,0,.2)}}
.modal{{box-shadow:0 24px 80px rgba(0,0,0,.6)}}
.empty-tile{{transition:transform .2s}}.empty-tile:hover{{transform:translateY(-2px)}}

</style>
</head>
<body>

<!-- HERO PAGE -->
<div id="hero" class="hero">
  <div class="hero-bg"></div>
  <div class="hero-gem"><span>◆</span></div>
  <div class="hero-title">SENECA</div>
  <div class="hero-sub">Intrinsic Value Oracle</div>
  <div class="hero-rule"></div>
  <div class="hero-pitch">Six <strong>classical valuation models</strong> — Graham, Buffett, Lynch and more — synthesised into one verdict. Stocks, ETFs and indexes. No noise. Just <strong>what it's actually worth.</strong></div>
  <div class="hero-btns">
    <button class="btn-teal" onclick="enterApp()">◈ &nbsp;Try Free Lookup</button>
    <button class="btn-outline" onclick="clickSubscribe()">✦ &nbsp;Subscribe — $9.99/mo</button>
  </div>
  <div class="hero-badges">
    <span class="badge">Graham · Buffett · Lynch</span>
    <span class="badge">Health Score</span>
    <span class="badge">AI Analysis</span>
    <span class="badge">ETF / Index Models</span>
    <span class="badge">Login · Any Device</span>
  </div>
  <div class="hero-note">First lookup free · <a onclick="openModal('login')">Sign in to your account</a></div>
</div>

<!-- APP -->
<div id="shell" class="shell">
  <div class="hdr">
    <!-- Logo — click to go home -->
    <div class="hdr-logo" onclick="goHome()" role="button" title="Return to home">
      <div class="hdr-gem"><span>◆</span></div>
      <div class="hdr-logo-text">
        <div class="hdr-logo-name">SENECA</div>
        <div class="hdr-logo-tag">Intrinsic Value Oracle</div>
      </div>
      <div class="logo-home-hint">↩ HOME</div>
    </div>
    <!-- Center decorative rule -->
    <div class="hdr-rule"></div>
    <!-- Right actions -->
    <div class="hdr-right">
      <button class="btn-acct hidden" id="btn-acct" onclick="openModal('acct')">◆ Account</button>
      <button class="btn-sub" id="btn-sub" onclick="clickSubscribe()">✦ Subscribe</button>
    </div>
  </div>

  <div id="wl-bar" class="wl-bar"><div class="wl-title">◈ WATCHLIST</div><div id="wl-items" class="wl-items"></div></div>

  <div class="search-wrap">
    <div class="search-box">
      <div class="search-row">
        <input id="search" class="search-input" type="text" placeholder="Apple · AAPL · SPY · S&P 500" maxlength="60" autocomplete="off" spellcheck="false"/>
        <button id="btn-go" class="btn-analyze" onclick="doAnalyze()">◈ Analyze</button>
      </div>
      <div class="search-hint">Ticker symbol OR company name OR ETF name</div>
    </div>
    <div class="chips">
      <span class="chip" onclick="setQ('AAPL')">AAPL</span>
      <span class="chip" onclick="setQ('MSFT')">MSFT</span>
      <span class="chip" onclick="setQ('TSLA')">TSLA</span>
      <span class="chip" onclick="setQ('NVDA')">NVDA</span>
      <span class="chip" onclick="setQ('SPY')">SPY</span>
      <span class="chip" onclick="setQ('QQQ')">QQQ</span>
      <span class="chip" onclick="setQ('VOO')">VOO</span>
      <span class="chip" onclick="setQ('KO')">KO</span>
      <span class="chip" onclick="setQ('AMZN')">AMZN</span>
      <span class="chip" onclick="setQ('BRK-B')">BRK-B</span>
    </div>
  </div>

  <div id="cmp-bar" class="cmp-bar">
    <div class="cmp-row">
      <input id="cmp1" class="cmp-input" type="text" placeholder="AAPL" maxlength="60" autocomplete="off"/>
      <span style="color:var(--dim);flex-shrink:0">vs</span>
      <input id="cmp2" class="cmp-input" type="text" placeholder="MSFT" maxlength="60" autocomplete="off"/>
      <button class="btn-cmp" onclick="doCompare()">Go</button>
    </div>
    <div class="cmp-hint">Tickers or company names both work</div>
  </div>

  <div id="status" class="status-bar">Enter a ticker, company name, or ETF to begin</div>
  <div class="tabs">
    <button class="tab on" id="tab-a" onclick="switchTab('a')">◈ &nbsp;Analyze</button>
    <button class="tab" id="tab-b" onclick="switchTab('b')">✦ &nbsp;About</button>
  </div>

  <main id="main-scroll">
    <div id="pane-a">
      <div id="spinner" class="spinner"><div class="ring"></div><div class="spin-txt">Consulting the oracle…</div></div>

      <!-- EMPTY STATE -->
      <div id="empty-state" class="empty-state">
        <div class="empty-quote-carousel">
          <div class="eq-slide active">
            <div class="eq-card">
              <div class="eq-text">Price is what you pay. Value is what you get.</div>
              <div class="eq-author">— WARREN BUFFETT</div>
            </div>
          </div>
          <div class="eq-slide">
            <div class="eq-card">
              <div class="eq-text">The stock market is a device for transferring money from the impatient to the patient.</div>
              <div class="eq-author">— WARREN BUFFETT</div>
            </div>
          </div>
          <div class="eq-slide">
            <div class="eq-card">
              <div class="eq-text">In the short run, the market is a voting machine, but in the long run, it is a weighing machine.</div>
              <div class="eq-author">— BENJAMIN GRAHAM</div>
            </div>
          </div>
          <div class="eq-slide">
            <div class="eq-card">
              <div class="eq-text">The four most dangerous words in investing are: this time it's different.</div>
              <div class="eq-author">— SIR JOHN TEMPLETON</div>
            </div>
          </div>
          <div class="eq-slide">
            <div class="eq-card">
              <div class="eq-text">Know what you own, and know why you own it.</div>
              <div class="eq-author">— PETER LYNCH</div>
            </div>
          </div>
          <div class="eq-slide">
            <div class="eq-card">
              <div class="eq-text">The individual investor should act consistently as an investor and not as a speculator.</div>
              <div class="eq-author">— BENJAMIN GRAHAM</div>
            </div>
          </div>
        </div>
        <div class="eq-dots" id="eq-dots"></div>
        <div class="empty-grid">
          <div class="empty-tile">
            <div class="empty-tile-icon">◆</div>
            <div class="empty-tile-title">6 MODELS</div>
            <div class="empty-tile-body">Graham, Buffett, Lynch, Simons, DCF — synthesised into one composite</div>
          </div>
          <div class="empty-tile">
            <div class="empty-tile-icon">⬡</div>
            <div class="empty-tile-title">HEALTH SCORE</div>
            <div class="empty-tile-body">Profitability, leverage, cash flow and accounting integrity graded A–F</div>
          </div>
          <div class="empty-tile">
            <div class="empty-tile-icon">◈</div>
            <div class="empty-tile-title">AI VERDICT</div>
            <div class="empty-tile-body">Claude cross-verifies fundamentals and surfaces hidden financial risks</div>
          </div>
          <div class="empty-tile">
            <div class="empty-tile-icon">⇄</div>
            <div class="empty-tile-title">COMPARE</div>
            <div class="empty-tile-body">Side-by-side valuation of any two stocks, ETFs or indexes</div>
          </div>
        </div>
        <div class="empty-cta">
          <div class="empty-cta-title">◈ &nbsp;QUICK START</div>
          <div class="market-ticker">
            <div class="mt-item" onclick="setQ('AAPL')"><div class="mt-sym">AAPL</div></div>
            <div class="mt-item" onclick="setQ('NVDA')"><div class="mt-sym">NVDA</div></div>
            <div class="mt-item" onclick="setQ('TSLA')"><div class="mt-sym">TSLA</div></div>
            <div class="mt-item" onclick="setQ('SPY')"><div class="mt-sym">SPY</div></div>
            <div class="mt-item" onclick="setQ('MSFT')"><div class="mt-sym">MSFT</div></div>
          </div>
          <div class="mt-tap">Tap any ticker above or type in the search bar</div>
        </div>
      </div>

      <div id="results" class="hidden results-wrap"></div>
      <div id="cmp-results" class="hidden results-wrap"></div>
    </div>

    <div id="pane-b" class="hidden">
      <div class="about-page">

        <!-- Hero banner -->
        <div class="about-hero">
          <div class="about-hero-gem"><span>◆</span></div>
          <div class="about-hero-title">SENECA</div>
          <div class="about-hero-sub">Intrinsic Value Oracle</div>
          <div class="about-hero-rule"></div>
          <div class="about-hero-pitch">Professional-grade fundamental analysis. Seven time-tested valuation models, an AI-powered health score, and forensic financial review — all synthesised into one clear verdict on any stock, ETF, or index.</div>
        </div>

        <!-- Feature grid -->
        <div class="about-section-label">◈ &nbsp;WHAT SENECA DELIVERS</div>
        <div class="about-features">

          <div class="af-card af-gold">
            <div class="af-icon">◆</div>
            <div class="af-title">7 VALUATION MODELS</div>
            <div class="af-body">Graham Number, Graham Growth, Buffett DCF, Peter Lynch PEG, Simons Quant, Free Cash Flow DCF, and Gordon Growth DDM — each grounded in decades of investment theory.</div>
            <div class="af-formula">Weighted composite = fair value consensus</div>
          </div>

          <div class="af-card af-turq">
            <div class="af-icon">⬡</div>
            <div class="af-title">HEALTH SCORE  A–F</div>
            <div class="af-body">A deterministic 0–100 score grading five financial pillars: Profitability, Leverage, Cash Flow Quality, Growth, and Valuation Sanity — flagging hidden risks most investors miss.</div>
            <div class="af-formula">Score = ROE + ROA + D/E + FCF + Coverage + Margins…</div>
          </div>

          <div class="af-card af-turq">
            <div class="af-icon">◈</div>
            <div class="af-title">AI FORENSIC ANALYSIS</div>
            <div class="af-body">Powered by Llama 3.3 70B, SENECA's AI cross-verifies every health score for accounting irregularities, hidden liabilities, altered definitions, and off-balance-sheet risk — in plain English.</div>
            <div class="af-formula">LLM × deterministic score = forensic verdict</div>
          </div>

          <div class="af-card af-gold">
            <div class="af-icon">⇄</div>
            <div class="af-title">SIDE-BY-SIDE COMPARE</div>
            <div class="af-body">Compare any two stocks, ETFs or indexes simultaneously. Composite fair value, health score, P/E, dividend yield, beta and verdict displayed side-by-side for clear relative analysis.</div>
            <div class="af-formula">Any two tickers · stocks, ETFs, indexes</div>
          </div>

          <div class="af-card af-muted">
            <div class="af-icon">◫</div>
            <div class="af-title">ETF &amp; INDEX MODELS</div>
            <div class="af-body">ETFs and index funds get purpose-built models: Fed Model (earnings yield vs Treasury), P/E Mean Reversion to historical 17×, and CAPM-based Gordon Growth DDM. Not just stock formulas applied blindly.</div>
            <div class="af-formula">Fed Model · P/E Reversion · ETF Gordon DDM</div>
          </div>

          <div class="af-card af-muted">
            <div class="af-icon">↓</div>
            <div class="af-title">PDF REPORT EXPORT</div>
            <div class="af-body">Download a professionally formatted report for any analysis — all seven models, health score, AI verdict, and composite — ready to share with clients, advisors, or your investment committee.</div>
            <div class="af-formula">Full report · branded · any device</div>
          </div>

        </div>

        <!-- Methodology section -->
        <div class="about-section-label">◈ &nbsp;THE MODELS</div>

        <div class="about-model-card">
          <div class="amc-left gold"></div>
          <div class="amc-body">
            <div class="amc-name">Graham Number</div>
            <div class="amc-eq">√( 22.5 × EPS × Book Value Per Share )</div>
            <div class="amc-desc">Benjamin Graham's bedrock formula. Takes the geometric mean of earnings and book value — the foundation of value investing since 1949. Only valid for profitable, asset-backed companies.</div>
          </div>
        </div>

        <div class="about-model-card">
          <div class="amc-left gold"></div>
          <div class="amc-body">
            <div class="amc-name">Graham Growth Formula</div>
            <div class="amc-eq">EPS × (8.5 + 2g) × 4.4 ÷ AAA bond yield</div>
            <div class="amc-desc">Graham's extension for growth stocks. Adjusts fair value based on expected earnings growth and the prevailing AAA bond yield — tying equity value to the cost of capital.</div>
          </div>
        </div>

        <div class="about-model-card">
          <div class="amc-left turq"></div>
          <div class="amc-body">
            <div class="amc-name">Buffett DCF</div>
            <div class="amc-eq">Σ EPS × (1+g)ⁿ ÷ (1.09)ⁿ + terminal value at 15×</div>
            <div class="amc-desc">Discounts ten years of projected EPS at a 9% rate with a 15× terminal earnings multiple — Warren Buffett's owner-earnings approach adapted for individual stocks.</div>
          </div>
        </div>

        <div class="about-model-card">
          <div class="amc-left turq"></div>
          <div class="amc-body">
            <div class="amc-name">Peter Lynch PEG</div>
            <div class="amc-eq">EPS × earnings growth % (PEG = 1 is fairly valued)</div>
            <div class="amc-desc">Lynch's famous insight: a fairly priced stock has a P/E equal to its growth rate. Simple, powerful, and famously effective for identifying growth stocks at reasonable prices.</div>
          </div>
        </div>

        <div class="about-model-card">
          <div class="amc-left muted"></div>
          <div class="amc-body">
            <div class="amc-name">Simons Quant Factor</div>
            <div class="amc-eq">Price × (ROE ÷ P/E) × (1 ÷ P/B) × (1 + 0.3 × momentum)</div>
            <div class="amc-desc">A Renaissance-inspired multi-factor signal blending quality (ROE), value (P/E, P/B), and price momentum. Penalises overpriced, low-quality stocks and rewards the opposite.</div>
          </div>
        </div>

        <div class="about-model-card">
          <div class="amc-left muted"></div>
          <div class="amc-body">
            <div class="amc-name">Free Cash Flow DCF</div>
            <div class="amc-eq">Σ FCF/share × (1+g)ⁿ ÷ (1.10)ⁿ + terminal value</div>
            <div class="amc-desc">Discounts actual free cash flow per share — the real cash the business generates — at a 10% discount rate with a 2.5% perpetual terminal growth assumption.</div>
          </div>
        </div>

        <div class="about-model-card">
          <div class="amc-left gold"></div>
          <div class="amc-body">
            <div class="amc-name">Gordon Growth DDM</div>
            <div class="amc-eq">D₁ ÷ (CAPM required return − sustainable dividend growth)</div>
            <div class="amc-desc">The Gordon Growth Model using CAPM for the required return (4.3% risk-free + beta × 5.5% equity premium) and dividend growth capped at 3.5%. Only applied to dividend-paying companies.</div>
          </div>
        </div>

        <!-- Pricing -->
        <div class="about-section-label">◈ &nbsp;FULL ACCESS</div>
        <div class="about-pricing">
          <div class="ap-left">
            <div class="ap-price">$9.99<span class="ap-per">/mo</span></div>
            <div class="ap-cancel">Cancel anytime · No contracts</div>
          </div>
          <div class="ap-divider"></div>
          <div class="ap-features">
            <div class="ap-feat">✦ &nbsp;Unlimited stock, ETF &amp; index lookups</div>
            <div class="ap-feat">✦ &nbsp;7 valuation models + composite score</div>
            <div class="ap-feat">✦ &nbsp;AI health score with forensic analysis</div>
            <div class="ap-feat">✦ &nbsp;Persistent watchlist across all devices</div>
            <div class="ap-feat">✦ &nbsp;PDF report export for every analysis</div>
            <div class="ap-feat">✦ &nbsp;Side-by-side stock comparison</div>
          </div>
        </div>
        <button class="about-subscribe-btn" onclick="clickSubscribe()">✦ &nbsp;Subscribe Now — $9.99/mo</button>

        <div class="about-disc">SENECA is for educational and research purposes only. Not investment advice. Always conduct your own due diligence before making any investment decision.</div>
      </div>
    </div>
  </main>
</div>

<!-- PAYWALL MODAL -->
<div id="modal-pay" class="overlay">
  <div class="modal"><div class="mband"></div><div class="mbody">
    <div class="mgem"><span>◆</span></div>
    <div class="mtitle">UNLOCK SENECA</div>
    <div class="msub">Free lookup used. Subscribe for unlimited access.</div>
    <div class="mprice"><div class="mprice-num">$9.99</div><div class="mprice-per">per month · cancel anytime</div></div>
    <ul class="mfeatures">
      <li>Unlimited lookups on any device</li>
      <li>Health Score — financial integrity grading</li>
      <li>AI forensic analysis of every stock</li>
      <li>Persistent watchlist across devices</li>
    </ul>
    <button class="btn-big" id="pay-btn" onclick="launchStripe()">✦ &nbsp;Subscribe Now — $9.99/mo</button>
    <button class="btn-ghost-modal" onclick="closeModal('modal-pay')">Maybe later</button>
  </div></div>
</div>

<!-- SIGNUP MODAL -->
<div id="modal-signup" class="overlay">
  <div class="modal"><div class="mband"></div><div class="mbody">
    <div class="mgem"><span>◆</span></div>
    <div class="mtitle">CREATE ACCOUNT</div>
    <div class="msub">Create account then subscribe. Watchlist saved across all devices.</div>
    <div class="aerror" id="signup-err"></div>
    <div class="afield"><label class="albl">EMAIL</label><input class="ainput" id="signup-email" type="email" placeholder="you@example.com" autocomplete="email"/></div>
    <div class="afield"><label class="albl">PASSWORD</label><input class="ainput" id="signup-pw" type="password" placeholder="••••••••"/></div>
    <button class="btn-big" id="signup-btn" onclick="doSignup()">✦ &nbsp;Create Account</button>
    <button class="btn-ghost-modal" onclick="closeModal('modal-signup')">Cancel</button>
    <div class="aswitch">Have an account? <a onclick="switchModal('modal-signup','modal-login')">Sign in</a></div>
  </div></div>
</div>

<!-- LOGIN MODAL -->
<div id="modal-login" class="overlay">
  <div class="modal"><div class="mband"></div><div class="mbody">
    <div class="mgem"><span>◆</span></div>
    <div class="mtitle">SIGN IN</div>
    <div class="msub">Welcome back to SENECA.</div>
    <div class="aerror" id="login-err"></div>
    <div class="afield"><label class="albl">EMAIL</label><input class="ainput" id="login-email" type="email" placeholder="you@example.com" autocomplete="email"/></div>
    <div class="afield"><label class="albl">PASSWORD</label><input class="ainput" id="login-pw" type="password" placeholder="••••••••"/></div>
    <button class="btn-big" id="login-btn" onclick="doLogin()">✦ &nbsp;Sign In</button>
    <button class="btn-ghost-modal" onclick="closeModal('modal-login')">Cancel</button>
    <div class="aswitch">No account? <a onclick="switchModal('modal-login','modal-signup')">Sign up</a></div>
  </div></div>
</div>

<!-- ACCOUNT MODAL -->
<div id="modal-acct" class="overlay">
  <div class="modal"><div class="mband"></div><div class="mbody">
    <div class="mtitle">◆ ACCOUNT</div>
    <div class="acct-email" id="acct-email"></div>
    <div id="acct-status" style="text-align:center;margin-bottom:16px;font-size:.8rem;color:var(--sub);font-style:italic"></div>
    <button class="btn-big" id="acct-sub-btn" onclick="closeModal('modal-acct');launchStripe()" style="display:none">✦ &nbsp;Subscribe Now — $9.99/mo</button>
    <button class="btn-big" style="background:var(--card2);color:var(--sub);border:1px solid var(--b2);margin-bottom:7px" onclick="doLogout()">Sign Out</button>
    <button class="btn-ghost-modal" onclick="closeModal('modal-acct')">Close</button>
  </div></div>
</div>

<div id="toast" class="toast"></div>

<script>
let userEmail = {json.dumps(email)};
let userSub   = {'true' if sub else 'false'};
let watchlist = [];
let lastTicker = '';
let cmpOpen = false;
let quoteSlide = 0;
const QUOTES_COUNT = 6;

// ── Init ──────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {{
  if (userEmail) {{
    userSub = {'true' if sub else 'false'};
    // Load server watchlist
    fetch('/api/watchlist').then(r=>r.json()).then(d=>{{
      if(d.ok){{ watchlist=d.watchlist||[]; renderWL(); }}
    }}).catch(()=>{{}});
    syncHeader();
  }} else {{
    watchlist = JSON.parse(sessionStorage.getItem('wl') || '[]');
    renderWL();
  }}
  initQuotes();
  {toast_js}
}});

// ── Quote carousel ────────────────────────────────────────────────────────────
function initQuotes() {{
  const dots = document.getElementById('eq-dots');
  if (!dots) return;
  for(let i=0;i<QUOTES_COUNT;i++) {{
    const d = document.createElement('div');
    d.className = 'eq-dot' + (i===0?' active':'');
    d.onclick = () => goQuote(i);
    dots.appendChild(d);
  }}
  setInterval(() => goQuote((quoteSlide+1)%QUOTES_COUNT), 5000);
}}
function goQuote(n) {{
  const slides = document.querySelectorAll('.eq-slide');
  const dots = document.querySelectorAll('.eq-dot');
  if (!slides.length) return;
  slides[quoteSlide].classList.remove('active');
  dots[quoteSlide].classList.remove('active');
  quoteSlide = n;
  slides[quoteSlide].classList.add('active');
  dots[quoteSlide].classList.add('active');
}}

// ── Header / state ────────────────────────────────────────────────────────────
function syncHeader() {{
  if (userEmail) {{
    document.getElementById('btn-acct').classList.remove('hidden');
    document.getElementById('btn-sub').style.display = userSub ? 'none' : '';
  }} else {{
    document.getElementById('btn-acct').classList.add('hidden');
    document.getElementById('btn-sub').style.display = '';
  }}
}}
function enterApp() {{
  document.getElementById('hero').style.display = 'none';
  document.getElementById('shell').classList.add('on');
}}
function goHome() {{
  // Return to hero page — reset app state cleanly
  document.getElementById('shell').classList.remove('on');
  document.getElementById('hero').style.display = '';
  // Scroll hero back to top
  window.scrollTo({{top:0,behavior:'smooth'}});
  // Reset analyze state
  document.getElementById('search').value = '';
  document.getElementById('results').classList.add('hidden');
  document.getElementById('cmp-results').classList.add('hidden');
  document.getElementById('empty-state').classList.remove('hidden');
  document.getElementById('spinner').classList.remove('on');
  setStatus('Enter a ticker, company name, or ETF to begin');
}}
function switchTab(t) {{
  ['a','b'].forEach(x=>{{
    document.getElementById('pane-'+x).classList.toggle('hidden',x!==t);
    document.getElementById('tab-'+x).classList.toggle('on',x===t);
  }});
}}
function setQ(v) {{ document.getElementById('search').value=v; doAnalyze(); }}
document.getElementById('search').addEventListener('keydown',e=>{{ if(e.key==='Enter'){{e.preventDefault();doAnalyze();}} }});

// ── Modals ────────────────────────────────────────────────────────────────────
function openModal(name) {{
  if(name==='acct') {{
    document.getElementById('acct-email').textContent='◆ '+userEmail;
    if(userSub) {{
      document.getElementById('acct-status').textContent='✦ Active subscription';
      document.getElementById('acct-status').style.color='var(--green)';
      document.getElementById('acct-sub-btn').style.display='none';
    }} else {{
      document.getElementById('acct-status').textContent='No active subscription';
      document.getElementById('acct-status').style.color='var(--dim)';
      document.getElementById('acct-sub-btn').style.display='block';
    }}
  }}
  document.getElementById('modal-'+name).classList.add('on');
}}
function closeModal(id) {{ document.getElementById(id).classList.remove('on'); }}
function switchModal(from,to) {{ closeModal(from); openModal(to.replace('modal-','')); }}
['modal-pay','modal-signup','modal-login','modal-acct'].forEach(id=>{{
  document.getElementById(id).addEventListener('click',e=>{{ if(e.target===e.currentTarget) closeModal(id); }});
}});

// ── Subscribe ─────────────────────────────────────────────────────────────────
function clickSubscribe() {{
  enterApp();
  if(userSub) {{ toast('✦ You already have full access!'); return; }}
  if(userEmail) {{ launchStripe(); }}
  else {{ openModal('signup'); }}
}}
async function launchStripe() {{
  const btn=document.getElementById('pay-btn');
  if(btn) {{ btn.disabled=true; btn.textContent='Redirecting…'; }}
  try {{
    const r=await fetch('/api/checkout',{{method:'POST'}});
    const d=await r.json();
    if(d.url) {{ window.location.href=d.url; }}
    else {{ userSub=true; syncHeader(); closeModal('modal-pay'); toast('✦ Demo mode: full access!'); }}
  }} catch(e) {{ userSub=true; syncHeader(); closeModal('modal-pay'); toast('✦ Demo mode!'); }}
  finally {{ if(btn) {{ btn.disabled=false; btn.textContent='✦  Subscribe Now — $9.99/mo'; }} }}
}}

// ── Auth ──────────────────────────────────────────────────────────────────────
async function doSignup() {{
  const email=document.getElementById('signup-email').value.trim();
  const pw=document.getElementById('signup-pw').value;
  const btn=document.getElementById('signup-btn');
  const err=document.getElementById('signup-err');
  err.textContent='';
  if(!email||!pw) {{ err.textContent='Please fill in both fields.'; return; }}
  btn.disabled=true; btn.textContent='Creating account…';
  try {{
    const r=await fetch('/api/signup',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email,pw}})}});
    const d=await r.json();
    if(!d.ok) {{ err.textContent=d.error||'Error'; return; }}
    userEmail=d.email; userSub=d.sub;
    watchlist=d.watchlist||[];
    renderWL(); syncHeader(); closeModal('modal-signup'); enterApp();
    toast('◆ Account created! Launching checkout…');
    setTimeout(launchStripe,600);
  }} catch(e) {{ err.textContent='Network error. Try again.'; }}
  finally {{ btn.disabled=false; btn.textContent='✦  Create Account'; }}
}}
async function doLogin() {{
  const email=document.getElementById('login-email').value.trim();
  const pw=document.getElementById('login-pw').value;
  const btn=document.getElementById('login-btn');
  const err=document.getElementById('login-err');
  err.textContent='';
  if(!email||!pw) {{ err.textContent='Please fill in both fields.'; return; }}
  btn.disabled=true; btn.textContent='Signing in…';
  try {{
    const r=await fetch('/api/login',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email,pw}})}});
    const d=await r.json();
    if(!d.ok) {{ err.textContent=d.error||'Error'; return; }}
    userEmail=d.email; userSub=d.sub;
    watchlist=d.watchlist||[];
    renderWL(); syncHeader(); closeModal('modal-login'); enterApp();
    toast(userSub?'✦ Welcome back! Subscription active.':'◆ Signed in.');
  }} catch(e) {{ err.textContent='Network error. Try again.'; }}
  finally {{ btn.disabled=false; btn.textContent='✦  Sign In'; }}
}}
async function doLogout() {{
  await fetch('/api/logout',{{method:'POST'}});
  userEmail=''; userSub=false; watchlist=[];
  syncHeader(); renderWL(); closeModal('modal-acct'); toast('Signed out.');
}}

// ── Watchlist (server-persisted for logged in users) ──────────────────────────
function saveWL() {{
  if(userEmail) {{
    fetch('/api/watchlist',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{watchlist}})}}).catch(()=>{{}});
  }} else {{
    sessionStorage.setItem('wl',JSON.stringify(watchlist));
  }}
}}
function renderWL() {{
  const bar=document.getElementById('wl-bar');
  const items=document.getElementById('wl-items');
  if(!watchlist.length) {{ bar.classList.remove('on'); return; }}
  bar.classList.add('on');
  items.innerHTML=watchlist.map(t=>
    `<span class="wl-chip" onclick="setQ('${{t}}')">${{t}}<button class="wl-rm" onclick="event.stopPropagation();removeWL('${{t}}')">✕</button></span>`
  ).join('');
}}
function addWL(ticker) {{
  if(!ticker||watchlist.includes(ticker)) return;
  watchlist.push(ticker); saveWL(); renderWL(); toast('◆ '+ticker+' saved to watchlist');
}}
function removeWL(ticker) {{
  watchlist=watchlist.filter(t=>t!==ticker); saveWL(); renderWL();
}}

// ── Compare ───────────────────────────────────────────────────────────────────
function toggleCmp() {{
  cmpOpen=!cmpOpen;
  document.getElementById('cmp-bar').classList.toggle('on',cmpOpen);
  document.getElementById('btn-cmp').classList.toggle('active',cmpOpen);
  if(cmpOpen) document.getElementById('cmp1').focus();
}}
async function doCompare() {{
  const t1=document.getElementById('cmp1').value.trim();
  const t2=document.getElementById('cmp2').value.trim();
  if(!t1||!t2) {{ toast('Enter two tickers or names'); return; }}
  switchTab('a');
  document.getElementById('spinner').classList.add('on');
  document.getElementById('results').classList.add('hidden');
  document.getElementById('cmp-results').classList.add('hidden');
  document.getElementById('empty-state').classList.add('hidden');
  setStatus('Comparing '+t1+' vs '+t2+'…','var(--t3)');
  try {{
    const [r1,r2]=await Promise.all([fetch('/api/quote?q='+encodeURIComponent(t1)),fetch('/api/quote?q='+encodeURIComponent(t2))]);
    if(r1.status===402||r2.status===402) {{ openModal('pay'); return; }}
    if(!r1.ok||!r2.ok) throw new Error('Could not fetch one or both tickers');
    const [d1,d2]=await Promise.all([r1.json(),r2.json()]);
    renderCmp(d1,d2);
    setStatus('Comparison: '+d1.ticker+' vs '+d2.ticker,'var(--green)');
  }} catch(e) {{ setStatus('⚠ '+e.message,'var(--red)'); }}
  finally {{ document.getElementById('spinner').classList.remove('on'); }}
}}
function renderCmp(d1,d2) {{
  function fp(v) {{ return(!v||v<=0)?'N/A':'$'+v.toLocaleString('en-US',{{minimumFractionDigits:2,maximumFractionDigits:2}}); }}
  const win1=(d1.composite&&d2.composite)?d1.composite>d2.composite:false;
  function col(d,win) {{
    const h=d.health||{{}};
    return `<div class="cmp-col ${{win?'win':''}}">
      <div class="cmp-hdr"><div class="cmp-tkr">${{d.ticker}}${{win?' ✦':''}}</div><div class="cmp-nm">${{d.name}}</div><div class="cmp-px">${{fp(d.price)}}</div></div>
      <div class="cmp-r"><span class="cl">Composite FV</span><span class="cv">${{fp(d.composite)}}</span></div>
      <div class="cmp-r"><span class="cl">Health Score</span><span class="cv">${{h.score||'—'}}/100 (Grade ${{h.grade||'?'}})</span></div>
      <div class="cmp-r"><span class="cl">P/E</span><span class="cv">${{d.pe?d.pe.toFixed(1)+'×':'—'}}</span></div>
      <div class="cmp-r"><span class="cl">Div Yield</span><span class="cv">${{d.div_y?d.div_y.toFixed(2)+'%':'—'}}</span></div>
      <div class="cmp-r"><span class="cl">Beta</span><span class="cv">${{d.beta?d.beta.toFixed(2):'—'}}</span></div>
      <div class="cmp-vd ${{d.verdict_cls==='down'?'dn':d.verdict_cls}}">${{d.verdict_text}}</div>
    </div>`;
  }}
  document.getElementById('cmp-results').innerHTML=`<div class="sec">◈ &nbsp;COMPARISON</div><div class="cmp-grid">${{col(d1,win1)}}${{col(d2,!win1)}}</div>`;
  document.getElementById('cmp-results').classList.remove('hidden');
  document.getElementById('results').classList.add('hidden');
}}

// ── PDF ───────────────────────────────────────────────────────────────────────
async function exportPDF() {{
  if(!lastTicker) {{ toast('Analyze a stock first'); return; }}
  toast('◆ Generating report…');
  try {{
    const r=await fetch('/api/pdf?q='+encodeURIComponent(lastTicker));
    if(r.status===402) {{ openModal('pay'); return; }}
    if(!r.ok) throw new Error('Report error');
    const blob=await r.blob();
    const url=URL.createObjectURL(blob);
    const a=document.createElement('a');
    a.href=url; a.download='SENECA-'+lastTicker+'-report.pdf';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url); toast('◆ Report downloaded!');
  }} catch(e) {{ toast('⚠ '+e.message); }}
}}

// ── AI verdict ────────────────────────────────────────────────────────────────
async function loadAI(ticker) {{
  const el=document.getElementById('ai-text');
  if(!el) return;
  try {{
    const r=await fetch('/api/ai?q='+encodeURIComponent(ticker));
    if(r.ok) {{
      const d=await r.json();
      el.innerHTML=d.verdict
        ?'<span class="ai-text">'+d.verdict+'</span>'
        :'<span style="color:var(--dim);font-size:.7rem;font-style:italic">Add GROQ_API_KEY in Railway to enable AI analysis</span>';
    }}
  }} catch(e) {{ el.innerHTML='<span style="color:var(--dim);font-size:.7rem;font-style:italic">AI unavailable</span>'; }}
}}

// ── Health AI ─────────────────────────────────────────────────────────────────
async function loadHealthAI(ticker) {{
  const el=document.getElementById('health-ai-text');
  if(!el) return;
  try {{
    const r=await fetch('/api/health-ai?q='+encodeURIComponent(ticker));
    if(r.ok) {{
      const d=await r.json();
      el.innerHTML=d.analysis
        ?'<span class="health-ai-text">'+d.analysis+'</span>'
        :'<span style="color:var(--dim);font-size:.7rem;font-style:italic">Add GROQ_API_KEY in Railway to enable AI forensics</span>';
    }}
  }} catch(e) {{ el.innerHTML='<span style="color:var(--dim);font-size:.7rem;font-style:italic">AI forensics unavailable</span>'; }}
}}

// ── Main analyze ──────────────────────────────────────────────────────────────
async function doAnalyze() {{
  const q=document.getElementById('search').value.trim();
  if(!q) return;
  switchTab('a'); enterApp();
  document.getElementById('btn-go').disabled=true;
  document.getElementById('results').classList.add('hidden');
  document.getElementById('cmp-results').classList.add('hidden');
  document.getElementById('empty-state').classList.add('hidden');
  document.getElementById('spinner').classList.add('on');
  setStatus('Consulting the oracle…','var(--t3)');
  try {{
    const r=await fetch('/api/quote?q='+encodeURIComponent(q));
    if(r.status===402) {{ setStatus('Free lookup used — subscribe for unlimited access','var(--g2)'); openModal('pay'); return; }}
    if(!r.ok) {{ const e=await r.json(); throw new Error(e.error||'Server error'); }}
    const d=await r.json();
    lastTicker=d.ticker; renderResult(d);
    setStatus('Analysis complete · '+d.ticker+' · '+new Date().toLocaleTimeString(),'var(--green)');
    loadAI(d.ticker);
    loadHealthAI(d.ticker);
  }} catch(e) {{
    setStatus('⚠ '+e.message,'var(--red)');
    // Map technical errors to friendly messages
    let friendly = e.message;
    if(friendly.includes('No market data') || friendly.includes('No price')) friendly = friendly;
    else if(friendly.includes('404') || friendly.includes('not found')) friendly = 'This symbol could not be found. Try typing the full company name or check the ticker.';
    else if(friendly.includes('network') || friendly.includes('fetch')) friendly = 'Connection issue. Please check your internet and try again.';
    else if(friendly.includes('timeout')) friendly = 'The request timed out. Markets may be closed or data is slow — please try again.';
    document.getElementById('results').innerHTML=`<div class="err"><div class="err-title">◈ Unable to load data</div><div class="err-body">${{friendly}}<br/><br/><span style="font-size:.7rem;color:var(--dim)">Tip: Try using the exact ticker symbol (e.g. AAPL instead of Apple Inc)</span></div></div>`;
    document.getElementById('results').classList.remove('hidden');
  }} finally {{
    document.getElementById('spinner').classList.remove('on');
    document.getElementById('btn-go').disabled=false;
  }}
}}

// ── Render result ─────────────────────────────────────────────────────────────
function renderResult(d) {{
  const p=d.price;
  const pct=(d.hi52>d.lo52&&d.lo52>0)?Math.min(Math.max((p-d.lo52)/(d.hi52-d.lo52),0),1)*100:null;
  const isEtf=d.asset_type==='etf';
  const compVal=d.composite&&d.composite>0?fp(d.composite):'N/A';
  const vc=d.verdict_cls==='down'?'dn':d.verdict_cls;
  const vcol=d.verdict_cls==='up'?'var(--green)':d.verdict_cls==='down'?'var(--red)':'var(--g2)';
  const margin=(d.composite&&d.composite>0&&p>0)?((d.composite-p)/p*100):null;
  const marginTxt=margin!==null?(margin>=0?'+':'')+margin.toFixed(1)+'% vs price':'';
  const h=d.health||{{}};
  let html='';

  // ── HERO CARD: price + composite at top ──
  html+=`<div class="hero-card">
    <div class="hero-card-band ${{vc}}"></div>
    <div class="hero-card-inner">
      <div class="hero-left">
        ${{isEtf?'<div class="etf-tag">◈ ETF / INDEX</div>':''}}
        <div class="hero-name">${{d.name}}</div>
        <div class="hero-ticker">${{d.ticker}}</div>
        <div class="hero-sector">${{d.sector}} · ${{fc(d.cap)}}</div>
      </div>
      <div class="hero-right">
        <div class="hero-price-block">
          <div class="hero-price-label">MARKET PRICE</div>
          <div class="hero-price">${{fp(p)}}</div>
          <div class="hero-chg ${{d.chg>=0?'up':'dn'}}">${{d.chg>=0?'▲':'▼'}} ${{Math.abs(d.chg).toFixed(2)}}%</div>
        </div>
        <div class="hero-divider-v"></div>
        <div class="hero-comp-block">
          <div class="hero-price-label">SENECA COMPOSITE</div>
          <div class="hero-comp-val">${{compVal}}</div>
          <div class="hero-margin ${{vc}}">${{marginTxt}}</div>
        </div>
      </div>
    </div>
    <div class="hero-verdict ${{vc}}">${{d.verdict_text}}</div>
    <div class="hero-range">
      <div class="rng-labels">
        <span class="rlo">${{d.lo52>0?'$'+d.lo52.toFixed(2):'—'}}</span>
        <span class="rmid">52 · W E E K · R A N G E</span>
        <span class="rhi">${{d.hi52>0?'$'+d.hi52.toFixed(2):'—'}}</span>
      </div>
      <div class="rng-track">
        <div class="rng-fill" style="width:${{pct!==null?pct:0}}%"></div>
        <div class="rng-thumb" style="left:${{pct!==null?pct:0}}%"></div>
      </div>
      <div class="rng-pct">${{pct!==null?pct.toFixed(0)+'th percentile':'unavailable'}}</div>
    </div>
  </div>`;

  // ── HEALTH SCORE ──
  const grade=h.grade||'?';
  const score=h.score||0;
  const breakdown=h.breakdown||{{}};
  const flags=h.flags||[];
  const bdKeys=Object.keys(breakdown).slice(0,8);
  html+=`<div class="health-card">
    <div class="health-header">
      <div class="health-title">⬡ &nbsp;HEALTH SCORE</div>
      <div class="health-grade-wrap">
        <div class="health-score">${{score}}<span style="font-size:.9rem;color:var(--dim)">/100</span></div>
        <div class="health-grade ${{grade}}">${{grade}}</div>
      </div>
    </div>
    <div class="health-bar-wrap">
      <div class="health-bar-track"><div class="health-bar-fill ${{grade}}" style="width:${{score}}%"></div></div>
    </div>
    ${{bdKeys.length?`<div class="health-breakdown">${{bdKeys.map(k=>`<div class="hb-item"><span class="hb-key">${{k}}</span><span class="hb-val">${{breakdown[k]}}</span></div>`).join('')}}</div>`:''}}`+
    `${{flags.length?`<div class="health-flags">${{flags.map(f=>`<span class="health-flag">⚠ ${{f}}</span>`).join('')}}</div>`:''}}
    <div class="health-ai-wrap">
      <div class="health-ai-label">◆ &nbsp;AI FORENSIC ANALYSIS</div>
      <div id="health-ai-text"><div class="health-ai-loading dots">
        <div class="dot"></div><div class="dot"></div><div class="dot"></div>
        <span style="color:var(--dim);font-size:.68rem;font-style:italic;margin-left:6px">Cross-verifying financials…</span>
      </div></div>
    </div>
  </div>`;

  // ── FUNDAMENTALS ──
  const funds=isEtf?[
    ['P/E Ratio',d.pe?d.pe.toFixed(1)+'×':'—'],
    ['Dividend Yield',d.div_y?d.div_y.toFixed(2)+'%':'—'],
    ['Beta',d.beta?d.beta.toFixed(2):'—'],
    ['52wk Momentum',d.mom.toFixed(1)+'%'],
    ['Earnings Yield',d.earnings_yield?d.earnings_yield.toFixed(2)+'%':'—'],
    ['Growth Est.',d.growth?d.growth.toFixed(1)+'%':'—'],
  ]:[
    ['EPS (TTM)',d.eps?'$'+d.eps.toFixed(2):'—'],
    ['Book Val/Shr',d.bvps?'$'+d.bvps.toFixed(2):'—'],
    ['P/E Ratio',d.pe?d.pe.toFixed(1)+'×':'—'],
    ['P/B Ratio',d.pb?d.pb.toFixed(2)+'×':'—'],
    ['ROE',d.roe?d.roe.toFixed(1)+'%':'—'],
    ['Growth Est.',d.growth?d.growth.toFixed(1)+'%':'—'],
    ['FCF/Share',d.fcf?'$'+d.fcf.toFixed(2):'—'],
    ['52wk Mom.',d.mom.toFixed(1)+'%'],
    ['Div Yield',d.div_y?d.div_y.toFixed(2)+'%':'—'],
    ['Beta',d.beta?d.beta.toFixed(2):'—'],
  ];
  html+=`<div class="lux-section">
    <div class="lux-section-title">◆ &nbsp;FUNDAMENTALS</div>
    <div class="fgrid">${{funds.map(([l,v])=>`<div class="fc"><span class="fl">${{l}}</span><span class="fv">${{v}}</span></div>`).join('')}}</div>
  </div>`;

  // ── ACTIONS ──
  html+=`<div class="actions">
    <button class="btn-sm" id="btn-cmp" onclick="toggleCmp()">⇄ Compare</button>
    <button class="btn-sm" onclick="addWL('${{d.ticker}}')">◈ Watchlist</button>
    <button class="btn-sm" onclick="exportPDF()">↓ PDF</button>
  </div>`;

  // ── MODELS ──
  html+=`<div class="lux-section-title">◆ &nbsp;${{isEtf?'INDEX VALUATION MODELS':'VALUATION MODELS'}}</div>`;
  html+=d.models.map(m=>{{
    const v=m.value&&m.value>0?fp(m.value):'N/A';
    const sc=m.sig_cls==='down'?'dn':m.sig_cls;
    return`<div class="mc"><div class="mbar ${{m.stripe}}"></div><div class="mbody">
      <div class="mrow"><span class="mname ${{m.cls}}">◆ ${{m.name}}</span><span class="mval ${{sc}}">${{v}}</span></div>
      <div class="msig ${{sc}}">${{m.sig_txt}}</div><div class="mfm">${{m.formula}}</div>
    </div></div>`;
  }}).join('');

  // ── VERDICT DETAIL ──
  html+=`<div class="verdict-detail-card ${{vc}}">
    <div class="vd-icon">${{d.verdict_cls==='up'?'▲':d.verdict_cls==='down'?'▼':'◈'}}</div>
    <div><div class="vd-title" style="color:${{vcol}}">${{d.verdict_text}}</div>
    <div class="vd-body">${{d.verdict_detail}}</div></div>
  </div>`;

  // ── AI VERDICT ──
  html+=`<div class="ai-card">
    <div class="ai-label">◆ &nbsp;SENECA AI ANALYSIS</div>
    <div id="ai-text"><div class="dots">
      <div class="dot"></div><div class="dot"></div><div class="dot"></div>
      <span style="color:var(--dim);font-size:.68rem;font-style:italic;margin-left:6px">Oracle is thinking…</span>
    </div></div>
  </div>`;

  document.getElementById('results').innerHTML=html;
  document.getElementById('results').classList.remove('hidden');
  document.getElementById('cmp-results').classList.add('hidden');
  document.getElementById('main-scroll').scrollTo({{top:0,behavior:'smooth'}});
}}

// ── Helpers ───────────────────────────────────────────────────────────────────
function toast(msg) {{
  const t=document.getElementById('toast'); t.textContent=msg; t.classList.add('on');
  setTimeout(()=>t.classList.remove('on'),4000);
}}
function setStatus(msg,col) {{ const e=document.getElementById('status'); e.textContent=msg; e.style.color=col||'var(--dim)'; }}
function fp(v) {{ if(!v||v<=0) return 'N/A'; return '$'+v.toLocaleString('en-US',{{minimumFractionDigits:2,maximumFractionDigits:2}}); }}
function fc(v) {{ if(!v||v<=0) return '—'; if(v>1e12) return '$'+(v/1e12).toFixed(2)+'T'; if(v>1e9) return '$'+(v/1e9).toFixed(1)+'B'; if(v>1e6) return '$'+(v/1e6).toFixed(0)+'M'; return '—'; }}
</script>
</body></html>"""

if __name__ == "__main__":
    app.run(debug=True, port=5678)
