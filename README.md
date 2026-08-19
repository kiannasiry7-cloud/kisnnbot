# Persian Anonymous Telegram Bot

ربات پیام ناشناس فارسی با لینک اختصاصی هر کاربر.

## امکانات

- لینک اختصاصی ناشناس برای هر کاربر
- ارسال ناشناس متن، عکس، ویدیو، ویس، فایل، استیکر و Video Note
- پاسخ ناشناس بدون نمایش هویت طرفین
- بلاک فرستنده
- گزارش پیام
- توقف/فعال‌سازی دریافت پیام
- پنل ادمین و مشاهده ۱۰ پیام آخر برای مدیریت و رسیدگی به گزارش‌ها
- SQLite و اجرای ساده با Long Polling
- Docker-ready

## اجرا

Python 3.12+:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
BOT_TOKEN=... ADMIN_ID=... python bot.py
```

## متغیرهای محیطی

- `BOT_TOKEN` — توکن BotFather (اجباری)
- `ADMIN_ID` — Telegram numeric user ID ادمین؛ اگر `0` باشد پنل ادمین غیرفعال است
- `DB_PATH` — مسیر SQLite، پیش‌فرض `bot.db`
- `LOG_LEVEL` — پیش‌فرض `INFO`

## حریم خصوصی

هویت فرستنده به کاربران نمایش داده نمی‌شود. برای امکان بلاک، گزارش و مدیریت سوءاستفاده، شناسه‌های داخلی sender/recipient در دیتابیس نگه‌داری می‌شوند. اگر `ADMIN_ID` تنظیم شود، ادمین می‌تواند لاگ مدیریتی پیام‌های اخیر را ببیند.

توکن را هرگز داخل GitHub commit نکنید؛ آن را فقط در Environment Variables سرویس میزبان قرار دهید.
