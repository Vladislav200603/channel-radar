# ADR-001: Server-rendered моноліт на Render + Neon + зовнішній scheduler

- Статус: прийнято
- Дата: 2026-09-16

## Контекст

Потрібен живий сервіс за три дні, без платного хостингу, для приблизно десяти каналів.
Render web instances засинають, а native Render Cron уже платний. Файлова SQLite на
Render ephemeral disk не є допустимою production БД.

## Рішення

Один FastAPI/Jinja process розміщується на Render Free, PostgreSQL — у Neon Free.
GitHub Actions двічі на годину викликає захищений `POST /internal/collect-all`.

## Наслідки

- один deployable і немає frontend build/worker/queue;
- scheduler будить sleeping instance і запускає ingestion;
- cold start видимий першому користувачу;
- GitHub schedule не гарантує запуск рівно в хвилину, але для аналітики це прийнятно;
- при зростанні понад десять каналів collect-all слід винести в worker/queue.

## Відхилені альтернативи

- Render Postgres: короткий життєвий цикл free database;
- Fly.io: немає постійного free tier;
- Cloud Run: для активації потрібен billing account;
- SPA + окремий API: зайва deployment surface для цього MVP.

