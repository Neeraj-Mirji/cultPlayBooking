# main.py
import os
import threading
import time
import traceback
from datetime import datetime
from typing import Optional

import pytz
import requests
from flask import Flask, request, jsonify
import schedule

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

# Scheduler config
SCHEDULE_TIME_ISO = "22:00"  # Run daily at 10 PM IST
IST_ZONE = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------
# Flask App & Globals
# ---------------------------------------------------------------------
app = Flask(__name__)

booking_completed = False
last_run_time: Optional[datetime] = None
last_status = ""

_scheduler_thread: Optional[threading.Thread] = None
_scheduler_event = threading.Event()  # True -> scheduler running

# ---------------------------------------------------------------------
# Utilities for logging
# ---------------------------------------------------------------------
def log(msg: str):
    """Print + logger for Render logs visibility."""
    print(msg, flush=True)
    app.logger.info(msg)

def log_exc(msg: str):
    print(msg, flush=True)
    app.logger.exception(msg)

# ---------------------------------------------------------------------
# Telegram Helper
# ---------------------------------------------------------------------
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

def send_telegram_async(msg, chat_id=None):
    threading.Thread(target=send_telegram, args=(msg, chat_id), daemon=True).start()

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
        # Attach UTC manually
        import time
        timestamp_seconds = int(time.mktime(dt.timetuple()))
        return timestamp_seconds * 1000
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
        try:
            data = resp.json()
        except Exception:
            data = None
        title = ""
        if isinstance(data, dict):
            title = data.get("header", {}).get("title", "") or ""
        if resp.status_code == 200 and ("Booked" in title or "confirmed" in title.lower()):
            msg = (
                "🎉 Booking Successful!\n"
                f"📍 Center: {center_id}\n"
                f"🆔 Slot ID: {slot_id}\n"
                f"⏰ Timestamp: {booking_timestamp}"
            )
            log(msg)
            return True
        else:
            return False
    except Exception as e:
        log_exc(f"book_slot exception: {e}")
        return False

# ---------------------------------------------------------------------
# Booking Task (Booking First, Telegram After)
# ---------------------------------------------------------------------
def booking_task():
    global booking_completed, last_run_time, last_status

    last_run_time = datetime.now(IST_ZONE)
    log(f"[JOB] Booking job started at {last_run_time.isoformat()}")

    if not BOOKING_PREFERENCES.get("enabled", True):
        last_status = "Booking disabled in preferences."
        send_telegram_async("⚠️ Booking is disabled. Use /enable_booking to enable.")
        return

    any_slot_found = False
    try:
        for center_id in BOOKING_PREFERENCES["centers"]:
            try:
                schedule_data = get_center_schedule(center_id)
            except Exception as e:
                log_exc(f"[JOB] Failed to fetch schedule for center {center_id}: {e}")
                continue

            available = display_available_slots(schedule_data, BOOKING_PREFERENCES["sport_id"])
            if available:
                any_slot_found = True
                first = available[0]

                # 1️⃣ Attempt booking immediately
                booking_timestamp = convert_utc_to_timestamp(first["start_time_utc"])
                booking_success = False
                if booking_timestamp:
                    booking_success = book_slot(center_id, first["class_id"],
                                                BOOKING_PREFERENCES["sport_id"],
                                                booking_timestamp)
                    if booking_success:
                        booking_completed = True
                        last_status = "Booking successful."
                    else:
                        last_status = "Booking attempted but failed."
                else:
                    last_status = "Could not parse slot timestamp."

                # 2️⃣ Send Telegram asynchronously
                msg = (
                    f"🏸 Slot Info (Center {center_id}):\n"
                    f"📅 Date: {first['date']}\n"
                    f"⏰ Time: {first['time']}\n"
                    f"🎟 Seats: {first['seats']}\n"
                    f"🆔 Class ID: {first['class_id']}\n"
                    f"✅ Booking Status: {'Success' if booking_success else 'Failed'}"
                )
                send_telegram_async(msg)

                if booking_success:
                    return

    except Exception as e:
        last_status = f"Error during booking run: {e}"
        log_exc(f"[JOB] booking_task exception: {e}\n{traceback.format_exc()}")
        send_telegram_async(f"❌ Booking job error: {e}")

    if not any_slot_found:
        last_status = "No matching slots found."
        send_telegram_async("ℹ️ No matching slots found in this run.")

# ---------------------------------------------------------------------
# Scheduler Thread
# ---------------------------------------------------------------------
class SchedulerThread(threading.Thread):
    def __init__(self, poll_interval: float = 0.5):
        super().__init__(daemon=True)
        self.poll_interval = poll_interval

    def run(self):
        log("[SCHEDULER] Thread started.")
        while _scheduler_event.is_set():
            try:
                schedule.run_pending()
            except Exception:
                log_exc("[SCHEDULER] Error while running scheduled jobs.")
            time.sleep(self.poll_interval)
        log("[SCHEDULER] Thread exiting.")

def start_scheduler_background():
    global _scheduler_thread
    if _scheduler_event.is_set():
        log("[SCHEDULER] Already running.")
        return False

    schedule.clear()
    schedule.every().day.at(SCHEDULE_TIME_ISO).do(booking_task)

    _scheduler_event.set()
    _scheduler_thread = SchedulerThread()
    _scheduler_thread.start()
    send_telegram_async(f"⏰ Scheduler started. Next run daily at {SCHEDULE_TIME_ISO} IST.")
    log(f"[SCHEDULER] Started; next run at {SCHEDULE_TIME_ISO}")
    return True

def stop_scheduler_background():
    if not _scheduler_event.is_set():
        log("[SCHEDULER] Not running.")
        return False
    _scheduler_event.clear()
    schedule.clear()
    send_telegram_async("⛔ Scheduler stopped.")
    log("[SCHEDULER] Stopped.")
    return True

def scheduler_status():
    return "running" if _scheduler_event.is_set() else "stopped"

# ---------------------------------------------------------------------
# Telegram Webhook
# ---------------------------------------------------------------------
def is_admin(chat_id):
    return str(chat_id) == str(TELEGRAM_ADMIN_CHAT_ID)

def handle_command(command: str, chat_id: str, text: str = "") -> str:
    cmd = command.strip().lower()
    commands_supported = [
        "/start", "/status", "/start_scheduler", "/stop_scheduler",
        "/preferences", "/enable_booking", "/disable_booking", "/run_now"
    ]
    if cmd == "/start":
        return (
            "🤖 *CultPlay Scheduler*\n"
            "Your automated booking assistant.\n\n"
            "📋 *Commands*\n"
            "/status - Show scheduler & booking status\n"
            "/start_scheduler - Start daily scheduler\n"
            "/stop_scheduler - Stop scheduler\n"
            "/preferences - View booking preferences\n"
            "/enable_booking - Enable automatic booking\n"
            "/disable_booking - Disable automatic booking\n"
            "/run_now - Run booking immediately (manual)\n"
        )

    if cmd not in commands_supported:
        return "❓ Unknown command. Supported commands: " + ", ".join(commands_supported)

    if not is_admin(chat_id):
        return "🔒 Unauthorized. Only the bot admin can use control commands."

    if cmd == "/status":
        return (
            f"🟢 Scheduler: {scheduler_status()}\n"
            f"🔔 Booking enabled: {BOOKING_PREFERENCES.get('enabled')}\n"
            f"✅ Booking completed: {booking_completed}\n"
            f"⏱ Last run: {last_run_time.strftime('%Y-%m-%d %H:%M:%S %Z') if last_run_time else 'never'}\n"
            f"📝 Last status: {last_status}"
        )

    if cmd == "/start_scheduler":
        ok = start_scheduler_background()
        return "✅ Scheduler started." if ok else "ℹ️ Scheduler already running."

    if cmd == "/stop_scheduler":
        ok = stop_scheduler_background()
        return "✅ Scheduler stopped." if ok else "ℹ️ Scheduler was not running."

    if cmd == "/preferences":
        prefs = BOOKING_PREFERENCES
        return (
            f"⚙️ Preferences\n"
            f"Centers: {prefs['centers']}\n"
            f"Timings: {prefs['preferred_timings']}\n"
            f"Sport ID: {prefs['sport_id']}\n"
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

# ---------------------------------------------------------------------
# Flask Routes
# ---------------------------------------------------------------------
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
                send_telegram_async(reply, chat_id=str(chat_id))
        return jsonify({"ok": True})
    except Exception as e:
        log_exc(f"[WEBHOOK] Error handling update: {e}\n{traceback.format_exc()}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/set-webhook", methods=["GET"])
def set_webhook():
    token = TELEGRAM_BOT_TOKEN
    if not token:
        return "Missing TELEGRAM_BOT_TOKEN env var", 400
    url_param = request.args.get("url")
    if not url_param:
        return "Provide ?url=https://yourdomain.com/webhook", 400
    set_url = f"https://api.telegram.org/bot{token}/setWebhook"
    resp = requests.post(set_url, data={"url": url_param}, timeout=10)
    return jsonify(resp.json())

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "scheduler": scheduler_status()})

# ---------------------------------------------------------------------
# Run app
# ---------------------------------------------------------------------
if __name__ == "__main__":
    start_scheduler_background()
    log("App starting - scheduler status: " + scheduler_status())
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
