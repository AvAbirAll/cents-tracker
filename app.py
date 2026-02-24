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
CHECK_EVERY = 1  # seconds

# ─── LOGGING ──────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── STATE ────────────────────────────────────────────────────
# users = { chat_id: { "pref": "both"|"uni"|"home", "added": timestamp } }
users = {}
# previously_found = set of seat keys already notified
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
    tg("sendMessage", {"chat_id": chat_id, "text": text,
                        "parse_mode": parse_mode,
                        "disable_web_page_preview": False})

def send_welcome(chat_id):
    msg = (
        "👋 *Welcome to CENT-S Seat Tracker!*\n\n"
        "🤖 This bot monitors *testcisia.it* 24/7 and alerts you "
        "the instant CENT-S seats become available.\n\n"
        "✅ You are now registered!\n\n"
        "📌 Go back to the website and enter your Chat ID to set your preferences.\n\n"
        f"🆔 Your Chat ID is: `{chat_id}`"
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

    for row in table.find_all("tr")[1:]:  # skip header
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

        link = state_td.find("a")
        state_text = state_td.get_text(strip=True).upper()

        try:
            count = int(seats_txt)
        except:
            count = 0

        if link and "AVAILABLE" in state_text and count > 0:
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

# ─── NOTIFICATION ─────────────────────────────────────────────
def notify_users(seat):
    fmt_emoji = "🏛" if seat["is_uni"] else "🏠"
    msg = (
        f"🚨 *CENT-S SEAT AVAILABLE!*\n\n"
        f"{fmt_emoji} *{seat['format']}*\n"
        f"🏛 {seat['uni']}\n"
        f"📍 {seat['city']}, {seat['region']}\n"
        f"🗓 Test Date: `{seat['date']}`\n"
        f"⏰ Booking Deadline: `{seat['deadline']}`\n"
        f"💺 Available Seats: *{seat['seats']}*\n\n"
        f"👉 [BOOK NOW — Click Here]({BOOKING_URL})\n\n"
        f"⚡ _Be quick — seats fill fast!_"
    )

    notified = 0
    for chat_id, info in list(users.items()):
        pref = info.get("pref", "both")
        if pref == "uni"  and not seat["is_uni"]:  continue
        if pref == "home" and not seat["is_home"]: continue
        send_message(chat_id, msg)
        notified += 1
        time.sleep(0.05)  # avoid flood

    stats["total_alerts_sent"] += notified
    log.info(f"Notified {notified} users for: {seat['uni']}")

# ─── MAIN CHECK JOB ───────────────────────────────────────────
def check_job():
    global previously_found
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
                log.info(f"NEW seat found: {seat['uni']} ({seat['seats']} seats)")

        log.info(f"Found {len(available)} available slot(s). Users: {len(users)}")

    except Exception as e:
        stats["status"] = "error"
        log.error(f"Check failed: {e}")

# ─── TELEGRAM WEBHOOK / POLLING ───────────────────────────────
def poll_telegram():
    """Long-poll Telegram for /start and preference commands."""
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
            updates = data.get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip().lower()

                if not chat_id:
                    continue

                if text in ["/start", "start"]:
                    if chat_id not in users:
                        users[chat_id] = {"pref": "both", "added": datetime.utcnow().isoformat()}
                    send_welcome(chat_id)

                elif text in ["/both", "both"]:
                    if chat_id in users:
                        users[chat_id]["pref"] = "both"
                        send_message(chat_id, "✅ You will be notified for *both @UNI and @HOME* seats.")

                elif text in ["/uni", "uni"]:
                    if chat_id in users:
                        users[chat_id]["pref"] = "uni"
                        send_message(chat_id, "✅ You will be notified for *@UNI* seats only.")

                elif text in ["/home", "home"]:
                    if chat_id in users:
                        users[chat_id]["pref"] = "home"
                        send_message(chat_id, "✅ You will be notified for *@HOME* seats only.")

                elif text == "/status":
                    s = stats
                    send_message(chat_id,
                        f"📊 *Tracker Status*\n\n"
                        f"🔄 Checks done: {s['total_checks']}\n"
                        f"🕐 Last check: {s['last_check']}\n"
                        f"💺 Currently available: {len(s['last_available'])}\n"
                        f"📨 Alerts sent: {s['total_alerts_sent']}\n"
                        f"👥 Registered users: {len(users)}"
                    )

                elif text == "/stop":
                    if chat_id in users:
                        del users[chat_id]
                        send_message(chat_id, "🛑 You have been unsubscribed. Send /start to re-subscribe.")

        except Exception as e:
            log.error(f"Polling error: {e}")
            time.sleep(5)

# ─── FLASK API ROUTES ─────────────────────────────────────────
@app.route("/")
def index():
    return jsonify({
        "service": "CENT-S Seat Tracker Backend",
        "status": stats["status"],
        "checks": stats["total_checks"],
        "last_check": stats["last_check"],
        "registered_users": len(users),
        "alerts_sent": stats["total_alerts_sent"]
    })

@app.route("/api/register", methods=["POST"])
def register():
    """Frontend calls this to register a user with their pref."""
    data = request.get_json() or {}
    chat_id = str(data.get("chat_id", "")).strip()
    pref    = data.get("pref", "both")

    if not chat_id or not chat_id.lstrip("-").isdigit():
        return jsonify({"ok": False, "error": "Invalid chat_id"}), 400

    users[chat_id] = {"pref": pref, "added": datetime.utcnow().isoformat()}
    log.info(f"Registered user {chat_id} with pref={pref}")

    send_message(chat_id,
        f"✅ *You are now registered!*\n\n"
        f"🎯 Preference: *{pref.upper()}*\n"
        f"🔔 You will be notified the moment CENT-S seats open.\n\n"
        f"Commands:\n"
        f"/both — notify for @UNI + @HOME\n"
        f"/uni — notify for @UNI only\n"
        f"/home — notify for @HOME only\n"
        f"/status — check tracker status\n"
        f"/stop — unsubscribe"
    )
    return jsonify({"ok": True, "message": "Registered successfully"})

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

@app.route("/api/seats")
def api_seats():
    return jsonify({
        "ok": True,
        "seats": stats["last_available"],
        "count": len(stats["last_available"]),
        "last_check": stats["last_check"]
    })

@app.route("/api/unregister", methods=["POST"])
def unregister():
    data = request.get_json() or {}
    chat_id = str(data.get("chat_id", "")).strip()
    if chat_id in users:
        del users[chat_id]
    return jsonify({"ok": True})

# ─── STARTUP ──────────────────────────────────────────────────
if __name__ == "__main__":
    # Start Telegram polling in background thread
    t = threading.Thread(target=poll_telegram, daemon=True)
    t.start()
    log.info("Telegram polling started.")

    # Start scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_job, "interval", seconds=CHECK_EVERY)
    scheduler.start()
    log.info(f"Scheduler started — checking every {CHECK_EVERY}s.")

    # Run first check immediately
    threading.Thread(target=check_job, daemon=True).start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, use_reloader=Fasle)
