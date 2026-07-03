# service/ — HTTP front door for report-operator

A thin FastAPI lane over the existing pipeline. BYOK, zero key retention,
stateless: every run happens in a temp dir that is deleted before the
response leaves.

## Endpoints
- `GET  /api/health`   — liveness + whether the email seam is active
- `POST /api/generate` — `{provider, api_key, tickers[], email?, brand?}`
  → JSON with the finished report as base64 (PDF, HTML fallback), plus
  whether it was emailed. `provider: "demo"` runs the bundled fixture
  data with no key.

## Run locally
    pip install -r service/requirements.txt
    playwright install chromium        # for the PDF step
    uvicorn service.app:app --reload
    # then open site/index.html — it targets localhost:8000 by default

## Deploy (Render free tier)
1. Push this repo (service/ included) to GitHub.
2. Render → New → Blueprint → point at the repo. `service/render.yaml`
   does the rest.
3. In the Render dashboard set the email secrets when ready:
   `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`.
   Until then, reports return as downloads and the site says so.
4. Set `ALLOWED_ORIGINS` to your GitHub Pages URL.
5. In `site/index.html`, set `API_BASE` to the Render URL
   (add `<script>window.API_BASE="https://your-app.onrender.com"</script>`
   above the main script, or edit the const).

Free instances sleep after ~15 min idle; the site's error message tells
visitors to wait for the wake-up.

## Site
`site/` is the static front end. Serve it with GitHub Pages
(Settings → Pages → deploy from `/site` on main, or copy to `/docs`).

## Not done yet (deliberate)
- Email is SMTP-seam only. For sending to strangers, swap creds for a
  transactional sender (Resend free tier) — same env vars.
- Rate limit is in-memory per-IP (6/hr). Fine for a demo, not for load.
