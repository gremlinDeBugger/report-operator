# Playwright's image ships headless Chromium — needed for the PDF step.
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble
WORKDIR /app
COPY service/requirements.txt service/requirements.txt
RUN pip install --no-cache-dir -r service/requirements.txt
COPY . .
# SMTP_* and ALLOWED_ORIGINS are set in the host's environment, never here.
EXPOSE 8000
# Render assigns $PORT (default 10000) at runtime; fall back to 8000 locally.
CMD ["sh", "-c", "uvicorn service.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
