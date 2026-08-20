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

# The directory is created at build time because the application refuses to
# boot if the audit path is not writable. Deferring this to first write would
# turn a configuration fault into a runtime failure mid-scan.
RUN mkdir -p /home/app/audit && chown -R app:app /home/app/audit

USER app
ENV ECOSENTIA_AUDIT_PATH=/home/app/audit/ecosentia_audit.jsonl
EXPOSE 7860

# A single worker. The audit log is a hash chain appended by one process;
# concurrent workers would interleave writes and break it. Threads provide
# request concurrency without duplicating the writer.
CMD ["gunicorn", "--bind", "0.0.0.0:7860", "--workers", "1", \
     "--threads", "4", "--timeout", "120", "app:app"]
