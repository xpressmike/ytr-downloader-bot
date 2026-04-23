#!/usr/bin/env bash
set -euo pipefail

AUDIO_ONLY=0
if [[ "${1:-}" == "--audio" ]]; then
    AUDIO_ONLY=1
    shift
fi

URL="${1:?usage: download.sh [--audio] <url>}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

VENV_BIN="$PROJECT_ROOT/.venv/bin"
STORAGE="$PROJECT_ROOT/storage/videos"
COOKIES_FILE="$PROJECT_ROOT/cookies.txt"
STATUS_FILE="${JOB_STATUS_FILE:-/tmp/va_status.txt}"
mkdir -p "$STORAGE"

MAX_BYTES=52428800       # 50 MB — Telegram Bot API hard limit
TARGET_BYTES=49807360    # 47.5 MB — transcode target with 5% headroom

ID="$(date +%Y%m%d_%H%M%S_%N)"

set_status() {
    printf '%s' "$1" > "$STATUS_FILE" 2>/dev/null || true
}

IS_ALBUM=0
if [[ $AUDIO_ONLY -eq 0 && "$URL" =~ instagram\.com/p/ ]]; then
    IS_ALBUM=1
fi

COOKIES_RO=""
COMPLETED=0
cleanup() {
    local exit_code=$?
    [[ -n "$COOKIES_RO" ]] && rm -f "$COOKIES_RO"
    if [[ $exit_code -ne 0 && $COMPLETED -eq 0 ]]; then
        rm -f "$STORAGE/${ID}".* "$STORAGE/${ID}_"*
    fi
}
trap cleanup EXIT

YTDLP_COOKIES=()
if [[ -f "$COOKIES_FILE" ]]; then
    COOKIES_RO="$(mktemp)"
    cp "$COOKIES_FILE" "$COOKIES_RO"
    YTDLP_COOKIES=(--cookies "$COOKIES_RO")
fi

set_status DOWNLOADING

# =========================================================================
# Audio-only mode
# =========================================================================
if [[ $AUDIO_ONLY -eq 1 ]]; then
    AUDIO_FILE="$STORAGE/${ID}.mp3"
    INFO_JSON="$STORAGE/${ID}.info.json"

    if ! "$VENV_BIN/yt-dlp" \
            "${YTDLP_COOKIES[@]}" \
            --js-runtimes node \
            --remote-components ejs:github \
            --no-playlist \
            --no-check-certificates \
            -f "bestaudio/best" \
            --extract-audio --audio-format mp3 --audio-quality 0 \
            --write-info-json \
            -o "$STORAGE/${ID}.%(ext)s" \
            "$URL" >&2; then
        echo "yt-dlp failed for URL: $URL" >&2
        exit 1
    fi

    audio_size=$(stat -c %s "$AUDIO_FILE")
    if [[ $audio_size -gt $MAX_BYTES ]]; then
        echo "ERROR: audio file exceeds 50MB: $audio_size bytes" >&2
        exit 1
    fi

    jq -n \
        --arg file_path "$AUDIO_FILE" \
        --arg original_url "$URL" \
        --arg kind "audio" \
        --slurpfile info "$INFO_JSON" \
        '{
            kind: $kind,
            file_path: $file_path,
            title: $info[0].title,
            uploader: $info[0].uploader,
            duration: $info[0].duration,
            description: ($info[0].description // ""),
            original_url: $original_url,
            transcript: ""
        }'

    COMPLETED=1
    exit 0
fi

# =========================================================================
# Video/album flow
# =========================================================================
if [[ $IS_ALBUM -eq 1 ]]; then
    OUTPUT_TPL="$STORAGE/${ID}_%(playlist_index)03d.%(ext)s"
    PLAYLIST_FLAGS=(--yes-playlist)
else
    OUTPUT_TPL="$STORAGE/${ID}.%(ext)s"
    PLAYLIST_FLAGS=(--no-playlist)
fi

if ! "$VENV_BIN/yt-dlp" \
        "${YTDLP_COOKIES[@]}" \
        --js-runtimes node \
        --remote-components ejs:github \
        --concurrent-fragments 4 \
        "${PLAYLIST_FLAGS[@]}" \
        --no-check-certificates \
        --no-write-subs \
        --no-embed-subs \
        -f "bv*[height<=1080][filesize<45M]+ba/b[height<=1080][filesize<45M]/bv*[height<=1080][filesize_approx<45M]+ba/b[height<=1080][filesize_approx<45M]/bv*[filesize<45M]+ba/b[filesize<45M]/bv*[height<=720]+ba/bv*[height<=720]+ba/b[height<=720]/b" \
        --merge-output-format mp4 \
        --remux-video mp4 \
        --write-info-json \
        -o "$OUTPUT_TPL" \
        "$URL" >&2; then
    echo "yt-dlp failed for URL: $URL" >&2
    exit 1
fi

shopt -s nullglob
if [[ $IS_ALBUM -eq 1 ]]; then
    FILES=("$STORAGE/${ID}_"*.mp4)
else
    FILES=("$STORAGE/${ID}.mp4")
fi
shopt -u nullglob

NUM_FILES=${#FILES[@]}
if [[ $NUM_FILES -eq 0 ]]; then
    echo "ERROR: no files downloaded" >&2
    exit 1
fi

# Single-item album → promote to canonical single-video layout
if [[ $IS_ALBUM -eq 1 && $NUM_FILES -eq 1 ]]; then
    old_mp4="${FILES[0]}"
    old_info="${old_mp4%.mp4}.info.json"
    if [[ ! -f "$old_info" ]]; then
        echo "ERROR: info.json not found for single-item album: $old_info" >&2
        exit 1
    fi
    mv "$old_mp4" "$STORAGE/${ID}.mp4"
    mv "$old_info" "$STORAGE/${ID}.info.json"
    FILES=("$STORAGE/${ID}.mp4")
    IS_ALBUM=0
fi

# =========================================================================
# Album branch: N>1 files, skip transcribe/transcode, emit array
# =========================================================================
if [[ $IS_ALBUM -eq 1 ]]; then
    for f in "${FILES[@]}"; do
        sz=$(stat -c %s "$f")
        if [[ $sz -gt $MAX_BYTES ]]; then
            echo "ERROR: album item exceeds 50MB: $f ($sz bytes)" >&2
            exit 1
        fi
    done

    INFO_JSON=$(ls "$STORAGE/${ID}_"*.info.json 2>/dev/null | head -1 || true)
    if [[ -z "$INFO_JSON" || ! -f "$INFO_JSON" ]]; then
        echo "ERROR: info.json not found for album" >&2
        exit 1
    fi

    files_json=$(printf '%s\n' "${FILES[@]}" | jq -R . | jq -s .)

    jq -n \
        --argjson file_paths "$files_json" \
        --arg original_url "$URL" \
        --arg kind "album" \
        --slurpfile info "$INFO_JSON" \
        '{
            kind: $kind,
            file_paths: $file_paths,
            title: $info[0].title,
            uploader: $info[0].uploader,
            duration: ($info[0].duration // null),
            description: ($info[0].description // ""),
            original_url: $original_url,
            transcript: ""
        }'

    COMPLETED=1
    exit 0
fi

# =========================================================================
# Single-video branch
# =========================================================================
VIDEO_FILE="${FILES[0]}"
INFO_JSON="$STORAGE/${ID}.info.json"
WAV_FILE="$STORAGE/${ID}.wav"
TXT_FILE="$STORAGE/${ID}.txt"

set_status TRANSCRIBING

ffmpeg -y -loglevel error -i "$VIDEO_FILE" -ac 1 -ar 16000 -vn "$WAV_FILE" >&2

"$VENV_BIN/python" "$PROJECT_ROOT/scripts/transcribe_client.py" \
    --audio "$WAV_FILE" \
    --output "$TXT_FILE" >&2

mp4_size=$(stat -c %s "$VIDEO_FILE")
if [[ $mp4_size -gt $MAX_BYTES ]]; then
    set_status TRANSCODING

    duration=$(ffprobe -v error -show_entries format=duration \
        -of default=nw=1:nk=1 "$VIDEO_FILE" || true)
    if ! [[ "$duration" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        echo "ERROR: ffprobe returned non-numeric duration: '$duration'" >&2
        exit 1
    fi
    if (( $(awk -v d="$duration" 'BEGIN { print (d <= 0) }') == 1 )); then
        echo "ERROR: ffprobe duration is not positive: $duration" >&2
        exit 1
    fi

    audio_kbps=64
    total_kbps=$(awk -v t=$TARGET_BYTES -v d="$duration" \
        'BEGIN { printf "%d", t * 8 / d / 1000 }')
    video_kbps=$(( total_kbps - audio_kbps ))
    (( video_kbps < 100 )) && video_kbps=100

    COMPRESSED_MP4="$STORAGE/${ID}.compressed.mp4"
    ffmpeg -y -loglevel error -i "$VIDEO_FILE" \
        -c:v libx264 -preset ultrafast \
        -b:v "${video_kbps}k" -maxrate "${video_kbps}k" \
        -bufsize "$((video_kbps * 2))k" \
        -vf "scale='min(1280,iw)':-2" \
        -c:a aac -b:a "${audio_kbps}k" \
        -movflags +faststart \
        "$COMPRESSED_MP4" >&2
    mv "$COMPRESSED_MP4" "$VIDEO_FILE"

    post_size=$(stat -c %s "$VIDEO_FILE")
    if [[ $post_size -gt $MAX_BYTES ]]; then
        echo "ERROR: transcoded mp4 still exceeds 50MB: $post_size bytes" >&2
        exit 1
    fi
fi

TRANSCRIPT="$(cat "$TXT_FILE")"

jq -n \
    --arg file_path "$VIDEO_FILE" \
    --arg original_url "$URL" \
    --arg kind "video" \
    --arg transcript "$TRANSCRIPT" \
    --slurpfile info "$INFO_JSON" \
    '{
        kind: $kind,
        file_path: $file_path,
        title: $info[0].title,
        uploader: $info[0].uploader,
        duration: $info[0].duration,
        description: ($info[0].description // ""),
        original_url: $original_url,
        transcript: $transcript
    }'

COMPLETED=1
rm -f "$WAV_FILE" "$TXT_FILE"
