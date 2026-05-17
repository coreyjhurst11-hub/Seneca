#!/usr/bin/env python3
"""
SENECA — Intrinsic Value Oracle
Enhanced with:
  1. AI Verdict Explanation (Claude API)
  2. Stock Comparison Tool (side-by-side 2 tickers)
  3. Watchlist (saved tickers per session)
  4. PDF Report Export (WeasyPrint)
"""

import os, math, json, io, base64
from flask import Flask, request, jsonify, session, send_file

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_PRICE_ID        = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
ANTHROPIC_API_KEY      = os.environ.get("ANTHROPIC_API_KEY", "")

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
    import yfinance as yf
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
            v = info.get(key); f = float(v)
            return f if math.isfinite(f) else fb
        except: return fb
    name   = info.get("longName") or info.get("shortName") or ticker
    sector = info.get("sector") or info.get("industry") or "—"
    eps    = g("trailingEps"); bvps = g("bookValue")
    pe     = g("trailingPE");  pb   = g("priceToBook")
    roe    = g("returnOnEquity") * 100
    div_y  = g("dividendYield") * 100
    beta   = g("beta")
    fcf    = g("freeCashflow") / shares if shares else 0
    growth = (g("earningsGrowth") or g("revenueGrowth") or g("earningsQuarterlyGrowth") or 0) * 100
    chg    = (price - prev) / prev * 100 if prev else 0
    mom    = (price - lo52) / lo52 * 100 if lo52 else 0
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
    verdict_text = verdict_detail = verdict_cls = ""
    if comp and comp > 0:
        m = (comp - price) / price * 100
        if   m >= 30:  verdict_text, verdict_detail, verdict_cls = f"✦ STRONG BUY · {m:.0f}% margin of safety",   "Deep value. Substantial gap between price and intrinsic worth.", "up"
        elif m >= 10:  verdict_text, verdict_detail, verdict_cls = f"✦ UNDERVALUED · {m:.0f}% upside",            "Price trades below the six-model consensus.", "up"
        elif m <=-30:  verdict_text, verdict_detail, verdict_cls = f"✦ AVOID · {abs(m):.0f}% above fair value",   "Significant optimism priced in beyond what fundamentals support.", "down"
        elif m <=-10:  verdict_text, verdict_detail, verdict_cls = f"✦ OVERVALUED · {abs(m):.0f}% premium",       "Price exceeds what the six models suggest is warranted.", "down"
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
        "cap": cap, "div_y": div_y, "beta": beta, "composite": comp,
        "models": models_out, "verdict_text": verdict_text,
        "verdict_detail": verdict_detail, "verdict_cls": verdict_cls,
    }

# ── AI Verdict via Anthropic ─────────────────────────────────────────────────
def get_ai_verdict(data):
    if not ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""You are SENECA, a stoic value investing oracle. Write a 3-sentence plain-English explanation of why {data['name']} ({data['ticker']}) appears {data['verdict_cls']} based on these fundamentals:

Price: ${data['price']:.2f} | Composite Fair Value: ${data['composite']:.2f if data['composite'] else 'N/A'}
EPS: {data['eps']:.2f} | P/E: {data['pe']:.1f} | P/B: {data['pb']:.2f}
ROE: {data['roe']:.1f}% | Growth: {data['growth']:.1f}% | Beta: {data['beta']:.2f}
Verdict: {data['verdict_text']}

Be direct, insightful, and speak like a wise investor. No disclaimers. No bullet points. Just 3 flowing sentences."""
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return None

# ── PDF Report ───────────────────────────────────────────────────────────────
def build_pdf_html(data, ai_text=None):
    def fp(v):
        if not v or v <= 0: return "N/A"
        return f"${v:,.2f}"
    models_rows = ""
    for m in data["models"]:
        models_rows += f"""<tr>
            <td>{m['name']}</td>
            <td style="font-family:monospace;font-size:10px">{m['formula']}</td>
            <td style="text-align:right;font-weight:bold">{fp(m['value'])}</td>
            <td style="text-align:right">{m['sig_txt']}</td>
        </tr>"""
    ai_section = f"""<div class="ai-box"><div class="ai-title">◆ SENECA AI ANALYSIS</div><p>{ai_text}</p></div>""" if ai_text else ""
    verdict_color = "#3a8a24" if data["verdict_cls"] == "up" else ("#a03020" if data["verdict_cls"] == "down" else "#c88a1a")
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/>
<style>
  body {{ font-family: Georgia, serif; background: #fff; color: #1a1a1a; margin: 0; padding: 32px; font-size: 12px; }}
  .header {{ border-bottom: 3px solid #c88a1a; padding-bottom: 16px; margin-bottom: 24px; display: flex; justify-content: space-between; align-items: flex-end; }}
  .logo {{ font-size: 28px; font-weight: 300; letter-spacing: 6px; color: #c88a1a; }}
  .logo-sub {{ font-size: 10px; color: #888; font-style: italic; letter-spacing: 2px; }}
  .date {{ font-size: 10px; color: #888; text-align: right; }}
  .company {{ margin-bottom: 20px; }}
  .company-name {{ font-size: 22px; font-weight: 600; color: #1a1a1a; }}
  .company-meta {{ font-size: 11px; color: #666; margin-top: 4px; }}
  .price-big {{ font-size: 36px; font-weight: 300; color: #c88a1a; margin: 12px 0; }}
  .section-title {{ font-size: 9px; letter-spacing: 3px; color: #888; border-bottom: 1px solid #e0d0b0; padding-bottom: 4px; margin: 20px 0 10px; text-transform: uppercase; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 16px; }}
  th {{ background: #f5ede0; font-size: 9px; letter-spacing: 1px; text-transform: uppercase; padding: 6px 8px; text-align: left; color: #888; }}
  td {{ padding: 7px 8px; border-bottom: 1px solid #f0e8d8; font-size: 11px; }}
  tr:nth-child(even) td {{ background: #fdf8f2; }}
  .funds-grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; margin-bottom: 16px; }}
  .fund-item {{ background: #fdf8f2; border: 1px solid #e8d8b8; border-radius: 6px; padding: 8px 10px; }}
  .fund-label {{ font-size: 9px; color: #888; letter-spacing: 1px; text-transform: uppercase; }}
  .fund-val {{ font-size: 14px; font-weight: 600; color: #1a1a1a; margin-top: 2px; }}
  .composite-box {{ background: #fdf5e0; border: 2px solid #c88a1a; border-radius: 10px; padding: 16px 20px; display: flex; justify-content: space-between; align-items: center; margin: 16px 0; }}
  .composite-label {{ font-size: 9px; letter-spacing: 3px; color: #888; text-transform: uppercase; }}
  .composite-val {{ font-size: 28px; font-weight: 300; color: #c88a1a; }}
  .verdict-box {{ border-left: 4px solid {verdict_color}; padding: 12px 16px; background: #fafafa; border-radius: 0 8px 8px 0; margin: 12px 0; }}
  .verdict-text {{ font-size: 14px; font-weight: 600; color: {verdict_color}; margin-bottom: 4px; }}
  .verdict-detail {{ font-size: 11px; color: #666; font-style: italic; }}
  .ai-box {{ background: #f0f8f5; border: 1px solid #1e7a6a; border-radius: 8px; padding: 14px 16px; margin: 12px 0; }}
  .ai-title {{ font-size: 9px; letter-spacing: 3px; color: #1e7a6a; text-transform: uppercase; margin-bottom: 8px; font-weight: 600; }}
  .ai-box p {{ font-size: 11px; line-height: 1.7; color: #1a1a1a; font-style: italic; margin: 0; }}
  .footer {{ border-top: 1px solid #e0d0b0; margin-top: 32px; padding-top: 10px; font-size: 9px; color: #aaa; font-style: italic; text-align: center; }}
</style>
</head><body>
<div class="header">
  <div><div class="logo">SENECA</div><div class="logo-sub">Intrinsic Value Oracle</div></div>
  <div class="date">Generated {__import__('datetime').datetime.now().strftime('%B %d, %Y at %I:%M %p')}<br/>For educational purposes only</div>
</div>
<div class="company">
  <div class="company-name">{data['name']} &nbsp;({data['ticker']})</div>
  <div class="company-meta">{data['sector']}</div>
  <div class="price-big">${data['price']:.2f} <span style="font-size:14px;color:#888">{'▲' if data['chg']>=0 else '▼'} {abs(data['chg']):.2f}%</span></div>
</div>
<div class="section-title">Fundamentals</div>
<div class="funds-grid">
  <div class="fund-item"><div class="fund-label">EPS (TTM)</div><div class="fund-val">${data['eps']:.2f}</div></div>
  <div class="fund-item"><div class="fund-label">Book Value/Shr</div><div class="fund-val">${data['bvps']:.2f}</div></div>
  <div class="fund-item"><div class="fund-label">P/E Ratio</div><div class="fund-val">{data['pe']:.1f}×</div></div>
  <div class="fund-item"><div class="fund-label">P/B Ratio</div><div class="fund-val">{data['pb']:.2f}×</div></div>
  <div class="fund-item"><div class="fund-label">Return on Equity</div><div class="fund-val">{data['roe']:.1f}%</div></div>
  <div class="fund-item"><div class="fund-label">Growth Est.</div><div class="fund-val">{data['growth']:.1f}%</div></div>
  <div class="fund-item"><div class="fund-label">FCF / Share</div><div class="fund-val">${data['fcf']:.2f}</div></div>
  <div class="fund-item"><div class="fund-label">Beta</div><div class="fund-val">{data['beta']:.2f}</div></div>
  <div class="fund-item"><div class="fund-label">Dividend Yield</div><div class="fund-val">{data['div_y']:.2f}%</div></div>
</div>
<div class="section-title">Valuation Models</div>
<table>
  <tr><th>Model</th><th>Formula</th><th style="text-align:right">Fair Value</th><th style="text-align:right">Signal</th></tr>
  {models_rows}
</table>
<div class="composite-box">
  <div><div class="composite-label">Seneca Composite</div><div style="font-size:10px;color:#aaa;font-style:italic">Weighted synthesis of six classical formulae</div></div>
  <div class="composite-val">{fp(data['composite'])}</div>
</div>
<div class="verdict-box">
  <div class="verdict-text">{data['verdict_text']}</div>
  <div class="verdict-detail">{data['verdict_detail']}</div>
</div>
{ai_section}
<div class="footer">SENECA is for educational and research purposes only. Not financial advice. Always conduct your own due diligence before making any investment decision.</div>
</body></html>"""

# ── HTML ─────────────────────────────────────────────────────────────────────
def build_html(stripe_pk="", success_flash=False):
    success_js = "subscribed=true;document.getElementById('upgrade-btn').style.display='none';showToast('✦ Subscription active! Unlimited lookups unlocked.');" if success_flash else ""
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
.hero-bg{{position:absolute;inset:0;background:
  radial-gradient(ellipse 60% 50% at 50% 0%,rgba(200,138,26,.08) 0%,transparent 70%),
  radial-gradient(ellipse 40% 40% at 80% 80%,rgba(30,122,106,.06) 0%,transparent 60%),
  repeating-linear-gradient(0deg,transparent,transparent 79px,rgba(78,48,16,.15) 80px),
  repeating-linear-gradient(90deg,transparent,transparent 79px,rgba(78,48,16,.15) 80px);}}
.hero-diamond{{width:72px;height:72px;background:linear-gradient(135deg,var(--gold2),var(--gold3));transform:rotate(45deg);display:flex;align-items:center;justify-content:center;margin-bottom:28px;box-shadow:0 0 0 1px var(--gold3),0 0 60px rgba(232,170,52,.25),0 0 120px rgba(232,170,52,.1)}}
.hero-diamond span{{transform:rotate(-45deg);font-size:1.3rem;color:var(--bg)}}
.hero-title{{font-size:clamp(2.8rem,8vw,5rem);font-weight:300;color:var(--gold3);letter-spacing:.35em;margin-bottom:6px;position:relative;z-index:1}}
.hero-sub{{font-size:1rem;color:var(--sub);font-style:italic;letter-spacing:.15em;margin-bottom:40px;position:relative;z-index:1}}
.hero-rule{{width:180px;height:1px;background:linear-gradient(90deg,transparent,var(--gold),transparent);margin:0 auto 40px}}
.hero-pitch{{max-width:520px;font-size:1.1rem;color:var(--sub);line-height:1.9;font-style:italic;margin-bottom:48px;position:relative;z-index:1}}
.hero-pitch strong{{color:var(--text);font-style:normal}}
.hero-cta{{display:flex;gap:14px;justify-content:center;flex-wrap:wrap;margin-bottom:48px;position:relative;z-index:1}}
.btn-primary{{background:linear-gradient(135deg,var(--turq),var(--turq2));color:var(--bg);border:none;border-radius:14px;padding:15px 32px;font-family:'Cormorant Garamond',serif;font-size:1.05rem;font-weight:600;letter-spacing:.1em;cursor:pointer;transition:all .25s;box-shadow:0 4px 24px rgba(30,122,106,.25)}}
.btn-primary:hover{{transform:translateY(-2px);box-shadow:0 8px 32px rgba(30,122,106,.35)}}
.btn-ghost{{background:transparent;color:var(--gold2);border:1px solid var(--border2);border-radius:14px;padding:15px 32px;font-family:'Cormorant Garamond',serif;font-size:1.05rem;letter-spacing:.1em;cursor:pointer;transition:all .25s}}
.btn-ghost:hover{{border-color:var(--gold);color:var(--gold3);transform:translateY(-2px)}}
.hero-models{{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;max-width:500px;position:relative;z-index:1}}
.hm-badge{{background:var(--card2);border:1px solid var(--border2);border-radius:20px;padding:5px 14px;font-family:'Share Tech Mono',monospace;font-size:.58rem;color:var(--sub);letter-spacing:.06em}}
.hero-free-note{{font-size:.75rem;color:var(--dim);font-style:italic;margin-top:16px;position:relative;z-index:1}}
.app-shell{{display:none}}.app-shell.visible{{display:flex;flex-direction:column;min-height:100vh}}
header{{background:var(--panel);border-bottom:1px solid var(--border2);padding:16px 20px 13px;padding-top:max(16px,env(safe-area-inset-top,16px));position:sticky;top:0;z-index:100}}
.hdr{{display:flex;align-items:center;justify-content:space-between}}
.logo-wrap{{display:flex;align-items:center;gap:13px}}
.diamond{{width:36px;height:36px;background:var(--gold2);transform:rotate(45deg);display:flex;align-items:center;justify-content:center;flex-shrink:0;box-shadow:0 0 0 1px var(--gold3),0 0 20px rgba(232,170,52,.2)}}
.diamond span{{transform:rotate(-45deg);font-size:.65rem;color:var(--bg);font-weight:600}}
.logo-name{{font-size:1.4rem;font-weight:600;color:var(--gold3);letter-spacing:.2em;line-height:1}}
.logo-tag{{font-size:.65rem;color:var(--sub);font-style:italic;letter-spacing:.05em;margin-top:3px}}
.hdr-right{{display:flex;align-items:center;gap:12px}}
.clock{{font-family:'Share Tech Mono',monospace;font-size:.6rem;color:var(--dim);text-align:right}}
.btn-upgrade{{background:linear-gradient(135deg,var(--gold),var(--gold2));color:var(--bg);border:none;border-radius:10px;padding:7px 14px;font-family:'Cormorant Garamond',serif;font-size:.8rem;font-weight:600;letter-spacing:.05em;cursor:pointer;transition:all .2s}}
.btn-upgrade:hover{{transform:translateY(-1px);box-shadow:0 4px 16px rgba(200,138,26,.3)}}
.search-outer{{background:var(--panel);padding:14px 18px 0;border-bottom:1px solid var(--border)}}
.search-box{{display:flex;gap:10px;align-items:center;background:var(--card);border:1px solid var(--border2);border-radius:14px;padding:9px 13px}}
.search-box input{{flex:1;background:transparent;border:none;outline:none;color:var(--gold3);font-family:'Cormorant Garamond',serif;font-size:1.5rem;font-weight:600;text-align:center;text-transform:uppercase;letter-spacing:.18em;caret-color:var(--turq3)}}
.search-box input::placeholder{{color:var(--dim);font-size:.95rem;letter-spacing:.04em;font-weight:400}}
.btn-go{{background:var(--turq);color:var(--bg);border:none;border-radius:10px;font-family:'Cormorant Garamond',serif;font-size:.95rem;font-weight:600;padding:11px 18px;cursor:pointer;letter-spacing:.05em;transition:all .2s;white-space:nowrap}}
.btn-go:hover{{background:var(--turq2);transform:translateY(-1px)}}.btn-go:active{{transform:scale(.97)}}
.btn-go:disabled{{background:var(--dim);color:var(--muted);transform:none;cursor:default}}
.chips{{display:flex;flex-wrap:wrap;gap:7px;padding:10px 18px 12px}}
.chip{{background:var(--card2);border:1px solid var(--border2);border-radius:20px;padding:4px 13px;font-family:'Share Tech Mono',monospace;font-size:.62rem;color:var(--sub);cursor:pointer;transition:all .15s}}
.chip:hover{{background:var(--turq);border-color:var(--turq2);color:var(--bg)}}
.status{{font-family:'Share Tech Mono',monospace;font-size:.63rem;color:var(--dim);text-align:center;padding:7px 18px 8px;background:var(--panel);border-bottom:1px solid var(--border);min-height:24px}}
.tabs{{display:flex;background:var(--panel);border-bottom:1px solid var(--border)}}
.tab{{flex:1;background:none;border:none;font-family:'Cormorant Garamond',serif;font-size:.95rem;color:var(--muted);padding:11px 8px;cursor:pointer;border-bottom:2px solid transparent;transition:all .2s;letter-spacing:.05em;position:relative;top:1px}}
.tab.active{{color:var(--gold2);border-bottom-color:var(--turq2)}}
main{{overflow-y:auto;padding:16px 15px;padding-bottom:calc(24px + var(--safe));flex:1}}
.div{{border:none;height:1px;background:linear-gradient(90deg,transparent,var(--border2),transparent);margin:12px 0}}
.sec{{display:flex;align-items:center;gap:10px;font-family:'Share Tech Mono',monospace;font-size:.6rem;color:var(--muted);letter-spacing:.14em;margin:18px 0 9px}}
.sec::before{{content:'';flex:1;height:1px;background:linear-gradient(90deg,transparent,var(--border))}}
.sec::after{{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--border),transparent)}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:16px;margin-bottom:13px;overflow:hidden}}
.card-t{{border-top:2px solid var(--turq2)}}.card-g{{border-top:2px solid var(--gold)}}
.ci{{padding:15px 17px}}
.co-name{{font-size:.8rem;color:var(--sub);font-style:italic;margin-bottom:5px}}
.price-row{{display:flex;align-items:baseline;gap:12px;margin-bottom:3px}}
.price{{font-size:2.8rem;font-weight:300;color:var(--gold3);line-height:1}}
.chg{{font-size:1rem;font-weight:600}}.up{{color:var(--green)}}.down{{color:var(--red)}}
.meta{{font-family:'Share Tech Mono',monospace;font-size:.62rem;color:var(--muted);line-height:1.85;margin-top:7px}}
.range-wrap{{margin-top:13px}}
.range-labels{{display:flex;justify-content:space-between;align-items:center;font-family:'Share Tech Mono',monospace;font-size:.6rem;margin-bottom:6px}}
.r-lo{{color:var(--red)}}.r-hi{{color:var(--green)}}.r-mid{{color:var(--dim)}}
.range-track{{height:8px;border-radius:4px;background:var(--card3);border:1px solid var(--border);position:relative}}
.range-fill{{height:100%;border-radius:4px;background:linear-gradient(90deg,var(--turq),var(--turq2));transition:width .9s cubic-bezier(.4,0,.2,1)}}
.range-thumb{{position:absolute;top:50%;transform:translate(-50%,-50%);width:18px;height:18px;background:var(--gold2);border:2px solid var(--gold3);border-radius:50%;transition:left .9s cubic-bezier(.4,0,.2,1)}}
.range-pct{{text-align:center;font-family:'Share Tech Mono',monospace;font-size:.58rem;color:var(--dim);margin-top:5px}}
.card-title{{font-size:.75rem;color:var(--gold);font-weight:600;letter-spacing:.1em;margin-bottom:11px;display:flex;align-items:center;gap:8px}}
.card-title::after{{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--border),transparent)}}
.fund-grid{{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--border)}}
.fc{{background:var(--card);padding:9px 11px;display:flex;justify-content:space-between;align-items:center}}
.fc:nth-child(4n+1),.fc:nth-child(4n+2){{background:var(--card2)}}
.fl{{font-family:'Share Tech Mono',monospace;font-size:.58rem;color:var(--muted)}}
.fv{{font-family:'Share Tech Mono',monospace;font-size:.7rem;color:var(--text);font-weight:bold}}
.mc{{background:var(--card);border:1px solid var(--border);border-radius:12px;margin-bottom:8px;display:flex;overflow:hidden;transition:transform .15s}}
.mc:hover{{transform:translateY(-1px)}}
.ms{{width:4px;flex-shrink:0}}
.ms.gold{{background:linear-gradient(180deg,var(--gold3),var(--gold))}}
.ms.turq{{background:linear-gradient(180deg,var(--turq3),var(--turq))}}
.ms.muted{{background:linear-gradient(180deg,var(--muted),var(--dim))}}
.mb{{padding:11px 15px;flex:1;min-width:0}}
.mt{{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:2px}}
.mn{{font-size:.68rem;font-weight:600;letter-spacing:.07em}}
.mn.gold{{color:var(--gold2)}}.mn.turq{{color:var(--turq3)}}.mn.muted{{color:var(--sub)}}
.mv{{font-size:1.22rem;font-weight:300;white-space:nowrap}}
.mv.up{{color:var(--green)}}.mv.down{{color:var(--red)}}.mv.fair{{color:var(--gold2)}}.mv.na{{color:var(--dim)}}
.msig{{font-size:.62rem;font-style:italic;text-align:right;margin-bottom:2px}}
.msig.up{{color:var(--green)}}.msig.down{{color:var(--red)}}.msig.fair{{color:var(--gold)}}.msig.na{{color:var(--dim)}}
.mf{{font-family:'Share Tech Mono',monospace;font-size:.54rem;color:var(--dim);margin-top:1px}}
.comp{{border-radius:16px;margin-bottom:12px;overflow:hidden;border:1px solid var(--gold);background:var(--panel)}}
.band{{height:3px;background:linear-gradient(90deg,var(--gold),var(--turq2),var(--gold3),var(--turq),var(--gold))}}
.comp-inner{{padding:19px 19px 17px;display:flex;justify-content:space-between;align-items:center}}
.comp-lbl{{font-size:.72rem;color:var(--gold);font-weight:600;letter-spacing:.1em;margin-bottom:3px}}
.comp-sub{{font-size:.6rem;color:var(--dim);font-style:italic}}
.comp-val{{font-size:2.2rem;font-weight:300;color:var(--gold3)}}
.verdict{{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:17px 19px;margin-bottom:13px;position:relative;overflow:hidden}}
.verdict::before{{content:'';position:absolute;left:0;top:0;bottom:0;width:3px}}
.verdict.up::before{{background:var(--green)}}.verdict.down::before{{background:var(--red)}}.verdict.fair::before{{background:var(--gold)}}
.vt{{font-size:1.08rem;font-weight:600;margin-bottom:5px}}
.vd{{font-size:.82rem;font-style:italic;color:var(--sub);line-height:1.6}}
.spin{{text-align:center;padding:52px 0;display:none}}.spin.on{{display:block}}
.ring{{width:42px;height:42px;border:2px solid var(--border2);border-top-color:var(--turq2);border-radius:50%;display:inline-block;animation:spin 1s linear infinite}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
.spin-txt{{font-size:.78rem;color:var(--sub);font-style:italic;margin-top:11px}}
.about-body{{font-size:.87rem;color:var(--sub);line-height:1.8;font-style:italic;margin-bottom:16px}}
.form-card{{background:var(--card);border:1px solid var(--border);border-left:3px solid var(--turq2);border-radius:0 10px 10px 0;padding:12px 15px;margin-bottom:9px}}
.form-name{{font-size:.8rem;color:var(--gold2);font-weight:600;margin-bottom:3px;letter-spacing:.04em}}
.form-eq{{font-family:'Share Tech Mono',monospace;font-size:.64rem;color:var(--turq3);margin-bottom:3px}}
.form-desc{{font-size:.72rem;color:var(--muted);font-style:italic;line-height:1.6}}
.disc{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:13px 15px;margin-top:13px}}
.disc p{{font-size:.66rem;color:var(--dim);font-style:italic;line-height:1.8}}
.hidden{{display:none!important}}
.err-card{{background:var(--card);border:1px solid var(--red);border-radius:14px;padding:18px 20px;margin-bottom:13px;border-left:4px solid var(--red)}}
.err-title{{font-size:1rem;font-weight:600;color:var(--red);margin-bottom:8px}}
.err-body{{font-size:.84rem;color:var(--sub);line-height:1.7}}
.modal-overlay{{position:fixed;inset:0;background:rgba(0,0,0,.85);backdrop-filter:blur(6px);z-index:1000;display:none;align-items:center;justify-content:center;padding:20px}}
.modal-overlay.open{{display:flex}}
.modal{{background:var(--panel);border:1px solid var(--gold);border-radius:22px;max-width:400px;width:100%;overflow:hidden;animation:slideUp .35s cubic-bezier(.22,1,.36,1)}}
@keyframes slideUp{{from{{opacity:0;transform:translateY(30px)}}to{{opacity:1;transform:translateY(0)}}}}
.modal-band{{height:3px;background:linear-gradient(90deg,var(--gold),var(--turq2),var(--gold3))}}
.modal-body{{padding:30px 28px 32px}}
.modal-diamond{{width:52px;height:52px;background:linear-gradient(135deg,var(--gold2),var(--gold3));transform:rotate(45deg);display:flex;align-items:center;justify-content:center;margin:0 auto 20px;box-shadow:0 0 40px rgba(232,170,52,.2)}}
.modal-diamond span{{transform:rotate(-45deg);font-size:1rem;color:var(--bg)}}
.modal-title{{text-align:center;font-size:1.6rem;font-weight:300;color:var(--gold3);letter-spacing:.15em;margin-bottom:8px}}
.modal-sub{{text-align:center;font-size:.82rem;color:var(--sub);font-style:italic;line-height:1.7;margin-bottom:26px}}
.modal-price{{text-align:center;margin-bottom:24px}}
.modal-price-num{{font-size:2.8rem;font-weight:300;color:var(--gold3)}}
.modal-price-per{{font-size:.9rem;color:var(--sub);font-style:italic}}
.modal-features{{list-style:none;margin-bottom:28px;display:flex;flex-direction:column;gap:8px}}
.modal-features li{{display:flex;align-items:center;gap:10px;font-size:.85rem;color:var(--sub);font-style:italic}}
.modal-features li::before{{content:'◆';color:var(--gold2);font-size:.55rem;flex-shrink:0}}
.btn-subscribe{{width:100%;background:linear-gradient(135deg,var(--turq),var(--turq2));color:var(--bg);border:none;border-radius:14px;padding:16px;font-family:'Cormorant Garamond',serif;font-size:1.05rem;font-weight:600;letter-spacing:.08em;cursor:pointer;transition:all .25s;margin-bottom:10px}}
.btn-subscribe:hover{{transform:translateY(-1px);box-shadow:0 6px 24px rgba(30,122,106,.35)}}
.btn-subscribe:disabled{{background:var(--dim);cursor:default;transform:none}}
.btn-cancel-modal{{width:100%;background:transparent;color:var(--dim);border:none;font-family:'Cormorant Garamond',serif;font-size:.82rem;font-style:italic;cursor:pointer;padding:6px}}
.btn-cancel-modal:hover{{color:var(--sub)}}
.toast{{position:fixed;bottom:32px;left:50%;transform:translateX(-50%) translateY(80px);background:var(--green);color:#fff;border-radius:12px;padding:12px 22px;font-size:.85rem;font-style:italic;letter-spacing:.05em;z-index:2000;opacity:0;transition:all .4s cubic-bezier(.22,1,.36,1)}}
.toast.show{{opacity:1;transform:translateX(-50%) translateY(0)}}
.pricing-card{{background:var(--card);border:1px solid var(--gold);border-radius:18px;overflow:hidden;margin-bottom:13px}}
.pricing-band{{height:2px;background:linear-gradient(90deg,var(--gold),var(--turq2),var(--gold3))}}
.pricing-inner{{padding:22px 20px}}
.pricing-title{{font-size:.72rem;color:var(--gold);font-weight:600;letter-spacing:.12em;margin-bottom:14px}}
.pricing-amount{{font-size:2.4rem;font-weight:300;color:var(--gold3);margin-bottom:3px}}
.pricing-note{{font-size:.72rem;color:var(--sub);font-style:italic;margin-bottom:18px}}
.ai-card{{background:var(--card);border:1px solid var(--turq);border-radius:16px;padding:16px 18px;margin-bottom:13px;position:relative;overflow:hidden}}
.ai-card::before{{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:linear-gradient(180deg,var(--turq3),var(--turq))}}
.ai-title{{font-family:'Share Tech Mono',monospace;font-size:.6rem;color:var(--turq3);letter-spacing:.14em;margin-bottom:9px}}
.ai-text{{font-size:.88rem;color:var(--sub);line-height:1.8;font-style:italic}}
.ai-loading{{font-size:.78rem;color:var(--dim);font-style:italic;display:flex;align-items:center;gap:8px}}
.ai-dot{{width:6px;height:6px;border-radius:50%;background:var(--turq2);animation:pulse 1.2s ease-in-out infinite}}
.ai-dot:nth-child(2){{animation-delay:.2s}}.ai-dot:nth-child(3){{animation-delay:.4s}}
@keyframes pulse{{0%,100%{{opacity:.3}}50%{{opacity:1}}}}
.compare-outer{{background:var(--panel);padding:14px 18px 0;border-bottom:1px solid var(--border);display:none}}
.compare-outer.visible{{display:block}}
.compare-inputs{{display:flex;gap:8px;align-items:center}}
.compare-inputs input{{flex:1;background:var(--card);border:1px solid var(--border2);border-radius:10px;padding:9px 12px;color:var(--gold3);font-family:'Cormorant Garamond',serif;font-size:1.2rem;font-weight:600;text-align:center;text-transform:uppercase;letter-spacing:.15em;outline:none}}
.compare-inputs input::placeholder{{color:var(--dim);font-size:.85rem;letter-spacing:.03em;font-weight:400}}
.btn-compare{{background:var(--gold);color:var(--bg);border:none;border-radius:10px;font-family:'Cormorant Garamond',serif;font-size:.9rem;font-weight:600;padding:10px 16px;cursor:pointer;white-space:nowrap;transition:all .2s}}
.btn-compare:hover{{background:var(--gold2)}}
.compare-note{{font-size:.62rem;color:var(--dim);font-style:italic;padding:6px 0 12px;text-align:center}}
.compare-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:13px}}
.compare-col{{background:var(--card);border:1px solid var(--border);border-radius:14px;overflow:hidden}}
.compare-col-header{{padding:12px 14px;border-bottom:1px solid var(--border)}}
.compare-ticker{{font-size:1.4rem;font-weight:600;color:var(--gold3);letter-spacing:.12em}}
.compare-name{{font-size:.65rem;color:var(--sub);font-style:italic;margin-top:2px}}
.compare-price{{font-size:1.6rem;font-weight:300;color:var(--gold3);margin:4px 0 2px}}
.compare-row{{display:flex;justify-content:space-between;padding:7px 14px;border-bottom:1px solid var(--border);font-size:.75rem}}
.compare-row:last-child{{border-bottom:none}}
.compare-lbl{{color:var(--muted);font-family:'Share Tech Mono',monospace;font-size:.58rem}}
.compare-val{{color:var(--text);font-family:'Share Tech Mono',monospace;font-size:.68rem;font-weight:bold}}
.compare-winner{{background:rgba(30,122,106,.12);border-color:var(--turq2)}}
.compare-verdict-row{{padding:10px 14px}}
.compare-verdict-txt{{font-size:.78rem;font-weight:600}}
.compare-verdict-txt.up{{color:var(--green)}}.compare-verdict-txt.down{{color:var(--red)}}.compare-verdict-txt.fair{{color:var(--gold2)}}
.watchlist-bar{{background:var(--panel);border-bottom:1px solid var(--border);padding:10px 18px;display:none}}
.watchlist-bar.visible{{display:block}}
.watchlist-title{{font-family:'Share Tech Mono',monospace;font-size:.58rem;color:var(--muted);letter-spacing:.12em;margin-bottom:7px}}
.watchlist-items{{display:flex;flex-wrap:wrap;gap:6px}}
.wl-item{{background:var(--card2);border:1px solid var(--border2);border-radius:20px;padding:4px 10px 4px 13px;display:flex;align-items:center;gap:6px;font-family:'Share Tech Mono',monospace;font-size:.62rem;color:var(--sub);cursor:pointer;transition:all .15s}}
.wl-item:hover{{border-color:var(--turq2);color:var(--text)}}
.wl-remove{{color:var(--dim);font-size:.7rem;line-height:1;padding:0 2px;background:none;border:none;cursor:pointer;color:var(--muted)}}
.wl-remove:hover{{color:var(--red)}}
.wl-empty{{font-size:.7rem;color:var(--dim);font-style:italic}}
.action-row{{display:flex;gap:8px;margin-bottom:13px;flex-wrap:wrap}}
.btn-action{{background:var(--card2);border:1px solid var(--border2);color:var(--sub);border-radius:10px;padding:8px 14px;font-family:'Cormorant Garamond',serif;font-size:.82rem;cursor:pointer;transition:all .2s;display:flex;align-items:center;gap:6px}}
.btn-action:hover{{border-color:var(--gold);color:var(--gold2)}}
.btn-action.compare-toggle.active{{border-color:var(--turq2);color:var(--turq3);background:rgba(30,122,106,.1)}}
</style>
</head>
<body>
<div class="hero" id="hero">
  <div class="hero-bg"></div>
  <div class="hero-diamond"><span>◆</span></div>
  <div class="hero-title">SENECA</div>
  <div class="hero-sub">Intrinsic Value Oracle</div>
  <div class="hero-rule"></div>
  <div class="hero-pitch">Six <strong>classical valuation models</strong> — Graham, Buffett, Lynch, and more — synthesised into one honest verdict. No noise. No hype. Just <strong>what a company is actually worth.</strong></div>
  <div class="hero-cta">
    <button class="btn-primary" onclick="enterApp()">◈ &nbsp;Try Free Lookup</button>
    <button class="btn-ghost" onclick="enterApp();setTimeout(()=>openPaywall(),400)">✦ &nbsp;Subscribe — $9/mo</button>
  </div>
  <div class="hero-models">
    <span class="hm-badge">Graham Number</span><span class="hm-badge">Graham Growth</span>
    <span class="hm-badge">Buffett DCF</span><span class="hm-badge">Peter Lynch PEG</span>
    <span class="hm-badge">Simons Quant</span><span class="hm-badge">FCF DCF</span>
    <span class="hm-badge">AI Verdict</span><span class="hm-badge">Comparison</span>
    <span class="hm-badge">Watchlist</span><span class="hm-badge">PDF Export</span>
  </div>
  <div class="hero-free-note">First ticker free · No credit card required</div>
</div>
<div class="app-shell" id="app">
<header>
<div class="hdr">
  <div class="logo-wrap">
    <div class="diamond"><span>◆</span></div>
    <div><div class="logo-name">SENECA</div><div class="logo-tag">Intrinsic Value Oracle</div></div>
  </div>
  <div class="hdr-right">
    <button class="btn-upgrade" id="upgrade-btn" onclick="openPaywall()">✦ Upgrade $9/mo</button>
    <div class="clock" id="clock"></div>
  </div>
</div>
</header>
<div class="watchlist-bar" id="wl-bar">
  <div class="watchlist-title">◈ &nbsp;WATCHLIST</div>
  <div class="watchlist-items" id="wl-items"></div>
</div>
<div class="search-outer">
  <div class="search-box">
    <input id="ti" type="text" placeholder="AAPL · MSFT · TSLA · KO" maxlength="10" autocomplete="off" spellcheck="false"/>
    <button class="btn-go" id="btn" onclick="go()">◈ Analyze</button>
  </div>
  <div class="chips">
    <span class="chip" onclick="pick('AAPL')">AAPL</span><span class="chip" onclick="pick('MSFT')">MSFT</span>
    <span class="chip" onclick="pick('TSLA')">TSLA</span><span class="chip" onclick="pick('NVDA')">NVDA</span>
    <span class="chip" onclick="pick('KO')">KO</span><span class="chip" onclick="pick('AMZN')">AMZN</span>
    <span class="chip" onclick="pick('JPM')">JPM</span><span class="chip" onclick="pick('BRK-B')">BRK-B</span>
    <span class="chip" onclick="pick('GOOGL')">GOOGL</span><span class="chip" onclick="pick('META')">META</span>
  </div>
</div>
<div class="compare-outer" id="compare-outer">
  <div class="compare-inputs">
    <input id="cmp1" type="text" placeholder="AAPL" maxlength="10" autocomplete="off" spellcheck="false"/>
    <span style="color:var(--dim);font-size:1.2rem">vs</span>
    <input id="cmp2" type="text" placeholder="MSFT" maxlength="10" autocomplete="off" spellcheck="false"/>
    <button class="btn-compare" onclick="doCompare()">◈ Compare</button>
  </div>
  <div class="compare-note">Side-by-side valuation of two tickers</div>
</div>
<div class="status" id="st">Enter a ticker or tap one above to begin</div>
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
      <div class="about-body">Named for the Stoic philosopher and the Seneca Nation — keepers of wisdom in the eastern longhouse. This oracle applies six time-tested valuation frameworks to reveal what a company is truly worth beneath the noise of the market.</div>
    </div></div>
    <div class="pricing-card">
      <div class="pricing-band"></div>
      <div class="pricing-inner">
        <div class="pricing-title">◆ &nbsp;FULL ACCESS</div>
        <div class="pricing-amount">$9<span style="font-size:1.2rem;color:var(--sub)">/mo</span></div>
        <div class="pricing-note">Unlimited lookups · AI Verdicts · PDF Reports · Cancel anytime</div>
        <button class="btn-subscribe" onclick="openPaywall()" style="max-width:240px">✦ &nbsp;Subscribe Now</button>
      </div>
    </div>
    <div class="form-card"><div class="form-name">Graham Number</div><div class="form-eq">√( 22.5 × EPS × Book Value )</div><div class="form-desc">Ben Graham's bedrock formula. The geometric mean of earnings and asset value.</div></div>
    <div class="form-card"><div class="form-name">Graham Growth</div><div class="form-eq">EPS × (8.5 + 2g) × 4.4 / AAA yield</div><div class="form-desc">Extends Graham to account for growth expectations relative to bond yields.</div></div>
    <div class="form-card"><div class="form-name">Buffett DCF</div><div class="form-eq">10yr discounted EPS @ 9% · 15× terminal</div><div class="form-desc">Discounts a decade of projected earnings to present value with a terminal multiple.</div></div>
    <div class="form-card"><div class="form-name">Peter Lynch PEG</div><div class="form-eq">EPS × growth% (PEG = 1)</div><div class="form-desc">Lynch's insight: a fairly priced stock has a P/E equal to its earnings growth rate.</div></div>
    <div class="form-card"><div class="form-name">Simons Quant Factor</div><div class="form-eq">ROE/PE × (1/PB) × momentum</div><div class="form-desc">Renaissance-style multi-factor signal combining quality metrics and price momentum.</div></div>
    <div class="form-card"><div class="form-name">Free Cash Flow DCF</div><div class="form-eq">10yr FCF @ 10% · 2.5% terminal growth</div><div class="form-desc">Pure cash generation discounted to today.</div></div>
    <div class="disc"><p>✦ Seneca is for educational and research purposes only. Not financial advice. Always conduct your own due diligence.</p></div>
  </div>
</main>
</div>
<div class="modal-overlay" id="modal">
  <div class="modal">
    <div class="modal-band"></div>
    <div class="modal-body">
      <div class="modal-diamond"><span>◆</span></div>
      <div class="modal-title">UNLOCK SENECA</div>
      <div class="modal-sub">You've used your free lookup.<br/>Subscribe for unlimited access to all features.</div>
      <div class="modal-price">
        <div class="modal-price-num">$9</div>
        <div class="modal-price-per">per month · cancel anytime</div>
      </div>
      <ul class="modal-features">
        <li>Unlimited ticker lookups, any time</li>
        <li>All six valuation models + Seneca Composite</li>
        <li>AI-powered verdict explanations</li>
        <li>Side-by-side stock comparison</li>
        <li>Watchlist &amp; PDF report export</li>
        <li>Real-time data via Yahoo Finance</li>
      </ul>
      <button class="btn-subscribe" id="sub-btn" onclick="subscribe()">✦ &nbsp;Subscribe Now</button>
      <button class="btn-cancel-modal" onclick="closePaywall()">Maybe later</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
const STRIPE_PK = "{stripe_pk}";
const stripe = STRIPE_PK ? Stripe(STRIPE_PK) : null;
let subscribed = false;
let watchlist = JSON.parse(sessionStorage.getItem('seneca_wl') || '[]');
let lastData = null;
let compareMode = false;

window.addEventListener('DOMContentLoaded',()=>{{ {success_js} renderWatchlist(); }});

(function tick(){{
  const n=new Date();
  const el=document.getElementById('clock');
  if(el) el.textContent=n.toLocaleTimeString([],{{hour:'2-digit',minute:'2-digit',second:'2-digit'}});
  setTimeout(tick,1000);
}})();

function enterApp(){{
  document.getElementById('hero').style.display='none';
  document.getElementById('app').classList.add('visible');
}}
function tabSwitch(t){{
  ['a','b'].forEach(x=>{{
    document.getElementById('pane-'+x).classList.toggle('hidden',x!==t);
    document.getElementById('nav-'+x).classList.toggle('active',x===t);
  }});
}}
function pick(s){{document.getElementById('ti').value=s;go();}}
document.getElementById('ti').addEventListener('keydown',e=>{{if(e.key==='Enter'){{e.preventDefault();go();}}}});
document.getElementById('ti').addEventListener('input',e=>{{e.target.value=e.target.value.toUpperCase();}});
document.getElementById('cmp1').addEventListener('input',e=>{{e.target.value=e.target.value.toUpperCase();}});
document.getElementById('cmp2').addEventListener('input',e=>{{e.target.value=e.target.value.toUpperCase();}});
function openPaywall(){{document.getElementById('modal').classList.add('open');}}
function closePaywall(){{document.getElementById('modal').classList.remove('open');}}
document.getElementById('modal').addEventListener('click',e=>{{if(e.target===e.currentTarget)closePaywall();}});

function renderWatchlist(){{
  const bar=document.getElementById('wl-bar');
  const items=document.getElementById('wl-items');
  if(watchlist.length===0){{bar.classList.remove('visible');return;}}
  bar.classList.add('visible');
  items.innerHTML=watchlist.map(t=>`<span class="wl-item" onclick="pick('${{t}}')">
    ${{t}}<button class="wl-remove" onclick="event.stopPropagation();removeFromWatchlist('${{t}}')">✕</button>
  </span>`).join('');
}}
function addToWatchlist(ticker){{
  if(!ticker||watchlist.includes(ticker))return;
  watchlist.push(ticker);
  sessionStorage.setItem('seneca_wl',JSON.stringify(watchlist));
  renderWatchlist();
  showToast('◆ '+ticker+' added to watchlist');
}}
function removeFromWatchlist(ticker){{
  watchlist=watchlist.filter(t=>t!==ticker);
  sessionStorage.setItem('seneca_wl',JSON.stringify(watchlist));
  renderWatchlist();
}}
function toggleCompare(){{
  compareMode=!compareMode;
  document.getElementById('compare-outer').classList.toggle('visible',compareMode);
  document.getElementById('compare-btn').classList.toggle('active',compareMode);
  if(compareMode)document.getElementById('cmp1').focus();
}}
async function go(){{
  const t=document.getElementById('ti').value.trim().toUpperCase();
  if(!t)return;
  tabSwitch('a');
  document.getElementById('btn').disabled=true;
  document.getElementById('res').classList.add('hidden');
  document.getElementById('cmp-res').classList.add('hidden');
  document.getElementById('spin').classList.add('on');
  st('Consulting the oracle for '+t+'…','var(--turq3)');
  try{{
    const r=await fetch('/api/quote?ticker='+encodeURIComponent(t));
    if(r.status===402){{
      document.getElementById('spin').classList.remove('on');
      document.getElementById('btn').disabled=false;
      st('Free lookup used — subscribe for unlimited access','var(--gold2)');
      openPaywall();return;
    }}
    if(!r.ok){{const e=await r.json();throw new Error(e.error||'Server error');}}
    const d=await r.json();
    lastData=d;
    render(d);
    st('Analysis complete · '+d.ticker+' · '+new Date().toLocaleTimeString(),'var(--green)');
    loadAiVerdict(d);
  }}catch(e){{
    st('⚠ '+e.message,'var(--red)');
    document.getElementById('res').innerHTML=`<div class="err-card"><div class="err-title">⚠ Could not fetch data</div><div class="err-body">${{e.message}}</div></div>`;
    document.getElementById('res').classList.remove('hidden');
  }}finally{{
    document.getElementById('spin').classList.remove('on');
    document.getElementById('btn').disabled=false;
  }}
}}
async function loadAiVerdict(d){{
  const aiEl=document.getElementById('ai-verdict-text');
  if(!aiEl)return;
  try{{
    const r=await fetch('/api/ai-verdict?ticker='+encodeURIComponent(d.ticker));
    if(r.ok){{
      const j=await r.json();
      aiEl.innerHTML=j.verdict?'<span class="ai-text">'+j.verdict+'</span>':'<span style="color:var(--dim);font-size:.72rem;font-style:italic">AI analysis unavailable — add ANTHROPIC_API_KEY to Railway</span>';
    }}
  }}catch(e){{aiEl.innerHTML='<span style="color:var(--dim);font-size:.72rem;font-style:italic">AI analysis unavailable</span>';}}
}}
async function doCompare(){{
  const t1=document.getElementById('cmp1').value.trim().toUpperCase();
  const t2=document.getElementById('cmp2').value.trim().toUpperCase();
  if(!t1||!t2){{showToast('Enter two tickers to compare');return;}}
  tabSwitch('a');
  document.getElementById('spin').classList.add('on');
  document.getElementById('res').classList.add('hidden');
  document.getElementById('cmp-res').classList.add('hidden');
  st('Comparing '+t1+' vs '+t2+'…','var(--turq3)');
  try{{
    const [r1,r2]=await Promise.all([fetch('/api/quote?ticker='+encodeURIComponent(t1)),fetch('/api/quote?ticker='+encodeURIComponent(t2))]);
    if(r1.status===402||r2.status===402){{st('Subscribe for comparison lookups','var(--gold2)');openPaywall();return;}}
    if(!r1.ok||!r2.ok) throw new Error('Could not fetch one or both tickers');
    const [d1,d2]=await Promise.all([r1.json(),r2.json()]);
    renderCompare(d1,d2);
    st('Comparison complete · '+t1+' vs '+t2,'var(--green)');
  }}catch(e){{st('⚠ '+e.message,'var(--red)');}}
  finally{{document.getElementById('spin').classList.remove('on');}}
}}
function renderCompare(d1,d2){{
  function fp(v){{if(!v||v<=0)return'N/A';return'$'+v.toLocaleString('en-US',{{minimumFractionDigits:2,maximumFractionDigits:2}});}}
  function pct(v){{return v?v.toFixed(1)+'%':'—';}}
  function ratio(v){{return v?v.toFixed(2)+'×':'—';}}
  const c1win=(d1.composite&&d2.composite)?d1.composite>d2.composite:false;
  function col(d,isWinner){{
    return`<div class="compare-col ${{isWinner?'compare-winner':''}}">
      <div class="compare-col-header">
        <div class="compare-ticker">${{d.ticker}}${{isWinner?' ✦':''}}</div>
        <div class="compare-name">${{d.name}}</div>
        <div class="compare-price">${{fp(d.price)}}</div>
      </div>
      <div class="compare-row"><span class="compare-lbl">Composite FV</span><span class="compare-val">${{fp(d.composite)}}</span></div>
      <div class="compare-row"><span class="compare-lbl">P/E</span><span class="compare-val">${{ratio(d.pe)}}</span></div>
      <div class="compare-row"><span class="compare-lbl">P/B</span><span class="compare-val">${{ratio(d.pb)}}</span></div>
      <div class="compare-row"><span class="compare-lbl">ROE</span><span class="compare-val">${{pct(d.roe)}}</span></div>
      <div class="compare-row"><span class="compare-lbl">Growth</span><span class="compare-val">${{pct(d.growth)}}</span></div>
      <div class="compare-row"><span class="compare-lbl">EPS</span><span class="compare-val">${{d.eps?'$'+d.eps.toFixed(2):'—'}}</span></div>
      <div class="compare-row"><span class="compare-lbl">Beta</span><span class="compare-val">${{d.beta?d.beta.toFixed(2):'—'}}</span></div>
      <div class="compare-verdict-row"><div class="compare-verdict-txt ${{d.verdict_cls}}">${{d.verdict_text}}</div></div>
    </div>`;
  }}
  document.getElementById('cmp-res').innerHTML=`<div class="sec">◈ &nbsp;COMPARISON</div><div class="compare-grid">${{col(d1,c1win)}}${{col(d2,!c1win)}}</div>`;
  document.getElementById('cmp-res').classList.remove('hidden');
  document.getElementById('res').classList.add('hidden');
}}
async function exportPDF(){{
  if(!lastData){{showToast('Analyze a stock first');return;}}
  showToast('◆ Generating PDF report…');
  try{{
    const r=await fetch('/api/export-pdf?ticker='+encodeURIComponent(lastData.ticker));
    if(r.status===402){{openPaywall();return;}}
    if(!r.ok){{const e=await r.json();throw new Error(e.error||'PDF error');}}
    const blob=await r.blob();
    const url=URL.createObjectURL(blob);
    const a=document.createElement('a');
    a.href=url;a.download='SENECA-'+lastData.ticker+'-report.pdf';
    document.body.appendChild(a);a.click();document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast('◆ PDF downloaded!');
  }}catch(e){{showToast('⚠ '+e.message);}}
}}
function render(d){{
  const p=d.price;let html='';
  const pct=(d.hi52>d.lo52&&d.lo52>0)?Math.min(Math.max((p-d.lo52)/(d.hi52-d.lo52),0),1)*100:null;
  html+=`<div class="card card-t"><div class="ci">
    <div class="co-name">${{d.name}} &nbsp;(${{d.ticker}})</div>
    <div class="price-row"><div class="price">${{fp(p)}}</div>
    <div class="chg ${{d.chg>=0?'up':'down'}}">${{d.chg>=0?'▲':'▼'}} ${{Math.abs(d.chg).toFixed(2)}}%</div></div>
    <div class="meta">${{d.sector}} &nbsp;·&nbsp; Cap ${{fc(d.cap)}} &nbsp;·&nbsp; Prev $${{d.prev.toFixed(2)}}</div>
    <hr class="div"/>
    <div class="range-wrap">
      <div class="range-labels">
        <span class="r-lo">${{d.lo52>0?'$'+d.lo52.toFixed(2):'—'}}</span>
        <span class="r-mid">52 · W E E K · R A N G E</span>
        <span class="r-hi">${{d.hi52>0?'$'+d.hi52.toFixed(2):'—'}}</span>
      </div>
      <div class="range-track">
        <div class="range-fill" style="width:${{pct!==null?pct:0}}%"></div>
        <div class="range-thumb" style="left:${{pct!==null?pct:0}}%"></div>
      </div>
      <div class="range-pct">${{pct!==null?pct.toFixed(0)+'th percentile of 52-week range':'52-week range unavailable'}}</div>
    </div>
  </div></div>`;
  const funds=[
    ['EPS (TTM)',d.eps?'$'+d.eps.toFixed(2):'—'],['Book Val / Shr',d.bvps?'$'+d.bvps.toFixed(2):'—'],
    ['P / E Ratio',d.pe?d.pe.toFixed(1)+'×':'—'],['P / B Ratio',d.pb?d.pb.toFixed(2)+'×':'—'],
    ['Ret. on Equity',d.roe?d.roe.toFixed(1)+'%':'—'],['Growth Est.',d.growth?d.growth.toFixed(1)+'%':'—'],
    ['FCF / Share',d.fcf?'$'+d.fcf.toFixed(2):'—'],['52wk Momentum',d.mom.toFixed(1)+'%'],
    ['Dividend Yield',d.div_y?d.div_y.toFixed(2)+'%':'—'],['Beta',d.beta?d.beta.toFixed(2):'—'],
  ];
  html+=`<div class="card card-g"><div class="ci"><div class="card-title">◆ &nbsp;FUNDAMENTALS</div>
    <div class="fund-grid">${{funds.map(([l,v])=>`<div class="fc"><span class="fl">${{l}}</span><span class="fv">${{v}}</span></div>`).join('')}}</div>
  </div></div>`;
  html+=`<div class="action-row">
    <button class="btn-action compare-toggle" id="compare-btn" onclick="toggleCompare()">⇄ &nbsp;Compare</button>
    <button class="btn-action" onclick="addToWatchlist('${{d.ticker}}')">◈ &nbsp;+ Watchlist</button>
    <button class="btn-action" onclick="exportPDF()">↓ &nbsp;PDF Report</button>
  </div>`;
  html+='<div class="sec">◈ &nbsp;VALUATION MODELS</div>';
  html+=d.models.map(m=>{{
    const vs=m.value&&m.value>0?fp(m.value):'N/A';
    return`<div class="mc"><div class="ms ${{m.stripe}}"></div><div class="mb">
      <div class="mt"><span class="mn ${{m.cls}}">◆ ${{m.name}}</span><span class="mv ${{m.sig_cls}}">${{vs}}</span></div>
      <div class="msig ${{m.sig_cls}}">${{m.sig_txt}}</div>
      <div class="mf">${{m.formula}}</div>
    </div></div>`;
  }}).join('');
  html+=`<div class="comp"><div class="band"></div>
    <div class="comp-inner">
      <div><div class="comp-lbl">◈ &nbsp;SENECA COMPOSITE</div><div class="comp-sub">Weighted synthesis of six classical formulae</div></div>
      <div class="comp-val">${{d.composite&&d.composite>0?fp(d.composite):'N/A'}}</div>
    </div><div class="band"></div></div>`;
  const vcol=d.verdict_cls==='up'?'var(--green)':d.verdict_cls==='down'?'var(--red)':'var(--gold2)';
  html+=`<div class="verdict ${{d.verdict_cls}}">
    <div class="vt" style="color:${{vcol}}">${{d.verdict_text}}</div>
    <div class="vd">${{d.verdict_detail}}</div>
  </div>`;
  html+=`<div class="ai-card">
    <div class="ai-title">◆ &nbsp;SENECA AI ANALYSIS</div>
    <div id="ai-verdict-text"><div class="ai-loading"><div class="ai-dot"></div><div class="ai-dot"></div><div class="ai-dot"></div><span style="color:var(--dim);font-size:.72rem;font-style:italic;margin-left:4px">Oracle is thinking…</span></div></div>
  </div>`;
  document.getElementById('res').innerHTML=html;
  document.getElementById('res').classList.remove('hidden');
  document.getElementById('cmp-res').classList.add('hidden');
  document.getElementById('scroll').scrollTo({{top:0,behavior:'smooth'}});
}}
async function subscribe(){{
  const btn=document.getElementById('sub-btn');
  btn.disabled=true;btn.textContent='Redirecting…';
  try{{
    const r=await fetch('/api/create-checkout-session',{{method:'POST'}});
    const d=await r.json();
    if(d.url){{window.location.href=d.url;}}
    else{{subscribed=true;document.getElementById('upgrade-btn').style.display='none';closePaywall();showToast('✦ Demo mode: access granted!');}}
  }}catch(e){{
    subscribed=true;document.getElementById('upgrade-btn').style.display='none';closePaywall();showToast('✦ Demo mode: access granted!');
  }}finally{{btn.disabled=false;btn.textContent='✦ Subscribe Now';}}
}}
function showToast(msg){{
  const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),4000);
}}
function st(msg,col){{const e=document.getElementById('st');e.textContent=msg;e.style.color=col||'var(--dim)';}}
function fp(v){{if(!v||v<=0)return'N/A';return'$'+v.toLocaleString('en-US',{{minimumFractionDigits:2,maximumFractionDigits:2}});}}
function fc(v){{if(!v||v<=0)return'—';if(v>1e12)return'$'+(v/1e12).toFixed(2)+'T';if(v>1e9)return'$'+(v/1e9).toFixed(1)+'B';if(v>1e6)return'$'+(v/1e6).toFixed(0)+'M';return'—';}}
</script>
</body></html>"""

# ── Routes ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return build_html(stripe_pk=STRIPE_PUBLISHABLE_KEY)

@app.route("/success")
def success():
    if not STRIPE_SECRET_KEY:
        session["subscribed"] = True
    else:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        sid = request.args.get("session_id", "")
        try:
            s = stripe.checkout.Session.retrieve(sid)
            if s.payment_status == "paid":
                session["subscribed"] = True
        except: pass
    return build_html(stripe_pk=STRIPE_PUBLISHABLE_KEY, success_flash=True)

@app.route("/api/quote")
def api_quote():
    ticker = request.args.get("ticker", "").strip().upper()
    if not ticker:
        return jsonify({"error": "No ticker provided"}), 400
    lookups = session.get("lookups", 0)
    is_subscribed = session.get("subscribed", False)
    if not is_subscribed and lookups >= 1:
        return jsonify({"error": "PAYWALL", "lookups_used": lookups}), 402
    try:
        data = fetch_quote(ticker)
        session["lookups"] = lookups + 1
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/ai-verdict")
def api_ai_verdict():
    ticker = request.args.get("ticker", "").strip().upper()
    if not ticker:
        return jsonify({"error": "No ticker"}), 400
    try:
        data = fetch_quote(ticker)
        verdict = get_ai_verdict(data)
        return jsonify({"verdict": verdict})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/export-pdf")
def api_export_pdf():
    ticker = request.args.get("ticker", "").strip().upper()
    if not ticker:
        return jsonify({"error": "No ticker"}), 400
    is_subscribed = session.get("subscribed", False)
    lookups = session.get("lookups", 0)
    if not is_subscribed and lookups < 1:
        return jsonify({"error": "PAYWALL"}), 402
    try:
        data = fetch_quote(ticker)
        ai_text = get_ai_verdict(data)
        html_content = build_pdf_html(data, ai_text)
        try:
            from weasyprint import HTML
            pdf_bytes = HTML(string=html_content).write_pdf()
            buf = io.BytesIO(pdf_bytes)
            buf.seek(0)
            return send_file(buf, mimetype='application/pdf',
                            download_name=f'SENECA-{ticker}-report.pdf',
                            as_attachment=True)
        except ImportError:
            buf = io.BytesIO(html_content.encode())
            buf.seek(0)
            return send_file(buf, mimetype='text/html',
                           download_name=f'SENECA-{ticker}-report.html',
                           as_attachment=True)
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

@app.route("/webhook", methods=["POST"])
def webhook():
    if not STRIPE_SECRET_KEY: return "", 200
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return str(e), 400
    return "", 200

if __name__ == "__main__":
    app.run(debug=True, port=5678)
