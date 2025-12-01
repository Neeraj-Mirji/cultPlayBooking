# main.py
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

import pytz
import requests
from flask import Flask, request, jsonify
import schedule

# -----------------------------------------------------------------------------
# ENV VARS
# -----------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")  
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", TELEGRAM_CHAT_ID) 

# Booking headers / cookies 
API_KEY = os.environ.get("CULT_API_KEY", "REPLACE_WITH_API_KEY")
ST_COOKIE = os.environ.get("CULT_ST_COOKIE", "REPLACE_WITH_ST_COOKIE")
AT_COOKIE = os.environ.get("CULT_AT_COOKIE", "REPLACE_WITH_AT_COOKIE")

COOKIES = {
    "st": ST_COOKIE,
    "at": AT_COOKIE,
}

HEADERS = {
    "apiKey": API_KEY,
    "Cookie": "; ".join([f"{k}={v}" for k, v in COOKIES.items()]),
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)"
}

# Booking preferences 
BOOKING_PREFERENCES = {
    "centers": [1106, 1107],
    "preferred_timings": [
        {"hour": 8, "minute": 0},
        {"hour": 9, "minute": 0}
    ],
    "sport_id": 350,  # badminton
    "enabled": True
}

# Scheduler config
SCHEDULE_TIME_ISO = os.environ.get("SCHEDULE_TIME", "22:00")  # "HH:MM" IST by default
IST_ZONE = pytz.timezone("Asia/Kolkata")

# -----------------------------------------------------------------------------
# App & globals
# -----------------------------------------------------------------------------
app = Flask(__name__)

booking_completed = False
last_run_time: Optional[datetime] = None
last_status = ""  # short text about last run

# Thread control for scheduler loop
_scheduler_thread: Optional[threading.Thread] = None
_scheduler_event = threading.Event()  # when set -> run loop


# -----------------------------------------------------------------------------
# Telegram helper
# -----------------------------------------------------------------------------
def send_telegram(message: str, chat_id: Optional[str] = None):
    """Send a Telegram message. Non blocking on failures."""
    try:
        token = TELEGRAM_BOT_TOKEN
        if not token:
            app.logger.warning("No TELEGRAM_BOT_TOKEN set; cannot send message.")
            return

        target_chat = chat_id or TELEGRAM_CHAT_ID
        if not target_chat:
            app.logger.warning("No target chat id to send message.")
            return

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": target_chat, "text": message}
        requests.post(url, data=payload, timeout=6)
        app.logger.info(f"Sent telegram message to {target_chat}")
    except Exception as e:
        app.logger.exception("Failed to send telegram message: %s", e)


# -----------------------------------------------------------------------------
# Booking Utils
# -----------------------------------------------------------------------------
def get_center_schedule(center_id):
    url = f"https://www.cult.fit/api/v2/fitso/web/schedule?centerId={center_id}"
    resp = requests.get(url=url, headers=HEADERS, timeout=8)
    return resp.json()


def convert_utc_to_timestamp(utc_string):
    try:
        dt_str = utc_string.replace(" GMT", "")
        dt = datetime.strptime(dt_str, "%a, %d %b %Y %H:%M:%S")
        timestamp_seconds = int(dt.replace(tzinfo=timezone.utc).timestamp())
        return timestamp_seconds * 1000
    except Exception as e:
        app.logger.exception("convert_utc_to_timestamp error: %s", e)
        return None


def parse_time_string(time_str):
    try:
        hour, minute = map(int, time_str.split(":")[:2])
        return hour, minute
    except:
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
    if "classByDateList" not in schedule_data:
        return None
    available = []
    for date_group in schedule_data["classByDateList"]:
        for time_group in date_group["classByTimeList"]:
            for slot in time_group["classes"]:
                if (
                    slot.get("workoutId") == sport_id
                    and slot.get("availableSeats", 0) > 0
                    and matches_preferred_timing(time_group["id"])
                ):
                    available.append({
                        "class_id": slot["id"],
                        "date": date_group["id"],
                        "time": time_group["id"],
                        "start_time_utc": slot["startDateTimeUTC"],
                        "seats": slot.get("availableSeats", 0),
                    })
    return available if available else None


def book_slot(center_id, slot_id, workout_id, booking_timestamp):
    payload = {
        "centerId": center_id,
        "slotId": str(slot_id),
        "workoutId": workout_id,
        "bookingTimestamp": booking_timestamp
    }
    url = "https://www.cult.fit/api/v2/fitso/web/class/book"
    try:
        resp = requests.post(url=url, headers=HEADERS, json=payload, timeout=10)
        data = resp.json()
        title = data.get("header", {}).get("title", "")
        if resp.status_code == 200 and ("Booked" in title or "confirmed" in title.lower()):
            msg = f"🎉 Booking successful!\nCenter: {center_id}\nSlot ID: {slot_id}\nTime(ts): {booking_timestamp}"
            app.logger.info(msg)
            send_telegram(msg)
            return True
        else:
            fail_msg = f"❌ Booking not successful for Center {center_id}. Title: {title}"
            app.logger.warning(fail_msg)
            send_telegram(fail_msg)
            return False
    except Exception as e:
        app.logger.exception("book_slot error: %s", e)
        send_telegram(f"Error booking at center {center_id}: {e}")
        return False


# -----------------------------------------------------------------------------
# Booking job 
# -----------------------------------------------------------------------------
def booking_task():
    global booking_completed, last_run_time, last_status

    last_run_time = datetime.now(IST_ZONE)
    app.logger.info("Booking job started at %s", last_run_time.isoformat())

    if not BOOKING_PREFERENCES.get("enabled", True):
        last_status = "Booking disabled in preferences."
        send_telegram("⚠️ Booking disabled in preferences. Use /enable_booking to enable.")
        return

    any_slot_found = False
    try:
        for center_id in BOOKING_PREFERENCES["centers"]:
            schedule_data = get_center_schedule(center_id)
            available = display_available_slots(schedule_data, BOOKING_PREFERENCES["sport_id"])
            if available:
                any_slot_found = True
                first = available[0]
                slot_msg = (f"🏸 Slot Available!\nCenter: {center_id}\nDate: {first['date']}\n"
                            f"Time: {first['time']}\nSeats: {first['seats']}\nClassID: {first['class_id']}")
                app.logger.info("Slot found: %s", slot_msg)
                send_telegram(slot_msg)

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
                    send_telegram(f"⚠️ Could not convert slot time to timestamp for Center {center_id}.")
    except Exception as e:
        last_status = f"Error during booking run: {e}"
        app.logger.exception("booking_task exception: %s", e)
        send_telegram(f"❌ Booking job error: {e}\n{traceback.format_exc()}")

    if not any_slot_found:
        last_status = "No matching slots found."
        send_telegram("ℹ️ No matching slots found in this run.")


# -----------------------------------------------------------------------------
# Scheduler thread management
# -----------------------------------------------------------------------------
class SchedulerThread(threading.Thread):
    def __init__(self, poll_interval: float = 0.5):
        super().__init__(daemon=True)
        self.poll_interval = poll_interval

    def run(self):
        app.logger.info("Scheduler thread running.")
        while _scheduler_event.is_set():
            try:
                schedule.run_pending()
            except Exception:
                app.logger.exception("Error while running scheduled jobs.")
            time.sleep(self.poll_interval)
        app.logger.info("Scheduler thread exiting.")


def start_scheduler_background():
    global _scheduler_thread
    if _scheduler_event.is_set():
        app.logger.info("Scheduler already running.")
        return False

    # Schedule the job at SCHEDULE_TIME_ISO daily in IST
    schedule.clear()  # clear previous jobs to avoid duplication
    hh_mm = SCHEDULE_TIME_ISO
    # schedule.every().day.at expects local time of the container; we will schedule at the time string (HH:MM).
    schedule.every().day.at(hh_mm).do(booking_task)

    _scheduler_event.set()
    _scheduler_thread = SchedulerThread()
    _scheduler_thread.start()
    send_telegram(f"⏰ Scheduler started. Next run scheduled daily at {hh_mm} IST.")
    app.logger.info("Scheduler started; next run at %s", hh_mm)
    return True


def stop_scheduler_background():
    if not _scheduler_event.is_set():
        app.logger.info("Scheduler not running.")
        return False
    _scheduler_event.clear()
    # schedule.clear() optionally
    schedule.clear()
    send_telegram("⛔ Scheduler stopped.")
    app.logger.info("Scheduler stopped.")
    return True


def scheduler_status():
    return "running" if _scheduler_event.is_set() else "stopped"


# -----------------------------------------------------------------------------
# Telegram webhook handling
# -----------------------------------------------------------------------------
def is_admin(chat_id):
    try:
        return str(chat_id) == str(TELEGRAM_ADMIN_CHAT_ID)
    except Exception:
        return False


def handle_command(command: str, chat_id: str, text: str = "") -> str:
    """Return a string reply (also send messages via send_telegram where appropriate)."""
    cmd = command.strip().lower()
    if cmd == "/start":
        help_msg = (
            "Bot connected.\n\nAvailable commands:\n"
            "/start - show this\n"
            "/status - scheduler & booking status\n"
            "/start_scheduler - start daily scheduler\n"
            "/stop_scheduler - stop scheduler\n"
            "/preferences - view booking preferences\n"
            "/enable_booking - enable bookings\n"
            "/disable_booking - disable bookings\n"
            "/run_now - run booking immediately\n"
        )
        return help_msg

    if not is_admin(chat_id):
        return "Unauthorized: only the admin can control this bot."

    if cmd == "/status":
        return (f"Scheduler: {scheduler_status()}\n"
                f"Booking enabled: {BOOKING_PREFERENCES.get('enabled')}\n"
                f"Booking completed this session: {booking_completed}\n"
                f"Last run: {last_run_time.strftime('%Y-%m-%d %H:%M:%S %Z') if last_run_time else 'never'}\n"
                f"Last status: {last_status}")

    if cmd == "/start_scheduler":
        ok = start_scheduler_background()
        return "Scheduler started." if ok else "Scheduler was already running."

    if cmd == "/stop_scheduler":
        ok = stop_scheduler_background()
        return "Scheduler stopped." if ok else "Scheduler was not running."

    if cmd == "/preferences":
        prefs = BOOKING_PREFERENCES
        return f"Preferences:\nCenters: {prefs['centers']}\nTimings: {prefs['preferred_timings']}\nSport ID: {prefs['sport_id']}\nEnabled: {prefs['enabled']}"

    if cmd == "/enable_booking":
        BOOKING_PREFERENCES["enabled"] = True
        return "Booking enabled."

    if cmd == "/disable_booking":
        BOOKING_PREFERENCES["enabled"] = False
        return "Booking disabled."

    if cmd == "/run_now":
        # Run booking synchronously (may take a few seconds)
        try:
            booking_task()
            return "Manual run completed. Check /status for results."
        except Exception as e:
            app.logger.exception("Manual run error: %s", e)
            return f"Manual run failed: {e}"

    return "Unknown command. Send /start for help."


@app.route("/webhook", methods=["POST"])
def webhook():
    """Telegram will POST updates here (set webhook to https://<your-host>/webhook)."""
    update = request.get_json(force=True)
    try:
        # handle message updates
        if "message" in update:
            msg = update["message"]
            chat = msg.get("chat", {})
            chat_id = chat.get("id")
            text = msg.get("text", "")
            if not text:
                return jsonify({"ok": True})

            # bot commands
            if text.strip().startswith("/"):
                reply = handle_command(text.strip().split()[0], chat_id, text)
                # reply to the same chat
                send_telegram(reply, chat_id=str(chat_id))
                return jsonify({"ok": True})

        # other update types ignored
        return jsonify({"ok": True})
    except Exception as e:
        app.logger.exception("Error in webhook handler: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


# Endpoint to set webhook from your side (call this once after deploy)
# NOTE: secure this in production (e.g., require a secret token)
@app.route("/set-webhook", methods=["GET"])
def set_webhook():
    """Call this to register the webhook URL with Telegram.
       Provide ?url=https://yourdomain/render/.../webhook
    """
    token = TELEGRAM_BOT_TOKEN
    if not token:
        return "Missing TELEGRAM_BOT_TOKEN env var", 400

    url_param = request.args.get("url")
    if not url_param:
        return "Provide ?url=https://yourdomain.com/webhook", 400

    set_url = f"https://api.telegram.org/bot{token}/setWebhook"
    resp = requests.post(set_url, data={"url": url_param}, timeout=10)
    return jsonify(resp.json())


# Health check
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "scheduler": scheduler_status()})


# On start - optionally start scheduler automatically if desired
@app.before_first_request
def on_startup():
    # You can choose to auto-start scheduler here by uncommenting:
    # start_scheduler_background()
    app.logger.info("App startup complete. Scheduler running: %s", _scheduler_event.is_set())


# -----------------------------------------------------------------------------
# Run
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # For local testing only (webhook requires HTTPS in production).
    # Use: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, CULT_* env vars before running.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
