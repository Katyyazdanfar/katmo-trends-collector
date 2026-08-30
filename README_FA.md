# KatMo Trends Collector v2 — 429 Fix

این نسخه برای کاهش خطای Google Trends 429 ساخته شده و جایگزین کامل نسخه قبلی است.

تغییرات اصلی:
- فاصله اجباری بین درخواست‌ها
- jitter تصادفی
- exponential backoff برای 429
- refresh کردن session/cookies بین retryها
- cache شش‌ساعته
- جلوگیری از اجرای 5-year بلافاصله بعد از 429 روی 12-month
- warm browser session

## جایگزینی روی GitHub

کل فایل‌های repo قبلی را با محتوای این ZIP جایگزین کن، با همین ساختار:

app/main.py
requirements.txt
Dockerfile
render.yaml
.dockerignore

فایل‌های Action داخل GPT نیاز به تغییر ندارند، چون endpointها همان‌اند:
GET /health
POST /validate-candidate

## بعد از Deploy

1. صبر کن Render Live شود.
2. health را باز کن:
   https://katmo-trends-collector-1.onrender.com/health
3. باید version = 2.0.0 ببینی.
4. سپس فقط یک Candidate را در GPT تست کن.
5. اگر FULL یا PARTIAL با داده واقعی برگشت، بعد سراغ تست 5–6 candidate برو.

اگر همچنان 429 کامل باقی ماند، مشکل به احتمال زیاد reputation/IP دیتاسنتر Render در برابر Google Trends است و دیگر با prompt یا retry بیشتر حل نمی‌شود.
