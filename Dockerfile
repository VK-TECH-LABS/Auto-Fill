# Public Auto-Fill service image.
# This image does not contain a service token, a database URL, or caller source.
# Pass AUTOFILL_SERVICE_TOKEN at runtime.

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    AUTOFILL_RUN_BROWSER=1 \
    AUTOFILL_BROWSER_NO_SANDBOX=1 \
    PORT=8080

WORKDIR /app

COPY LICENSE NOTICE README.md pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /root/.cache/pip \
    && useradd --create-home --shell /usr/sbin/nologin autofill \
    && chown -R autofill:autofill /app

USER autofill

EXPOSE 8080

CMD ["python", "-m", "autofill.service"]
