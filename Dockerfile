FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

WORKDIR /app

# Зависимости отдельным слоем: правка кода не тянет переустановку всего.
COPY pyproject.toml README.md ./
COPY alert_bot/__init__.py ./alert_bot/
RUN pip install --no-cache-dir .

COPY alert_bot ./alert_bot
COPY scripts ./scripts

# Процесс не должен работать от root: он ходит в сеть и разбирает чужой ввод.
RUN useradd --create-home --uid 10001 botuser \
    && mkdir -p /app/data \
    && chown -R botuser:botuser /app
USER botuser

# Инструкции VOLUME здесь намеренно нет: Railway её не принимает и отклоняет
# сборку целиком. Каталог /app/data подключается снаружи — в docker-compose
# через bind mount, на Railway через их собственный Volume.

# Контейнер «запущен» и бот работает — разные вещи: цикл цены может уткнуться
# в зависший сокет, пока процесс формально жив. Проверяется возраст пульса,
# который цикл ставит на каждом обороте.
HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
    CMD python -m alert_bot.health || exit 1

CMD ["python", "-m", "alert_bot.main"]
