#!/usr/bin/env python3
"""SENECA — Intrinsic Value Oracle (clean rebuild, all buttons work)"""

import os, math, io, hashlib, json, pathlib
from datetime import datetime
from flask import Flask, request, jsonify, session, send_file

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "seneca-secret-2025")

STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_PRICE_ID        = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
ANTHROPIC_API_KEY      = os.environ.get("ANTHROPIC_API_KEY", "")

# ── User store ────────────────────────────────────────────────────────────────
USER_FILE = pathlib.Path("/tmp/seneca_users.json")

def load_users():
    try: return json.loads(USER_FILE.read_text()) if USER_FILE.exists() else {}
    except: return {}

def save_users(u):
    try: USER_FILE.write_text(json.dumps(u))
    except: pass

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

def create_user(email, pw):
    users = load_users()
    email = email.lower().strip()
    if email in users: return False, "Email already registered"
    users[email] = {"pw": hash_pw(pw), "subscribed": False}
    save_users(users); return True, "ok"

def verify_user(email, pw):
    users = load_users()
    email = email.lower().strip()
    u = users.get(email)
    if not u: return False, "Email not found"
    if u["pw"] != hash_pw(pw): return False, "Incorrect password"
    return True, u

def get_user(email):
    return load_users().get(email.lower().strip())

def set_subscribed(email):
    users = load_users()
    email = email.lower().strip()
    if email in users:
        users[email]["subscribed"] = True
        save_users(users)

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
    import yfinance as yf
    q = q.strip()
    if len(q) <= 6 and q.replace("-","").replace(".","").isalpha():
        return q.upper()
    try:
        res = yf.Search(q, max_results=5)
        quotes = res.quotes
        if quotes:
            for r in quotes:
                if r.get("quoteType","").upper() == "EQUITY": return r["symbol"]
            return quotes[0]["symbol"]
    except: pass
    return q.upper()

# ── Valuation ─────────────────────────────────────────────────────────────────
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

def comp_stock(vals):
    w={"gn":.20,"gg":.15,"buf":.25,"lyn":.15,"sim":.10,"dcf":.15}; t=ws=0
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

def fetch_quote(query):
    import yfinance as yf
    ticker = resolve_ticker(query)
    t = yf.Ticker(ticker); fi = t.fast_info
    price=float(fi.last_price or 0); prev=float(fi.previous_close or 0)
    lo52=float(fi.year_low or 0); hi52=float(fi.year_high or 0)
    cap=float(fi.market_cap or 0); shares=float(fi.shares or 1)
    if not price: raise ValueError(f"No price data for '{query}'. Check the symbol.")
    info=t.info
    def g(k,fb=0.0):
        try: v=info.get(k); f=float(v); return f if math.isfinite(f) else fb
        except: return fb
    name  = info.get("longName") or info.get("shortName") or ticker
    sector= info.get("sector") or info.get("industry") or info.get("categoryName") or "—"
    eps=g("trailingEps"); bvps=g("bookValue"); pe=g("trailingPE"); pb=g("priceToBook")
    roe=g("returnOnEquity")*100; div_y=g("dividendYield")*100; beta=g("beta")
    fcf_ps=g("freeCashflow")/shares if shares else 0
    growth=(g("earningsGrowth") or g("revenueGrowth") or g("earningsQuarterlyGrowth") or 0)*100
    chg=(price-prev)/prev*100 if prev else 0
    mom=(price-lo52)/lo52*100 if lo52 else 0
    ey=(1/pe*100) if pe>0 else 0
    fund = is_fund(ticker, info)

    if fund:
        iv={}
        if ey>0: iv["fed"]=price*(ey/4.3)
        if pe>0: iv["per"]=price*(17.0/pe)
        d=price*(div_y/100)
        if d>0: iv["ddm"]=d/(0.08-0.03)
        models=[]
        for k,nm,fm,sc,cl in [
            ("fed","FED MODEL",        "Price × (Earnings Yield ÷ Treasury 4.3%)","turq","turq"),
            ("per","P/E MEAN REVERSION","Price × (Hist. 17× P/E ÷ Current P/E)",  "gold","gold"),
            ("ddm","DIVIDEND DISCOUNT","Dividend ÷ (8% return − 3% growth)",       "muted","muted"),
        ]:
            v=iv.get(k); sc2,st2=signal(v,price) if v else ("na","Insufficient data")
            models.append({"name":nm,"formula":fm,"stripe":sc,"cls":cl,"value":v,"sig_cls":sc2,"sig_txt":st2})
        vals=[m["value"] for m in models if m["value"] and m["value"]>0]
        comp=sum(vals)/len(vals) if vals else None
        atype="etf"
    else:
        vd={"gn":gn(eps,bvps),"gg":gg(eps,growth),"buf":buf(eps,growth),
            "lyn":lyn(eps,growth),"sim":sim(price,pe or 1,pb or 1,roe,mom),"dcf":fdcf(fcf_ps,growth)}
        comp=comp_stock(vd); models=[]
        for k,nm,fm,sc,cl in [
            ("gn", "GRAHAM NUMBER",     "√( 22.5 × EPS × Book Value )",        "gold","gold"),
            ("gg", "GRAHAM GROWTH",     "EPS × (8.5+2g) × 4.4/AAA yield",     "gold","gold"),
            ("buf","BUFFETT DCF",       "10yr EPS @ 9% · 15× terminal",        "turq","turq"),
            ("lyn","PETER LYNCH PEG",   "EPS × growth% (PEG=1)",               "turq","turq"),
            ("sim","SIMONS QUANT",      "ROE/PE × (1/PB) × momentum",          "muted","muted"),
            ("dcf","FREE CASH FLOW DCF","10yr FCF @ 10% · 2.5% terminal",      "muted","muted"),
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
            "verdict_detail":vd2,"verdict_cls":vc,"earnings_yield":ey}

def get_ai_verdict(data):
    if not ANTHROPIC_API_KEY: return None
    try:
        import anthropic
        c=anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        comp_str = f"${data['composite']:.2f}" if data['composite'] else 'N/A'
        p = (f"You are SENECA, a stoic value investing oracle. 3 sentences explaining why "
             f"{data['name']} ({data['ticker']}) appears {data['verdict_cls']}:\n"
             f"Price ${data['price']:.2f} | Fair Value {comp_str} | P/E {data['pe']:.1f} | "
             f"Verdict: {data['verdict_text']}\nDirect, wise, no disclaimers, no bullets.")
        msg=c.messages.create(model="claude-sonnet-4-20250514",max_tokens=200,messages=[{"role":"user","content":p}])
        return msg.content[0].text.strip()
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
    email = (d.get("email","")).strip().lower()
    pw = d.get("pw","")
    if not email or not pw: return jsonify({"ok":False,"error":"Email and password required"}), 400
    ok, msg = create_user(email, pw)
    if not ok: return jsonify({"ok":False,"error":msg}), 400
    session["email"] = email; session["sub"] = False
    return jsonify({"ok":True,"email":email,"sub":False})

@app.route("/api/login", methods=["POST"])
def api_login():
    d = request.get_json()
    email = (d.get("email","")).strip().lower()
    pw = d.get("pw","")
    if not email or not pw: return jsonify({"ok":False,"error":"Email and password required"}), 400
    ok, result = verify_user(email, pw)
    if not ok: return jsonify({"ok":False,"error":result}), 401
    session["email"] = email; session["sub"] = result.get("subscribed", False)
    return jsonify({"ok":True,"email":email,"sub":result.get("subscribed",False)})

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear(); return jsonify({"ok":True})

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
        data = fetch_quote(q)
        session["lookups"] = lookups + 1
        return jsonify(data)
    except Exception as e: return jsonify({"error":str(e)}), 500

@app.route("/api/ai")
def api_ai():
    q = request.args.get("q","").strip()
    if not q: return jsonify({"verdict":None})
    try: return jsonify({"verdict": get_ai_verdict(fetch_quote(q))})
    except: return jsonify({"verdict":None})

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

# ── HTML renderer ─────────────────────────────────────────────────────────────
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
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400&family=Share+Tech+Mono&display=swap" rel="stylesheet"/>
{"<script src='https://js.stripe.com/v3/'></script>" if stripe_pk else ""}
<style>
:root{{--bg:#0d0a04;--panel:#140d05;--card:#1e1308;--card2:#271908;--card3:#301f0c;
  --border:#4e3010;--b2:#6e4514;--gold:#c88a1a;--g2:#e8aa34;--g3:#f5cc60;
  --turq:#1e7a6a;--t2:#28a892;--t3:#48c4ae;--green:#3a8a24;--red:#a03020;
  --muted:#6a4e2c;--text:#ede4c8;--sub:#b88a4c;--dim:#4a3418;}}
*{{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}}
html,body{{min-height:100%;background:var(--bg);color:var(--text);font-family:'Cormorant Garamond',serif;}}
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
.shell{{display:none;flex-direction:column;min-height:100vh}}
.shell.on{{display:flex}}
.hdr{{background:var(--panel);border-bottom:1px solid var(--b2);padding:13px 16px;padding-top:max(13px,env(safe-area-inset-top,13px));position:sticky;top:0;z-index:100;display:flex;align-items:center;justify-content:space-between;gap:8px}}
.hdr-left{{display:flex;align-items:center;gap:10px;flex-shrink:0}}
.gem-sm{{width:30px;height:30px;background:var(--g2);transform:rotate(45deg);display:flex;align-items:center;justify-content:center;flex-shrink:0}}
.gem-sm span{{transform:rotate(-45deg);font-size:.58rem;color:var(--bg);font-weight:700}}
.logo-text .name{{font-size:1.15rem;font-weight:600;color:var(--g3);letter-spacing:.2em;line-height:1}}
.logo-text .tag{{font-size:.58rem;color:var(--sub);font-style:italic;margin-top:1px}}
.hdr-right{{display:flex;align-items:center;gap:8px;flex-shrink:0}}
.btn-sub{{background:linear-gradient(135deg,var(--gold),var(--g2));color:var(--bg);border:none;border-radius:9px;padding:6px 13px;font-family:'Cormorant Garamond',serif;font-size:.78rem;font-weight:600;cursor:pointer;white-space:nowrap}}
.btn-acct{{background:var(--card2);color:var(--g2);border:1px solid var(--b2);border-radius:9px;padding:6px 12px;font-family:'Share Tech Mono',monospace;font-size:.56rem;cursor:pointer;white-space:nowrap}}
.search-wrap{{background:var(--panel);padding:12px 14px 0;border-bottom:1px solid var(--border)}}
.search-box{{background:var(--card);border:1px solid var(--b2);border-radius:14px;padding:10px 13px}}
.search-row{{display:flex;align-items:center;gap:8px;margin-bottom:6px}}
.search-input{{flex:1;min-width:0;width:0;background:transparent;border:none;outline:none;color:var(--g3);font-family:'Cormorant Garamond',serif;font-size:1.25rem;font-weight:600;text-align:center;letter-spacing:.1em;caret-color:var(--t3)}}
.search-input::placeholder{{color:var(--dim);font-size:.85rem;font-weight:400;letter-spacing:.02em}}
.btn-analyze{{background:var(--turq);color:var(--bg);border:none;border-radius:9px;padding:10px 15px;font-family:'Cormorant Garamond',serif;font-size:.9rem;font-weight:600;cursor:pointer;flex-shrink:0;white-space:nowrap}}
.btn-analyze:hover{{background:var(--t2)}}
.btn-analyze:disabled{{background:var(--muted);cursor:default}}
.search-hint{{font-size:.59rem;color:var(--dim);font-style:italic;text-align:center;padding-bottom:2px}}
.chips{{display:flex;flex-wrap:wrap;gap:6px;padding:8px 14px 10px}}
.chip{{background:var(--card2);border:1px solid var(--b2);border-radius:20px;padding:3px 11px;font-family:'Share Tech Mono',monospace;font-size:.58rem;color:var(--sub);cursor:pointer}}
.chip:hover{{background:var(--turq);border-color:var(--t2);color:var(--bg)}}
.status-bar{{font-family:'Share Tech Mono',monospace;font-size:.6rem;color:var(--dim);text-align:center;padding:6px 14px;background:var(--panel);border-bottom:1px solid var(--border);min-height:21px}}
.tabs{{display:flex;background:var(--panel);border-bottom:1px solid var(--border)}}
.tab{{flex:1;background:none;border:none;border-bottom:2px solid transparent;font-family:'Cormorant Garamond',serif;font-size:.9rem;color:var(--muted);padding:10px 8px;cursor:pointer;transition:all .2s;letter-spacing:.05em;position:relative;top:1px}}
.tab.on{{color:var(--g2);border-bottom-color:var(--t2)}}
main{{flex:1;overflow-y:auto;padding:13px 12px;padding-bottom:calc(20px + env(safe-area-inset-bottom,0px))}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:15px;margin-bottom:11px;overflow:hidden}}
.card-teal{{border-top:2px solid var(--t2)}}.card-gold{{border-top:2px solid var(--gold)}}
.ci{{padding:13px 14px}}
.lbl{{font-size:.76rem;color:var(--sub);font-style:italic;margin-bottom:4px}}
.price-row{{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:3px}}
.price-big{{font-size:2.5rem;font-weight:300;color:var(--g3);line-height:1}}
.chg{{font-size:.92rem;font-weight:600}}.up{{color:var(--green)}}.dn{{color:var(--red)}}
.meta-txt{{font-family:'Share Tech Mono',monospace;font-size:.58rem;color:var(--muted);line-height:1.85;margin-top:5px}}
.etf-tag{{display:inline-block;background:rgba(30,122,106,.2);border:1px solid var(--t2);border-radius:5px;padding:1px 7px;font-family:'Share Tech Mono',monospace;font-size:.53rem;color:var(--t3);letter-spacing:.07em;margin-bottom:5px}}
.divider{{border:none;height:1px;background:linear-gradient(90deg,transparent,var(--b2),transparent);margin:9px 0}}
.rng-labels{{display:flex;justify-content:space-between;font-family:'Share Tech Mono',monospace;font-size:.56rem;margin-bottom:4px}}
.rlo{{color:var(--red)}}.rhi{{color:var(--green)}}.rmid{{color:var(--dim)}}
.rng-track{{height:7px;border-radius:4px;background:var(--card3);border:1px solid var(--border);position:relative}}
.rng-fill{{height:100%;border-radius:4px;background:linear-gradient(90deg,var(--turq),var(--t2));transition:width .8s cubic-bezier(.4,0,.2,1)}}
.rng-thumb{{position:absolute;top:50%;transform:translate(-50%,-50%);width:15px;height:15px;background:var(--g2);border:2px solid var(--g3);border-radius:50%;transition:left .8s cubic-bezier(.4,0,.2,1)}}
.rng-pct{{text-align:center;font-family:'Share Tech Mono',monospace;font-size:.54rem;color:var(--dim);margin-top:4px}}
.ctitle{{font-size:.7rem;color:var(--gold);font-weight:600;letter-spacing:.1em;margin-bottom:9px;display:flex;align-items:center;gap:8px}}
.ctitle::after{{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--border),transparent)}}
.fgrid{{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--border)}}
.fc{{background:var(--card);padding:7px 9px;display:flex;justify-content:space-between;align-items:center}}
.fc:nth-child(4n+1),.fc:nth-child(4n+2){{background:var(--card2)}}
.fl{{font-family:'Share Tech Mono',monospace;font-size:.54rem;color:var(--muted)}}
.fv{{font-family:'Share Tech Mono',monospace;font-size:.66rem;color:var(--text);font-weight:700}}
.mc{{background:var(--card);border:1px solid var(--border);border-radius:11px;margin-bottom:7px;display:flex;overflow:hidden}}
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
.comp-box{{border-radius:15px;margin-bottom:11px;overflow:hidden;border:1px solid var(--gold);background:var(--panel)}}
.comp-band{{height:3px;background:linear-gradient(90deg,var(--gold),var(--t2),var(--g3),var(--turq),var(--gold))}}
.comp-inner{{padding:15px 16px;display:flex;justify-content:space-between;align-items:center}}
.comp-label{{font-size:.68rem;color:var(--gold);font-weight:600;letter-spacing:.1em;margin-bottom:2px}}
.comp-sub{{font-size:.56rem;color:var(--dim);font-style:italic}}
.comp-val{{font-size:1.9rem;font-weight:300;color:var(--g3)}}
.verdict{{background:var(--card);border:1px solid var(--border);border-radius:15px;padding:14px 16px;margin-bottom:11px;position:relative;overflow:hidden}}
.verdict::before{{content:'';position:absolute;left:0;top:0;bottom:0;width:3px}}
.verdict.up::before{{background:var(--green)}}.verdict.dn::before{{background:var(--red)}}.verdict.fair::before{{background:var(--gold)}}
.vt{{font-size:1rem;font-weight:600;margin-bottom:4px}}
.vd{{font-size:.78rem;font-style:italic;color:var(--sub);line-height:1.6}}
.ai-card{{background:var(--card);border:1px solid var(--turq);border-radius:15px;padding:13px 15px;margin-bottom:11px;position:relative;overflow:hidden}}
.ai-card::before{{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:linear-gradient(180deg,var(--t3),var(--turq))}}
.ai-label{{font-family:'Share Tech Mono',monospace;font-size:.56rem;color:var(--t3);letter-spacing:.13em;margin-bottom:7px}}
.ai-text{{font-size:.84rem;color:var(--sub);line-height:1.8;font-style:italic}}
.dots{{display:flex;gap:5px;align-items:center}}
.dot{{width:5px;height:5px;border-radius:50%;background:var(--t2);animation:pulse 1.2s ease-in-out infinite}}
.dot:nth-child(2){{animation-delay:.2s}}.dot:nth-child(3){{animation-delay:.4s}}
@keyframes pulse{{0%,100%{{opacity:.25}}50%{{opacity:1}}}}
.actions{{display:flex;gap:7px;margin-bottom:11px;flex-wrap:wrap}}
.btn-sm{{background:var(--card2);border:1px solid var(--b2);color:var(--sub);border-radius:9px;padding:7px 12px;font-family:'Cormorant Garamond',serif;font-size:.78rem;cursor:pointer;transition:all .2s}}
.btn-sm:hover{{border-color:var(--gold);color:var(--g2)}}
.btn-sm.active{{border-color:var(--t2);color:var(--t3);background:rgba(30,122,106,.1)}}
.wl-bar{{background:var(--panel);border-bottom:1px solid var(--border);padding:8px 14px;display:none}}
.wl-bar.on{{display:block}}
.wl-title{{font-family:'Share Tech Mono',monospace;font-size:.54rem;color:var(--muted);letter-spacing:.1em;margin-bottom:5px}}
.wl-items{{display:flex;flex-wrap:wrap;gap:5px}}
.wl-chip{{background:var(--card2);border:1px solid var(--b2);border-radius:18px;padding:3px 8px 3px 11px;display:flex;align-items:center;gap:4px;font-family:'Share Tech Mono',monospace;font-size:.58rem;color:var(--sub);cursor:pointer}}
.wl-chip:hover{{border-color:var(--t2);color:var(--text)}}
.wl-rm{{background:none;border:none;cursor:pointer;color:var(--muted);font-size:.66rem;line-height:1;padding:0 2px}}
.wl-rm:hover{{color:var(--red)}}
.cmp-bar{{background:var(--panel);padding:11px 13px 0;border-bottom:1px solid var(--border);display:none}}
.cmp-bar.on{{display:block}}
.cmp-row{{display:flex;gap:7px;align-items:center}}
.cmp-input{{flex:1;min-width:0;background:var(--card);border:1px solid var(--b2);border-radius:9px;padding:8px 10px;color:var(--g3);font-family:'Cormorant Garamond',serif;font-size:1rem;font-weight:600;text-align:center;letter-spacing:.1em;outline:none}}
.cmp-input::placeholder{{color:var(--dim);font-size:.78rem;font-weight:400;letter-spacing:0}}
.btn-cmp{{background:var(--gold);color:var(--bg);border:none;border-radius:9px;font-family:'Cormorant Garamond',serif;font-size:.86rem;font-weight:600;padding:9px 13px;cursor:pointer;white-space:nowrap;flex-shrink:0}}
.btn-cmp:hover{{background:var(--g2)}}
.cmp-hint{{font-size:.57rem;color:var(--dim);font-style:italic;padding:4px 0 9px;text-align:center}}
.cmp-grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:11px}}
.cmp-col{{background:var(--card);border:1px solid var(--border);border-radius:13px;overflow:hidden}}
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
.sec{{display:flex;align-items:center;gap:9px;font-family:'Share Tech Mono',monospace;font-size:.56rem;color:var(--muted);letter-spacing:.13em;margin:15px 0 7px}}
.sec::before,.sec::after{{content:'';flex:1;height:1px}}
.sec::before{{background:linear-gradient(90deg,transparent,var(--border))}}
.sec::after{{background:linear-gradient(90deg,var(--border),transparent)}}
.spinner{{text-align:center;padding:50px 0;display:none}}.spinner.on{{display:block}}
.ring{{width:40px;height:40px;border:2px solid var(--b2);border-top-color:var(--t2);border-radius:50%;display:inline-block;animation:spin 1s linear infinite}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
.spin-txt{{font-size:.74rem;color:var(--sub);font-style:italic;margin-top:9px}}
.err{{background:var(--card);border:1px solid var(--red);border-left:4px solid var(--red);border-radius:13px;padding:15px 17px;margin-bottom:11px}}
.err-title{{font-size:.92rem;font-weight:600;color:var(--red);margin-bottom:5px}}
.err-body{{font-size:.8rem;color:var(--sub);line-height:1.7}}
.form-card{{background:var(--card);border:1px solid var(--border);border-left:3px solid var(--t2);border-radius:0 9px 9px 0;padding:10px 13px;margin-bottom:7px}}
.fn{{font-size:.76rem;color:var(--g2);font-weight:600;margin-bottom:2px;letter-spacing:.04em}}
.feq{{font-family:'Share Tech Mono',monospace;font-size:.6rem;color:var(--t3);margin-bottom:2px}}
.fdesc{{font-size:.68rem;color:var(--muted);font-style:italic;line-height:1.6}}
.disc-box{{background:var(--card);border:1px solid var(--border);border-radius:11px;padding:11px 13px;margin-top:11px}}
.disc-box p{{font-size:.62rem;color:var(--dim);font-style:italic;line-height:1.8}}
.hidden{{display:none!important}}
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
</style>
</head>
<body>

<div id="hero" class="hero">
  <div class="hero-bg"></div>
  <div class="hero-gem"><span>◆</span></div>
  <div class="hero-title">SENECA</div>
  <div class="hero-sub">Intrinsic Value Oracle</div>
  <div class="hero-rule"></div>
  <div class="hero-pitch">Six <strong>classical valuation models</strong> — Graham, Buffett, Lynch and more — synthesised into one verdict. Stocks, ETFs and indexes. No noise. Just <strong>what it's actually worth.</strong></div>
  <div class="hero-btns">
    <button class="btn-teal" onclick="enterApp()">◈ &nbsp;Try Free Lookup</button>
    <button class="btn-outline" onclick="clickSubscribe()">✦ &nbsp;Subscribe — $9/mo</button>
  </div>
  <div class="hero-badges">
    <span class="badge">Graham · Buffett · Lynch</span>
    <span class="badge">Fed Model · ETF/Index</span>
    <span class="badge">AI Verdict</span>
    <span class="badge">PDF Export</span>
    <span class="badge">Login · Any Device</span>
  </div>
  <div class="hero-note">First lookup free · <a onclick="openModal('login')">Sign in to your account</a></div>
</div>

<div id="shell" class="shell">
  <div class="hdr">
    <div class="hdr-left">
      <div class="gem-sm"><span>◆</span></div>
      <div class="logo-text"><div class="name">SENECA</div><div class="tag">Intrinsic Value Oracle</div></div>
    </div>
    <div class="hdr-right">
      <button class="btn-acct hidden" id="btn-acct" onclick="openModal('acct')">◆ Account</button>
      <button class="btn-sub" id="btn-sub" onclick="clickSubscribe()">✦ $9/mo</button>
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
      <div id="results" class="hidden"></div>
      <div id="cmp-results" class="hidden"></div>
    </div>
    <div id="pane-b" class="hidden">
      <div class="card card-gold"><div class="ci">
        <div class="ctitle">✦ &nbsp;ABOUT SENECA</div>
        <div style="font-size:.84rem;color:var(--sub);line-height:1.8;font-style:italic">Named for the Stoic philosopher. Six valuation frameworks reveal what a stock, ETF, or index is truly worth. Create an account to access from any device.</div>
      </div></div>
      <div class="price-card"><div class="price-band"></div><div class="price-inner">
        <div class="price-title">◆ FULL ACCESS</div>
        <div class="price-amt">$9<span style="font-size:1rem;color:var(--sub)">/mo</span></div>
        <div class="price-note">Unlimited lookups · AI Verdicts · ETFs &amp; Indexes · Any device · Cancel anytime</div>
        <button class="btn-big" onclick="clickSubscribe()" style="max-width:220px">✦ &nbsp;Subscribe Now</button>
      </div></div>
      <div class="form-card"><div class="fn">Graham Number</div><div class="feq">√( 22.5 × EPS × Book Value )</div><div class="fdesc">Ben Graham's bedrock formula for stocks.</div></div>
      <div class="form-card"><div class="fn">Buffett DCF</div><div class="feq">10yr EPS @ 9% discount · 15× terminal</div><div class="fdesc">Discounts projected earnings to present value.</div></div>
      <div class="form-card"><div class="fn">Peter Lynch PEG</div><div class="feq">EPS × growth% (PEG = 1)</div><div class="fdesc">A fair stock has P/E equal to its growth rate.</div></div>
      <div class="form-card"><div class="fn">Fed Model (ETFs)</div><div class="feq">Price × (Earnings Yield ÷ 10yr Treasury)</div><div class="fdesc">Compares earnings yield to the risk-free rate.</div></div>
      <div class="form-card"><div class="fn">P/E Mean Reversion (ETFs)</div><div class="feq">Price × (Hist. 17× P/E ÷ Current P/E)</div><div class="fdesc">Fair value at a historically normal valuation.</div></div>
      <div class="form-card"><div class="fn">Dividend Discount (ETFs)</div><div class="feq">Dividend ÷ (8% return − 3% growth)</div><div class="fdesc">Intrinsic value based on dividend income.</div></div>
      <div class="disc-box"><p>✦ Seneca is for educational and research purposes only. Not financial advice.</p></div>
    </div>
  </main>
</div>

<div id="modal-pay" class="overlay">
  <div class="modal"><div class="mband"></div><div class="mbody">
    <div class="mgem"><span>◆</span></div>
    <div class="mtitle">UNLOCK SENECA</div>
    <div class="msub">Free lookup used. Subscribe for unlimited access.</div>
    <div class="mprice"><div class="mprice-num">$9</div><div class="mprice-per">per month · cancel anytime</div></div>
    <ul class="mfeatures">
      <li>Unlimited lookups on any device</li>
      <li>Stocks, ETFs &amp; indexes</li>
      <li>AI-powered verdict explanations</li>
      <li>Watchlist · PDF export</li>
    </ul>
    <button class="btn-big" id="pay-btn" onclick="launchStripe()">✦ &nbsp;Subscribe Now — $9/mo</button>
    <button class="btn-ghost-modal" onclick="closeModal('modal-pay')">Maybe later</button>
  </div></div>
</div>

<div id="modal-signup" class="overlay">
  <div class="modal"><div class="mband"></div><div class="mbody">
    <div class="mgem"><span>◆</span></div>
    <div class="mtitle">CREATE ACCOUNT</div>
    <div class="msub">Create account then subscribe. Access from any device.</div>
    <div class="aerror" id="signup-err"></div>
    <div class="afield"><label class="albl">EMAIL</label><input class="ainput" id="signup-email" type="email" placeholder="you@example.com" autocomplete="email"/></div>
    <div class="afield"><label class="albl">PASSWORD</label><input class="ainput" id="signup-pw" type="password" placeholder="••••••••"/></div>
    <button class="btn-big" id="signup-btn" onclick="doSignup()">✦ &nbsp;Create Account</button>
    <button class="btn-ghost-modal" onclick="closeModal('modal-signup')">Cancel</button>
    <div class="aswitch">Have an account? <a onclick="switchModal('modal-signup','modal-login')">Sign in</a></div>
  </div></div>
</div>

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

<div id="modal-acct" class="overlay">
  <div class="modal"><div class="mband"></div><div class="mbody">
    <div class="mtitle">◆ ACCOUNT</div>
    <div class="acct-email" id="acct-email"></div>
    <div id="acct-status" style="text-align:center;margin-bottom:16px;font-size:.8rem;color:var(--sub);font-style:italic"></div>
    <button class="btn-big" id="acct-sub-btn" onclick="closeModal('modal-acct');launchStripe()" style="display:none">✦ &nbsp;Subscribe Now — $9/mo</button>
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

window.addEventListener('DOMContentLoaded', () => {{
  if (userEmail) {{
    watchlist = JSON.parse(localStorage.getItem('wl_' + userEmail) || '[]');
    syncHeader();
  }} else {{
    watchlist = JSON.parse(sessionStorage.getItem('wl') || '[]');
  }}
  renderWL();
  {toast_js}
}});

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
function switchTab(t) {{
  ['a','b'].forEach(x => {{
    document.getElementById('pane-' + x).classList.toggle('hidden', x !== t);
    document.getElementById('tab-' + x).classList.toggle('on', x === t);
  }});
}}
function setQ(v) {{ document.getElementById('search').value = v; doAnalyze(); }}
document.getElementById('search').addEventListener('keydown', e => {{
  if (e.key === 'Enter') {{ e.preventDefault(); doAnalyze(); }}
}});
function openModal(name) {{
  if (name === 'acct') {{
    document.getElementById('acct-email').textContent = '◆ ' + userEmail;
    if (userSub) {{
      document.getElementById('acct-status').textContent = '✦ Active subscription';
      document.getElementById('acct-status').style.color = 'var(--green)';
      document.getElementById('acct-sub-btn').style.display = 'none';
    }} else {{
      document.getElementById('acct-status').textContent = 'No active subscription';
      document.getElementById('acct-status').style.color = 'var(--dim)';
      document.getElementById('acct-sub-btn').style.display = 'block';
    }}
  }}
  document.getElementById('modal-' + name).classList.add('on');
}}
function closeModal(id) {{ document.getElementById(id).classList.remove('on'); }}
function switchModal(from, to) {{ closeModal(from); openModal(to.replace('modal-','')); }}
['modal-pay','modal-signup','modal-login','modal-acct'].forEach(id => {{
  document.getElementById(id).addEventListener('click', e => {{
    if (e.target === e.currentTarget) closeModal(id);
  }});
}});
function clickSubscribe() {{
  enterApp();
  if (userSub) {{ toast('✦ You already have full access!'); return; }}
  if (userEmail) {{ launchStripe(); }}
  else {{ openModal('signup'); }}
}}
async function launchStripe() {{
  const btn = document.getElementById('pay-btn');
  if (btn) {{ btn.disabled = true; btn.textContent = 'Redirecting…'; }}
  try {{
    const r = await fetch('/api/checkout', {{ method: 'POST' }});
    const d = await r.json();
    if (d.url) {{ window.location.href = d.url; }}
    else {{ userSub = true; syncHeader(); closeModal('modal-pay'); toast('✦ Demo mode: full access!'); }}
  }} catch(e) {{
    userSub = true; syncHeader(); closeModal('modal-pay'); toast('✦ Demo mode: full access!');
  }} finally {{
    if (btn) {{ btn.disabled = false; btn.textContent = '✦  Subscribe Now — $9/mo'; }}
  }}
}}
async function doSignup() {{
  const email = document.getElementById('signup-email').value.trim();
  const pw = document.getElementById('signup-pw').value;
  const btn = document.getElementById('signup-btn');
  const err = document.getElementById('signup-err');
  err.textContent = '';
  if (!email || !pw) {{ err.textContent = 'Please fill in both fields.'; return; }}
  btn.disabled = true; btn.textContent = 'Creating account…';
  try {{
    const r = await fetch('/api/signup', {{
      method: 'POST', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{ email, pw }})
    }});
    const d = await r.json();
    if (!d.ok) {{ err.textContent = d.error || 'Error'; return; }}
    userEmail = d.email; userSub = d.sub;
    watchlist = JSON.parse(localStorage.getItem('wl_' + userEmail) || '[]');
    renderWL(); syncHeader(); closeModal('modal-signup'); enterApp();
    toast('◆ Account created! Launching checkout…');
    setTimeout(launchStripe, 600);
  }} catch(e) {{ err.textContent = 'Network error. Try again.'; }}
  finally {{ btn.disabled = false; btn.textContent = '✦  Create Account'; }}
}}
async function doLogin() {{
  const email = document.getElementById('login-email').value.trim();
  const pw = document.getElementById('login-pw').value;
  const btn = document.getElementById('login-btn');
  const err = document.getElementById('login-err');
  err.textContent = '';
  if (!email || !pw) {{ err.textContent = 'Please fill in both fields.'; return; }}
  btn.disabled = true; btn.textContent = 'Signing in…';
  try {{
    const r = await fetch('/api/login', {{
      method: 'POST', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{ email, pw }})
    }});
    const d = await r.json();
    if (!d.ok) {{ err.textContent = d.error || 'Error'; return; }}
    userEmail = d.email; userSub = d.sub;
    watchlist = JSON.parse(localStorage.getItem('wl_' + userEmail) || '[]');
    renderWL(); syncHeader(); closeModal('modal-login'); enterApp();
    toast(userSub ? '✦ Welcome back! Subscription active.' : '◆ Signed in.');
  }} catch(e) {{ err.textContent = 'Network error. Try again.'; }}
  finally {{ btn.disabled = false; btn.textContent = '✦  Sign In'; }}
}}
async function doLogout() {{
  await fetch('/api/logout', {{ method: 'POST' }});
  userEmail = ''; userSub = false; watchlist = [];
  syncHeader(); renderWL(); closeModal('modal-acct'); toast('Signed out.');
}}
function saveWL() {{
  if (userEmail) localStorage.setItem('wl_' + userEmail, JSON.stringify(watchlist));
  else sessionStorage.setItem('wl', JSON.stringify(watchlist));
}}
function renderWL() {{
  const bar = document.getElementById('wl-bar');
  const items = document.getElementById('wl-items');
  if (!watchlist.length) {{ bar.classList.remove('on'); return; }}
  bar.classList.add('on');
  items.innerHTML = watchlist.map(t =>
    `<span class="wl-chip" onclick="setQ('${{t}}')">${{t}}<button class="wl-rm" onclick="event.stopPropagation();removeWL('${{t}}')">✕</button></span>`
  ).join('');
}}
function addWL(ticker) {{
  if (!ticker || watchlist.includes(ticker)) return;
  watchlist.push(ticker); saveWL(); renderWL(); toast('◆ ' + ticker + ' added to watchlist');
}}
function removeWL(ticker) {{
  watchlist = watchlist.filter(t => t !== ticker); saveWL(); renderWL();
}}
function toggleCmp() {{
  cmpOpen = !cmpOpen;
  document.getElementById('cmp-bar').classList.toggle('on', cmpOpen);
  document.getElementById('btn-cmp').classList.toggle('active', cmpOpen);
  if (cmpOpen) document.getElementById('cmp1').focus();
}}
async function doCompare() {{
  const t1 = document.getElementById('cmp1').value.trim();
  const t2 = document.getElementById('cmp2').value.trim();
  if (!t1 || !t2) {{ toast('Enter two tickers or names'); return; }}
  switchTab('a');
  document.getElementById('spinner').classList.add('on');
  document.getElementById('results').classList.add('hidden');
  document.getElementById('cmp-results').classList.add('hidden');
  setStatus('Comparing ' + t1 + ' vs ' + t2 + '…', 'var(--t3)');
  try {{
    const [r1,r2] = await Promise.all([
      fetch('/api/quote?q=' + encodeURIComponent(t1)),
      fetch('/api/quote?q=' + encodeURIComponent(t2))
    ]);
    if (r1.status===402 || r2.status===402) {{ openModal('pay'); return; }}
    if (!r1.ok || !r2.ok) throw new Error('Could not fetch one or both tickers');
    const [d1,d2] = await Promise.all([r1.json(), r2.json()]);
    renderCmp(d1,d2);
    setStatus('Comparison: ' + d1.ticker + ' vs ' + d2.ticker, 'var(--green)');
  }} catch(e) {{ setStatus('⚠ ' + e.message, 'var(--red)'); }}
  finally {{ document.getElementById('spinner').classList.remove('on'); }}
}}
function renderCmp(d1,d2) {{
  function fp(v) {{ return (!v||v<=0)?'N/A':'$'+v.toLocaleString('en-US',{{minimumFractionDigits:2,maximumFractionDigits:2}}); }}
  const win1 = (d1.composite&&d2.composite) ? d1.composite>d2.composite : false;
  function col(d,win) {{
    return `<div class="cmp-col ${{win?'win':''}}">
      <div class="cmp-hdr"><div class="cmp-tkr">${{d.ticker}}${{win?' ✦':''}}</div><div class="cmp-nm">${{d.name}}</div><div class="cmp-px">${{fp(d.price)}}</div></div>
      <div class="cmp-r"><span class="cl">Composite FV</span><span class="cv">${{fp(d.composite)}}</span></div>
      <div class="cmp-r"><span class="cl">P/E</span><span class="cv">${{d.pe?d.pe.toFixed(1)+'×':'—'}}</span></div>
      <div class="cmp-r"><span class="cl">Div Yield</span><span class="cv">${{d.div_y?d.div_y.toFixed(2)+'%':'—'}}</span></div>
      <div class="cmp-r"><span class="cl">Beta</span><span class="cv">${{d.beta?d.beta.toFixed(2):'—'}}</span></div>
      <div class="cmp-r"><span class="cl">Type</span><span class="cv">${{d.asset_type==='etf'?'ETF/Index':'Stock'}}</span></div>
      <div class="cmp-vd ${{d.verdict_cls==='down'?'dn':d.verdict_cls}}">${{d.verdict_text}}</div>
    </div>`;
  }}
  document.getElementById('cmp-results').innerHTML =
    `<div class="sec">◈ &nbsp;COMPARISON</div><div class="cmp-grid">${{col(d1,win1)}}${{col(d2,!win1)}}</div>`;
  document.getElementById('cmp-results').classList.remove('hidden');
  document.getElementById('results').classList.add('hidden');
}}
async function exportPDF() {{
  if (!lastTicker) {{ toast('Analyze a stock first'); return; }}
  toast('◆ Generating report…');
  try {{
    const r = await fetch('/api/pdf?q=' + encodeURIComponent(lastTicker));
    if (r.status === 402) {{ openModal('pay'); return; }}
    if (!r.ok) throw new Error('Report error');
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'SENECA-' + lastTicker + '-report.pdf';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url); toast('◆ Report downloaded!');
  }} catch(e) {{ toast('⚠ ' + e.message); }}
}}
async function loadAI(ticker) {{
  const el = document.getElementById('ai-text');
  if (!el) return;
  try {{
    const r = await fetch('/api/ai?q=' + encodeURIComponent(ticker));
    if (r.ok) {{
      const d = await r.json();
      el.innerHTML = d.verdict
        ? '<span class="ai-text">' + d.verdict + '</span>'
        : '<span style="color:var(--dim);font-size:.7rem;font-style:italic">Add ANTHROPIC_API_KEY in Railway to enable AI analysis</span>';
    }}
  }} catch(e) {{ el.innerHTML = '<span style="color:var(--dim);font-size:.7rem;font-style:italic">AI unavailable</span>'; }}
}}
async function doAnalyze() {{
  const q = document.getElementById('search').value.trim();
  if (!q) return;
  switchTab('a'); enterApp();
  document.getElementById('btn-go').disabled = true;
  document.getElementById('results').classList.add('hidden');
  document.getElementById('cmp-results').classList.add('hidden');
  document.getElementById('spinner').classList.add('on');
  setStatus('Consulting the oracle…', 'var(--t3)');
  try {{
    const r = await fetch('/api/quote?q=' + encodeURIComponent(q));
    if (r.status === 402) {{ setStatus('Free lookup used — subscribe for unlimited access', 'var(--g2)'); openModal('pay'); return; }}
    if (!r.ok) {{ const e = await r.json(); throw new Error(e.error || 'Server error'); }}
    const d = await r.json();
    lastTicker = d.ticker; renderResult(d);
    setStatus('Analysis complete · ' + d.ticker + ' · ' + new Date().toLocaleTimeString(), 'var(--green)');
    loadAI(d.ticker);
  }} catch(e) {{
    setStatus('⚠ ' + e.message, 'var(--red)');
    document.getElementById('results').innerHTML = `<div class="err"><div class="err-title">⚠ Could not fetch data</div><div class="err-body">${{e.message}}</div></div>`;
    document.getElementById('results').classList.remove('hidden');
  }} finally {{
    document.getElementById('spinner').classList.remove('on');
    document.getElementById('btn-go').disabled = false;
  }}
}}
function renderResult(d) {{
  const p = d.price;
  const pct = (d.hi52>d.lo52&&d.lo52>0) ? Math.min(Math.max((p-d.lo52)/(d.hi52-d.lo52),0),1)*100 : null;
  const isEtf = d.asset_type === 'etf';
  let html = '';
  html += `<div class="card card-teal"><div class="ci">
    ${{isEtf ? '<div class="etf-tag">◈ ETF / INDEX FUND</div>' : ''}}
    <div class="lbl">${{d.name}} &nbsp;(${{d.ticker}})</div>
    <div class="price-row"><div class="price-big">${{fp(p)}}</div><div class="chg ${{d.chg>=0?'up':'dn'}}">${{d.chg>=0?'▲':'▼'}} ${{Math.abs(d.chg).toFixed(2)}}%</div></div>
    <div class="meta-txt">${{d.sector}} · ${{fc(d.cap)}} · Prev $${{d.prev.toFixed(2)}}</div>
    <hr class="divider"/>
    <div class="rng-labels"><span class="rlo">${{d.lo52>0?'$'+d.lo52.toFixed(2):'—'}}</span><span class="rmid">52 · W E E K</span><span class="rhi">${{d.hi52>0?'$'+d.hi52.toFixed(2):'—'}}</span></div>
    <div class="rng-track"><div class="rng-fill" style="width:${{pct!==null?pct:0}}%"></div><div class="rng-thumb" style="left:${{pct!==null?pct:0}}%"></div></div>
    <div class="rng-pct">${{pct!==null?pct.toFixed(0)+'th percentile':'unavailable'}}</div>
  </div></div>`;
  const funds = isEtf ? [
    ['P/E Ratio',d.pe?d.pe.toFixed(1)+'×':'—'],['Dividend Yield',d.div_y?d.div_y.toFixed(2)+'%':'—'],
    ['Beta',d.beta?d.beta.toFixed(2):'—'],['52wk Momentum',d.mom.toFixed(1)+'%'],
    ['Earnings Yield',d.earnings_yield?d.earnings_yield.toFixed(2)+'%':'—'],['Growth Est.',d.growth?d.growth.toFixed(1)+'%':'—'],
  ] : [
    ['EPS (TTM)',d.eps?'$'+d.eps.toFixed(2):'—'],['Book Val/Shr',d.bvps?'$'+d.bvps.toFixed(2):'—'],
    ['P/E Ratio',d.pe?d.pe.toFixed(1)+'×':'—'],['P/B Ratio',d.pb?d.pb.toFixed(2)+'×':'—'],
    ['ROE',d.roe?d.roe.toFixed(1)+'%':'—'],['Growth Est.',d.growth?d.growth.toFixed(1)+'%':'—'],
    ['FCF/Share',d.fcf?'$'+d.fcf.toFixed(2):'—'],['52wk Mom.',d.mom.toFixed(1)+'%'],
    ['Div Yield',d.div_y?d.div_y.toFixed(2)+'%':'—'],['Beta',d.beta?d.beta.toFixed(2):'—'],
  ];
  html += `<div class="card card-gold"><div class="ci"><div class="ctitle">◆ &nbsp;FUNDAMENTALS</div>
    <div class="fgrid">${{funds.map(([l,v])=>`<div class="fc"><span class="fl">${{l}}</span><span class="fv">${{v}}</span></div>`).join('')}}</div>
  </div></div>`;
  html += `<div class="actions">
    <button class="btn-sm" id="btn-cmp" onclick="toggleCmp()">⇄ Compare</button>
    <button class="btn-sm" onclick="addWL('${{d.ticker}}')">◈ Watchlist</button>
    <button class="btn-sm" onclick="exportPDF()">↓ PDF</button>
  </div>`;
  html += `<div class="sec">◈ &nbsp;${{isEtf?'INDEX VALUATION MODELS':'VALUATION MODELS'}}</div>`;
  html += d.models.map(m => {{
    const v = m.value&&m.value>0 ? fp(m.value) : 'N/A';
    const sc = m.sig_cls==='down' ? 'dn' : m.sig_cls;
    return `<div class="mc"><div class="mbar ${{m.stripe}}"></div><div class="mbody">
      <div class="mrow"><span class="mname ${{m.cls}}">◆ ${{m.name}}</span><span class="mval ${{sc}}">${{v}}</span></div>
      <div class="msig ${{sc}}">${{m.sig_txt}}</div><div class="mfm">${{m.formula}}</div>
    </div></div>`;
  }}).join('');
  const cl = isEtf ? 'INDEX COMPOSITE' : 'SENECA COMPOSITE';
  const cs = isEtf ? 'Average of Fed Model, P/E Reversion &amp; DDM' : 'Weighted synthesis of six classical formulae';
  html += `<div class="comp-box"><div class="comp-band"></div>
    <div class="comp-inner"><div><div class="comp-label">◈ &nbsp;${{cl}}</div><div class="comp-sub">${{cs}}</div></div>
    <div class="comp-val">${{d.composite&&d.composite>0?fp(d.composite):'N/A'}}</div></div>
    <div class="comp-band"></div></div>`;
  const vc = d.verdict_cls==='down'?'dn':d.verdict_cls;
  const vcol = d.verdict_cls==='up'?'var(--green)':d.verdict_cls==='down'?'var(--red)':'var(--g2)';
  html += `<div class="verdict ${{vc}}"><div class="vt" style="color:${{vcol}}">${{d.verdict_text}}</div><div class="vd">${{d.verdict_detail}}</div></div>`;
  html += `<div class="ai-card"><div class="ai-label">◆ &nbsp;SENECA AI ANALYSIS</div>
    <div id="ai-text"><div class="dots"><div class="dot"></div><div class="dot"></div><div class="dot"></div>
    <span style="color:var(--dim);font-size:.68rem;font-style:italic;margin-left:6px">Oracle is thinking…</span></div></div></div>`;
  document.getElementById('results').innerHTML = html;
  document.getElementById('results').classList.remove('hidden');
  document.getElementById('cmp-results').classList.add('hidden');
  document.getElementById('main-scroll').scrollTo({{top:0,behavior:'smooth'}});
}}
function toast(msg) {{
  const t = document.getElementById('toast'); t.textContent = msg; t.classList.add('on');
  setTimeout(() => t.classList.remove('on'), 4000);
}}
function setStatus(msg, col) {{ const e=document.getElementById('status'); e.textContent=msg; e.style.color=col||'var(--dim)'; }}
function fp(v) {{ if(!v||v<=0) return 'N/A'; return '$'+v.toLocaleString('en-US',{{minimumFractionDigits:2,maximumFractionDigits:2}}); }}
function fc(v) {{ if(!v||v<=0) return '—'; if(v>1e12) return '$'+(v/1e12).toFixed(2)+'T'; if(v>1e9) return '$'+(v/1e9).toFixed(1)+'B'; if(v>1e6) return '$'+(v/1e6).toFixed(0)+'M'; return '—'; }}
</script>
</body></html>"""

@app.route("/api/pdf")
def api_pdf():
    q = request.args.get("q","").strip()
    if not q: return jsonify({"error":"No ticker"}), 400
    sub = session.get("sub", False)
    lookups = session.get("lookups", 0)
    if not sub and lookups < 1: return jsonify({"error":"PAYWALL"}), 402
    try:
        data = fetch_quote(q)
        ai = get_ai_verdict(data)
        def fp(v): return "N/A" if not v or v<=0 else f"${v:,.2f}"
        rows = "".join(f"<tr><td>{m['name']}</td><td>{m['formula']}</td><td style='text-align:right;font-weight:bold'>{fp(m['value'])}</td><td style='text-align:right'>{m['sig_txt']}</td></tr>" for m in data["models"])
        vc = "#3a8a24" if data["verdict_cls"]=="up" else "#a03020" if data["verdict_cls"]=="down" else "#c88a1a"
        ai_html = (f"<div style='background:#f0f8f5;border:1px solid #1e7a6a;border-radius:8px;padding:14px;margin:12px 0'>"
                   f"<div style='font-size:9px;letter-spacing:3px;color:#1e7a6a;text-transform:uppercase;margin-bottom:8px;font-weight:600'>SENECA AI ANALYSIS</div>"
                   f"<p style='font-size:11px;line-height:1.7;font-style:italic;margin:0'>{ai}</p></div>") if ai else ""
        chg_arrow = "▲" if data["chg"] >= 0 else "▼"
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

if __name__ == "__main__":
    app.run(debug=True, port=5678)
