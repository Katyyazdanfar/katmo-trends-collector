# KatMo Trends Collector v3 — Failed-to-fetch Fix

این نسخه جایگزین کامل v2 است.

خطای مشاهده‌شده:
`Page.evaluate: TypeError: Failed to fetch`

علت:
نسخه v2 درخواست Google Trends را از داخل `page.evaluate(fetch(...))` اجرا می‌کرد.
در محیط Render این fetch مرورگری شکست می‌خورد و endpoint با 500 تمام می‌شد.

اصلاح v3:
- حذف کامل `page.evaluate(fetch(...))`
- استفاده از `BrowserContext.request.get(...)`
- حفظ session/cookies مرورگر Playwright
- حفظ pace / backoff / jitter / cache
- اضافه‌شدن route اصلی `/` تا GET / دیگر 404 ندهد
- خطاهای شبکه حالا به‌صورت structured response برمی‌گردند، نه crash/500

## کاری که باید انجام شود

کل محتویات repo قبلی را با فایل‌های این ZIP جایگزین کن:

- app/main.py
- requirements.txt
- Dockerfile
- render.yaml
- .dockerignore

بعد Commit کن.

Render باید خودکار Deploy کند.

## تست بعد از Live شدن

این آدرس را باز کن:

https://katmo-trends-collector-1.onrender.com/health

باید ببینی:

"version": "3.0.0"

و:

"fetch_strategy": "playwright-context-request"

بعد فقط یک بار `validateCandidateTrends` را در GPT Builder تست کن.

اگر نتیجه 429 بود، دیگر مشکل crash نیست و محدودیت IP خود Render/Google Trends مطرح است.
