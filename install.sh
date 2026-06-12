#!/bin/bash
set -e

echo ""
echo "======================================"
echo "  文字起こしツール セットアップ"
echo "======================================"
echo ""

TOOL_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Step 1: Xcode コマンドラインツール ---
echo "▶ Xcode コマンドラインツールを確認中..."
if ! xcode-select -p &>/dev/null; then
    echo "  インストールします（ポップアップが表示されたら「インストール」をクリック）..."
    xcode-select --install
    echo "  インストール完了を待っています..."
    until xcode-select -p &>/dev/null; do
        sleep 5
    done
fi
echo "  Xcode コマンドラインツール: OK"

# --- Step 2: Homebrew ---
echo "▶ Homebrew を確認中..."
if ! command -v brew &>/dev/null; then
    echo "  インストールします（数分かかります）..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Apple Silicon のパスを通す
    if [ -f /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
        echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> "$HOME/.zprofile"
    fi
fi
echo "  Homebrew: OK"

# --- Step 3: ffmpeg ---
echo "▶ ffmpeg を確認中..."
if ! command -v ffmpeg &>/dev/null; then
    echo "  インストールします..."
    brew install ffmpeg
fi
echo "  ffmpeg: OK"

# --- Step 4: Python ---
echo "▶ Python を確認中..."
PYTHON=""
for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON="$cmd"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "  Python が見つかりません。Homebrew でインストールします..."
    brew install python@3.11
    PYTHON="python3.11"
fi
echo "  Python: $($PYTHON --version) → OK"

# --- Step 5: 仮想環境 ---
VENV_DIR="$HOME/whisper-env"
echo "▶ 仮想環境を準備中..."
if [ ! -d "$VENV_DIR" ]; then
    "$PYTHON" -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip

# --- Step 6: パッケージのインストール ---
echo "▶ パッケージをインストール中..."
echo "  （合計10〜20分かかることがあります。そのままお待ちください）"
echo ""

echo "  [1/7] torch（AI エンジン）..."
pip install --quiet torch

echo "  [2/7] mlx-whisper（文字起こし / Apple Silicon 最適化）..."
pip install --quiet mlx-whisper

echo "  [3/7] pyannote.audio（話者分離 / community-1）..."
pip install --quiet "pyannote.audio"

echo "  [4/7] WebUI（fastapi / uvicorn / python-docx）..."
pip install --quiet fastapi uvicorn python-docx

echo "  [5/7] customtkinter（デスクトップ版の画面・任意）..."
pip install --quiet customtkinter || echo "  ※ customtkinter は省略（WebUI版のみ使うなら不要）"

echo "  [6/7] tkinterdnd2（ドラッグ＆ドロップ・任意）..."
pip install --quiet tkinterdnd2 || echo "  ※ tkinterdnd2 は省略（なくても動作します）"

echo "  [7/7] psutil（メモリ自動判定）..."
pip install --quiet psutil || echo "  ※ psutil は省略（軽量モードの自動判定だけ無効）"

echo ""
echo "  パッケージ: OK"

# --- Step 7: HuggingFace トークン ---
echo ""
echo "======================================"
echo "  HuggingFace トークンの設定"
echo "======================================"
echo ""
echo "  手順書の STEP 2〜4 を完了してからトークンを貼り付けてください"
echo "  （アカウント作成 → 利用規約同意 → トークン発行）"
echo ""

HF_TOKEN_PATH="$HOME/.huggingface/token"
if [ -f "$HF_TOKEN_PATH" ] && [ -s "$HF_TOKEN_PATH" ]; then
    echo "  既存のトークン: $(cat "$HF_TOKEN_PATH" | head -c 10)..."
    read -r -p "  上書きしますか？ [y/N]: " overwrite
    if [[ "$overwrite" =~ ^[Yy]$ ]]; then
        read -r -p "  トークンを貼り付けてください (hf_...): " hf_token
        mkdir -p "$HOME/.huggingface"
        printf '%s' "$hf_token" > "$HF_TOKEN_PATH"
        echo "  保存しました ✓"
    fi
else
    read -r -p "  トークンを貼り付けてください (hf_...): " hf_token
    mkdir -p "$HOME/.huggingface"
    printf '%s' "$hf_token" > "$HF_TOKEN_PATH"
    echo "  保存しました ✓"
fi

# --- Step 8: ランチャー作成 ---
echo ""
echo "▶ 起動ファイルを作成中..."
LAUNCHER="$HOME/Desktop/文字起こし.command"
cat > "$LAUNCHER" << EOF
#!/bin/bash
source "$VENV_DIR/bin/activate"
cd "$TOOL_DIR"
# WebUI版を起動（自動でブラウザ http://localhost:8080 が開きます）
python server.py
EOF
chmod +x "$LAUNCHER"

# Gatekeeper 対策（隔離フラグを外す）
xattr -d com.apple.quarantine "$LAUNCHER" 2>/dev/null || true

echo "  デスクトップに「文字起こし.command」を作成しました ✓"

# --- 完了 ---
echo ""
echo "======================================"
echo "  セットアップ完了！"
echo "======================================"
echo ""
echo "  デスクトップの「文字起こし.command」をダブルクリックして起動してください"
echo "  → 自動でブラウザ（http://localhost:8080）が開き、WebUI が表示されます"
echo "  ※ 黒いターミナル画面は閉じないでください（閉じるとツールが止まります）"
echo ""
echo "  ⚠️  初回起動時のみ、AIモデルのダウンロードが自動で始まります"
echo "     （文字起こし large-v3-turbo 約800MB ＋ 話者分離 community-1。初回は5〜10分）"
echo "     完了すると WebUI が使えるようになります"
echo "     ※ モデルは ~/.cache/huggingface/hub にキャッシュされます"
echo ""
