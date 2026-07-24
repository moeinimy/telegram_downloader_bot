# راهنمای دیپلوی (گیت‌محور)

کد روی گیت‌هابه. سرور مستقیم از گیت‌هاب clone می‌کنه و هر آپدیت فقط یه دستوره:

```bash
bash /opt/telegram_downloader_bot/deploy/update.sh
```

فایل‌های حساس (`.env`، کوکی‌ها، دانلودها) داخل گیت نیستن و آپدیت بهشون دست نمی‌زنه.

---

## چی لازم داری؟ (تقریبا هیچی)

فقط **توکن بات تلگرام**. بقیه چیزا اختیاریه:

| بخش | نیاز به اکانت/کلید؟ |
|---|---|
| یوتیوب (ویدیو + MP3) | ❌ نه — از player client های جایگزین استفاده می‌کنه |
| اسپاتیفای (ترک/آلبوم/پلی‌لیست/آرتیست) | ❌ نه — از صفحه embed عمومی می‌خونه |
| ساندکلاد | ❌ نه |
| سرچ متنی آهنگ | ❌ نه |
| شزم (تشخیص آهنگ) + متن ترانه | ❌ نه |
| اینستاگرام: ریلز و پست ویدیویی | ❌ نه |
| اینستاگرام: **استوری** و پست چندعکسی | ✅ کوکی یه اکانت یه‌بارمصرف |
| آپلود بالای ۵۰ مگ | ✅ Local Bot API (پایین توضیح داده شده) |

---

## نصب روی سرور جدید (Ubuntu 24)

### ۱) کلون

ریپو پرایوته، پس اول روی سرور کلید بساز:

```bash
ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N "" -q
```

کلید عمومی (`cat /root/.ssh/id_ed25519.pub`) رو تو گیت‌هاب اضافه کن:
Settings ریپو → Deploy keys → Add deploy key (فقط read).

```bash
git clone git@github.com:moeinimy/telegram_downloader_bot.git /opt/telegram_downloader_bot
```

### ۲) ساخت .env

```bash
cp /opt/telegram_downloader_bot/.env.example /opt/telegram_downloader_bot/.env
nano /opt/telegram_downloader_bot/.env
```

فقط `TELEGRAM_BOT_TOKEN` رو پر کن. بقیه رو خالی بذار.

### ۳) نصب

```bash
bash /opt/telegram_downloader_bot/deploy/install.sh
```

خودش python، ffmpeg، Deno، کاربر `botuser`، venv و سرویس systemd رو راه می‌ندازه.

---

## اختیاری: آپلود تا ۲ گیگ (Local Bot API)

```bash
apt-get install -y docker.io && systemctl enable --now docker

docker run -d --name telegram-bot-api --restart always \
  -p 127.0.0.1:8081:8081 \
  -e TELEGRAM_API_ID=<API_ID> \
  -e TELEGRAM_API_HASH=<API_HASH> \
  -v telegram-bot-api-data:/var/lib/telegram-bot-api \
  aiogram/telegram-bot-api:latest --local
```

`API_ID` و `API_HASH` از https://my.telegram.org می‌گیری. اگه بات قبلا به کلود تلگرام وصل بوده اول logout کن:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/logOut"
```

بعد تو `.env`:

```
BOT_API_BASE_URL=http://127.0.0.1:8081
MAX_UPLOAD_MB=2000
```

و `systemctl restart tg-downloader-bot`

---

## اختیاری: استوری اینستاگرام

با یه اکانت یه‌بارمصرف تو مرورگر لاگین کن، بعد از DevTools → Application → Cookies → instagram.com این‌ها رو بردار و تو `.env` بذار:

```
INSTAGRAM_USERNAME=<یوزرنیم اکانت>
IG_SESSIONID=...
IG_CSRFTOKEN=...
IG_DS_USER_ID=...
```

بدون این‌ها ربات ریلز و پست ویدیویی رو مشکلی نداره؛ فقط استوری و پست چندعکسی کار نمی‌کنه.

---

## آپدیت

روی ویندوز:

```bash
git add -A && git commit -m "update" && git push
```

روی سرور:

```bash
bash /opt/telegram_downloader_bot/deploy/update.sh
```

---

## دستورات مفید

```bash
systemctl status tg-downloader-bot
```

```bash
journalctl -u tg-downloader-bot -f
```
