import os
import time
import requests
import threading
import logging
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler

# ─── CONFIG ───────────────────────────────────────────────────
BOT_TOKEN   = "8614297250:AAFonU98gkZygF9b1T17J1GdI_8OwmOfOb8"
SOURCE_URL  = "https://testcisia.it/calendario.php?tolc=cents&l=gb&lingua=inglese"
BOOKING_URL = "https://testcisia.it/studenti_tolc/login_sso.php"
CHECK_EVERY = 60  # seconds

# ─── LOGGING ──────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── STATE ────────────────────────────────────────────────────
users = {}
previously_found = set()
stats = {
    "total_checks": 0,
    "last_check": None,
    "last_available": [],
    "total_alerts_sent": 0,
    "status": "idle"
}

app = Flask(__name__)
CORS(app, origins="*")

# ─── TELEGRAM HELPERS ─────────────────────────────────────────
def tg(method, payload):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            json=payload, timeout=10
        )
        return r.json()
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return {}

def send_message(chat_id, text, parse_mode="Markdown"):
    tg("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    })

def send_welcome(chat_id):
    msg = (
        "✅ *CENT-S Seat Tracker — Connected!*\n\n"
        "🤖 This bot monitors *testcisia.it* 24/7\n"
        "and alerts you the instant CENT-S seats open.\n\n"
        "📌 *Commands:*\n"
        "/both — notify for @UNI + @HOME _(default)_\n"
        "/uni — notify for @UNI only\n"
        "/home — notify for @HOME only\n"
        "/status — check tracker status\n"
        "/stop — unsubscribe\n\n"
        f"🆔 Your Chat ID: `{chat_id}`\n\n"
        "⚡ _You will be notified automatically — no further action needed!_"
    )
    send_message(chat_id, msg)

# ─── SCRAPER ──────────────────────────────────────────────────
def scrape_seats():
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CENTSTracker/1.0)"}
    r = requests.get(SOURCE_URL, headers=headers, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    seats = []
    table = soup.find("table")
    if not table:
        return seats
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 7:
            continue
        fmt      = cells[0].get_text(strip=True).upper()
        uni      = cells[1].get_text(strip=True)
        region   = cells[2].get_text(strip=True)
        city     = cells[3].get_text(strip=True)
        deadline = cells[4].get_text(strip=True)
        seats_txt= cells[5].get_text(strip=True)
        state_td = cells[6]
        date     = cells[7].get_text(strip=True) if len(cells) > 7 else "—"
        link     = state_td.find("a")
        state    = state_td.get_text(strip=True).upper()
        try:
            count = int(seats_txt)
        except:
            count = 0
        if link and "AVAILABLE" in state and count > 0:
            seats.append({
                "format":   fmt,
                "uni":      uni,
                "region":   region,
                "city":     city,
                "deadline": deadline,
                "seats":    count,
                "date":     date,
                "is_uni":   "@UNI" in fmt,
                "is_home":  "@HOME" in fmt,
                "key":      f"{fmt}|{uni}|{date}"
            })
    return seats

# ─── NOTIFY ───────────────────────────────────────────────────
def notify_users(seat):
    emoji = "🏛" if seat["is_uni"] else "🏠"
    msg = (
        f"🚨 *CENT-S SEAT AVAILABLE!*\n\n"
        f"{emoji} *{seat['format']}*\n"
        f"🏫 {seat['uni']}\n"
        f"📍 {seat['city']}, {seat['region']}\n"
        f"🗓 Test Date: `{seat['date']}`\n"
        f"⏰ Deadline: `{seat['deadline']}`\n"
        f"💺 Seats: *{seat['seats']}*\n\n"
        f"👉 BOOK NOW: {BOOKING_URL}\n\n"
        f"⚡ _Be quick — seats fill fast!_"
    )
    notified = 0
    for chat_id, info in list(users.items()):
        pref = info.get("pref", "both")
        if pref == "uni"  and not seat["is_uni"]:  continue
        if pref == "home" and not seat["is_home"]: continue
        send_message(chat_id, msg)
        notified += 1
        time.sleep(0.05)
    stats["total_alerts_sent"] += notified
    log.info(f"Notified {notified} users for: {seat['uni']}")

# ─── SEAT CHECK JOB ───────────────────────────────────────────
def check_job():
    stats["total_checks"] += 1
    stats["last_check"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    stats["status"] = "checking"
    log.info(f"Check #{stats['total_checks']} — {stats['last_check']}")
    try:
        available = scrape_seats()
        stats["last_available"] = available
        stats["status"] = "ok"
        for seat in available:
            if seat["key"] not in previously_found:
                previously_found.add(seat["key"])
                if users:
                    notify_users(seat)
                log.info(f"NEW seat: {seat['uni']} ({seat['seats']} seats)")
        log.info(f"Found {len(available)} available. Registered users: {len(users)}")
    except Exception as e:
        stats["status"] = "error"
        log.error(f"Check failed: {e}")

# ─── TELEGRAM POLLING ─────────────────────────────────────────
def poll_telegram():
    log.info("Telegram polling loop started.")
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset
            resp = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params=params, timeout=40
            )
            data = resp.json()
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg     = upd.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text    = msg.get("text", "").strip()
                if not chat_id or not text:
                    continue
                cmd = text.split()[0].lower().replace("@cents_seat_tracker_bot", "")
                log.info(f"Received '{cmd}' from {chat_id}")
                if cmd == "/start":
                    users[chat_id] = {"pref": "both", "added": datetime.utcnow().isoformat()}
                    send_welcome(chat_id)
                elif cmd == "/both":
                    users.setdefault(chat_id, {})["pref"] = "both"
                    send_message(chat_id, "✅ You will receive alerts for *both @UNI and @HOME* seats.")
                elif cmd == "/uni":
                    users.setdefault(chat_id, {})["pref"] = "uni"
                    send_message(chat_id, "✅ You will receive alerts for *@UNI* seats only.")
                elif cmd == "/home":
                    users.setdefault(chat_id, {})["pref"] = "home"
                    send_message(chat_id, "✅ You will receive alerts for *@HOME* seats only.")
                elif cmd == "/status":
                    s = stats
                    send_message(chat_id,
                        f"📊 *Tracker Status*\n\n"
                        f"🔄 Checks: {s['total_checks']}\n"
                        f"🕐 Last: {s['last_check']}\n"
                        f"💺 Available now: {len(s['last_available'])}\n"
                        f"📨 Alerts sent: {s['total_alerts_sent']}\n"
                        f"👥 Users: {len(users)}"
                    )
                elif cmd == "/stop":
                    users.pop(chat_id, None)
                    send_message(chat_id, "🛑 Unsubscribed. Send /start to re-subscribe anytime.")
        except Exception as e:
            log.error(f"Polling error: {e}")
            time.sleep(5)

# ─── FLASK ROUTES ─────────────────────────────────────────────
@app.route("/")
def index():
    return jsonify({
        "service": "CENT-S Seat Tracker",
        "status":  stats["status"],
        "checks":  stats["total_checks"],
        "last_check": stats["last_check"],
        "registered_users": len(users),
        "alerts_sent": stats["total_alerts_sent"]
    })

@app.route("/api/status")
def api_status():
    return jsonify({
        "ok": True,
        "checks": stats["total_checks"],
        "last_check": stats["last_check"],
        "status": stats["status"],
        "available_now": stats["last_available"],
        "alerts_sent": stats["total_alerts_sent"],
        "registered_users": len(users)
    })

@app.route("/health")
def health():
    return jsonify({"ok": True, "checks": stats["total_checks"]})

# ─── START BACKGROUND SERVICES ────────────────────────────────
# NOTE: This runs at module import time so gunicorn picks it up
def _start():
    log.info("Initializing background services...")
    threading.Thread(target=poll_telegram, daemon=True).start()
    sched = BackgroundScheduler()
    sched.add_job(check_job, "interval", seconds=CHECK_EVERY)
    sched.start()
    threading.Thread(target=check_job, daemon=True).start()
    log.info("All background services started.")

_start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)
