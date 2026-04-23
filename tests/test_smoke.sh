#!/usr/bin/env bash
# Smoke tests for the video-archive pipeline.
# Run: bash tests/test_smoke.sh
set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$PROJECT_DIR/.venv/bin/python"

PASSED=0
FAILED=0

check() {
    local expected=$1 desc=$2 got=$3
    if [[ $got -eq $expected ]]; then
        echo "  OK: $desc"
        PASSED=$((PASSED + 1))
    else
        echo "FAIL: $desc (expected exit=$expected, got=$got)"
        FAILED=$((FAILED + 1))
    fi
}

echo "== download.sh =="
"$PROJECT_DIR/scripts/download.sh" >/dev/null 2>&1
check 1 "download.sh without URL fails" $?

echo
echo "== transcribe_client.py =="
"$VENV_PY" "$PROJECT_DIR/scripts/transcribe_client.py" --help >/dev/null 2>&1
check 0 "transcribe_client --help" $?

"$VENV_PY" "$PROJECT_DIR/scripts/transcribe_client.py" \
    --audio /nonexistent.wav --output /tmp/_vatest_out.txt >/dev/null 2>&1
check 1 "transcribe_client rejects missing audio" $?

echo
echo "== send_telegram.py =="
echo "not-json" | "$VENV_PY" "$PROJECT_DIR/scripts/send_telegram.py" >/dev/null 2>&1
check 1 "send_telegram rejects invalid JSON" $?

echo '{"file_path":"/nonexistent","chat_id":999,"caption":""}' | \
    "$VENV_PY" "$PROJECT_DIR/scripts/send_telegram.py" >/dev/null 2>&1
check 1 "send_telegram rejects non-whitelisted chat_id" $?

echo '{"file_paths":[],"chat_id":999,"caption":""}' | \
    "$VENV_PY" "$PROJECT_DIR/scripts/send_telegram.py" >/dev/null 2>&1
check 1 "send_telegram rejects empty file_paths" $?

echo
echo "== send_telegram.py safe_truncate_caption =="
"$VENV_PY" -c "
import sys
sys.path.insert(0, '$PROJECT_DIR/scripts')
from send_telegram import safe_truncate_caption
# short string unchanged
assert safe_truncate_caption('hello', 100) == 'hello'
# Clean break at space
long = ('word ' * 500).strip()
result = safe_truncate_caption(long, 100)
assert len(result) <= 100
assert result.endswith('…')
# HTML tag preserved (cut after closing tag)
html = '<b>title</b> ' + ('body ' * 500).strip()
result = safe_truncate_caption(html, 80)
assert len(result) <= 80
assert '<b>' not in result or '</b>' in result, 'open tag without close'
print('safe_truncate_caption assertions passed')
" 2>&1 | tail -1
check 0 "safe_truncate_caption" $?

echo
echo "== listener.py URL whitelist + audio prefix =="
"$VENV_PY" -c "
import sys
sys.path.insert(0, '$PROJECT_DIR')
from listener import is_allowed_url, parse_audio_mode

# Whitelist — new domains
assert is_allowed_url('https://www.youtube.com/shorts/abc')
assert is_allowed_url('https://youtu.be/abc')
assert is_allowed_url('https://secure.instagram.com/reel/abc')
assert is_allowed_url('https://www.tiktok.com/@x/video/1')
assert is_allowed_url('https://twitter.com/u/status/1'), 'twitter'
assert is_allowed_url('https://x.com/u/status/1'), 'x.com'
assert is_allowed_url('https://www.x.com/u/status/1'), 'www.x.com'

# Rejects
assert not is_allowed_url('https://evil.example.com/rce')
assert not is_allowed_url('ftp://youtube.com/x')
assert not is_allowed_url('not-a-url')
assert not is_allowed_url('https://youtube.com.evil.com/bypass')

# Audio prefix parser
assert parse_audio_mode('audio https://youtube.com/abc')
assert parse_audio_mode('mp3 https://youtube.com/abc')
assert parse_audio_mode('аудио https://youtube.com/abc')
assert parse_audio_mode('звук https://youtube.com/abc')
assert parse_audio_mode('AUDIO https://youtube.com/abc')
assert parse_audio_mode('audio: https://youtube.com/abc')
assert not parse_audio_mode('https://youtube.com/abc')
assert not parse_audio_mode('play https://youtube.com/abc')
assert not parse_audio_mode('')

print('all listener assertions passed')
" 2>&1 | tail -1
check 0 "URL whitelist + audio prefix" $?

echo
echo "=============="
echo "Passed: $PASSED, Failed: $FAILED"
[[ $FAILED -eq 0 ]]
