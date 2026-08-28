FROM python:3.14-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# After the install, so changing the build stamp does not invalidate the
# dependency layer. Only the suffix comes from the build - the version itself is
# declared in app/version.py, so a plain `docker build` reports the bare release.
ARG APP_BUILD=""
ENV APP_BUILD=${APP_BUILD}

COPY app/ ./app/
COPY templates/ ./templates/
COPY static/ ./static/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
