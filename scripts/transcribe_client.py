#!/usr/bin/env python3
import argparse
import json
import os
import socket
import sys
from pathlib import Path

SOCKET_PATH = os.getenv("WHISPER_SOCKET", "/tmp/whisper.sock")
TIMEOUT = 300


def main() -> int:
    parser = argparse.ArgumentParser(description="Client for whisper daemon")
    parser.add_argument("--audio", required=True, help="path to input wav file")
    parser.add_argument("--output", required=True, help="path to output txt file")
    parser.add_argument("--model", default=None, help="ignored (daemon owns model choice)")
    args = parser.parse_args()

    audio_path = Path(args.audio).resolve()
    output_path = Path(args.output)

    if not audio_path.is_file():
        print(f"audio file not found: {audio_path}", file=sys.stderr)
        return 1

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(TIMEOUT)
    try:
        sock.connect(SOCKET_PATH)
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout) as e:
        print(f"cannot connect to whisper daemon at {SOCKET_PATH}: {e}", file=sys.stderr)
        return 1

    try:
        req = json.dumps({"audio": str(audio_path)}) + "\n"
        sock.sendall(req.encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)

        buf = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()

    try:
        resp = json.loads(buf.decode("utf-8").strip())
    except json.JSONDecodeError as e:
        print(f"invalid response from daemon: {e}", file=sys.stderr)
        return 1

    if not resp.get("ok"):
        print(f"whisper error: {resp.get('error', 'unknown')}", file=sys.stderr)
        return 1

    output_path.write_text(resp.get("text", ""), encoding="utf-8")

    lang = resp.get("language", "?")
    prob = resp.get("language_probability", 0.0)
    dur = resp.get("duration", 0.0)
    print(f"language={lang} prob={prob:.3f} duration={dur:.2f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
