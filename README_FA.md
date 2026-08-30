# KatMo Trends Collector — Render Ready

این پکیج را مستقیم داخل GitHub repo `katmo-trends-collector` آپلود کن.

## الان فقط این کار را انجام بده

1. ZIP را Extract کن.
2. وارد repo گیت‌هاب شو.
3. `Add file` → `Upload files`
4. تمام فایل‌ها و پوشه‌های داخل ZIP را آپلود کن.
5. مطمئن شو این فایل‌ها در ریشه repo دیده می‌شوند:
   - `Dockerfile`
   - `requirements.txt`
   - `render.yaml`
   - `openapi-schema-template.yaml`
   - `KATMO_RESEARCH_INSTRUCTIONS.md`
   - پوشه `app`
6. Commit changes را بزن.

بعد فعلاً هیچ کار دیگری نکن.

وقتی Upload و Commit تمام شد، فقط بگو:

`فایل‌ها روی GitHub رفت`

مرحله بعد: اتصال repo به Render و Deploy.

## ساختار سرویس

GPT Action
→ Render API
→ Playwright Chromium
→ Google Trends
→ Trends receipt
→ همان GPT

## نکته امنیتی

API key واقعی روی Render ساخته می‌شود.
در GitHub هیچ secret قرار نداده‌ایم.
