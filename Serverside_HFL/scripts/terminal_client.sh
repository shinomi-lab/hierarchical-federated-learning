#!/usr/bin/env bash
# bash版: エッジサーバへ端末ペイロードを送信（リトライあり）
# 使い方: ./scripts/terminal_client.sh -u http://127.0.0.1:8001 -t sim-term-01 -r 1 [-f /path/to/payload.bin]
#
# ※ 元のPowerShell版 terminal_client.ps1 をbashに変換したもの

set -euo pipefail

EDGE_URL="http://127.0.0.1:8001"
TERMINAL_ID=""
ROUND=""
FILE=""
RUN_ID=""
LOCAL_SEQ=""
ATTEMPTS=5

usage() {
    echo "Usage: $0 -t <terminal_id> -r <round> [-u <edge_url>] [-f <file>] [-i <run_id>] [-l <local_seq>] [-n <attempts>]"
    exit 2
}

while getopts "u:t:r:f:i:l:n:h" opt; do
    case $opt in
        u) EDGE_URL="$OPTARG" ;;
        t) TERMINAL_ID="$OPTARG" ;;
        r) ROUND="$OPTARG" ;;
        f) FILE="$OPTARG" ;;
        i) RUN_ID="$OPTARG" ;;
        l) LOCAL_SEQ="$OPTARG" ;;
        n) ATTEMPTS="$OPTARG" ;;
        h) usage ;;
        *) usage ;;
    esac
done

if [[ -z "$TERMINAL_ID" || -z "$ROUND" ]]; then
    echo "Error: -t (TerminalId) and -r (Round) are required."
    usage
fi

# ペイロード準備
TMP_FILE=""
if [[ -n "$FILE" ]]; then
    PAYLOAD_PATH="$FILE"
else
    TMP_FILE=$(mktemp)
    date > "$TMP_FILE"
    PAYLOAD_PATH="$TMP_FILE"
fi

# SHA-256 計算
SHAHEX=$(shasum -a 256 "$PAYLOAD_PATH" | awk '{print $1}')
CONTENT_SIG="sha256:${SHAHEX}"

URL="${EDGE_URL%/}/receive_terminal_weights/${TERMINAL_ID}"

attempt=0
while (( attempt < ATTEMPTS )); do
    attempt=$(( attempt + 1 ))
    echo "Attempt ${attempt}/${ATTEMPTS} -> POST ${URL}"

    # フォームフィールドを構築
    FORM_ARGS=(
        -F "round_id=${ROUND}"
        -F "base_hash=${CONTENT_SIG}"
        -F "n_samples=1"
        -F "payload_kind=full"
        -F "dtype=f32_flat"
        -F "content_sha256=${CONTENT_SIG}"
    )
    [[ -n "$RUN_ID"    ]] && FORM_ARGS+=(-F "run_id=${RUN_ID}")
    [[ -n "$LOCAL_SEQ" ]] && FORM_ARGS+=(-F "local_seq=${LOCAL_SEQ}")
    FORM_ARGS+=(-F "weights=@${PAYLOAD_PATH};type=application/octet-stream")

    if RESP=$(curl -sf --max-time 120 -X POST "${URL}" "${FORM_ARGS[@]}"); then
        echo "Response: ${RESP}"
        STATUS=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',''))" 2>/dev/null || true)
        ACK=$(echo "$RESP"    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('ack',''))"    2>/dev/null || true)
        if [[ "$ACK" == "True" || "$ACK" == "true" || "$STATUS" == "duplicate_ignored" ]]; then
            echo "Upload acknowledged."
            break
        else
            echo "Warning: no ack in response"
        fi
    else
        echo "Warning: upload attempt ${attempt} failed (curl exit $?)"
    fi

    if (( attempt >= ATTEMPTS )); then
        echo "Error: all ${ATTEMPTS} attempts failed." >&2
        [[ -n "$TMP_FILE" ]] && rm -f "$TMP_FILE"
        exit 1
    fi

    # 指数バックオフ (jitter付き)
    DELAY=$(python3 -c "import random,math; d=min(300,2*2**(${attempt}-1)); print(max(0.5, d*(1+random.uniform(-0.3,0.3))))")
    echo "Retrying in ${DELAY}s..."
    sleep "$DELAY"
done

[[ -n "$TMP_FILE" ]] && rm -f "$TMP_FILE"
echo "done"
