#!/usr/bin/env python3
import json
import logging
import os
import socket
import sys
from pathlib import Path

from faster_whisper import WhisperModel

SOCKET_PATH = os.getenv("WHISPER_SOCKET", "/tmp/whisper.sock")
MODEL_SIZE = os.getenv("WHISPER_MODEL", "tiny")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("whisper-worker")


def transcribe(model, audio_path: str):
    segments, info = model.transcribe(
        audio_path,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        beam_size=1,
        best_of=1,
    )
    parts = [seg.text.strip() for seg in segments]
    text = " ".join(p for p in parts if p)
    return text, info


def read_request(conn: socket.socket) -> bytes:
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def handle(conn: socket.socket, model) -> None:
    try:
        raw = read_request(conn)
        req = json.loads(raw.decode("utf-8").strip())
        audio = req["audio"]
        if not Path(audio).is_file():
            resp = {"ok": False, "error": f"file not found: {audio}"}
        else:
            text, info = transcribe(model, audio)
            resp = {
                "ok": True,
                "text": text,
                "language": info.language,
                "language_probability": info.language_probability,
                "duration": info.duration,
            }
            log.info(
                "transcribed %s lang=%s prob=%.3f duration=%.2fs",
                audio, info.language, info.language_probability, info.duration,
            )
    except Exception as e:
        log.exception("handler failed")
        resp = {"ok": False, "error": str(e)}
    try:
        conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
    except Exception:
        log.exception("failed to send response")
    finally:
        conn.close()


def main() -> int:
    cpu_threads = os.cpu_count() or 1
    log.info("loading whisper model: %s (cpu_threads=%d)", MODEL_SIZE, cpu_threads)
    model = WhisperModel(
        MODEL_SIZE,
        device="cpu",
        compute_type="int8",
        cpu_threads=cpu_threads,
        num_workers=1,
    )
    log.info("model loaded")

    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o660)
    server.listen(1)
    log.info("listening on %s", SOCKET_PATH)

    try:
        while True:
            conn, _ = server.accept()
            handle(conn, model)
    finally:
        server.close()
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
