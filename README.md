# YTR (Youtube TikTok Reels Downloader Bot)

Личный Telegram-бот, который скачивает видео из YouTube / Instagram / TikTok / Twitter, расшифровывает речь, формирует читаемую подпись через Claude-агента и отправляет обратно в твой Telegram-чат как self-hosted архив. Рассчитан на работу как systemd-сервис на 1-CPU VPS с преднагруженной моделью Whisper в отдельном демоне.

## Возможности

- 🎬 **Мультиплатформа** — YouTube (включая Shorts), Instagram (Reels / посты / карусели), TikTok, Twitter / X.
- 🎙️ **Речь → текст** — `faster-whisper` (модель `tiny`, int8-квантизация), преднагруженный как Unix-socket демон, распознаёт с почти нулевой задержкой.
- 🤖 **Генерация подписи через Claude Haiku** — listener оркестрирует `download.sh → claude -p → send_telegram.py`; Haiku 4.5 пишет подпись по шаблону из `CLAUDE.md`.
- 🎵 **Аудио-режим** — префикс `audio` / `mp3` / `аудио` / `звук` перед URL → получишь MP3 (LAME V0 VBR, ~220-260 kbps) через `sendAudio` вместо видео.
- 🖼️ **Поддержка альбомов** — Instagram-карусели (`/p/<id>/`) скачиваются как playlist и приходят последовательностью сообщений, подпись — на первом.
- 📊 **Живой прогресс** — сообщение в Telegram редактируется по стадиям: `📥 Скачиваю` → `🎙️ Расшифровываю` → `🎬 Сжимаю под 50 MB` → `📤 Отправляю` → `✅ Готово`.
- 🔒 **Access control** — whitelist `TELEGRAM_ALLOWED_CHAT_IDS`; whitelist доменов URL на листенере; HTML-escape + редактирование токенов в сообщениях об ошибках.
- 🗜️ **Авто-пересжатие** — видео > 50 MB (лимит Telegram Bot API) пересжимаются через `ffmpeg libx264 -preset ultrafast` с битрейтом, посчитанным по длительности.
- 🛡️ **Надёжная отправка** — HTTP/2 через `httpx`, ретраи на 429 от Telegram с учётом `Retry-After`, fallback с HTML-парсинга на plain text при невалидной разметке подписи.

## Стек

| Слой | Инструмент |
|---|---|
| Bot framework | Python 3.10+ · aiogram 3 · asyncio |
| HTTP-клиент | httpx (HTTP/2) |
| Скачивание | yt-dlp |
| Обработка аудио | ffmpeg / ffprobe |
| Распознавание речи | faster-whisper (int8, CPU, CTranslate2) |
| Генерация подписи | Claude Code CLI (`claude -p ...`, Haiku 4.5) |
| Конфиг | python-dotenv, `.env` |
| Деплой | systemd (два сервиса: listener + whisper daemon) |

## Архитектура

```
      ┌────────────┐
      │ Telegram   │
      └──────┬─────┘
             │  сообщения (URL)
             ▼
      ┌──────────────────────┐
      │  listener.py         │  aiogram-поллинг · whitelist URL · ACL chat_id
      │   (оркестратор)      │  · живой прогресс через per-job status-файл
      └──┬───────────┬───────┘  · download_lock + caption_semaphore
         │           │   └──────────────┐
         ▼           ▼                  ▼
   download.sh   claude -p        send_telegram.py
    │   │         (Haiku 4.5)           │
    │   │         генерит подпись       │ HTTP/2
    │   │         из CLAUDE.md          ▼
    │   │                          Telegram API
    │   ▼
    │ transcribe_client.py ──AF_UNIX──▶ transcribe_worker.py (daemon)
    │                                    · faster-whisper преднагружен
    ▼
  yt-dlp + ffmpeg
```

## Структура файлов

```
ytr-downloader-bot/
├── CLAUDE.md                   # шаблон подписи — listener пробрасывает его в prompt к claude -p
├── README.md
├── requirements.txt            # закреплённые Python-зависимости
├── listener.py                 # aiogram-бот + прогресс-трекер
├── ytr-downloader-bot.service  # systemd-юнит листенера
├── whisper-worker.service      # systemd-юнит whisper-демона
├── .env.example                # шаблон — скопируй в .env и заполни секретами
├── .gitignore
├── scripts/
│   ├── download.sh             # yt-dlp + ffmpeg + транскрипт + пересжатие
│   ├── transcribe_worker.py    # долгоживущий whisper-демон (Unix-сокет)
│   ├── transcribe_client.py    # тонкий клиент, которым пользуется download.sh
│   └── send_telegram.py        # HTTP/2-аплоад с ретраями + чистка мусора
├── tests/
│   └── test_smoke.sh           # smoke-тесты: CLI-валидация, whitelist, truncate
└── storage/
    └── videos/                 # временная рабочая директория (чистится скриптами)
```

## Установка и запуск

### 1. Системные пакеты

```bash
sudo apt install -y python3.10 python3.10-venv ffmpeg jq nodejs
```

`nodejs` нужен yt-dlp для решения YouTube n-challenge (`--remote-components ejs:github --js-runtimes node`).

### 2. Python-окружение

```bash
cd ~/ytr-downloader-bot
python3.10 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

### 3. Секреты

```bash
cp .env.example .env
# отредактируй .env и заполни:
#   TELEGRAM_BOT_TOKEN        — из @BotFather
#   TELEGRAM_ALLOWED_CHAT_IDS — твои chat_id через запятую
chmod 600 .env
```

Если нужна авторизация YouTube (Shorts / возрастные ограничения / приватные видео), экспортируй `cookies.txt` в Netscape-формате из **инкогнито**-сессии браузера и положи его в `~/ytr-downloader-bot/cookies.txt` (`chmod 600`). Необходимые имена куков (`SID`, `HSID`, `SSID`, `APISID`, `SAPISID`, `LOGIN_INFO`) описаны в комментариях `scripts/download.sh`.

### 4. Claude Code

Листенер запускает `claude -p ...`, поэтому [Claude Code CLI](https://claude.com/claude-code) должна быть установлена и авторизована на хосте:

```bash
# следуй инструкциям Claude Code
claude --version
```

### 5. systemd-юниты

В `.service`-файлах путь к проекту и имя пользователя — плейсхолдер `USERNAME`. Подставь свой логин через `sed` при копировании:

```bash
sed "s|USERNAME|$USER|g" whisper-worker.service       | sudo tee /etc/systemd/system/whisper-worker.service
sed "s|USERNAME|$USER|g" ytr-downloader-bot.service   | sudo tee /etc/systemd/system/ytr-downloader-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now whisper-worker ytr-downloader-bot
sudo systemctl status whisper-worker ytr-downloader-bot
```

Проект должен лежать в `~/ytr-downloader-bot` (директория жёстко прописана в шаблоне). Если у тебя другой путь — отредактируй service-файлы вручную вместо `sed`.

`whisper-worker` грузит модель (`~150-250 MB RAM`) при старте; `ytr-downloader-bot` зависит от него через `Requires=`.

### 6. Smoke-тесты

```bash
bash tests/test_smoke.sh
```

Проверяет: валидацию CLI, отклонение невалидного JSON / чужих chat_id, whitelist URL, обрезку подписи.

## Переменные окружения (`.env`)

| Переменная | Обязательная | Описание |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Токен бота от [@BotFather](https://t.me/BotFather). |
| `TELEGRAM_ALLOWED_CHAT_IDS` | ✅ | Список разрешённых chat_id через запятую (например `123456789,987654321`). Первый в списке — получатель по умолчанию для исходящих сообщений; все в списке могут управлять ботом. |

`whisper-worker.service` также задаёт две опциональные `Environment=` переменные:

| Переменная | По умолчанию | Описание |
|---|---|---|
| `WHISPER_MODEL` | `tiny` | размер faster-whisper модели (`tiny`, `base`, `small`, …). |
| `WHISPER_SOCKET` | `/tmp/whisper.sock` | путь до Unix-сокета демона. |

## Использование

Отправь боту сообщение с URL видео:

```
https://www.youtube.com/shorts/abc123
```

Для аудио-режима:

```
audio https://www.youtube.com/watch?v=abc123
mp3   https://youtu.be/abc123
```

Для Instagram-каруселей используй ссылку на пост (`/p/<id>/`). Одиночные посты и reels идут обычным single-file флоу.

## Для разработчиков

- Claude вызывается только для **генерации подписи** (text-only, без tool use). Метаданные от `download.sh` (title, uploader, description, transcript) подставляются в prompt вместе с шаблоном из `CLAUDE.md`. Промпт явно запрещает отказываться при пустом транскрипте — Haiku использует `description` как fallback. `TELEGRAM_BOT_TOKEN` в env не передаётся (`listener.build_claude_env()` вырезает).
- `send_telegram.py` принимает `--chat-id`, `--caption-file`, позиционные файлы (и сохраняет stdin-JSON режим для обратной совместимости). Проверяет что файлы внутри `storage/videos/` — защита от отправки произвольного файла.
- Транскодер срабатывает только если mp4 > 50 MB (лимит Telegram Bot API); в этом случае целится в 47.5 MB с `libx264 -preset ultrafast`. yt-dlp предпочитает 1080p пока размер < 45 MB — короткие ролики приходят в оригинальном качестве без пересжатия.
- Параллельный пайплайн: `download_lock` (1 параллельно — ffmpeg CPU-heavy) + `caption_semaphore(2)` (API IO) + send без блокировок. Статус-файл per-request (`/tmp/va_status_<hash>.txt`), чтобы параллельные задачи не затирали стадии друг друга.

## Лицензия

MIT — см. `LICENSE`.
