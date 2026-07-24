# راهنمای دیپلوی (گیت‌محور)

جریان کار: کد روی گیت‌هابه. سرور مستقیم از گیت‌هاب clone می‌کنه و هر آپدیت فقط یه دستوره:

```bash
bash /opt/telegram_downloader_bot/deploy/update.sh
```

فایل‌های حساس (`.env`، کوکی‌ها، دانلودها) داخل گیت نیستن و آپدیت بهشون دست نمی‌زنه.

---

## نصب روی سرور جدید (Ubuntu 24)

### 1) کلون پروژه

```bash
git clone https://github.com/<USERNAME>/telegram_downloader_bot.git /opt/telegram_downloader_bot
cd /opt/telegram_downloader_bot
```

اگه ریپو **پرایوته**، یه Personal Access Token بساز (GitHub → Settings → Developer settings → Fine-grained tokens → فقط دسترسی read به همین ریپو) و اینجوری کلون کن:

```bash
git clone https://<TOKEN>@github.com/<USERNAME>/telegram_downloader_bot.git /opt/telegram_downloader_bot
```

### 2) ساخت .env

```bash
cp .env.example .env
nano .env
```

مقادیر لازم: `TELEGRAM_BOT_TOKEN`، `SPOTIFY_CLIENT_ID/SECRET`، کوکی‌های اینستاگرام (`IG_SESSIONID`, `IG_CSRFTOKEN`, `IG_DS_USER_ID`, `INSTAGRAM_USERNAME`)، و اگه Local Bot API راه انداختی `BOT_API_BASE_URL` و `MAX_UPLOAD_MB=2000`.

### 3) آپلود کوکی یوتیوب

فایل `youtube_cookies.txt` (فرمت Netscape، اکسپورت از مرورگر لاگین‌شده) رو با SFTP بذار توی:

```
/opt/telegram_downloader_bot/youtube_cookies.txt
```

### 4) اجرای نصب

```bash
bash deploy/install.sh
```

این اسکریپت خودش python/ffmpeg/Deno رو نصب می‌کنه، کاربر `botuser` می‌سازه، venv می‌سازه و سرویس systemd رو فعال می‌کنه.

### 5) (اختیاری ولی پیشنهادی) Local Bot API — لیمیت آپلود 2GB

```bash
apt-get install -y docker.io
systemctl enable --now docker

docker run -d --name telegram-bot-api --restart always \
  -p 127.0.0.1:8081:8081 \
  -e TELEGRAM_API_ID=<API_ID> \
  -e TELEGRAM_API_HASH=<API_HASH> \
  -v telegram-bot-api-data:/var/lib/telegram-bot-api \
  aiogram/telegram-bot-api:latest --local
```

`API_ID` و `API_HASH` از https://my.telegram.org می‌گیری. بعد توی `.env`:

```
BOT_API_BASE_URL=http://127.0.0.1:8081
MAX_UPLOAD_MB=2000
```

اگه بات قبلا به سرور کلود تلگرام وصل بوده، اول باید از کلود logout کنی:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/logOut"
```

بعد: `systemctl restart tg-downloader-bot`

---

## آپدیت (بعد از هر تغییر کد)

روی ویندوز (پوش به گیت‌هاب):

```bash
git add -A && git commit -m "update" && git push
```

روی سرور (یه دستور):

```bash
bash /opt/telegram_downloader_bot/deploy/update.sh
```

---

## دستورات مفید

```bash
systemctl status tg-downloader-bot        # وضعیت
journalctl -u tg-downloader-bot -f        # لاگ زنده
systemctl restart tg-downloader-bot       # ریستارت دستی
```
