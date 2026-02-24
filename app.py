import os
import time
import requests
import threading
import logging
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS
from bs4 import BeautifulSoup

# ── CONFIG ────────────────────────────────────────────────────────
BOT_TOKEN   = "8614297250:AAFonU98gkZygF9b1T17J1GdI_8OwmOfOb8"
SOURCE_URL  = "https://testcisia.it/calendario.php?tolc=cents&l=gb&lingua=inglese"
BOOKING_URL = "https://testcisia.it/studenti_tolc/login_sso.php"
CHECK_EVERY = 60

# ── LOGGING ───────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── SHARED STATE ──────────────────────────────────────────────────
users          = {}   # { chat_id: {"pref": "both"|"uni"|"home"} }
found_keys     = set()
total_checks   = 0
total_alerts   = 0
last_check     = None
last_available = []
svc_status     = "starting"

# ── FLASK ─────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, origins="*")

# ── TELEGRAM ──────────────────────────────────────────────────────
def send(chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text,
                  "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
            timeout=10
        )
    except Exception as e:
        log.error(f"send error: {e}")

def welcome(chat_id):
    send(chat_id,
        "✅ *CENT-S Seat Tracker — Connected!*\n\n"
        "🤖 Monitoring *testcisia.it* 24/7\n"
        "Alert fires the instant seats open.\n\n"
        f"🆔 Your Chat ID: `{chat_id}`\n\n"
        "📌 *Commands:*\n"
        "/both — @UNI + @HOME _(default)_\n"
        "/uni — @UNI only\n"
        "/home — @HOME only\n"
        "/status — tracker stats\n"
        "/stop — unsubscribe"
    )

# ── SCRAPER ───────────────────────────────────────────────────────
def scrape():
    r = requests.get(
        SOURCE_URL,
        headers={"User-Agent": "Mozilla/5.0 (CENTSTracker/1.0)"},
        timeout=15
    )
    r.raise_for_status()
    soup  = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    seats = []
    if not table:
        return seats
    for row in table.find_all("tr")[1:]:
        c = row.find_all("td")
        if len(c) < 7:
            continue
        fmt  = c[0].get_text(strip=True).upper()
        uni  = c[1].get_text(strip=True)
        reg  = c[2].get_text(strip=True)
        city = c[3].get_text(strip=True)
        dl   = c[4].get_text(strip=True)
        raw  = c[5].get_text(strip=True)
        st   = c[6].get_text(strip=True).upper()
        lnk  = c[6].find("a")
        date = c[7].get_text(strip=True) if len(c) > 7 else "—"
        try:    n = int(raw)
        except: n = 0
        if lnk and "AVAILABLE" in st and n > 0:
            seats.append({
                "fmt":  fmt,  "uni":  uni,
                "reg":  reg,  "city": city,
                "dl":   dl,   "n":    n,
                "date": date,
                "isu":  "@UNI"  in fmt,
                "ish":  "@HOME" in fmt,
                "key":  f"{fmt}|{uni}|{date}"
            })
    return seats

# ── NOTIFY ────────────────────────────────────────────────────────
def notify(seat):
    global total_alerts
    emoji = "🏛" if seat["isu"] else "🏠"
    msg = (
        f"🚨 *CENT-S SEAT AVAILABLE!*\n\n"
        f"{emoji} *{seat['fmt']}*\n"
        f"🏫 {seat['uni']}\n"
        f"📍 {seat['city']}, {seat['reg']}\n"
        f"🗓 Test: `{seat['date']}`\n"
        f"⏰ Deadline: `{seat['dl']}`\n"
        f"💺 Seats: *{seat['n']}*\n\n"
        f"👉 Book: {BOOKING_URL}\n\n"
        f"⚡ _Be quick — seats fill fast!_"
    )
    for cid, info in list(users.items()):
        p = info.get("pref", "both")
        if p == "uni"  and not seat["isu"]: continue
        if p == "home" and not seat["ish"]: continue
        send(cid, msg)
        total_alerts += 1
        time.sleep(0.05)
    log.info(f"Notified users for: {seat['uni']}")

# ── CHECK LOOP ────────────────────────────────────────────────────
def check_loop():
    global total_checks, last_check, last_available, svc_status, found_keys
    log.info("Check loop started.")
    while True:
        total_checks += 1
        last_check    = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        svc_status    = "checking"
        log.info(f"Check #{total_checks} | users: {len(users)}")
        try:
            seats      = scrape()
            last_available = seats
            svc_status = "ok"
            log.info(f"Found {len(seats)} available seats.")
            for seat in seats:
                if seat["key"] not in found_keys:
                    found_keys.add(seat["key"])
                    if users:
                        notify(seat)
        except Exception as e:
            svc_status = "error"
            log.error(f"Check failed: {e}")
        time.sleep(CHECK_EVERY)

# ── TELEGRAM POLL LOOP ────────────────────────────────────────────
def poll_loop():
    global users
    log.info("Telegram poll loop started.")
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset
            r    = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params=params, timeout=40
            )
            data = r.json()
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg    = upd.get("message", {})
                cid    = str(msg.get("chat", {}).get("id", ""))
                txt    = msg.get("text", "").strip()
                if not cid or not txt:
                    continue
                cmd = txt.split()[0].lower().split("@")[0]
                log.info(f"cmd={cmd} from={cid}")
                if cmd == "/start":
                    users[cid] = {"pref": "both"}
                    welcome(cid)
                elif cmd == "/both":
                    users.setdefault(cid, {})["pref"] = "both"
                    send(cid, "✅ You'll get alerts for *both @UNI and @HOME* seats.")
                elif cmd == "/uni":
                    users.setdefault(cid, {})["pref"] = "uni"
                    send(cid, "✅ You'll get alerts for *@UNI* seats only.")
                elif cmd == "/home":
                    users.setdefault(cid, {})["pref"] = "home"
                    send(cid, "✅ You'll get alerts for *@HOME* seats only.")
                elif cmd == "/status":
                    send(cid,
                        f"📊 *Tracker Status*\n\n"
                        f"🔄 Checks: {total_checks}\n"
                        f"🕐 Last: {last_check}\n"
                        f"💺 Available now: {len(last_available)}\n"
                        f"📨 Alerts sent: {total_alerts}\n"
                        f"👥 Registered: {len(users)}"
                    )
                elif cmd == "/stop":
                    users.pop(cid, None)
                    send(cid, "🛑 Unsubscribed. Send /start anytime to re-subscribe.")
        except Exception as e:
            log.error(f"Poll error: {e}")
            time.sleep(5)

# ── ROUTES ────────────────────────────────────────────────────────
@app.route("/")
def index():
    return jsonify({
        "service": "CENT-S Seat Tracker",
        "status":  svc_status,
        "checks":  total_checks,
        "last_check": last_check,
        "users":   len(users),
        "alerts":  total_alerts
    })

@app.route("/api/register", methods=["POST"])
def register():
    d   = request.get_json() or {}
    cid = str(d.get("chat_id", "")).strip()
    p   = d.get("pref", "both")
    if not cid or not cid.lstrip("-").isdigit():
        return jsonify({"ok": False, "error": "invalid chat_id"}), 400
    users[cid] = {"pref": p}
    log.info(f"Registered {cid} pref={p}")
    send(cid,
        f"✅ *Connected via website!*\n\n"
        f"🎯 Filter: *{p.upper()}*\n"
        f"🔔 Alerts fire instantly when seats open.\n\n"
        f"Commands: /both · /uni · /home · /status · /stop"
    )
    return jsonify({"ok": True})

@app.route("/api/unregister", methods=["POST"])
def unregister():
    d   = request.get_json() or {}
    cid = str(d.get("chat_id", "")).strip()
    users.pop(cid, None)
    return jsonify({"ok": True})

@app.route("/api/status")
def api_status():
    return jsonify({
        "ok": True,
        "checks": total_checks,
        "last_check": last_check,
        "status": svc_status,
        "available_now": last_available,
        "alerts_sent": total_alerts,
        "registered_users": len(users)
    })

@app.route("/health")
def health():
    return jsonify({"ok": True, "checks": total_checks, "status": svc_status})

# ── BOOT — runs at import time (gunicorn safe) ────────────────────
log.info("=== CENT-S Tracker booting ===")
threading.Thread(target=check_loop, daemon=True).start()
threading.Thread(target=poll_loop,  daemon=True).start()
log.info("=== Background threads launched ===")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)
