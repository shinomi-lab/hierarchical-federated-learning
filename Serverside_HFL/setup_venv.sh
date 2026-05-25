#!/usr/bin/env bash
# =============================================================
# setup_venv.sh  –  HFL サーバ用 Python 仮想環境セットアップ
# =============================================================
# 使い方:
#   bash setup_venv.sh          # venv 作成 + パッケージインストール
#   source .venv/bin/activate   # 有効化（以降は python start.py で起動可能）
# =============================================================

set -euo pipefail

VENV_DIR=".venv"
PYTHON=""

# ── Python 3.12 を優先して探す ────────────────────────────────
for candidate in \
    /opt/homebrew/bin/python3.12 \
    /usr/local/bin/python3.12 \
    python3.12 \
    /opt/homebrew/bin/python3 \
    python3; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c "import sys; print(sys.version_info[:2])")
        # (3, 10) 以上なら OK
        if "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.10 以上が見つかりません。Homebrew でインストールしてください:"
    echo "  brew install python@3.12"
    exit 1
fi

echo "使用 Python: $PYTHON ($($PYTHON --version))"

# ── venv 作成 ─────────────────────────────────────────────────
if [ -d "$VENV_DIR" ]; then
    echo "既存の $VENV_DIR を再利用します（新規作成をスキップ）"
else
    echo "仮想環境を作成中: $VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR"
fi

# ── pip アップグレード ────────────────────────────────────────
echo "pip をアップグレード中..."
"$VENV_DIR/bin/pip" install --upgrade pip --quiet

# ── PyTorch（Apple Silicon 対応）────────────────────────────
echo "PyTorch をインストール中（時間がかかる場合があります）..."
ARCH=$(uname -m)
if [ "$ARCH" = "arm64" ]; then
    # Apple Silicon: MPS 対応の通常版
    "$VENV_DIR/bin/pip" install torch torchvision torchaudio --quiet
else
    # Intel Mac / Linux
    "$VENV_DIR/bin/pip" install torch torchvision torchaudio --quiet
fi

# ── その他の依存パッケージ ────────────────────────────────────
echo "依存パッケージをインストール中..."
"$VENV_DIR/bin/pip" install \
    "fastapi>=0.95.0" \
    "uvicorn>=0.22.0" \
    "httpx>=0.24.0" \
    "aiofiles>=23.1.0" \
    "requests>=2.31.0" \
    "psutil>=5.9.0" \
    --quiet

# オプション（エラーになっても続行）
"$VENV_DIR/bin/pip" install \
    "pytest>=7.0.0" \
    "pandas>=2.0.0" \
    --quiet 2>/dev/null || true

# ── 確認 ─────────────────────────────────────────────────────
echo ""
echo "インストール確認中..."
"$VENV_DIR/bin/python" -c "
import uvicorn, fastapi, torch, httpx, aiofiles
print('  uvicorn :', uvicorn.__version__)
print('  fastapi :', fastapi.__version__)
print('  torch   :', torch.__version__)
print('  httpx   :', httpx.__version__)
"

echo ""
echo "======================================================"
echo " セットアップ完了！"
echo "======================================================"
echo ""
echo "  有効化コマンド:"
echo "    source .venv/bin/activate"
echo ""
echo "  サーバ起動:"
echo "    python start.py"
echo ""
echo "  ドライランテスト:"
echo "    python test_runner.py B"
echo ""

# .venv を .gitignore に追加（なければ）
if [ -f ".gitignore" ]; then
    if ! grep -q "^\.venv" .gitignore; then
        echo ".venv/" >> .gitignore
        echo "  .gitignore に .venv/ を追加しました"
    fi
fi
