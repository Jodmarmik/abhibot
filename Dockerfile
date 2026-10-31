FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && useradd --create-home bot
COPY --chown=bot:bot autofilter ./autofilter
COPY --chown=bot:bot bot.py gen_session.py ./
USER bot
CMD ["python", "bot.py"]
