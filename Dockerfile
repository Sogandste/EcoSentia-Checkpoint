FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Spaces runs containers as UID 1000. Declaring the user explicitly keeps
# ownership of the audit directory predictable instead of platform-dependent.
RUN useradd --create-home --uid 1000 app
WORKDIR /home/app

COPY --chown=app:app requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .

RUN mkdir -p /home/app/audit && chown -R app:app /home/app/audit

USER app

# Render injects PORT at runtime; 10000 is only the local fallback.
ENV PORT=10000
EXPOSE 10000

# Shell form is required here. In exec form (the JSON array), ${PORT} is passed
# to gunicorn as a literal string and the bind silently fails.
CMD gunicorn \
    --bind "0.0.0.0:${PORT}" \
    --workers "${WEB_CONCURRENCY:-1}" \
    --threads 4 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    app:app
