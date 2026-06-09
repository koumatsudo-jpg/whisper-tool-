import customtkinter as ctk
import threading
import concurrent.futures
import os
import json
import subprocess
import tempfile
import urllib.error
import urllib.request
from tkinter import filedialog
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
    "model": "llama3.1",
    "api_key": "",
}

BACKEND_DEFAULT_MODELS = {
    "ollama": "llama3.1",
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
        "以下の会議・会話の文字起こしを日本語で要約してください。\n\n"
        "出力形式:\n"
        "1. 議題\n"
        "2. 決定事項\n"
        "3. ToDo\n"
        "4. 発言者ごとの要点\n"
        "5. 補足・未決事項\n\n"
        "話者ラベルとタイムスタンプは必要な箇所だけ残してください。\n\n"
        "文字起こし:\n"
        f"{transcript}"
    )


def _api_key_for_backend(backend, settings):
    if backend == "claude":
        return os.environ.get("ANTHROPIC_API_KEY") or settings.get("api_key", "")
    if backend == "openai":
        return os.environ.get("OPENAI_API_KEY") or settings.get("api_key", "")
    return ""


def summarize_text(transcript, settings):
    backend = settings.get("backend", "ollama")
    model = settings.get("model") or BACKEND_DEFAULT_MODELS.get(backend, "llama3.1")
    prompt = _summary_prompt(transcript)

    if backend == "ollama":
        try:
            data = _json_post(
                "http://localhost:11434/api/generate",
                {"model": model, "prompt": prompt, "stream": False},
                timeout=180,
            )
        except RuntimeError as e:
            raise RuntimeError(
                "Ollamaが見つかりません。`brew install ollama` 後に "
                f"`ollama pull {model}` を実行し、Ollamaを起動してください。\n\n{e}"
            ) from e
        return data.get("response", "").strip()

    if backend == "claude":
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
        parts = data.get("content", [])
        return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()

    if backend == "openai":
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
        self.geometry("750x800")
        self.resizable(False, False)

        # --- 状態 ---
        self.file_paths = []          # バッチ対象ファイルリスト
        self.processing = False
        self._temp_files = []

        # --- モデルキャッシュ ---
        self._diarization_pipeline = None
        self._whisper_repo = None  # mlx-whisper は関数ベースなので repo 名のみ保持

        # --- 進捗追跡 ---
        self._elapsed_timer_running = False
        self._start_time = None
        self._file_start_time = None

        # --- バッチ結果 ---
        self._batch_results = []
        self._batch_errors = []
        self._output_path = None
        self._summary_output_path = None
        self._summary_running = False
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
        self.model_var = ctk.StringVar(value=recommended_model_for_memory(_low_memory_default))
        self.model_menu = ctk.CTkOptionMenu(
            self.controls_frame,
            variable=self.model_var,
            values=["tiny", "base", "small", "medium", "distil-large-v3", "large-v3-turbo"],
            width=180
        )
        self.model_menu.pack(side="left")

        self.run_button = ctk.CTkButton(
            self.controls_frame, text="文字起こし開始",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=36, width=180,
            fg_color="#2563EB", hover_color="#1D4ED8",
            command=self.start_transcribe
        )
        self.run_button.pack(side="right")

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

        self.preview_tabs = ctk.CTkTabview(self.preview_section, fg_color="#111827")
        self.preview_tabs.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        self.preview_tabs.add("文字起こし")
        self.preview_tabs.add("サマリー")

        self.preview_text = ctk.CTkTextbox(
            self.preview_tabs.tab("文字起こし"),
            font=ctk.CTkFont(size=12),
            state="disabled", fg_color="#0B1120"
        )
        self.preview_text.pack(fill="both", expand=True, padx=8, pady=8)

        self.summary_text = ctk.CTkTextbox(
            self.preview_tabs.tab("サマリー"),
            font=ctk.CTkFont(size=12),
            state="disabled", fg_color="#0B1120"
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
        self.after(1000, self._update_elapsed)

    def stop_elapsed_timer(self):
        self._elapsed_timer_running = False

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

    def open_result_file(self):
        if self._output_path and os.path.exists(self._output_path):
            os.system(f'open "{self._output_path}"')

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
        self._batch_results = []
        self._batch_errors = []
        self._output_path = None
        self._summary_output_path = None
        self._file_start_time = None
        self.run_button.configure(state="disabled")
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
                    "pyannote/speaker-diarization-3.1",
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
        if ext == WAV_EXT:
            audio_path = file_path
        else:
            self.after(0, self.update_progress, 2, "音声を WAV に変換中...")
            temp_audio = tempfile.mktemp(suffix=".wav")
            self._temp_files.append(temp_audio)
            extract_audio(file_path, temp_audio)
            audio_path = temp_audio
            self.after(0, self.update_progress, 5, "音声変換完了")

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
                d_future.result()
                t_future.result()
        elif diarization_enabled:
            do_diarization()
            self.after(0, self.update_progress, 45, "話者分離完了")
            do_transcription()
        else:
            self.after(0, self.update_progress, 18, "文字起こしを開始（話者分離スキップ）...")
            do_transcription()

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
        model_size = self.model_var.get()
        total_files = len(self.file_paths)

        # モデル準備（全バッチで1回）
        try:
            self._ensure_models(model_size)
        except Exception as e:
            self.after(0, self._on_fatal_error, e)
            return

        for idx, file_path in enumerate(self.file_paths, start=1):
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
            except Exception as e:
                self._batch_errors.append({
                    "file": file_path,
                    "error": str(e),
                    "detail": traceback.format_exc(),
                })
            finally:
                self.cleanup_temp()

        batch_elapsed = time.time() - self._start_time
        self.after(0, self.on_batch_complete, batch_elapsed)

    def _on_fatal_error(self, error):
        """モデルロード失敗など致命的エラー"""
        self.stop_elapsed_timer()
        self.processing = False
        self.run_button.configure(state="normal")
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
        self.show_preview(f"モデルの読み込みに失敗しました:\n\n{str(error)}\n\n{traceback.format_exc()}")

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

        if error_count == 0:
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

        # サマリーテキスト生成
        summary_lines = [
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "  バッチ処理完了",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            f"総所要時間: {bm}分{bs:02d}秒",
            f"成功: {success_count}件 / 失敗: {error_count}件",
            "",
        ]

        for r in self._batch_results:
            fm = int(r["elapsed"] // 60)
            fs = int(r["elapsed"] % 60)
            summary_lines.append(f"✓ {os.path.basename(r['file'])}")
            summary_lines.append(f"  → {os.path.basename(r['output'])}（{fm}分{fs:02d}秒）")
            summary_lines.append("")

        for e in self._batch_errors:
            summary_lines.append(f"✗ {os.path.basename(e['file'])}")
            summary_lines.append(f"  エラー: {e['error']}")
            summary_lines.append("")

        self.show_preview("\n".join(summary_lines))
        self.progress.set(1)

        if self._batch_results:
            self.open_file_button.configure(state="normal")
            if self.summary_enabled_var.get():
                self._start_summary_generation()

    # ==================== AIサマリー ====================
    def _start_summary_generation(self):
        if self._summary_running:
            return
        self.summary_settings["summary_enabled"] = True
        save_settings(self.summary_settings)
        settings = self.summary_settings.copy()
        self._summary_running = True
        self.run_button.configure(state="disabled")
        self.model_menu.configure(state="disabled")
        self.clear_files_button.configure(state="disabled")
        self.summary_switch.configure(state="disabled")
        self.summary_settings_button.configure(state="disabled")
        self._refresh_file_list()
        backend = settings.get("backend", "ollama")
        model = settings.get("model") or BACKEND_DEFAULT_MODELS.get(backend, "llama3.1")
        self.status_label.configure(
            text=f"AIサマリー生成中...（{backend} / {model}）",
            text_color="#BFDBFE",
            fg_color="#172554"
        )
        self.show_summary("AIサマリーを生成中です...\n\n文字起こし結果は保存済みです。")
        thread = threading.Thread(
            target=self._run_summary_generation,
            args=(settings,),
            daemon=True
        )
        thread.start()

    def _run_summary_generation(self, settings):
        lines = []
        errors = []
        for idx, result in enumerate(self._batch_results, start=1):
            name = os.path.basename(result["file"])
            self.after(
                0,
                self._update_batch_status,
                idx,
                len(self._batch_results),
                result["file"]
            )
            try:
                summary = summarize_text(result.get("text", ""), settings)
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
                self.after(0, self.show_summary, "\n".join(lines))
            except Exception as e:
                message = str(e)
                errors.append({"file": result["file"], "error": message})
                lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
                lines.append(f"  {name}")
                lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
                lines.append("サマリー生成に失敗しました。")
                lines.append(message)
                lines.append("")
                self.after(0, self.show_summary, "\n".join(lines))

        self.after(0, self._on_summary_complete, errors)

    def _on_summary_complete(self, errors):
        self._summary_running = False
        self.run_button.configure(state="normal")
        self.model_menu.configure(state="normal")
        self.clear_files_button.configure(state="normal")
        self.summary_switch.configure(state="normal")
        self.summary_settings_button.configure(state="normal")
        self._refresh_file_list()
        if errors:
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
                    command=lambda p=path: os.system(f'open -R "{p}"')
                )
                btn.pack(side="right", padx=(4, 0))

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
