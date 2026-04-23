#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

CAPTION_LIMIT = 1024
TIMEOUT = 300
MAX_SIZE_BYTES = 50 * 1024 * 1024
MAX_429_RETRIES = 3
STATUS_FILE = Path(os.getenv("JOB_STATUS_FILE", "/tmp/va_status.txt"))
STORAGE_DIR = (Path(__file__).resolve().parent.parent / "storage" / "videos").resolve()

AUDIO_EXTENSIONS = (".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wav", ".aac")


def die(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(1)


def set_status(s: str) -> None:
    try:
        STATUS_FILE.write_text(s)
    except OSError:
        pass


def detect_method(path: Path) -> tuple[str, str, str]:
    if path.suffix.lower() in AUDIO_EXTENSIONS:
        return "sendAudio", "audio", "audio/mpeg"
    return "sendVideo", "video", "video/mp4"


def safe_truncate_caption(caption: str, limit: int) -> str:
    """Trim caption to <= limit, trying not to cut inside an HTML tag."""
    if len(caption) <= limit:
        return caption
    cut = caption[: limit - 1]
    for sep in ("</a>", "</pre>", "</code>", "</b>", "</i>", "\n", " "):
        idx = cut.rfind(sep)
        if idx > limit // 2:
            return cut[: idx + len(sep)] + "…"
    return cut + "…"


def upload_with_retries(
    client: httpx.Client,
    url: str,
    chat_id: int,
    caption: str,
    fp: Path,
    field: str,
    content_type: str,
) -> dict:
    """
    Upload file to Telegram. Handles:
      - Telegram 429 with Retry-After (up to MAX_429_RETRIES)
      - HTML parse_mode fallback on 'can't parse entities'
    Returns parsed response dict on success; calls die() on terminal failure.
    """
    parse_mode: str | None = "HTML"
    html_retry_used = False

    for attempt in range(MAX_429_RETRIES + 1):
        data_payload = {"chat_id": chat_id, "caption": caption}
        if parse_mode:
            data_payload["parse_mode"] = parse_mode

        try:
            with fp.open("rb") as f:
                resp = client.post(
                    url,
                    data=data_payload,
                    files={field: (fp.name, f, content_type)},
                )
        except httpx.RequestError as e:
            die(f"request failed for {fp.name}: {e}")

        try:
            data = resp.json()
        except ValueError:
            die(
                f"non-JSON response (status {resp.status_code}) for {fp.name}: "
                f"{resp.text[:500]}"
            )

        if data.get("ok"):
            return data

        # 429: honor Retry-After
        if data.get("error_code") == 429 and attempt < MAX_429_RETRIES:
            ra = int(data.get("parameters", {}).get("retry_after", 1))
            print(
                f"429 for {fp.name}, sleeping {ra}s "
                f"(attempt {attempt + 1}/{MAX_429_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(ra)
            continue

        # HTML parse error: one-shot fallback to plain text
        desc = str(data.get("description", "")).lower()
        if parse_mode and "can't parse" in desc and not html_retry_used:
            print(
                f"HTML parse failed for {fp.name}, retrying plain: "
                f"{data.get('description')}",
                file=sys.stderr,
            )
            parse_mode = None
            html_retry_used = True
            continue

        die(f"telegram error for {fp.name}: {data}")

    die(f"exhausted retries for {fp.name}")
    return {}  # unreachable


load_dotenv(Path(__file__).resolve().parent.parent / ".env")
token = os.getenv("TELEGRAM_BOT_TOKEN")
if not token:
    die("TELEGRAM_BOT_TOKEN must be set in .env")
raw_allowed = os.environ["TELEGRAM_ALLOWED_CHAT_IDS"]
ALLOWED_CHAT_IDS = {int(x.strip()) for x in raw_allowed.split(",") if x.strip()}

# Two input modes: CLI args OR stdin JSON (backward-compat).
# CLI is preferred — avoids the "brace + quote" bash sandbox heuristic that
# blocks `echo '{"...":"..."}' | send_telegram.py` style heredocs.
if len(sys.argv) > 1:
    parser = argparse.ArgumentParser(
        description="Send video/audio/album to Telegram.",
    )
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument(
        "--caption-file",
        type=Path,
        required=True,
        help="Path to a plain text file with the HTML caption.",
    )
    parser.add_argument(
        "files",
        nargs="+",
        type=Path,
        help="One file → sendVideo/sendAudio; many files → album.",
    )
    args = parser.parse_args()
    chat_id = args.chat_id
    try:
        caption = args.caption_file.read_text(encoding="utf-8")
    except OSError as e:
        die(f"cannot read caption file: {e}")
    file_paths = list(args.files)
else:
    try:
        payload = json.load(sys.stdin)
        caption = str(payload.get("caption", ""))
        chat_id = int(payload["chat_id"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        die(f"invalid stdin JSON: {e}")

    if "file_paths" in payload:
        raw_paths = payload["file_paths"]
        if not isinstance(raw_paths, list) or not raw_paths:
            die("file_paths must be a non-empty list")
        file_paths = [Path(p) for p in raw_paths]
    elif "file_path" in payload:
        file_paths = [Path(payload["file_path"])]
    else:
        die("missing file_path or file_paths in payload")

if chat_id not in ALLOWED_CHAT_IDS:
    die(f"chat_id {chat_id} not in TELEGRAM_ALLOWED_CHAT_IDS")

for fp in file_paths:
    resolved = fp.resolve()
    if not resolved.is_relative_to(STORAGE_DIR):
        die(f"file_path outside storage dir: {fp}")
    if not resolved.is_file():
        die(f"file not found: {fp}")
    sz = resolved.stat().st_size
    if sz > MAX_SIZE_BYTES:
        die(
            f"file exceeds Telegram 50MB limit: {sz} bytes ({fp.name}). "
            f"download.sh must prepare it."
        )

caption = safe_truncate_caption(caption, CAPTION_LIMIT)

set_status("UPLOADING")

message_ids: list[int] = []
sent_paths: list[Path] = []

try:
    with httpx.Client(http2=True, timeout=TIMEOUT) as client:
        for idx, fp in enumerate(file_paths):
            method, field, content_type = detect_method(fp)
            url = f"https://api.telegram.org/bot{token}/{method}"
            this_caption = caption if idx == 0 else ""

            data = upload_with_retries(
                client, url, chat_id, this_caption, fp, field, content_type
            )

            try:
                message_ids.append(data["result"]["message_id"])
            except KeyError:
                die(f"unexpected response for {fp.name}: {data}")
            sent_paths.append(fp)
finally:
    # Only clean up files that were successfully sent.
    # Partial failure leaves orphans for diagnosis.
    for fp in sent_paths:
        fp.unlink(missing_ok=True)
        fp.with_suffix(".info.json").unlink(missing_ok=True)

json.dump({"ok": True, "message_ids": message_ids}, sys.stdout)
sys.stdout.write("\n")
