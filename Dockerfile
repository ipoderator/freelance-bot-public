# Образ для сервера (deploy/docker-compose.yml). Python и версии зависимостей — те же,
# на которых проходит `make check` (deploy/constraints.txt снят с .venv).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY deploy/constraints.txt deploy/constraints.txt
COPY pyproject.toml ./
COPY freelance_bot freelance_bot
# tzdata: zoneinfo для Europe/Moscow в slim-образе без системной базы часовых поясов
RUN pip install -c deploy/constraints.txt . tzdata==2025.2 \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin bot

COPY settings.toml ./
COPY profile/profile.toml profile/profile.toml
COPY deploy/healthcheck.py deploy/healthcheck.py

USER bot
CMD ["python", "-m", "freelance_bot", "run"]
