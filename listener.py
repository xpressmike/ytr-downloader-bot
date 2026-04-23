import asyncio
import json
import logging
import os
import re
import time
import traceback
import uuid
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).parent.resolve()
CAPTION_TEMPLATE_FILE = PROJECT_DIR / "CLAUDE.md"
LOG_FILE = PROJECT_DIR / "listener.log"
URL_RE = re.compile(r"https?://\S+")

DOWNLOAD_TIMEOUT = 900      # 15 min — yt-dlp + whisper + ffmpeg transcode
CAPTION_TIMEOUT = 60        # 1 min — Haiku text-only, typically <10 sec
SEND_TIMEOUT = 360          # 6 min — Telegram upload
PROGRESS_POLL_SEC = 2

# Concurrency: download is CPU-heavy (ffmpeg transcode) — serialize.
# Caption is a network call to Anthropic — allow a couple in parallel.
# Send is pure IO to Telegram — no lock.
MAX_PARALLEL_CAPTIONS = 2

AUDIO_PREFIXES = ("audio", "mp3", "аудио", "звук")

ALLOWED_HOST_SUFFIXES = (
    ".youtube.com", ".youtu.be",
    ".instagram.com",
    ".tiktok.com",
    ".twitter.com", ".x.com",
)
ALLOWED_EXACT_HOSTS = {
    "youtube.com", "youtu.be",
    "instagram.com",
    "tiktok.com",
    "twitter.com", "x.com",
}

STAGE_LABELS = {
    "DOWNLOADING": "📥 Скачиваю…",
    "TRANSCRIBING": "🎙️ Расшифровываю речь…",
    "TRANSCODING": "🎬 Сжимаю под 50 MB (это может занять несколько минут)…",
    "CAPTIONING": "✍️ Пишу подпись…",
    "UPLOADING": "📤 Отправляю в Telegram…",
}

load_dotenv(PROJECT_DIR / ".env")
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
_raw_ids = os.environ["TELEGRAM_ALLOWED_CHAT_IDS"]
ALLOWED_CHAT_IDS = {int(x.strip()) for x in _raw_ids.split(",") if x.strip()}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE),
    ],
)
log = logging.getLogger("listener")

CLAUDE_ENV_BLOCKLIST = frozenset({"TELEGRAM_BOT_TOKEN"})

dp = Dispatcher()
download_lock = asyncio.Lock()
caption_semaphore = asyncio.Semaphore(MAX_PARALLEL_CAPTIONS)


def is_allowed_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    if not host:
        return False
    if host in ALLOWED_EXACT_HOSTS:
        return True
    return any(host.endswith(s) for s in ALLOWED_HOST_SUFFIXES)


def parse_audio_mode(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    first_word = stripped.split(None, 1)[0].lower().rstrip(":,.!?")
    return first_word in AUDIO_PREFIXES


def redact(text: str) -> str:
    return text.replace(TOKEN, "***REDACTED***") if TOKEN else text


async def track_progress(status_msg: Message, status_file: Path) -> None:
    last_stage = None
    try:
        while True:
            await asyncio.sleep(PROGRESS_POLL_SEC)
            try:
                raw = status_file.read_text().strip()
            except (OSError, FileNotFoundError):
                continue
            if raw and raw != last_stage and raw in STAGE_LABELS:
                try:
                    await status_msg.edit_text(f"⏳ {STAGE_LABELS[raw]}")
                except Exception as e:
                    log.debug("progress edit_text failed: %s", e)
                last_stage = raw
    except asyncio.CancelledError:
        return


def set_status(status_file: Path, s: str) -> None:
    try:
        status_file.write_text(s)
    except OSError:
        pass


def job_env(status_file: Path, *, strip_claude_secrets: bool = False) -> dict[str, str]:
    """Env for subprocess — injects JOB_STATUS_FILE so each job writes its own progress."""
    base = os.environ if not strip_claude_secrets else {
        k: v for k, v in os.environ.items() if k not in CLAUDE_ENV_BLOCKLIST
    }
    return {**base, "JOB_STATUS_FILE": str(status_file)}


async def _run_subprocess(
    cmd: list[str],
    timeout: int,
    *,
    env: dict[str, str] | None = None,
) -> tuple[bytes, bytes]:
    """Run a subprocess with timeout. Raises RuntimeError on non-zero exit or timeout."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(PROJECT_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"timeout ({timeout}s) for: {cmd[0]}")
    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"{cmd[0]} exited {proc.returncode}: {redact(err)}")
    return stdout, stderr


async def run_download(url: str, audio_mode: bool, status_file: Path) -> dict:
    """Call ./scripts/download.sh, return parsed JSON metadata."""
    cmd = ["./scripts/download.sh"]
    if audio_mode:
        cmd.append("--audio")
    cmd.append(url)
    log.info("download.sh for %s (audio=%s)", url, audio_mode)
    stdout, _ = await _run_subprocess(
        cmd, DOWNLOAD_TIMEOUT, env=job_env(status_file)
    )
    try:
        return json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"download.sh returned non-JSON: {e}")


async def generate_caption(meta: dict, status_file: Path) -> str:
    """Call claude -p to produce HTML caption text. Returns plain string."""
    try:
        template = CAPTION_TEMPLATE_FILE.read_text(encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"cannot read {CAPTION_TEMPLATE_FILE.name}: {e}")

    meta_for_prompt = {
        k: v for k, v in meta.items()
        if k in ("title", "uploader", "duration", "original_url",
                 "description", "transcript")
    }
    prompt = (
        template
        + "\n\n---\n\n"
        + "Сгенерируй готовую подпись по шаблону выше.\n\n"
        + "**ВАЖНО (обязательно соблюдать):**\n"
        + "- Верни ТОЛЬКО сам текст подписи — без пояснений, без markdown-блоков, "
        + "без «вот подпись:», без тройных кавычек. Первая строка вывода = первая строка подписи.\n"
        + "- НИКОГДА не отказывайся и не задавай уточняющих вопросов. "
        + "Всегда выводи готовую подпись с тем, что есть.\n"
        + "- Пустой `transcript` — нормально (танцы/музыка без речи). В этом случае "
        + "бери смысл из `title` и `description` (там часто хэштеги и краткое описание), "
        + "и/или из `uploader`. Придумай title и summary опираясь на эти поля.\n"
        + "- Если и title, и description, и transcript пустые/мусорные — всё равно выведи "
        + "подпись: summary напиши в стиле «короткое видео от {uploader}, {duration_formatted}», "
        + "теги возьми тематические по платформе (#tiktok, #reels, #shorts и т.п.).\n\n"
        + "Данные:\n"
        + "```json\n"
        + json.dumps(meta_for_prompt, ensure_ascii=False, indent=2)
        + "\n```"
    )
    cmd = [
        "claude",
        "-p", prompt,
        "--permission-mode", "default",
        "--model", "claude-haiku-4-5-20251001",
    ]
    set_status(status_file, "CAPTIONING")
    log.info("claude caption for %s", meta.get("original_url", "?"))
    stdout, _ = await _run_subprocess(
        cmd, CAPTION_TIMEOUT, env=job_env(status_file, strip_claude_secrets=True)
    )
    caption = stdout.decode("utf-8", errors="replace").strip()
    if not caption:
        raise RuntimeError("empty caption from claude")
    return caption


async def send_to_telegram(
    chat_id: int, caption: str, file_paths: list[str], status_file: Path
) -> None:
    """Write caption to a temp file in project storage, call send_telegram.py."""
    storage = PROJECT_DIR / "storage"
    storage.mkdir(exist_ok=True)
    caption_file = storage / f".caption_{chat_id}_{uuid.uuid4().hex[:8]}.txt"
    caption_file.write_text(caption, encoding="utf-8")
    try:
        cmd = [
            ".venv/bin/python", "scripts/send_telegram.py",
            "--chat-id", str(chat_id),
            "--caption-file", str(caption_file),
            *file_paths,
        ]
        log.info("send_telegram for chat_id=%s files=%s", chat_id, file_paths)
        await _run_subprocess(cmd, SEND_TIMEOUT, env=job_env(status_file))
    finally:
        caption_file.unlink(missing_ok=True)


async def run_pipeline(
    url: str, chat_id: int, audio_mode: bool, status_file: Path,
) -> str | None:
    """Download → caption → send. Stages use separate locks so batches overlap:
    only download serialized (CPU-heavy), caption semaphore-limited, send free.
    Returns HTML error message on failure, else None.
    """
    try:
        async with download_lock:
            meta = await run_download(url, audio_mode, status_file)
    except RuntimeError as e:
        log.error("download failed for %s: %s", url, e)
        return f"Не удалось скачать:\n<pre>{escape(str(e))[:400]}</pre>"

    try:
        async with caption_semaphore:
            caption = await generate_caption(meta, status_file)
    except RuntimeError as e:
        log.error("caption failed for %s: %s", url, e)
        return f"Не удалось сгенерировать подпись:\n<pre>{escape(str(e))[:400]}</pre>"

    if "file_paths" in meta and isinstance(meta["file_paths"], list):
        file_paths = [str(p) for p in meta["file_paths"]]
    elif "file_path" in meta:
        file_paths = [str(meta["file_path"])]
    else:
        log.error("no file_path(s) in download.sh output: %s", meta)
        return "Не удалось скачать: download.sh не вернул путь к файлу"

    try:
        await send_to_telegram(chat_id, caption, file_paths, status_file)
    except RuntimeError as e:
        log.error("send failed for %s: %s", url, e)
        return f"Не удалось отправить в Telegram:\n<pre>{escape(str(e))[:400]}</pre>"

    log.info("pipeline ok for %s", url)
    return None


@dp.message(F.text)
async def handle_message(message: Message) -> None:
    if message.chat.id not in ALLOWED_CHAT_IDS:
        u = message.from_user
        log.warning(
            "Rejected message from chat_id=%s, user_id=%s, username=@%s, name=%s",
            message.chat.id,
            u.id if u else "-",
            u.username if u else "-",
            u.full_name if u else "-",
        )
        await message.reply(
            "🚫 Вам не доступен бот, обратитесь в СПОРТЛОТО\n\n"
            f"<i>Ваш chat_id: <code>{message.chat.id}</code></i>",
            parse_mode="HTML",
        )
        return

    text = message.text or ""
    m = URL_RE.search(text)
    if not m:
        await message.answer(
            "Отправь ссылку на видео (YouTube / Instagram / TikTok / Twitter/X).\n"
            "Можно добавить префикс <b>audio</b> (или <b>mp3</b>) — пришлю только звук.",
            parse_mode="HTML",
        )
        return

    url = m.group(0)
    if not is_allowed_url(url):
        log.info("rejected url (domain not in whitelist): %s", url)
        await message.answer(
            "❌ Этот домен не поддерживается.\n"
            "Поддерживаются: YouTube, Instagram, TikTok, Twitter/X."
        )
        return

    audio_mode = parse_audio_mode(text)
    log.info("accepted url=%s audio_mode=%s", url, audio_mode)

    initial_label = "⏳ Принял, обрабатываю…" + (" (режим аудио)" if audio_mode else "")
    status_msg = await message.answer(initial_label)

    status_file = Path(f"/tmp/va_status_{uuid.uuid4().hex[:8]}.txt")
    try:
        status_file.write_text("")
    except OSError:
        pass

    progress_task = asyncio.create_task(track_progress(status_msg, status_file))
    error: str | None = None
    try:
        error = await run_pipeline(url, message.chat.id, audio_mode, status_file)
    except Exception as e:
        log.error("unhandled error for url=%s:\n%s", url, traceback.format_exc())
        error = f"Внутренняя ошибка: {escape(str(e))[:300]}"
    finally:
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass
        try:
            status_file.unlink(missing_ok=True)
        except OSError:
            pass

    try:
        if error:
            await status_msg.edit_text(f"❌ {error}", parse_mode="HTML")
        else:
            await status_msg.edit_text("✅ Готово")
    except Exception as e:
        log.warning("failed to update final status: %s", e)


async def main() -> None:
    bot = Bot(token=TOKEN)
    log.info(
        "Listener started, allowed chat_ids: %s, timeouts: download=%ds caption=%ds send=%ds",
        sorted(ALLOWED_CHAT_IDS),
        DOWNLOAD_TIMEOUT, CAPTION_TIMEOUT, SEND_TIMEOUT,
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
