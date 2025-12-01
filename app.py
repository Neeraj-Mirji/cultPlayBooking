# main.py
import os
import traceback
from datetime import datetime
from typing import Optional

import pytz
import requests
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

# ---------------------------------------------------------------------
# ENV VARS
# ---------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", TELEGRAM_CHAT_ID)

API_KEY = os.environ.get("CULT_API_KEY", "REPLACE_WITH_API_KEY")
ST_COOKIE = os.environ.get("CULT_ST_COOKIE", "REPLACE_WITH_ST_COOKIE")
AT_COOKIE = os.environ.get("CULT_AT_COOKIE", "REPLACE_WITH_AT_COOKIE")

COOKIES = {"st": ST_COOKIE, "at": AT_COOKIE}

HEADERS = {
    "apiKey": API_KEY,
    "Cookie": "; ".join([f"{k}={v}" for k, v in COOKIES.items()]),
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)",
}

# ---------------------------------------------------------------------
# Booking Preferences
# ---------------------------------------------------------------------
BOOKING_PREFERENCES = {
    "centers": [1106, 1107],
    "preferred_timings": [{"hour": 8, "minute": 0}, {"hour": 9, "minute": 0}],
    "sport_id": 350,  # badminton
    "enabled": True,
}

# Timezone
IST_ZONE = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------
# Flask App & Globals
# ---------------------------------------------------------------------
app = Flask(__name__)

booking_completed = False
last_run_time: Optional[datetime] = None
last_status = ""

scheduler = BackgroundScheduler(timezone=IST_ZONE)

# ---------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------
def log(msg: str):
    print(msg, flush=True)
    app.logger.info(msg)

def log_exc(msg: str):
    print(msg, flush=True)
    app.logger.exception(msg)

def send_telegram(message: str, chat_id: Optional[str] = None):
    try:
        if not TELEGRAM_BOT_TOKEN:
            log("No TELEGRAM_BOT_TOKEN set; cannot send Telegram message.")
            return
        target_chat = chat_id or TELEGRAM_CHAT_ID
        if not target_chat:
            log("No Telegram chat_id configured; skipping send.")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": target_chat, "text": message}
        resp = requests.post(url, data=payload, timeout=6)
        log(f"Sent Telegram message to {target_chat} (status {resp.status_code})")
    except Exception as e:
        log_exc(f"Failed to send Telegram message: {e}")

# ---------------------------------------------------------------------
# Booking Utils
# ---------------------------------------------------------------------
def get_center_schedule(center_id):
    url = f"https://www.cult.fit/api/v2/fitso/web/schedule?centerId={center_id}"
    log(f"[HTTP] GET {url} (headers masked)")
    resp = requests.get(url=url, headers=HEADERS, timeout=8)
    try:
        return resp.json()
    except Exception:
        log(f"[HTTP] Failed to parse JSON for schedule: status={resp.status_code} text={resp.text}")
        raise

def convert_utc_to_timestamp(utc_string):
    try:
        dt_str = utc_string.replace(" GMT", "")
        dt = datetime.strptime(dt_str, "%a, %d %b %Y %H:%M:%S")
        dt = dt.replace(tzinfo=pytz.UTC)
        return int(dt.timestamp()) * 1000
    except Exception as e:
        log_exc(f"convert_utc_to_timestamp error: {e}")
        return None

def parse_time_string(time_str):
    try:
        hour, minute = map(int, time_str.split(":")[:2])
        return hour, minute
    except Exception:
        return None, None

def matches_preferred_timing(time_str):
    hour, minute = parse_time_string(time_str)
    if hour is None:
        return False
    for pref in BOOKING_PREFERENCES["preferred_timings"]:
        if hour == pref["hour"] and minute == pref["minute"]:
            return True
    return False

def display_available_slots(schedule_data, sport_id):
    if not isinstance(schedule_data, dict) or "classByDateList" not in schedule_data:
        return None
    available = []
    for date_group in schedule_data["classByDateList"]:
        for time_group in date_group.get("classByTimeList", []):
            for slot in time_group.get("classes", []):
                if (
                    slot.get("workoutId") == sport_id
                    and slot.get("availableSeats", 0) > 0
                    and matches_preferred_timing(time_group.get("id", ""))
                ):
                    available.append({
                        "class_id": slot.get("id"),
                        "date": date_group.get("id"),
                        "time": time_group.get("id"),
                        "start_time_utc": slot.get("startDateTimeUTC"),
                        "seats": slot.get("availableSeats", 0),
                        "raw": slot
                    })
    return available if available else None

def book_slot(center_id, slot_id, workout_id, booking_timestamp):
    payload = {
        "centerId": center_id,
        "slotId": str(slot_id),
        "workoutId": workout_id,
        "bookingTimestamp": booking_timestamp,
    }
    url = "https://www.cult.fit/api/v2/fitso/web/class/book"
    try:
        log(f"[BOOKING] Request -> center={center_id} slot={slot_id} timestamp={booking_timestamp}")
        resp = requests.post(url=url, headers=HEADERS, json=payload, timeout=10)
        data = None
        try:
            data = resp.json()
        except:
            log(f"[BOOKING] Response Text: {resp.text}")
        title = data.get("header", {}).get("title", "") if isinstance(data, dict) else ""
        if resp.status_code == 200 and ("Booked" in title or "confirmed" in title.lower()):
            msg = f"🎉 Booking Successful!\n📍 Center: {center_id}\n🆔 Slot ID: {slot_id}\n⏰ Timestamp: {booking_timestamp}"
            log(msg)
            send_telegram(msg)
            return True
        else:
            reason = title or (data.get("message") if isinstance(data, dict) else resp.text[:300])
            fail_msg = f"❌ Booking Failed\n📍 Center: {center_id}\n📝 Reason: {reason}"
            log(fail_msg)
            send_telegram(fail_msg)
            return False
    except Exception as e:
        log_exc(f"book_slot exception: {e}")
        send_telegram(f"❌ Booking exception at center {center_id}: {e}")
        return False

# ---------------------------------------------------------------------
# Booking Task
# ---------------------------------------------------------------------
def booking_task():
    global booking_completed, last_run_time, last_status

    last_run_time = datetime.now(IST_ZONE)
    log(f"[JOB] Booking job started at {last_run_time.isoformat()}")

    if not BOOKING_PREFERENCES.get("enabled", True):
        last_status = "Booking disabled in preferences."
        send_telegram("⚠️ Booking is disabled. Use /enable_booking to enable.")
        return

    any_slot_found = False
    try:
        for center_id in BOOKING_PREFERENCES["centers"]:
            log(f"[JOB] Checking center {center_id} for preferred slots...")
            try:
                schedule_data = get_center_schedule(center_id)
            except Exception as e:
                log_exc(f"[JOB] Failed to fetch schedule for center {center_id}: {e}")
                continue

            available = display_available_slots(schedule_data, BOOKING_PREFERENCES["sport_id"])
            log(f"[JOB] Available slots for center {center_id}: {available if available else 'None'}")

            if available:
                any_slot_found = True
                first = available[0]
                slot_msg = (
                    f"🏸 Slot Available!\n📍 Center: {center_id}\n📅 Date: {first['date']}\n"
                    f"⏰ Time: {first['time']}\n🎟 Seats: {first['seats']}\n🆔 Class ID: {first['class_id']}"
                )
                send_telegram(slot_msg)
                log(f"[JOB] Notified Telegram about slot (center {center_id}).")

                booking_timestamp = convert_utc_to_timestamp(first["start_time_utc"])
                if booking_timestamp:
                    ok = book_slot(center_id, first["class_id"], BOOKING_PREFERENCES["sport_id"], booking_timestamp)
                    if ok:
                        booking_completed = True
                        last_status = "Booking successful."
                        return
                    else:
                        last_status = "Booking attempted but failed."
                else:
                    last_status = "Could not parse slot timestamp."
                    send_telegram(f"⚠️ Could not convert slot time for Center {center_id} (invalid/missing).")
    except Exception as e:
        last_status = f"Error during booking run: {e}"
        log_exc(f"[JOB] booking_task exception: {e}\n{traceback.format_exc()}")
        send_telegram(f"❌ Booking job error: {e}")

    if not any_slot_found:
        last_status = "No matching slots found."
        send_telegram("ℹ️ No matching slots found in this run.")

# ---------------------------------------------------------------------
# Scheduler Setup
# ---------------------------------------------------------------------
def start_scheduler():
    scheduler.remove_all_jobs()
    scheduler.add_job(booking_task, 'cron', hour=22, minute=0)  # Runs at 10 PM IST
    if not scheduler.running:
        scheduler.start()
    send_telegram("⏰ Scheduler started. Booking daily at 10 PM IST.")
    log("[SCHEDULER] Started daily at 10 PM IST.")

def stop_scheduler():
    scheduler.remove_all_jobs()
    send_telegram("⛔ Scheduler stopped.")
    log("[SCHEDULER] Stopped.")

def scheduler_status():
    return "running" if scheduler.get_jobs() else "stopped"

# ---------------------------------------------------------------------
# Telegram Webhook
# ---------------------------------------------------------------------
def is_admin(chat_id):
    return str(chat_id) == str(TELEGRAM_ADMIN_CHAT_ID)

def supported_commands():
    return [
        "/status", "/start_scheduler", "/stop_scheduler",
        "/preferences", "/enable_booking", "/disable_booking", "/run_now"
    ]

def handle_command(command: str, chat_id: str, text: str = "") -> str:
    cmd = command.strip().lower()
    if cmd == "/start":
        return (
            "🤖 CultPlay Scheduler\n"
            "Automated booking assistant.\n\n"
            "📋 Commands:\n" +
            "\n".join(supported_commands())
        )

    if not is_admin(chat_id):
        return "🔒 Unauthorized. Only bot admin can use control commands."

    if cmd == "/status":
        return (
            f"🟢 Scheduler: {scheduler_status()}\n"
            f"🔔 Booking enabled: {BOOKING_PREFERENCES.get('enabled')}\n"
            f"✅ Booking completed: {booking_completed}\n"
            f"⏱ Last run: {last_run_time.strftime('%Y-%m-%d %H:%M:%S %Z') if last_run_time else 'never'}\n"
            f"📝 Last status: {last_status}"
        )

    if cmd == "/start_scheduler":
        start_scheduler()
        return "✅ Scheduler started."

    if cmd == "/stop_scheduler":
        stop_scheduler()
        return "✅ Scheduler stopped."

    if cmd == "/preferences":
        prefs = BOOKING_PREFERENCES
        return (
            f"⚙️ Preferences\nCenters: {prefs['centers']}\n"
            f"Timings: {prefs['preferred_timings']}\nSport ID: {prefs['sport_id']}\n"
            f"Enabled: {prefs['enabled']}"
        )

    if cmd == "/enable_booking":
        BOOKING_PREFERENCES["enabled"] = True
        return "🔔 Booking enabled."

    if cmd == "/disable_booking":
        BOOKING_PREFERENCES["enabled"] = False
        return "🔕 Booking disabled."

    if cmd == "/run_now":
        try:
            booking_task()
            return "⚡ Manual run executed. Check status with /status."
        except Exception as e:
            log_exc(f"[CMD] Manual run error: {e}")
            return f"❌ Manual run failed: {e}"

    # If unknown command, show supported commands
    return "❓ Unknown command. Supported commands:\n" + "\n".join(supported_commands())

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    try:
        if "message" in update:
            msg = update["message"]
            chat = msg.get("chat", {})
            chat_id = chat.get("id")
            text = msg.get("text", "")
            if not text:
                return jsonify({"ok": True})
            if text.strip().startswith("/"):
                reply = handle_command(text.strip().split()[0], chat_id, text)
                send_telegram(reply, chat_id=str(chat_id))
        return jsonify({"ok": True})
    except Exception as e:
        log_exc(f"[WEBHOOK] Error handling update: {e}\n{traceback.format_exc()}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "scheduler": scheduler_status()})

# ---------------------------------------------------------------------
# Run App
# ---------------------------------------------------------------------
if __name__ == "__main__":
    start_scheduler()  # Start automatically
    log(f"App starting - scheduler status: {scheduler_status()}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
