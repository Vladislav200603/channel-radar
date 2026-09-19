# Channel Radar

> **Live service:** https://channel-radar-6kls.onrender.com
>
> **Repository:** https://github.com/Vladislav200603/channel-radar

Живий сервіс аналітики публічних Telegram-каналів. Користувач додає username у браузері,
сервіс збирає останні пости з анонімного `t.me/s/<channel>`, зберігає часові зрізи метрик,
показує дашборд і створює семиденний AI-дайджест.

## Що вже працює

- додавання `@username`, `t.me/username` або `t.me/s/username` з інтерфейсу;
- явні стани збору: `pending → collecting → healthy/degraded/error`;
- інкрементальний збір із природним ключем `(channel_id, telegram_post_id)`;
- resumable пагінація за серверним `rel=prev`: великий gap наздоганяється кількома
  bounded-запусками без тихого пропуску постів;
- історія підписників, переглядів, forwards і реакцій погодинними snapshots;
- загальний огляд, сторінка каналу, фільтр періоду, графіки, список і сторінка поста;
- простий сигнал аномалії: пост має щонайменше `2×` медіани переглядів поточної вибірки;
- AI-дайджест за сім днів через Gemini з graceful degradation;
- захищений endpoint регулярного збору для GitHub Actions;
- міграції Alembic і 31 тест без зовнішньої мережі.

## Архітектура

```text
Browser ──▶ FastAPI + Jinja + Chart.js ──▶ Neon PostgreSQL
                 │              │
                 │              └────────▶ Gemini API (не критичний шлях)
                 │
                 └───────────────────────▶ t.me/s/<channel>
                           ▲
                           │ POST /internal/collect-all
                    GitHub Actions cron
```

Це один server-rendered моноліт без SPA, черги й окремого worker. Для десятка каналів та
триденного тестового це зменшує кількість точок відмови й дає один зрозумілий deployable.
Web-шар не знає HTML Telegram, parser не знає БД, а AI-адаптер не є частиною ingestion.

## Локальний запуск

Потрібен Python 3.12+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
copy .env.example .env  # на macOS/Linux: cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

Відкрити <http://localhost:8000>. За замовчуванням локально використовується SQLite;
production завжди використовує зовнішній PostgreSQL через `DATABASE_URL`.

### Змінні середовища

| Змінна | Обов'язкова у production | Призначення |
|---|---:|---|
| `DATABASE_URL` | так | connection string Neon PostgreSQL |
| `CRON_SECRET` | так | довгий випадковий секрет для внутрішнього collect endpoint |
| `GEMINI_API_KEY` | для AI | ключ Google AI Studio, ніколи не зберігається в git |
| `GEMINI_MODEL` | ні | за замовчуванням `gemini-3.8-flash` |
| `GEMINI_FALLBACK_MODEL` | ні | резервна модель, за замовчуванням `gemini-3.5-flash-lite` |
| `BASE_URL` | так | публічний URL сервісу |
| `COLLECTION_BUCKET_MINUTES` | ні | ширина idempotency bucket, за замовчуванням 60 хв |

## Модель даних та ідемпотентність

### `channels`

Нормалізований lowercase `username` є унікальним. Запис також тримає останній health,
помилку, час останньої спроби, останнього успішного збору та безпечний Telegram cursor
незавершеного backfill для швидкого overview і відновлення після рестарту.

### `posts`

`UNIQUE(channel_id, telegram_post_id)` — природний ключ Telegram. Повторний імпорт
оновлює текст/метадані та `last_seen_at`, але не створює другий пост.

### `channel_metric_snapshots` і `post_metric_snapshots`

Метрики змінюються після публікації, тому вони не перезаписуються лише в `posts`.
Для кожної години існує одна точка:

- `UNIQUE(channel_id, observed_bucket)` для каналу;
- `UNIQUE(post_id, observed_bucket)` для поста.

Повтор того самого збору в межах години оновлює цю точку — retry ідемпотентний. Наступна
година додає нову точку й формує часовий ряд. Відсутня реакція/метрика зберігається як
`NULL`, а не як вигаданий нуль. Графік переглядів показує середнє на один видимий пост,
а не суму змінної за розміром preview/backfill-вибірки, тому великий backfill не створює
штучний стрибок лише через кількість рядків.

### `channel_digests`

Зберігає успішні відповіді AI з періодом, provider/model і часом. Помилка нового виклику
не видаляє попередній дайджест.

Повна мотивація: [ADR-002](docs/adr/002-metric-snapshots.md).

## Збір Telegram

Запити йдуть тільки на валідований fixed origin `https://t.me/s/{username}`, тому форма
не створює SSRF. Клієнт не слідує redirect: Telegram перенаправляє приватні/неіснуючі
канали з `/s/` на загальну сторінку. Parser — чиста функція `HTML → ParsedChannel` і
перевіряється локальними fixtures.

При кожному запуску сервіс читає свіже вікно. Якщо воно не перетинається з уже відомими
post IDs, collector переходить за `link[rel=prev]` до перетину (не вгадує ID арифметично).
Один запуск обмежений п'ятьма сторінками. Якщо цього мало, перевірений same-origin cursor
зберігається у БД, канал переходить у `degraded`, а наступний запуск продовжує саме цей
gap. Лише після його закриття collector знову відкриває latest window — так одночасні
великі gap не витісняють один одного. Пости upsert-яться, а метрики отримують snapshot.

Перший збір навмисно зберігає свіже preview-вікно одразу, щоб користувач не чекав. Якщо
Telegram має старіші сторінки, їхній cursor також зберігається: наступні заплановані
збори поступово дозавантажують історію блоками до п'яти сторінок. Лічильник на огляді
тому підписаний «пости в базі», а не видається за повну кількість дописів каналу.

### Відомі обмеження джерела

- `t.me/s` показує лише вікно останніх постів, а не гарантований повний архів;
- перегляди, reactions і subscribers приходять округленими (`14.3K`, `10.7M`), тому це
  приблизні значення;
- web-preview не дозволяє надійно відрізнити приватний канал від неіснуючого;
- HTML Telegram — неофіційний контракт: зміна layout дає явну помилку, а не тихі нулі;
- коли старий пост зникає з preview, його історія лишається в БД, але нові точки для
  нього більше не з'являються.

## AI-шар

Окрема дія на сторінці каналу надсилає до Gemini до 40 останніх постів за сім днів.
Prompt вимагає українською:

1. короткий підсумок;
2. 3–5 головних тем;
3. спостереження щодо фокусу або тональності.

Виклик має timeout і не лежить у критичному шляху збору. Без ключа, при quota `429`,
timeout або невалідній відповіді метрики та сторінки працюють далі; користувач бачить
спокійне повідомлення, а останній успішний digest зберігається. Free Tier Gemini може
використовувати надіслані дані для покращення продуктів Google; тут передаються лише
вже публічні Telegram-пости. Див. [ADR-003](docs/adr/003-gemini-digest.md).

## Production deployment

Обрано **Render Free Web Service + Neon Free PostgreSQL + GitHub Actions schedule**.
Render найшвидше дає публічний Python URL без картки, Neon не прив'язує життєвий цикл БД
до sleeping web-процесу, а зовнішній HTTP-trigger одночасно будить сервіс і запускає збір.

1. Створити Neon project і скопіювати pooled connection string.
2. Створити Render Web Service з цього repository або через `render.yaml`.
3. Додати Render secrets: `DATABASE_URL`, `CRON_SECRET`, `GEMINI_API_KEY`, `BASE_URL`.
4. У GitHub Actions додати secret `CRON_SECRET`; URL цього deployment уже вказаний у workflow.
5. Запустити workflow `Collect channel updates` вручну й перевірити `/readyz`.

Актуальні на 18.09.2026 обмеження, які враховані:

- Render Free: 0.1 CPU, 512 MB, 750 год/місяць, sleep після 15 хвилин; cold start може
  тривати близько хвилини;
- Render Cron вже платний, тому не використовується;
- Neon Free: 0.5 GB storage і scale-to-zero; `pool_pre_ping` відновлює idle connection;
- workflow запускається двічі на годину, має timeout/retry і секрет у header;
- один `collect-all` обмежено десятьма каналами й короткими upstream timeouts.

Див. [ADR-001](docs/adr/001-runtime-and-hosting.md),
[Render Free docs](https://render.com/docs/free) та [Neon pricing](https://neon.com/pricing).

## Тести

```bash
pytest
ruff check .
```

Покриті головні ризики:

- нормалізація username і compact numbers;
- парсинг постів, main text замість reply quote, reactions і відсутні reactions;
- fail-loud перевірка malformed layout, redirect і безпечних pagination cursors;
- повторний збір у тому самому bucket без дублікатів;
- новий bucket створює нову metric point, але не новий post;
- epoch-aligned buckets до 24 годин і resumable backfill між запусками;
- арифметика anomaly signal;
- Gemini success, quota/auth failure та відсутність витоку ключа через `httpx.MockTransport`.

`pytest-socket` дозволяє лише loopback, потрібний event loop на Windows, і блокує всю
зовнішню мережу; HTML лежить у `tests/fixtures`, AI використовує тільки mock transport.

## Що свідомо не зроблено

Порівняння каналів, алерти та export для `pulse` залишені поза MVP, бо вони позначені як
необов'язкові. За триденний термін пріоритет — надійний vertical slice: додати канал,
побачити дані, історію, AI-аналіз і мати живий URL.
