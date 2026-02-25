import os, time, requests, threading, logging
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS
from bs4 import BeautifulSoup

BOT_TOKEN   = "8614297250:AAFonU98gkZygF9b1T17J1GdI_8OwmOfOb8"
SOURCE_URL  = "https://testcisia.it/calendario.php?tolc=cents&l=gb&lingua=inglese"
BOOKING_URL = "https://testcisia.it/studenti_tolc/login_sso.php"
INTERVAL    = 5  # seconds between checks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

users      = {}
found_keys = set()
state      = {"checks": 0, "last": None, "available": [], "alerts": 0, "status": "boot"}

app = Flask(__name__)
CORS(app, origins="*")

def tg_send(chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=10
        )
    except: pass

def scrape():
    r = requests.get(SOURCE_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    r.raise_for_status()
    seats = []
    for row in BeautifulSoup(r.text, "html.parser").select("table tr")[1:]:
        c = row.find_all("td")
        if len(c) < 7: continue
        fmt  = c[0].get_text(strip=True).upper()
        uni  = c[1].get_text(strip=True)
        reg  = c[2].get_text(strip=True)
        city = c[3].get_text(strip=True)
        dl   = c[4].get_text(strip=True)
        n    = c[5].get_text(strip=True)
        st   = c[6].get_text(strip=True).upper()
        lnk  = c[6].find("a")
        date = c[7].get_text(strip=True) if len(c) > 7 else "—"
        try: n = int(n)
        except: n = 0
        if lnk and "AVAILABLE" in st and n > 0:
            seats.append({"fmt":fmt,"uni":uni,"reg":reg,"city":city,"dl":dl,"n":n,"date":date,
                          "isu":"@UNI" in fmt,"ish":"@HOME" in fmt,"key":f"{fmt}|{uni}|{date}"})
    return seats

def notify_all(seat):
    emoji = "🏛" if seat["isu"] else "🏠"
    msg = (f"🚨 *CENT-S SEAT AVAILABLE!*\n\n"
           f"{emoji} *{seat['fmt']}*\n"
           f"🏫 {seat['uni']}\n"
           f"📍 {seat['city']}, {seat['reg']}\n"
           f"🗓 Test: `{seat['date']}`\n"
           f"⏰ Deadline: `{seat['dl']}`\n"
           f"💺 Seats: *{seat['n']}*\n\n"
           f"👉 {BOOKING_URL}\n\n⚡ _Be quick!_")
    for cid, info in list(users.items()):
        p = info.get("pref", "both")
        if p == "uni"  and not seat["isu"]: continue
        if p == "home" and not seat["ish"]: continue
        tg_send(cid, msg)
        state["alerts"] += 1
        time.sleep(0.05)

def check_loop():
    log.info("CHECK LOOP STARTED")
    while True:
        try:
            state["checks"] += 1
            state["last"]    = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            state["status"]  = "checking"
            seats = scrape()
            state["available"] = seats
            state["status"]    = "ok"
            log.info(f"Check #{state['checks']} — {len(seats)} available — {len(users)} users")
            for seat in seats:
                if seat["key"] not in found_keys:
                    found_keys.add(seat["key"])
                    if users: notify_all(seat)
        except Exception as e:
            state["status"] = "error"
            log.error(f"Check error: {e}")
        time.sleep(INTERVAL)

def poll_loop():
    log.info("POLL LOOP STARTED")
    offset = None
    while True:
        try:
            p = {"timeout": 25, "allowed_updates": ["message"]}
            if offset: p["offset"] = offset
            r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", params=p, timeout=35)
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg  = upd.get("message", {})
                cid  = str(msg.get("chat", {}).get("id", ""))
                txt  = msg.get("text", "").strip()
                if not cid or not txt: continue
                cmd  = txt.split()[0].lower().split("@")[0]
                log.info(f"TG: {cmd} from {cid}")
                if cmd == "/start":
                    users[cid] = {"pref": "both"}
                    tg_send(cid,
                        f"✅ *CENT-S Seat Tracker — Connected!*\n\n"
                        f"🤖 Monitoring testcisia.it every {INTERVAL}s\n"
                        f"Alert fires the instant seats open.\n\n"
                        f"🆔 Your Chat ID: `{cid}`\n\n"
                        f"Commands:\n/both · /uni · /home · /status · /stop")
                elif cmd == "/both":
                    users.setdefault(cid, {})["pref"] = "both"
                    tg_send(cid, "✅ Alerts for *both @UNI and @HOME* seats.")
                elif cmd == "/uni":
                    users.setdefault(cid, {})["pref"] = "uni"
                    tg_send(cid, "✅ Alerts for *@UNI* seats only.")
                elif cmd == "/home":
                    users.setdefault(cid, {})["pref"] = "home"
                    tg_send(cid, "✅ Alerts for *@HOME* seats only.")
                elif cmd == "/status":
                    tg_send(cid,
                        f"📊 *Status*\n\n"
                        f"🔄 Checks: {state['checks']}\n"
                        f"🕐 Last: {state['last']}\n"
                        f"💺 Available: {len(state['available'])}\n"
                        f"📨 Alerts: {state['alerts']}\n"
                        f"👥 Users: {len(users)}")
                elif cmd == "/stop":
                    users.pop(cid, None)
                    tg_send(cid, "🛑 Unsubscribed. /start to re-subscribe.")
        except Exception as e:
            log.error(f"Poll error: {e}")
            time.sleep(3)

@app.route("/")
def index():
    return jsonify({"service":"CENT-S Seat Tracker","status":state["status"],
                    "checks":state["checks"],"last_check":state["last"],
                    "users":len(users),"alerts":state["alerts"]})

@app.route("/api/register", methods=["POST"])
def register():
    d   = request.get_json() or {}
    cid = str(d.get("chat_id","")).strip()
    p   = d.get("pref","both")
    if not cid or not cid.lstrip("-").isdigit():
        return jsonify({"ok":False,"error":"invalid chat_id"}), 400
    users[cid] = {"pref": p}
    tg_send(cid, f"✅ *Connected!* Filter: *{p.upper()}*\nAlerts fire instantly when seats open.\n\n/both · /uni · /home · /status · /stop")
    return jsonify({"ok":True})

@app.route("/api/unregister", methods=["POST"])
def unregister():
    cid = str((request.get_json() or {}).get("chat_id","")).strip()
    users.pop(cid, None)
    return jsonify({"ok":True})

@app.route("/api/status")
def api_status():
    return jsonify({"ok":True,"checks":state["checks"],"last_check":state["last"],
                    "status":state["status"],"available_now":state["available"],
                    "alerts_sent":state["alerts"],"registered_users":len(users)})

@app.route("/health")
def health():
    return "OK", 200

# START THREADS AT MODULE LEVEL — works with gunicorn
log.info("=== BOOTING CENT-S TRACKER ===")
threading.Thread(target=check_loop, daemon=True, name="checker").start()
threading.Thread(target=poll_loop,  daemon=True, name="poller").start()
log.info("=== THREADS LAUNCHED ===")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), use_reloader=False)
