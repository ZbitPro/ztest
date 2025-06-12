import os, time, hmac, json, hashlib, threading, requests, pandas as pd, streamlit as st
from streamlit_autorefresh import st_autorefresh
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv

# 1) LOAD ENV
load_dotenv()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", None)
WEBHOOK_PORT   = int(os.getenv("WEBHOOK_PORT", 80))    # default to 80

# 2) BYBIT & DASHBOARD CONFIG
BASE_URL         = "https://api.bybit.com"
POSITIONS_PATH   = "/v5/position/list"
AUTO_MARGIN_PATH = "/v5/position/set-auto-add-margin"
RECV_WINDOW      = 5000   # ms
REFRESH_INTERVAL = 10_000 # ms

# credentials (can be set in .env or entered in sidebar)
API_KEY    = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")

# 3) SIGNATURE HELPERS
def _build_param_string(p): return "&".join(f"{k}={p[k]}" for k in sorted(p)) if p else ""
def generate_signature(secret, ts, api_key, recv_window, params, body_str):
    pre = f"{ts}{api_key}{recv_window}{_build_param_string(params)}{body_str}"
    return hmac.new(secret.encode(), pre.encode(), hashlib.sha256).hexdigest()

# 4) BYBIT API
@st.cache_data(ttl=REFRESH_INTERVAL/1000 - 1)
def fetch_open_positions(key, secret, category, symbol, settle_coin):
    params = {"category": category}
    if symbol: params["symbol"] = symbol
    else:      params["settleCoin"] = settle_coin

    ts = str(int(time.time()*1000))
    sign = generate_signature(secret, ts, key, RECV_WINDOW, params, "")
    hdr = {
        "Content-Type":"application/json",
        "X-BAPI-API-KEY": key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": str(RECV_WINDOW),
        "X-BAPI-SIGN": sign
    }
    r = requests.get(BASE_URL+POSITIONS_PATH, headers=hdr, params=params, timeout=5)
    r.raise_for_status()
    j = r.json()
    if j.get("retCode",0)!=0: raise RuntimeError(f"{j['retCode']}: {j['retMsg']}")
    return pd.json_normalize(j["result"]["list"]) if j["result"]["list"] else pd.DataFrame()

def set_auto_add_margin(key, secret, category, symbol, side, enable):
    body = {"category":category,"symbol":symbol,"side":side,
            "autoAddMargin": "1" if enable else "0"}
    body_str = json.dumps(body, separators=(",",":"), ensure_ascii=False)
    ts = str(int(time.time()*1000))
    sign = generate_signature(secret, ts, key, RECV_WINDOW, {}, body_str)
    hdr = {
        "Content-Type":"application/json",
        "X-BAPI-API-KEY":key,
        "X-BAPI-TIMESTAMP":ts,
        "X-BAPI-RECV-WINDOW":str(RECV_WINDOW),
        "X-BAPI-SIGN":sign
    }
    r = requests.post(BASE_URL+AUTO_MARGIN_PATH, headers=hdr,
                      data=body_str.encode("utf-8"), timeout=5)
    r.raise_for_status()
    j = r.json()
    if j.get("retCode",0)!=0: raise RuntimeError(f"{j['retCode']}: {j['retMsg']}")
    return j

# 5) FASTAPI WEBHOOK
app = FastAPI()

@app.post("/webhook")
async def webhook(req: Request):
    if WEBHOOK_SECRET:
        auth = req.headers.get("Authorization","")
        if auth.replace("Bearer ","") != WEBHOOK_SECRET:
            raise HTTPException(401,"Invalid webhook secret")
    data = await req.json()
    try:
        sym    = data["symbol"].upper()
        side   = data["side"].capitalize()
        act    = data["action"].lower()    # "enable" / "disable"
        cat    = data["category"]
        settle = data.get("settleCoin","")
    except KeyError as e:
        raise HTTPException(400,f"Missing {e}")
    enable = (act=="enable")
    try:
        res = set_auto_add_margin(API_KEY, API_SECRET, cat, sym, side, enable)
    except Exception as e:
        raise HTTPException(500,str(e))
    # bust cache so dashboard picks up change immediately
    try: fetch_open_positions.clear()
    except: pass
    return {"status":"ok","result":res}

def run_webhook():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=WEBHOOK_PORT)

# start listener once
_webhook_started = False
def start_webhook():
    global _webhook_started
    if not _webhook_started:
        t = threading.Thread(target=run_webhook, daemon=True)
        t.start()
        _webhook_started = True

# 6) STREAMLIT DASHBOARD
# patch rerun if missing
if not hasattr(st, "experimental_rerun"):
    try:
        runner = __import__("streamlit.runtime.scriptrunner.script_runner",
                             fromlist=["RerunException"])
        RerunException = runner.RerunException
        def _rerun(): raise RerunException(None)
        st.experimental_rerun = _rerun
    except: pass

st.set_page_config(page_title="Bybit Dashboard", layout="wide")
st.title("📊 Bybit Open Positions & Auto-Margin Control")

# creds in sidebar
st.sidebar.header("🔑 API Credentials")
key_in    = st.sidebar.text_input("Bybit API Key",    value=API_KEY)
secret_in = st.sidebar.text_input("Bybit API Secret", value=API_SECRET, type="password")
if not key_in or not secret_in:
    st.sidebar.error("Enter your Bybit API Key & Secret"); st.stop()
API_KEY, API_SECRET = key_in.strip(), secret_in.strip()

# start webhook
start_webhook()
st.sidebar.success(f"Webhook → http://<YOUR_HOST>:{WEBHOOK_PORT}/webhook")

# filters
st.sidebar.header("⚙️ Filters")
category    = st.sidebar.selectbox("Category", ["linear","inverse","option","spot"])
symbol      = st.sidebar.text_input("Symbol (e.g. BTCUSDT)", "").strip().upper()
settle_coin = st.sidebar.text_input("Settle Coin (e.g. USDT)", "USDT").strip().upper()
if not symbol and not settle_coin:
    st.sidebar.error("Enter Symbol or Settle Coin"); st.stop()

# manual refresh
if st.sidebar.button("🔄 Refresh Now"):
    fetch_open_positions.clear()
    st.experimental_rerun()

# auto-refresh
st_autorefresh(interval=REFRESH_INTERVAL, limit=None, key="ticker")

# fetch & show
try:
    df = fetch_open_positions(API_KEY, API_SECRET, category, symbol, settle_coin)
except Exception as e:
    st.error(e); st.stop()
if df.empty:
    st.info("No open positions."); st.stop()

# table + inline buttons
cols_def = [1]*10 + [0.6]
fields   = ["symbol","side","size","entryPrice","markPrice",
            "leverage","unrealisedPnl","liqPrice","positionValue","autoAddMargin"]
names    = dict(symbol="Symbol", side="Side", size="Qty", entryPrice="Entry",
                markPrice="Mark", leverage="Lev", unrealisedPnl="UnrealPnL",
                liqPrice="Liq", positionValue="Value", autoAddMargin="AutoAdd")
# header
hdrs = st.columns(cols_def)
for c,f in zip(hdrs, fields+["action"]):
    c.markdown(f"**{names.get(f,f.title()) if f!='action' else 'Action'}**")
# rows
for i,pos in df.iterrows():
    row = st.columns(cols_def)
    for c,f in zip(row, fields):
        c.write(pos.get(f,""))
    cur = str(pos.get("autoAddMargin","0"))
    lbl = "Disable" if cur=="1" else "Enable"
    if row[-1].button(lbl, key=f"btn-{i}"):
        try:
            set_auto_add_margin(API_KEY, API_SECRET, category,
                                pos["symbol"], pos["side"],
                                enable=(cur=="0"))
            fetch_open_positions.clear()
            st.experimental_rerun()
        except Exception as e:
            st.error(e)

st.markdown("---")
st.caption("▶️ Use the inline buttons or send TradingView webhooks to toggle auto-add-margin.")