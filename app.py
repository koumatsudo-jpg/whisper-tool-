import customtkinter as ctk
import threading
import concurrent.futures
import os
import json
import subprocess
import tempfile
import urllib.error
import urllib.request
from tkinter import filedialog, messagebox
from datetime import datetime
import traceback
import time

# D&D対応（tkinterdnd2がなくてもクリック選択で動作する）
HAS_DND = False
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    pass

# --- アプリ設定 ---
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.json")
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

DEFAULT_SETTINGS = {
    "summary_enabled": False,
    "backend": "ollama",
    "model": "qwen2.5:14b",
    "api_key": "",
    "output_format": "txt",   # "txt" | "md" | "docx"
    "output_dir": "",         # "" = 入力ファイルと同じフォルダ内の「文字起こし」サブフォルダ
}

BACKEND_DEFAULT_MODELS = {
    "ollama": "qwen2.5:14b",
    "claude": "claude-sonnet-4-6",
    "openai": "gpt-4.1-mini",
}

# 文字起こし／話者分離パイプラインに直接渡せるのは WAV のみ。
# 動画はもちろん、.m4a / .mp3 / .flac 等の音声コンテナも pyannote が読めずフリーズすることがあるため、
# .wav 以外はすべて ffmpeg で 16kHz mono WAV に統一変換してから渡す。
WAV_EXT = ".wav"

# モデルサイズ → mlx-community HuggingFace リポジトリ
MLX_REPO_MAP = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "distil-large-v3": "mlx-community/distil-whisper-large-v3",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}

# モデル選択の表示ラベル ↔ 内部名（非エンジニア向けに日本語の説明を付ける）
MODEL_DISPLAY_CHOICES = [
    ("最速・精度低（tiny）", "tiny"),
    ("高速・精度低め（base）", "base"),
    ("バランス型（small）", "small"),
    ("精度高め・遅い（medium）", "medium"),
    ("高精度・速め（distil-large-v3）", "distil-large-v3"),
    ("最高精度・推奨（large-v3-turbo）", "large-v3-turbo"),
]
MODEL_LABEL_TO_NAME = {label: name for label, name in MODEL_DISPLAY_CHOICES}
MODEL_NAME_TO_LABEL = {name: label for label, name in MODEL_DISPLAY_CHOICES}


class CancelledError(Exception):
    """ユーザーによる処理キャンセル"""


def detect_low_memory_default():
    """物理メモリ 16GB 以下の Mac を自動で「軽量モード」のデフォルト ON にする"""
    try:
        import psutil
        total_gb = psutil.virtual_memory().total / (1024 ** 3)
        return total_gb <= 16
    except ImportError:
        # psutil が無い場合は OFF（従来どおり）で起動する
        return False


def recommended_model_for_memory(low_memory):
    """メモリ状況に応じた推奨デフォルトモデル"""
    return "small" if low_memory else "large-v3-turbo"


def load_history():
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_history(history):
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def load_settings():
    settings = DEFAULT_SETTINGS.copy()
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                settings.update({k: loaded.get(k, v) for k, v in DEFAULT_SETTINGS.items()})
        except Exception:
            pass
    backend = settings.get("backend", "ollama")
    if backend not in BACKEND_DEFAULT_MODELS:
        backend = "ollama"
    settings["backend"] = backend
    if not settings.get("model"):
        settings["model"] = BACKEND_DEFAULT_MODELS[backend]
    settings["summary_enabled"] = bool(settings.get("summary_enabled"))
    settings["api_key"] = str(settings.get("api_key") or "")
    return settings


def save_settings(settings):
    normalized = DEFAULT_SETTINGS.copy()
    normalized.update({k: settings.get(k, v) for k, v in DEFAULT_SETTINGS.items()})

    def opener(path, flags):
        return os.open(path, flags, 0o600)

    with open(SETTINGS_PATH, "w", encoding="utf-8", opener=opener) as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except Exception:
        pass


def _json_post(url, payload, headers=None, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"APIエラー: HTTP {e.code}\n{detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(str(e.reason)) from e
    return json.loads(body)


def _summary_prompt(transcript):
    return (
        "以下の会議・会話・商談の文字起こしを日本語で要約してください。\n\n"
        "厳守事項:\n"
        "- 文字起こしに書かれている事実だけを使うこと。本文に無い人物名・会議・タスク・担当者・期限・金額を勝手に作らない。\n"
        "- 推測・憶測・補完をしない。情報が無い項目は『記載なし』と書く。\n"
        "- 担当・期限・金額・条件は、本文に明記がある場合のみ書く。\n"
        "- 決定事項には、本文中で明確に合意・決定された事項のみを書く。提案中・検討中・交渉中の事項は『論点・未決事項』に書く。\n"
        "- 出力はすべて日本語で書く。中国語の簡体字（例: 导・时・报）を使わない。\n\n"
        "出力形式（Markdown見出し）:\n"
        "## 概要（2〜3文）\n"
        "## 決定事項（箇条書き）\n"
        "## ToDo（担当・期限が本文にあれば付記）\n"
        "## 論点・未決事項\n\n"
        "内容が商談・営業の場合は、上記に加えて以下も抽出してください。"
        "本文に該当する情報がある見出しは必ず含め、無い見出しのみ省略すること:\n"
        "## 顧客の課題・ニーズ\n"
        "## 予算・条件・導入時期\n"
        "## 決裁プロセス・キーパーソン\n"
        "## 懸念・反対意見\n"
        "## ネクストアクション（次回の約束・フォロー事項）\n\n"
        "話者ラベルがある場合は、誰の発言かを必要な箇所だけ明記してください。\n\n"
        "文字起こし:\n"
        f"{transcript}"
    )


# ローカルLLM（特にQwen系）が稀に混入させる簡体字を日本語の漢字へ正規化する。
# 日本語として使われない字のみ対象（誤置換防止）。
_SIMPLIFIED_TO_JA = str.maketrans({
    "导": "導", "时": "時", "报": "報", "应": "応", "设": "設",
    "实": "実", "现": "現", "间": "間", "题": "題", "议": "議",
    "记": "記", "录": "録", "务": "務", "经": "経", "说": "説",
    "对": "対", "进": "進", "确": "確", "认": "認", "门": "門",
    "审": "審", "业": "業", "长": "長", "东": "東", "风": "風",
    "车": "車", "书": "書", "头": "頭", "处": "処", "价": "価",
    "优": "優", "质": "質", "总": "総", "结": "結", "构": "構",
    "终": "終", "转": "転", "开": "開", "闭": "閉", "问": "問",
    "询": "詢", "验": "験", "检": "検", "续": "続", "单": "単",
    "见": "見", "贵": "貴", "员": "員",
})


def _normalize_summary_text(text):
    return text.translate(_SIMPLIFIED_TO_JA)


def _api_key_for_backend(backend, settings):
    if backend == "claude":
        return os.environ.get("ANTHROPIC_API_KEY") or settings.get("api_key", "")
    if backend == "openai":
        return os.environ.get("OPENAI_API_KEY") or settings.get("api_key", "")
    return ""


# Ollamaに一度に渡す文字起こしの上限（文字数）。
# 日本語はおおむね1文字≒1トークンなので、これを超えるとnum_ctxを上げても
# KVキャッシュのメモリ消費が大きくなりすぎる。超過分は分割要約→統合する。
_MAX_SUMMARY_INPUT_CHARS = 20000


def _merge_summaries_prompt(partial_summaries):
    return (
        "以下は同じ会議・会話の文字起こしを前半・後半などに分割して要約したものです。"
        "重複を整理し、同じ出力形式（Markdown見出し）のまま1つの要約に統合してください。\n\n"
        "厳守事項:\n"
        "- 部分要約に書かれている事実だけを使い、新しい情報を加えない。\n"
        "- 情報が無い項目は『記載なし』と書く。\n"
        "- 出力はすべて日本語で書く。\n\n"
        "部分要約:\n"
        f"{partial_summaries}"
    )


def _ollama_generate(model, prompt, cancel_check=None):
    # Ollamaのデフォルトnum_ctx(4096)では長い文字起こしでプロンプト先頭の
    # 指示文が切り捨てられるため、入力長に応じて明示的に確保する。
    approx_tokens = len(prompt)
    num_ctx = min(32768, max(8192, approx_tokens + 1500))
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": cancel_check is not None,
        "options": {"num_ctx": num_ctx},
    }
    if cancel_check is None:
        data = _json_post(
            "http://localhost:11434/api/generate",
            payload,
            timeout=600,
        )
        return data.get("response", "").strip()

    request = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    parts = []
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            for raw_line in response:
                cancel_check()
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                data = json.loads(line)
                if data.get("error"):
                    raise RuntimeError(data["error"])
                parts.append(data.get("response", ""))
                if data.get("done"):
                    break
    except CancelledError:
        raise
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollamaエラー: HTTP {e.code}\n{detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(str(e.reason)) from e

    cancel_check()
    return "".join(parts).strip()


def summarize_text(transcript, settings, cancel_check=None):
    backend = settings.get("backend", "ollama")
    model = settings.get("model") or BACKEND_DEFAULT_MODELS.get(backend, "llama3.1")
    prompt = _summary_prompt(transcript)

    def _maybe_cancel():
        if cancel_check is not None:
            cancel_check()

    _maybe_cancel()

    if backend == "ollama":
        try:
            if len(transcript) <= _MAX_SUMMARY_INPUT_CHARS:
                _maybe_cancel()
                result = _ollama_generate(model, prompt, cancel_check=cancel_check)
                _maybe_cancel()
            else:
                chunks = [
                    transcript[i:i + _MAX_SUMMARY_INPUT_CHARS]
                    for i in range(0, len(transcript), _MAX_SUMMARY_INPUT_CHARS)
                ]
                partials = []
                for chunk in chunks:
                    _maybe_cancel()
                    partials.append(_ollama_generate(
                        model,
                        _summary_prompt(chunk),
                        cancel_check=cancel_check,
                    ))
                    _maybe_cancel()
                _maybe_cancel()
                result = _ollama_generate(
                    model,
                    _merge_summaries_prompt("\n\n---\n\n".join(partials)),
                    cancel_check=cancel_check,
                )
                _maybe_cancel()
        except RuntimeError as e:
            raise RuntimeError(
                "Ollamaが見つかりません。`brew install ollama` 後に "
                f"`ollama pull {model}` を実行し、Ollamaを起動してください。\n\n{e}"
            ) from e
        return _normalize_summary_text(result)

    if backend == "claude":
        _maybe_cancel()
        api_key = _api_key_for_backend("claude", settings)
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY または設定画面のAPIキーを指定してください。")
        data = _json_post(
            "https://api.anthropic.com/v1/messages",
            {
                "model": model,
                "max_tokens": 1600,
                "messages": [{"role": "user", "content": prompt}],
            },
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            timeout=120,
        )
        _maybe_cancel()
        parts = data.get("content", [])
        return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()

    if backend == "openai":
        _maybe_cancel()
        api_key = _api_key_for_backend("openai", settings)
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY または設定画面のAPIキーを指定してください。")
        data = _json_post(
            "https://api.openai.com/v1/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            },
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120,
        )
        _maybe_cancel()
        return data["choices"][0]["message"]["content"].strip()

    raise RuntimeError(f"未対応のサマリーAIです: {backend}")


def extract_audio(video_path, output_path):
    """ffmpegで動画から音声を抽出（16kHz mono WAV）"""
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le",
        "-ar", "16000", "-ac", "1",
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"音声抽出に失敗しました:\n{result.stderr}")


def get_audio_duration(audio_path):
    """ffprobeで音声の長さを取得（秒）"""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", audio_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        info = json.loads(result.stdout)
        return float(info["format"]["duration"])
    return 0


def split_audio_chunks(audio_path, chunk_seconds=300):
    """音声をチャンクに分割してリストを返す [(chunk_path, offset_seconds), ...]"""
    duration = get_audio_duration(audio_path)
    if duration <= chunk_seconds:
        return [(audio_path, 0.0)], duration

    chunks = []
    start = 0.0
    i = 0
    while start < duration:
        chunk_path = tempfile.mktemp(suffix=f"_chunk{i}.wav")
        cmd = [
            "ffmpeg", "-y", "-i", audio_path,
            "-ss", str(start), "-t", str(chunk_seconds),
            "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            chunk_path
        ]
        subprocess.run(cmd, capture_output=True, text=True)
        chunks.append((chunk_path, start))
        start += chunk_seconds
        i += 1

    return chunks, duration


class WhisperApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        global HAS_DND
        if HAS_DND:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
            except (RuntimeError, Exception):
                HAS_DND = False

        self.title("文字起こしツール")
        self.geometry("860x880")
        self.resizable(True, True)
        self.minsize(760, 760)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # --- 状態 ---
        self.file_paths = []          # バッチ対象ファイルリスト
        self.processing = False
        self._cancel_requested = False
        self._closing = False
        self._temp_files = []

        # --- モデルキャッシュ ---
        self._diarization_pipeline = None
        self._whisper_repo = None  # mlx-whisper は関数ベースなので repo 名のみ保持

        # --- 進捗追跡 ---
        self._elapsed_timer_running = False
        self._elapsed_after_id = None
        self._start_time = None
        self._file_start_time = None

        # --- バッチ結果 ---
        self._batch_results = []
        self._batch_errors = []
        self._output_path = None
        self._summary_output_path = None
        self._summary_running = False
        self._summary_cancelled = False
        self.summary_settings = load_settings()

        # --- メインコンテナ ---
        self.main_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.main_frame.pack(fill="both", expand=True, padx=30, pady=20)

        # === ヘッダー ===
        ctk.CTkLabel(
            self.main_frame, text="🎙 文字起こしツール",
            font=ctk.CTkFont(size=26, weight="bold")
        ).pack(pady=(0, 2))

        ctk.CTkLabel(
            self.main_frame, text="MLX Whisper + 話者分離 + ローカル優先AIサマリー",
            font=ctk.CTkFont(size=13), text_color="#9CA3AF"
        ).pack(pady=(0, 16))

        # === 入力セクション ===
        self.input_section = ctk.CTkFrame(
            self.main_frame, fg_color="#141A22", border_width=1, border_color="#223044"
        )
        self.input_section.pack(fill="x", pady=(0, 12))

        # --- ヘッダー行（タイトル + クリアボタン） ---
        input_header_frame = ctk.CTkFrame(self.input_section, fg_color="transparent")
        input_header_frame.pack(fill="x", padx=16, pady=(12, 8))

        ctk.CTkLabel(
            input_header_frame, text="📁 ファイル選択",
            font=ctk.CTkFont(size=14, weight="bold"), anchor="w"
        ).pack(side="left")

        self.clear_files_button = ctk.CTkButton(
            input_header_frame, text="クリア",
            font=ctk.CTkFont(size=11), height=24, width=60,
            fg_color="#263241", hover_color="#334155", command=self._clear_files
        )
        self.clear_files_button.pack(side="right")

        # --- ファイルリスト（スクロール可能）---
        self.file_list_frame = ctk.CTkScrollableFrame(
            self.input_section, height=120,
            fg_color=("gray90", "gray20")
        )
        self.file_list_frame.pack(fill="x", padx=16, pady=(0, 12))

        if HAS_DND:
            self.file_list_frame.drop_target_register(DND_FILES)
            self.file_list_frame.dnd_bind("<<Drop>>", self.on_drop)
            self.file_list_frame.dnd_bind("<<DragEnter>>", self.on_drag_enter)
            self.file_list_frame.dnd_bind("<<DragLeave>>", self.on_drag_leave)
        self.file_list_frame.bind("<Button-1>", self.select_file)

        self._refresh_file_list()

        # --- モデル選択 + 実行ボタン ---
        self.controls_frame = ctk.CTkFrame(self.input_section, fg_color="transparent")
        self.controls_frame.pack(fill="x", padx=16, pady=(0, 12))

        ctk.CTkLabel(
            self.controls_frame, text="モデル:",
            font=ctk.CTkFont(size=13)
        ).pack(side="left", padx=(0, 8))

        _low_memory_default = detect_low_memory_default()
        self.model_var = ctk.StringVar(
            value=MODEL_NAME_TO_LABEL[recommended_model_for_memory(_low_memory_default)]
        )
        self.model_menu = ctk.CTkOptionMenu(
            self.controls_frame,
            variable=self.model_var,
            values=[label for label, _ in MODEL_DISPLAY_CHOICES],
            width=230
        )
        self.model_menu.pack(side="left")

        self.run_button = ctk.CTkButton(
            self.controls_frame, text="文字起こし開始",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=36, width=160,
            fg_color="#2563EB", hover_color="#1D4ED8",
            command=self.start_transcribe
        )
        self.run_button.pack(side="right")

        self.cancel_button = ctk.CTkButton(
            self.controls_frame, text="キャンセル",
            font=ctk.CTkFont(size=13),
            height=36, width=100,
            fg_color="#7F1D1D", hover_color="#991B1B",
            command=self.request_cancel, state="disabled"
        )
        self.cancel_button.pack(side="right", padx=(0, 8))

        # --- モード切替 ---
        self.mode_frame = ctk.CTkFrame(self.input_section, fg_color="transparent")
        self.mode_frame.pack(fill="x", padx=16, pady=(0, 12))

        self.turbo_var = ctk.BooleanVar(value=False)
        self.turbo_switch = ctk.CTkSwitch(
            self.mode_frame,
            text="高速",
            variable=self.turbo_var,
            font=ctk.CTkFont(size=12),
            onvalue=True, offvalue=False
        )
        self.turbo_switch.pack(side="left")

        self.diarization_var = ctk.BooleanVar(value=True)
        self.diarization_switch = ctk.CTkSwitch(
            self.mode_frame,
            text="話者分離",
            variable=self.diarization_var,
            font=ctk.CTkFont(size=12),
            onvalue=True, offvalue=False
        )
        self.diarization_switch.pack(side="left", padx=(16, 0))

        # 軽量モード（8〜16GB Mac向け。batch_size を下げて高速モードを抑制）
        self.low_memory_var = ctk.BooleanVar(value=_low_memory_default)
        self.low_memory_switch = ctk.CTkSwitch(
            self.mode_frame,
            text="軽量",
            variable=self.low_memory_var,
            font=ctk.CTkFont(size=12),
            onvalue=True, offvalue=False
        )
        self.low_memory_switch.pack(side="left", padx=(16, 0))

        self.summary_enabled_var = ctk.BooleanVar(
            value=bool(self.summary_settings.get("summary_enabled", False))
        )
        self.summary_switch = ctk.CTkSwitch(
            self.mode_frame,
            text="サマリー",
            variable=self.summary_enabled_var,
            font=ctk.CTkFont(size=12),
            onvalue=True, offvalue=False,
            command=self._on_summary_toggle
        )
        self.summary_switch.pack(side="left", padx=(16, 0))

        self.summary_settings_button = ctk.CTkButton(
            self.mode_frame,
            text="⚙",
            font=ctk.CTkFont(size=14),
            width=34, height=26,
            fg_color="#263241", hover_color="#334155",
            command=self.open_summary_settings
        )
        self.summary_settings_button.pack(side="left", padx=(8, 0))

        self.mode_hint = ctk.CTkLabel(
            self.mode_frame, text="ローカル優先",
            font=ctk.CTkFont(size=11), text_color="gray"
        )
        self.mode_hint.pack(side="right")
        self.turbo_var.trace_add("write", self._on_mode_change)

        # === 進捗セクション ===
        self.progress_section = ctk.CTkFrame(
            self.main_frame, fg_color="#141A22", border_width=1, border_color="#223044"
        )
        self.progress_section.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(
            self.progress_section, text="⏱ 進捗",
            font=ctk.CTkFont(size=14, weight="bold"), anchor="w"
        ).pack(fill="x", padx=16, pady=(12, 8))

        # バッチ進捗ラベル（ファイル X/Y）
        self._batch_status_label = ctk.CTkLabel(
            self.progress_section, text="",
            font=ctk.CTkFont(size=12), text_color="#3B8ED0", anchor="w"
        )
        self._batch_status_label.pack(fill="x", padx=16, pady=(0, 4))

        self.progress = ctk.CTkProgressBar(self.progress_section, mode="determinate")
        self.progress.pack(fill="x", padx=16, pady=(0, 4))
        self.progress.set(0)

        self.status_label = ctk.CTkLabel(
            self.progress_section, text="待機中",
            font=ctk.CTkFont(size=12), text_color="#CBD5E1", anchor="w",
            fg_color="#1F2937", corner_radius=8
        )
        self.status_label.pack(fill="x", padx=16, pady=(0, 4))

        self.elapsed_label = ctk.CTkLabel(
            self.progress_section, text="",
            font=ctk.CTkFont(size=11), text_color="gray", anchor="w"
        )
        self.elapsed_label.pack(fill="x", padx=16, pady=(0, 12))

        # === 結果プレビューセクション ===
        self.preview_section = ctk.CTkFrame(
            self.main_frame, fg_color="#141A22", border_width=1, border_color="#223044"
        )
        self.preview_section.pack(fill="both", expand=True, pady=(0, 12))

        preview_header_frame = ctk.CTkFrame(self.preview_section, fg_color="transparent")
        preview_header_frame.pack(fill="x", padx=16, pady=(12, 8))

        ctk.CTkLabel(
            preview_header_frame, text="📝 結果プレビュー",
            font=ctk.CTkFont(size=14, weight="bold"), anchor="w"
        ).pack(side="left")

        self.open_file_button = ctk.CTkButton(
            preview_header_frame, text="ファイルを開く",
            font=ctk.CTkFont(size=12), height=28, width=100,
            fg_color="#263241", hover_color="#334155",
            command=self.open_result_file, state="disabled"
        )
        self.open_file_button.pack(side="right")

        self.regen_summary_button = ctk.CTkButton(
            preview_header_frame, text="サマリー再生成",
            font=ctk.CTkFont(size=12), height=28, width=110,
            fg_color="#263241", hover_color="#334155",
            command=self._regenerate_summary, state="disabled"
        )
        self.regen_summary_button.pack(side="right", padx=(0, 8))

        self.copy_button = ctk.CTkButton(
            preview_header_frame, text="コピー",
            font=ctk.CTkFont(size=12), height=28, width=70,
            fg_color="#263241", hover_color="#334155",
            command=self.copy_active_tab
        )
        self.copy_button.pack(side="right", padx=(0, 8))

        self.fullscreen_preview_button = ctk.CTkButton(
            preview_header_frame, text="全文表示",
            font=ctk.CTkFont(size=12), height=28, width=80,
            fg_color="#263241", hover_color="#334155",
            command=self.open_active_tab_window
        )
        self.fullscreen_preview_button.pack(side="right", padx=(0, 8))

        self.preview_tabs = ctk.CTkTabview(self.preview_section, fg_color="#111827")
        self.preview_tabs.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        self.preview_tabs.add("文字起こし")
        self.preview_tabs.add("サマリー")

        self.preview_text = ctk.CTkTextbox(
            self.preview_tabs.tab("文字起こし"),
            font=ctk.CTkFont(size=12),
            state="disabled", fg_color="#0B1120",
            height=260, wrap="word"
        )
        self.preview_text.pack(fill="both", expand=True, padx=8, pady=8)

        self.summary_text = ctk.CTkTextbox(
            self.preview_tabs.tab("サマリー"),
            font=ctk.CTkFont(size=12),
            state="disabled", fg_color="#0B1120",
            height=260, wrap="word"
        )
        self.summary_text.pack(fill="both", expand=True, padx=8, pady=8)

        # === 履歴セクション ===
        self.history_section = ctk.CTkFrame(
            self.main_frame, fg_color="#141A22", border_width=1, border_color="#223044"
        )
        self.history_section.pack(fill="x", pady=(0, 0))

        history_header_frame = ctk.CTkFrame(self.history_section, fg_color="transparent")
        history_header_frame.pack(fill="x", padx=16, pady=(12, 8))

        ctk.CTkLabel(
            history_header_frame, text="📚 処理履歴",
            font=ctk.CTkFont(size=14, weight="bold"), anchor="w"
        ).pack(side="left")

        self.clear_history_button = ctk.CTkButton(
            history_header_frame, text="クリア",
            font=ctk.CTkFont(size=11), height=24, width=60,
            fg_color="#263241", hover_color="#334155", command=self.clear_history
        )
        self.clear_history_button.pack(side="right")

        self.history_list = ctk.CTkScrollableFrame(
            self.history_section, height=80,
            fg_color=("gray90", "gray17")
        )
        self.history_list.pack(fill="x", padx=16, pady=(0, 12))

        self.load_history_display()

    # ==================== モード切替 ====================
    def _on_mode_change(self, *args):
        if self.turbo_var.get():
            self.mode_hint.configure(text="高速（並列処理・メモリ多め）", text_color="#3B8ED0")
        else:
            self.mode_hint.configure(text="ローカル優先", text_color="gray")

    def _on_summary_toggle(self):
        self.summary_settings["summary_enabled"] = bool(self.summary_enabled_var.get())
        save_settings(self.summary_settings)

    def open_summary_settings(self):
        window = ctk.CTkToplevel(self)
        window.title("サマリー設定")
        window.geometry("460x360")
        window.resizable(False, False)
        window.transient(self)
        window.grab_set()

        container = ctk.CTkFrame(window, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=22, pady=18)

        ctk.CTkLabel(
            container,
            text="AIサマリー設定",
            font=ctk.CTkFont(size=18, weight="bold"),
            anchor="w"
        ).pack(fill="x", pady=(0, 4))

        helper = ctk.CTkLabel(
            container,
            text="Ollamaは外部送信なし。Claude/OpenAIは文字起こしテキストのみ送信します。",
            font=ctk.CTkFont(size=12),
            text_color="#9CA3AF",
            anchor="w",
            wraplength=410,
            justify="left"
        )
        helper.pack(fill="x", pady=(0, 16))

        backend_var = ctk.StringVar(value=self.summary_settings.get("backend", "ollama"))
        model_var = ctk.StringVar(
            value=self.summary_settings.get("model") or BACKEND_DEFAULT_MODELS["ollama"]
        )
        api_key_var = ctk.StringVar(value=self.summary_settings.get("api_key", ""))

        row_backend = ctk.CTkFrame(container, fg_color="transparent")
        row_backend.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(row_backend, text="バックエンド", width=110, anchor="w").pack(side="left")
        backend_menu = ctk.CTkOptionMenu(
            row_backend,
            values=["ollama", "claude", "openai"],
            variable=backend_var,
            width=190
        )
        backend_menu.pack(side="left")

        row_model = ctk.CTkFrame(container, fg_color="transparent")
        row_model.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(row_model, text="モデル", width=110, anchor="w").pack(side="left")
        model_entry = ctk.CTkEntry(row_model, textvariable=model_var, width=260)
        model_entry.pack(side="left", fill="x", expand=True)

        row_key = ctk.CTkFrame(container, fg_color="transparent")
        row_key.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(row_key, text="APIキー", width=110, anchor="w").pack(side="left")
        api_key_entry = ctk.CTkEntry(row_key, textvariable=api_key_var, width=260, show="*")
        api_key_entry.pack(side="left", fill="x", expand=True)

        key_hint = ctk.CTkLabel(
            container,
            text="環境変数 ANTHROPIC_API_KEY / OPENAI_API_KEY がある場合はそちらを優先します。",
            font=ctk.CTkFont(size=11),
            text_color="#9CA3AF",
            anchor="w",
            wraplength=410,
            justify="left"
        )
        key_hint.pack(fill="x", pady=(0, 18))

        def refresh_key_visibility(*args):
            backend = backend_var.get()
            if backend == "ollama":
                row_key.pack_forget()
                key_hint.configure(text="ローカルOllamaは外部送信なし。未起動なら文字起こし完了後に案内を表示します。")
            else:
                row_key.pack(fill="x", pady=(0, 8), before=key_hint)
                key_hint.configure(
                    text="環境変数 ANTHROPIC_API_KEY / OPENAI_API_KEY がある場合はそちらを優先します。"
                )
            if not model_var.get().strip():
                model_var.set(BACKEND_DEFAULT_MODELS.get(backend, "llama3.1"))

        def on_backend_change(choice):
            current = model_var.get().strip()
            previous_defaults = set(BACKEND_DEFAULT_MODELS.values())
            if not current or current in previous_defaults:
                model_var.set(BACKEND_DEFAULT_MODELS.get(choice, "llama3.1"))
            refresh_key_visibility()

        backend_menu.configure(command=on_backend_change)
        refresh_key_visibility()

        button_row = ctk.CTkFrame(container, fg_color="transparent")
        button_row.pack(fill="x", pady=(8, 0))

        def save_and_close():
            backend = backend_var.get()
            self.summary_settings.update({
                "summary_enabled": bool(self.summary_enabled_var.get()),
                "backend": backend,
                "model": model_var.get().strip() or BACKEND_DEFAULT_MODELS.get(backend, "llama3.1"),
                "api_key": api_key_var.get().strip(),
            })
            save_settings(self.summary_settings)
            window.destroy()

        ctk.CTkButton(
            button_row,
            text="保存",
            width=120,
            fg_color="#2563EB",
            hover_color="#1D4ED8",
            command=save_and_close
        ).pack(side="right")

        ctk.CTkButton(
            button_row,
            text="キャンセル",
            width=100,
            fg_color="#263241",
            hover_color="#334155",
            command=window.destroy
        ).pack(side="right", padx=(0, 8))

    # ==================== ファイルリストUI ====================
    def _refresh_file_list(self):
        """ファイルリストを再描画"""
        for widget in self.file_list_frame.winfo_children():
            widget.destroy()

        if not self.file_paths:
            hint = "ドラッグ＆ドロップ or クリックでファイルを追加" if HAS_DND else "クリックしてファイルを選択"
            hint_label = ctk.CTkLabel(
                self.file_list_frame, text=hint,
                font=ctk.CTkFont(size=13), text_color="gray"
            )
            hint_label.pack(expand=True, pady=40)
            hint_label.bind("<Button-1>", self.select_file)
        else:
            for path in self.file_paths:
                row = ctk.CTkFrame(self.file_list_frame, fg_color="transparent")
                row.pack(fill="x", pady=2)
                ctk.CTkLabel(
                    row, text=os.path.basename(path),
                    font=ctk.CTkFont(size=12), anchor="w"
                ).pack(side="left", fill="x", expand=True, padx=(4, 0))
                if not self.processing and not self._summary_running:
                    remove_btn = ctk.CTkButton(
                        row, text="×",
                        font=ctk.CTkFont(size=11), height=20, width=28,
                        fg_color="gray40",
                        command=lambda p=path: self._remove_file(p)
                    )
                    remove_btn.pack(side="right", padx=(4, 4))

    def _remove_file(self, path):
        if self.processing or self._summary_running:
            return
        if path in self.file_paths:
            self.file_paths.remove(path)
        self._refresh_file_list()

    def _clear_files(self):
        if self.processing or self._summary_running:
            return
        self.file_paths.clear()
        self._refresh_file_list()

    # ==================== D&D ====================
    def on_drop(self, event):
        if self.processing or self._summary_running:
            return
        try:
            paths = self.tk.splitlist(event.data)
        except Exception:
            paths = [event.data.strip()]
        added = False
        for p in paths:
            if os.path.isfile(p) and p not in self.file_paths:
                self.file_paths.append(p)
                added = True
        if added:
            self._refresh_file_list()
        self.file_list_frame.configure(border_width=0)

    def on_drag_enter(self, event):
        if not self.processing and not self._summary_running:
            self.file_list_frame.configure(border_width=2, border_color="#3B8ED0")

    def on_drag_leave(self, event):
        self.file_list_frame.configure(border_width=0)

    # ==================== ファイル選択 ====================
    def select_file(self, event=None):
        if self.processing or self._summary_running:
            return
        paths = filedialog.askopenfilenames(
            filetypes=[
                ("動画/音声ファイル", "*.mp4 *.mp3 *.wav *.m4a *.webm *.mov *.ogg *.flac *.avi *.mkv"),
                ("すべてのファイル", "*.*")
            ]
        )
        added = False
        for p in paths:
            if p not in self.file_paths:
                self.file_paths.append(p)
                added = True
        if added:
            self._refresh_file_list()

    # ==================== 進捗 ====================
    def update_progress(self, percent, text):
        self.progress.set(percent / 100)
        self.status_label.configure(
            text=f"{text}（{percent}%）",
            text_color="#F8FAFC",
            fg_color="#1F2937"
        )

    def _update_batch_status(self, idx, total, file_path):
        self._batch_status_label.configure(
            text=f"ファイル {idx}/{total}: {os.path.basename(file_path)}"
        )

    def start_elapsed_timer(self):
        self.stop_elapsed_timer()
        self._start_time = time.time()
        self._elapsed_timer_running = True
        self._update_elapsed()

    def _update_elapsed(self):
        if not self._elapsed_timer_running:
            return
        elapsed = time.time() - self._start_time
        m = int(elapsed // 60)
        s = int(elapsed % 60)
        text = f"経過時間: {m}分{s:02d}秒"
        if self._file_start_time is not None:
            fe = time.time() - self._file_start_time
            fm = int(fe // 60)
            fs = int(fe % 60)
            text += f"（ファイル: {fm}分{fs:02d}秒）"
        self.elapsed_label.configure(text=text)
        self._elapsed_after_id = self.after(1000, self._update_elapsed)

    def stop_elapsed_timer(self):
        self._elapsed_timer_running = False
        if self._elapsed_after_id is not None:
            try:
                self.after_cancel(self._elapsed_after_id)
            except Exception:
                pass
            self._elapsed_after_id = None

    # ==================== プレビュー ====================
    def _set_textbox_text(self, textbox, text):
        textbox.configure(state="normal")
        textbox.delete("1.0", "end")
        textbox.insert("1.0", text)
        textbox.configure(state="disabled")

    def show_preview(self, text):
        self._set_textbox_text(self.preview_text, text)
        self.preview_tabs.set("文字起こし")

    def show_summary(self, text):
        self._set_textbox_text(self.summary_text, text)
        self.preview_tabs.set("サマリー")

    # ==================== キャンセル ====================
    def request_cancel(self):
        if not (self.processing or self._summary_running):
            return
        self._cancel_requested = True
        self.cancel_button.configure(state="disabled", text="停止中...")
        self.status_label.configure(
            text="キャンセル中...（処理の区切りで停止します）",
            text_color="#FCD34D",
            fg_color="#3B2F12"
        )
        self._log_event("cancel requested")

    def _check_cancel(self):
        if self._cancel_requested:
            raise CancelledError()

    def _reset_cancel_button(self):
        self.cancel_button.configure(state="disabled", text="キャンセル")

    # ==================== コピー / 再生成 / 終了 ====================
    def copy_active_tab(self):
        tab = self.preview_tabs.get()
        textbox = self.summary_text if tab == "サマリー" else self.preview_text
        text = textbox.get("1.0", "end-1c")
        placeholders = {
            "",
            "AIサマリーを生成中です...\n\n文字起こし結果は保存済みです。",
            "（この履歴にはサマリーがありません）",
            "文字起こし結果がありません",
        }
        if text.strip() in placeholders:
            self.copy_button.configure(text="内容なし")
            self.after(1500, lambda: self.copy_button.configure(text="コピー"))
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.copy_button.configure(text="✓ コピー済み")
        self.after(1500, lambda: self.copy_button.configure(text="コピー"))

    def open_active_tab_window(self):
        tab = self.preview_tabs.get()
        textbox = self.summary_text if tab == "サマリー" else self.preview_text
        text = textbox.get("1.0", "end-1c").strip()
        placeholders = {
            "",
            "AIサマリーを生成中です...\n\n文字起こし結果は保存済みです。",
            "（この履歴にはサマリーがありません）",
            "文字起こし結果がありません",
        }
        if text in placeholders:
            self.fullscreen_preview_button.configure(text="内容なし")
            self.after(1500, lambda: self.fullscreen_preview_button.configure(text="全文表示"))
            return

        window = ctk.CTkToplevel(self)
        window.title(f"{tab} - 全文表示")
        window.geometry("900x680")
        window.minsize(640, 420)
        window.transient(self)

        container = ctk.CTkFrame(window, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=18, pady=16)

        header = ctk.CTkFrame(container, fg_color="transparent")
        header.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(
            header, text=tab,
            font=ctk.CTkFont(size=16, weight="bold"),
            anchor="w"
        ).pack(side="left")

        def copy_popup_text():
            window.clipboard_clear()
            window.clipboard_append(text)
            popup_copy_button.configure(text="✓ コピー済み")
            window.after(1500, lambda: popup_copy_button.configure(text="コピー"))

        popup_copy_button = ctk.CTkButton(
            header, text="コピー",
            font=ctk.CTkFont(size=12), height=28, width=80,
            fg_color="#263241", hover_color="#334155",
            command=copy_popup_text
        )
        popup_copy_button.pack(side="right")

        full_textbox = ctk.CTkTextbox(
            container,
            font=ctk.CTkFont(size=13),
            fg_color="#0B1120",
            wrap="word"
        )
        full_textbox.pack(fill="both", expand=True)
        full_textbox.insert("1.0", text)
        full_textbox.configure(state="disabled")

    def _regenerate_summary(self):
        if self.processing or self._summary_running or not self._batch_results:
            return
        self._cancel_requested = False
        self._start_summary_generation()

    def _on_close(self):
        if self.processing or self._summary_running:
            if not messagebox.askokcancel(
                "処理中です",
                "文字起こし／サマリー生成が進行中です。\n"
                "終了すると進行中の処理は失われます。終了しますか？",
                parent=self,
            ):
                return
            self._closing = True
            self._cancel_requested = True
            self._log_event("forced close during active processing")
            try:
                self.destroy()
            finally:
                os._exit(0)
        self.cleanup_temp()
        self.destroy()

    def _log_event(self, message):
        try:
            with open("/tmp/whisper-app-events.log", "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().strftime('%m-%d %H:%M:%S')} {message}\n")
        except Exception:
            pass

    def report_callback_exception(self, exc, val, tb):
        # GUIコールバック内の例外は標準では握り潰されがちなのでログに残す
        self._log_event(
            "GUI callback error: "
            + "".join(traceback.format_exception(exc, val, tb))
        )

    def open_result_file(self):
        if self._output_path and os.path.exists(self._output_path):
            subprocess.run(["open", self._output_path])

    # ==================== クリーンアップ ====================
    def cleanup_temp(self):
        for path in self._temp_files:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
        self._temp_files = []

    # ==================== 処理開始 ====================
    def start_transcribe(self):
        if self.processing or self._summary_running:
            return
        if not self.file_paths:
            self.status_label.configure(text="ファイルを選択してください", text_color="red")
            return
        self.processing = True
        self._cancel_requested = False
        self._batch_results = []
        self._batch_errors = []
        self._output_path = None
        self._summary_output_path = None
        self._file_start_time = None
        self.run_button.configure(state="disabled")
        self.cancel_button.configure(state="normal", text="キャンセル")
        self.regen_summary_button.configure(state="disabled")
        self.model_menu.configure(state="disabled")
        self.clear_files_button.configure(state="disabled")
        self.summary_switch.configure(state="disabled")
        self.summary_settings_button.configure(state="disabled")
        self.progress.set(0)
        self._batch_status_label.configure(text="")
        self._set_textbox_text(self.preview_text, "")
        self._set_textbox_text(self.summary_text, "")
        self.preview_tabs.set("文字起こし")
        self.open_file_button.configure(state="disabled")
        self.start_elapsed_timer()
        self._refresh_file_list()  # ×ボタンを隠す
        thread = threading.Thread(target=self.run_transcribe, daemon=True)
        thread.start()

    # ==================== モデル準備 ====================
    def _ensure_models(self, model_size):
        """モデルをロード（キャッシュあれば再利用）"""
        if self.diarization_var.get():
            if self._diarization_pipeline is None:
                self.after(0, self.update_progress, 6, "話者分離モデルを読み込み中...")
                from pyannote.audio import Pipeline
                import torch
                token_path = os.path.expanduser("~/.huggingface/token")
                with open(token_path, "r") as f:
                    hf_token = f.read().strip()
                self._diarization_pipeline = Pipeline.from_pretrained(
                    "pyannote/speaker-diarization-community-1",
                    token=hf_token
                )
                if torch.backends.mps.is_available():
                    self._diarization_pipeline = self._diarization_pipeline.to(torch.device("mps"))
            # 軽量モードON: batch_size=16、OFF: batch_size=64（メモリ圧を可変で調整）
            batch_size = 16 if self.low_memory_var.get() else 64
            if hasattr(self._diarization_pipeline, "segmentation_batch_size"):
                self._diarization_pipeline.segmentation_batch_size = batch_size
            if hasattr(self._diarization_pipeline, "embedding_batch_size"):
                self._diarization_pipeline.embedding_batch_size = batch_size
            self.after(0, self.update_progress, 10,
                       f"話者分離モデル準備完了（MPS・batch={batch_size}）")

        target_repo = MLX_REPO_MAP[model_size]
        if self._whisper_repo != target_repo:
            self.after(0, self.update_progress, 11,
                       f"Whisper ({model_size}) を準備中...")
            self._whisper_repo = target_repo
            self.after(0, self.update_progress, 14, "Whisperモデル準備完了")

    # ==================== 1ファイル処理 ====================
    def _process_one_file(self, file_path, model_size):
        """1ファイルを処理して (output_path, result_text) を返す"""
        # 軽量モード時は並列実行（turbo）を強制 OFF にし、メモリ競合を避ける
        turbo = self.turbo_var.get() and not self.low_memory_var.get()
        ext = os.path.splitext(file_path)[1].lower()

        # Step 1: 入力を WAV に統一変換
        # .wav 以外（動画も .m4a / .mp3 / .flac 等の音声コンテナも）は pyannote が直接読めず
        # 話者分離フェーズでフリーズする実害が出る。よって .wav 以外は全部 ffmpeg で WAV 化する。
        self._check_cancel()
        if ext == WAV_EXT:
            audio_path = file_path
        else:
            self.after(0, self.update_progress, 2, "音声を WAV に変換中...")
            temp_audio = tempfile.mktemp(suffix=".wav")
            self._temp_files.append(temp_audio)
            extract_audio(file_path, temp_audio)
            audio_path = temp_audio
            self.after(0, self.update_progress, 5, "音声変換完了")
        self._check_cancel()

        # Step 2: チャンク分割
        self.after(0, self.update_progress, 15, "音声を分割中...")
        chunks, total_duration = split_audio_chunks(audio_path, chunk_seconds=300)
        total_chunks = len(chunks)
        for chunk_path, _ in chunks:
            if chunk_path != audio_path:
                self._temp_files.append(chunk_path)
        self.after(0, self.update_progress, 16, f"音声分割完了（{total_chunks}チャンク）")

        # Step 3 & 4: 話者分離 + 文字起こし
        diarization = None
        all_segments = []
        diarization_enabled = self.diarization_var.get()

        def do_diarization():
            nonlocal diarization
            self.after(0, self.update_progress, 18, "話者を分析中...")
            diarization_output = self._diarization_pipeline(audio_path, num_speakers=2)
            if hasattr(diarization_output, 'speaker_diarization'):
                diarization = diarization_output.speaker_diarization
            else:
                diarization = diarization_output

        def do_transcription():
            nonlocal all_segments
            import mlx_whisper
            for i, (chunk_path, offset) in enumerate(chunks):
                self._check_cancel()
                pct = 18 + int((i / total_chunks) * 62)
                self.after(0, self.update_progress, pct,
                           f"文字起こし中... チャンク {i+1}/{total_chunks}")
                result = mlx_whisper.transcribe(
                    chunk_path,
                    path_or_hf_repo=self._whisper_repo,
                    language="ja",
                    verbose=False,
                    word_timestamps=False,
                    condition_on_previous_text=False,
                    no_speech_threshold=0.6,
                )
                for seg in result["segments"]:
                    all_segments.append({
                        "start": seg["start"] + offset,
                        "end": seg["end"] + offset,
                        "text": seg["text"],
                    })

        if turbo and diarization_enabled:
            self.after(0, self.update_progress, 17, "並列処理中（高速モード）...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                d_future = executor.submit(do_diarization)
                t_future = executor.submit(do_transcription)
                waiting_for_diarization = False
                while True:
                    done, _ = concurrent.futures.wait(
                        [d_future, t_future],
                        timeout=0.5,
                        return_when=concurrent.futures.ALL_COMPLETED,
                    )
                    if len(done) == 2:
                        break
                    if self._cancel_requested and not waiting_for_diarization:
                        waiting_for_diarization = True
                        self.after(
                            0,
                            self.update_progress,
                            70,
                            "キャンセル中...話者分離の完了を待っています",
                        )
                d_future.result()
                t_future.result()
        elif diarization_enabled:
            do_diarization()
            self.after(0, self.update_progress, 45, "話者分離完了")
            do_transcription()
        else:
            self.after(0, self.update_progress, 18, "文字起こしを開始（話者分離スキップ）...")
            do_transcription()

        self._check_cancel()
        self.after(0, self.update_progress, 85, "結合中...")

        # Step 5: 話者とテキストを結合（話者分離OFFのときは全セグメントを単一話者として扱う）
        if diarization is not None:
            diarization_list = [
                (turn.start, turn.end, speaker)
                for turn, _, speaker in diarization.itertracks(yield_label=True)
            ]
        else:
            diarization_list = []

        def get_speaker(seg_start, seg_end):
            if not diarization_list:
                return "話者1"
            best_speaker = "不明"
            best_overlap = 0
            for d_start, d_end, speaker in diarization_list:
                overlap = max(0, min(seg_end, d_end) - max(seg_start, d_start))
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_speaker = speaker
            return best_speaker

        import re
        filler_pattern = re.compile(
            r'^(えー[っと]*|あー[っと]*|あのー?|まあ?|うーん|そのー?|ええと|んー+|ねえ?|うん|はい)[、。,.]?\s*',
        )

        def remove_fillers(text):
            text = text.strip()
            text = filler_pattern.sub('', text)
            return text.strip()

        turns = []
        for seg in all_segments:
            speaker = get_speaker(seg["start"], seg["end"])
            cleaned = remove_fillers(seg["text"])
            if not cleaned:
                continue
            if turns and turns[-1]["speaker"] == speaker:
                turns[-1]["text"] += cleaned
                turns[-1]["end"] = seg["end"]
            else:
                turns.append({
                    "speaker": speaker,
                    "start": seg["start"],
                    "end": seg["end"],
                    "text": cleaned,
                })

        lines = []
        for turn in turns:
            m = int(turn["start"] // 60)
            s = int(turn["start"] % 60)
            lines.append(f"[{m:02d}:{s:02d}] {turn['speaker']}:\n{turn['text']}\n")

        result_text = "\n".join(lines)

        self.after(0, self.update_progress, 95, "保存中...")
        base_name = os.path.splitext(file_path)[0]
        output_path = base_name + "_文字起こし.txt"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(result_text)

        self.after(0, self.update_progress, 100, "完了！")
        return output_path, result_text

    # ==================== バッチメイン処理 ====================
    def run_transcribe(self):
        # 表示ラベル（日本語）から内部モデル名に変換
        model_size = MODEL_LABEL_TO_NAME.get(self.model_var.get(), self.model_var.get())
        total_files = len(self.file_paths)

        # モデル準備（全バッチで1回）
        try:
            self._check_cancel()
            self._ensure_models(model_size)
        except CancelledError:
            pass
        except Exception as e:
            self.after(0, self._on_fatal_error, e, traceback.format_exc())
            return

        for idx, file_path in enumerate(self.file_paths, start=1):
            if self._cancel_requested:
                break
            self._file_start_time = time.time()
            self.after(0, self._update_batch_status, idx, total_files, file_path)
            self.after(0, self.progress.set, 0)
            try:
                output_path, result_text = self._process_one_file(file_path, model_size)
                elapsed = time.time() - self._file_start_time
                self._batch_results.append({
                    "file": file_path,
                    "output": output_path,
                    "elapsed": elapsed,
                    "text": result_text,
                })
                self.after(0, self.add_history_entry, file_path, output_path, model_size)
                self._output_path = output_path
            except CancelledError:
                self._log_event(f"cancelled during: {os.path.basename(file_path)}")
                break
            except Exception as e:
                self._batch_errors.append({
                    "file": file_path,
                    "error": str(e),
                    "detail": traceback.format_exc(),
                })
                self._log_event(
                    f"file error: {os.path.basename(file_path)}: {e}\n"
                    + traceback.format_exc()
                )
            finally:
                self.cleanup_temp()

        batch_elapsed = time.time() - self._start_time
        self.after(0, self.on_batch_complete, batch_elapsed)

    def _on_fatal_error(self, error, tb_text=None):
        """モデルロード失敗など致命的エラー"""
        self.stop_elapsed_timer()
        self.processing = False
        self._file_start_time = None
        self.run_button.configure(state="normal")
        self._reset_cancel_button()
        self.model_menu.configure(state="normal")
        self.clear_files_button.configure(state="normal")
        self.summary_switch.configure(state="normal")
        self.summary_settings_button.configure(state="normal")
        self._refresh_file_list()
        self.status_label.configure(
            text=f"エラー: {str(error)[:80]}",
            text_color="#FCA5A5",
            fg_color="#3F1D24"
        )
        self._log_event(
            "fatal error:\n"
            + (
                tb_text
                if tb_text
                else "".join(traceback.format_exception(type(error), error, error.__traceback__))
            )
        )
        self.show_preview(
            "処理を開始できませんでした。\n\n"
            f"原因: {error}\n\n"
            "対処のヒント:\n"
            "- 初回は話者分離モデルの取得に Hugging Face トークン"
            "（~/.huggingface/token）が必要です\n"
            "- メモリ不足の場合は「軽量」をONにするか、小さめのモデルをお試しください\n"
            "- ネットワーク接続もご確認ください（モデルの初回ダウンロード時のみ）\n\n"
            "詳細ログ: /tmp/whisper-app-events.log"
        )

    # ==================== バッチ完了 ====================
    def on_batch_complete(self, batch_elapsed):
        self.stop_elapsed_timer()
        self._file_start_time = None
        self.processing = False
        self.run_button.configure(state="normal")
        self.model_menu.configure(state="normal")
        self.clear_files_button.configure(state="normal")
        self.summary_switch.configure(state="normal")
        self.summary_settings_button.configure(state="normal")
        self._refresh_file_list()  # ×ボタンを再表示

        success_count = len(self._batch_results)
        error_count = len(self._batch_errors)
        bm = int(batch_elapsed // 60)
        bs = int(batch_elapsed % 60)

        if self._cancel_requested:
            self._reset_cancel_button()
            self._batch_status_label.configure(
                text=f"キャンセルしました（完了分: {success_count}件）"
            )
            self.status_label.configure(
                text=f"キャンセルしました（{bm}分{bs:02d}秒）",
                text_color="#FCD34D",
                fg_color="#3B2F12"
            )
        elif error_count == 0:
            self._batch_status_label.configure(text=f"全{success_count}件 完了！")
            self.status_label.configure(
                text=f"完了！ {success_count}件処理（{bm}分{bs:02d}秒）",
                text_color="#BBF7D0",
                fg_color="#12351F"
            )
        else:
            self._batch_status_label.configure(
                text=f"完了（成功: {success_count}件 / 失敗: {error_count}件）"
            )
            self.status_label.configure(
                text=f"完了（一部エラー）{bm}分{bs:02d}秒",
                text_color="#FDBA74",
                fg_color="#3B2412"
            )

        # 「文字起こし」タブには文字起こし本文を表示する（複数ファイルはヘッダ付きで連結）
        preview_parts = []
        multi = len(self._batch_results) > 1
        for r in self._batch_results:
            text = (r.get("text") or "").strip()
            if multi:
                preview_parts.append(f"━━━ {os.path.basename(r['file'])} ━━━")
            preview_parts.append(text if text else "(本文が空でした)")
            preview_parts.append("")

        # 失敗したファイルは末尾に注記
        for e in self._batch_errors:
            preview_parts.append(f"✗ {os.path.basename(e['file'])} — エラー: {e['error']}")

        body = "\n".join(preview_parts).strip()
        self.show_preview(body if body else "文字起こし結果がありません")
        if self._cancel_requested:
            self.progress.set(0)
        else:
            self.progress.set(1)

        if self._batch_results:
            self.open_file_button.configure(state="normal")
            self.regen_summary_button.configure(state="normal")
            self._log_event(
                f"batch complete: success={len(self._batch_results)} "
                f"errors={len(self._batch_errors)} "
                f"summary_enabled={self.summary_enabled_var.get()} "
                f"cancelled={self._cancel_requested}"
            )
            if self.summary_enabled_var.get() and not self._cancel_requested:
                self._start_summary_generation()
            else:
                self._reset_cancel_button()
        else:
            self._reset_cancel_button()

    # ==================== AIサマリー ====================
    def _start_summary_generation(self):
        if self._summary_running:
            return
        settings = self.summary_settings.copy()
        self._summary_running = True
        self.run_button.configure(state="disabled")
        self.cancel_button.configure(state="normal", text="キャンセル")
        self.regen_summary_button.configure(state="disabled")
        self.model_menu.configure(state="disabled")
        self.clear_files_button.configure(state="disabled")
        self.summary_switch.configure(state="disabled")
        self.summary_settings_button.configure(state="disabled")
        self._refresh_file_list()
        backend = settings.get("backend", "ollama")
        model = settings.get("model") or BACKEND_DEFAULT_MODELS.get(backend, "llama3.1")
        self.status_label.configure(
            text=f"AIサマリー生成中...（{model}）初回はモデル読み込みで数十秒余分にかかります",
            text_color="#BFDBFE",
            fg_color="#172554"
        )
        # 生成中はプログレスバーを動かし続けて「固まっていない」ことを見せる
        self.progress.configure(mode="indeterminate")
        self.progress.start()
        self._file_start_time = None
        self.start_elapsed_timer()
        self.show_summary("AIサマリーを生成中です...\n\n文字起こし結果は保存済みです。")
        self._log_event(f"summary start: files={len(self._batch_results)} backend={backend} model={model}")
        # ワーカースレッドの結果受け渡し用（GUI更新はメインスレッドの_poll_summaryが行う）
        self._summary_display_text = None
        self._summary_display_shown = None
        self._summary_progress = None
        self._summary_progress_shown = None
        self._summary_done = False
        self._summary_cancelled = False
        self._summary_errors = []
        thread = threading.Thread(
            target=self._run_summary_generation,
            args=(settings,),
            daemon=True
        )
        thread.start()
        self.after(700, self._poll_summary)

    def _run_summary_generation(self, settings):
        # ワーカースレッド。バックグラウンドスレッドからのafter()によるGUI更新は
        # 失われることがあるため、ここではGUIを一切触らず属性に結果を置くだけにする。
        lines = []
        errors = []
        total = len(self._batch_results)
        for idx, result in enumerate(self._batch_results, start=1):
            if self._cancel_requested:
                lines.append("（キャンセルしました。以降のファイルはスキップ）")
                self._summary_display_text = "\n".join(lines)
                self._summary_cancelled = True
                break
            name = os.path.basename(result["file"])
            self._summary_progress = (idx, total, result["file"])
            try:
                summary = summarize_text(
                    result.get("text", ""), settings,
                    cancel_check=self._check_cancel,
                )
                self._check_cancel()
                if not summary:
                    summary = "サマリーが空でした。"
                summary_path = os.path.splitext(result["file"])[0] + "_サマリー.txt"
                with open(summary_path, "w", encoding="utf-8") as f:
                    f.write(summary)
                result["summary_output"] = summary_path
                self._summary_output_path = summary_path
                lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
                lines.append(f"  {name}")
                lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
                lines.append(summary)
                lines.append("")
                self._summary_display_text = "\n".join(lines)
            except CancelledError:
                lines.append("（キャンセルしました）")
                self._summary_display_text = "\n".join(lines)
                self._summary_cancelled = True
                break
            except Exception as e:
                message = str(e)
                errors.append({"file": result["file"], "error": message})
                lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
                lines.append(f"  {name}")
                lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
                lines.append("サマリー生成に失敗しました。")
                lines.append(message)
                lines.append("")
                self._summary_display_text = "\n".join(lines)

        self._summary_errors = errors
        self._summary_done = True

    def _poll_summary(self):
        """メインスレッドでサマリー生成の進捗・結果を表示に反映する"""
        done = self._summary_done
        progress = self._summary_progress
        if progress is not None and progress != self._summary_progress_shown:
            self._summary_progress_shown = progress
            self._update_batch_status(*progress)
        text = self._summary_display_text
        if text is not None and text != self._summary_display_shown:
            self._summary_display_shown = text
            self._log_event(f"summary display update: {len(text)} chars")
            self.show_summary(text)
        if done:
            self._log_event(f"summary complete: errors={len(self._summary_errors)}")
            self._on_summary_complete(self._summary_errors)
        else:
            self.after(700, self._poll_summary)

    def _on_summary_complete(self, errors):
        self._summary_running = False
        self.stop_elapsed_timer()
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(0 if self._summary_cancelled else 1)
        self.run_button.configure(state="normal")
        self._reset_cancel_button()
        if self._batch_results:
            self.regen_summary_button.configure(state="normal")
        self.model_menu.configure(state="normal")
        self.clear_files_button.configure(state="normal")
        self.summary_switch.configure(state="normal")
        self.summary_settings_button.configure(state="normal")
        self._refresh_file_list()
        self._record_summary_outputs()
        if self._summary_cancelled:
            self.status_label.configure(
                text="サマリー生成をキャンセルしました（文字起こしは保存済み）",
                text_color="#FCD34D",
                fg_color="#3B2F12"
            )
        elif errors:
            self.status_label.configure(
                text=f"文字起こし完了 / サマリーは{len(errors)}件失敗",
                text_color="#FDBA74",
                fg_color="#3B2412"
            )
        else:
            self.status_label.configure(
                text="文字起こし完了 / AIサマリー保存完了",
                text_color="#BBF7D0",
                fg_color="#12351F"
            )

    def _record_summary_outputs(self):
        """生成済みサマリーのパスを履歴に記録する"""
        summary_by_output = {
            r.get("output"): r.get("summary_output")
            for r in self._batch_results
            if r.get("summary_output")
        }
        if not summary_by_output:
            return
        history = load_history()
        changed = False
        for entry in history:
            summary_path = summary_by_output.get(entry.get("output"))
            if summary_path and entry.get("summary") != summary_path:
                entry["summary"] = summary_path
                changed = True
        if changed:
            save_history(history)
            self.load_history_display()

    # ==================== 履歴 ====================
    def load_history_display(self):
        for widget in self.history_list.winfo_children():
            widget.destroy()
        history = load_history()
        if not history:
            ctk.CTkLabel(
                self.history_list, text="履歴はありません",
                font=ctk.CTkFont(size=12), text_color="gray"
            ).pack(pady=4)
            return
        for entry in reversed(history[-10:]):
            row = ctk.CTkFrame(self.history_list, fg_color="transparent")
            row.pack(fill="x", pady=1)
            ctk.CTkLabel(
                row,
                text=f"{entry['date']}  {entry['file']}  ({entry['model']})",
                font=ctk.CTkFont(size=11), text_color="gray70", anchor="w"
            ).pack(side="left", fill="x", expand=True)
            if os.path.exists(entry.get("output", "")):
                path = entry["output"]
                btn = ctk.CTkButton(
                    row, text="開く", font=ctk.CTkFont(size=10),
                    height=20, width=40, fg_color="gray30",
                    command=lambda p=path: subprocess.run(["open", "-R", p])
                )
                btn.pack(side="right", padx=(4, 0))
                show_btn = ctk.CTkButton(
                    row, text="表示", font=ctk.CTkFont(size=10),
                    height=20, width=40, fg_color="gray30",
                    command=lambda e=entry: self._show_history_entry(e)
                )
                show_btn.pack(side="right", padx=(4, 0))

    def _show_history_entry(self, entry):
        """履歴の文字起こし・サマリーをプレビュータブに読み込む"""
        if self.processing or self._summary_running:
            return
        output = entry.get("output", "")
        try:
            with open(output, "r", encoding="utf-8") as f:
                transcript = f.read()
        except OSError as e:
            self.status_label.configure(
                text=f"履歴ファイルを開けませんでした: {e}",
                text_color="#FCA5A5", fg_color="#3F1D24"
            )
            return
        summary_path = entry.get("summary", "")
        summary_text = "（この履歴にはサマリーがありません）"
        if summary_path and os.path.exists(summary_path):
            try:
                with open(summary_path, "r", encoding="utf-8") as f:
                    summary_text = f.read()
            except OSError:
                pass
        self._set_textbox_text(self.summary_text, summary_text)
        self.show_preview(transcript)
        self._output_path = output
        self.open_file_button.configure(state="normal")
        self.regen_summary_button.configure(state="disabled")
        self.status_label.configure(
            text=f"履歴を表示中: {entry.get('file', '')}",
            text_color="#CBD5E1", fg_color="#1F2937"
        )

    def add_history_entry(self, file_path, output_path, model):
        history = load_history()
        history.append({
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "file": os.path.basename(file_path),
            "output": output_path,
            "model": model
        })
        if len(history) > 50:
            history = history[-50:]
        save_history(history)
        self.load_history_display()

    def clear_history(self):
        save_history([])
        self.load_history_display()


if __name__ == "__main__":
    app = WhisperApp()
    app.mainloop()
