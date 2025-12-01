🎯 Cult Play Auto-Booking Bot

Automated Cult badminton slot booking bot with a Telegram interface, scheduler, and Render deployment support.

This bot keeps checking for available slots and instantly books them based on your preferences — with live Telegram alerts.

⭐ Features

✓ Automatically checks for available slots
✓ Auto-booking when matching slot is found
✓ Telegram alerts for availability & status updates
✓ Scheduler you can start/stop anytime
✓ Commands to change preferences (coming soon)
✓ Deployable on Render free tier
✓ Fully environment-variable driven

📁 Project Structure
project/
│── app.py                  # Flask app + Telegram webhook
│── telegram_bot.py         # Telegram command handlers
│── scheduler.py            # APScheduler logic
│── cult_client.py          # Cult API calls (login, search, book)
│── booking.py              # Booking logic wrapper
│── utils.py                # Helpers & logging
│── requirements.txt
│── .env
│── README.md

🔧 Setup Instructions (Local)
1️⃣ Clone the repo
git clone https://github.com/yourusername/cult-auto-booking.git
cd cult-auto-booking

2️⃣ Create .env file

Create a file named .env in the root:

# Telegram Config
TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=YOUR_CHAT_ID

# Cult login details
CULT_USERNAME=your_phone_or_email
CULT_PASSWORD=your_password

# Scheduler interval
SCHEDULER_INTERVAL_MINUTES=3

# After deployment set:
WEBHOOK_URL=https://your-render-url.onrender.com/webhook

3️⃣ Install dependencies
pip install -r requirements.txt

4️⃣ Run the server
python app.py


The server runs at:

http://127.0.0.1:5000/

5️⃣ Test locally
Health endpoint
http://127.0.0.1:5000/

Trigger booking check manually
http://127.0.0.1:5000/run_now

Start scheduler
http://127.0.0.1:5000/start_scheduler

Stop scheduler
http://127.0.0.1:5000/stop_scheduler

🤖 Telegram Bot Setup
1️⃣ Start the bot

Open Telegram → search for your bot → click Start.

2️⃣ Send /start

You will see the list of commands.

3️⃣ Commands available
/start – help menu
/status – current state of scheduler + preferences
/start_scheduler – begin auto-checking
/stop_scheduler – stop auto-checking
/preferences – view monitoring preferences
/enable_booking – enable auto booking
/disable_booking – disable auto booking
/run_now – manually run booking check

🌐 Deploying on Render
1️⃣ Push your repository to GitHub

Make sure it contains:

app.py

requirements.txt

other Python files

2️⃣ Create Render Web Service

Visit: https://render.com

New → Web Service

Connect your GitHub repo

Configure:

Setting	Value
Runtime	Python 3.10+
Build Command	pip install -r requirements.txt
Start Command	gunicorn app:app
3️⃣ Add environment variables

Render → Your Service → Environment

Paste the same values from your .env.

4️⃣ Deploy

Render will give you a URL like:

https://cultplaybooking.onrender.com

🤖 Set Telegram Webhook (Required)

Replace <TOKEN> and use your Render URL:

https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://your-app.onrender.com/webhook


Example:

https://api.telegram.org/bot12345:ABC/setWebhook?url=https://cultplaybooking.onrender.com/webhook


Success response:

{"ok":true,"result":true,"description":"Webhook was set"}