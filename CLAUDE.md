# Agent contract

## Межі шарів

- `app/main.py` — HTTP, валідація форми й composition. Тут не парсимо Telegram HTML і не
  пишемо prompt logic.
- `app/services/telegram.py` — fixed-origin HTTP adapter та чистий parser. Він не імпортує
  SQLAlchemy models і не знає про UI.
- `app/services/collector.py` — orchestration та idempotent persistence. Один source failure
  не повинен зупинити інші канали.
- `app/services/ai.py` — optional adapter. LLM ніколи не блокує ingestion і не видаляє
  останній успішний результат.
- `app/services/analytics.py` — детерміновані чисті розрахунки.
- `templates/` не виконують БД-запити та не містять secrets.

## Незмінні домовленості

1. Telegram username проходить normalization + strict regex; URL користувача не можна
   передавати HTTP-клієнту напряму.
2. Natural key поста — `(channel_id, telegram_post_id)`.
3. Metric snapshots ідемпотентні в межах configured time bucket.
4. `NULL` означає «джерело не показало метрику»; не перетворювати це на `0`.
5. Tests не звертаються в мережу. Telegram — fixtures, Gemini — mock transport.
6. Secrets тільки в environment. Не логувати API keys, DB URL або cron token.
7. Зміна schema завжди має Alembic migration.
8. Кожний збір спочатку зберігає свіже preview-вікно. Recent gaps мають окремі cursor,
   фіксовану нижню межу ID та чергу; архів має незалежний cursor і ніколи не завершується
   лише через знайомий post ID. Архівні помилки не скасовують свіжі дані. `degraded`
   означає незавершений recent gap; archive progress/error показуються окремо.

## Перед завершенням зміни

Запустити:

```bash
ruff check .
pytest
```

Для parser changes додати fixture, що відтворює саме новий HTML-case.
