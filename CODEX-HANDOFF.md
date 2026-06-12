# Codex Handoff — whisper-tool WebUI フロントエンド実装

## 概要

ローカルで動くmacOS音声文字起こしツールのWebUIフロントエンドを実装してほしい。
バックエンド（FastAPI）は完成済み。`static/index.html` を中心としたシングルページアプリを作ること。

---

## バックエンド情報

- サーバー: `http://localhost:8080`
- ドキュメント: `http://localhost:8080/docs`（Swagger UI で全エンドポイント確認可能）
- フロントの置き場: `/Users/matsudo/whisper-tool-/static/`（`index.html` を置けば自動で配信される）

---

## APIリファレンス

### ファイル管理
```
GET    /api/files              → { files: [{index, path, name}] }
POST   /api/files              → body: { paths: ["/path/to/audio.m4a"] }
DELETE /api/files/{index}      → 1件削除
DELETE /api/files              → 全クリア
```

### 設定
```
GET    /api/settings           → { summary_enabled, backend, model, api_key,
                                   model_choices: [{label, value}],
                                   low_memory, recommended_model }
POST   /api/settings           → body: { summary_enabled?, backend?, model?, api_key? }
```

### 文字起こしジョブ
```
POST   /api/jobs               → body: { model, diarize, turbo, lightweight }
                                  開始。処理中は409。ファイル未選択は400。
DELETE /api/jobs/current       → キャンセル

GET    /api/status             → { processing, summary_running, cancel_requested,
                                   staged_files, batch_results, batch_errors }
```

### 結果
```
GET    /api/transcript         → { text: "..." }
GET    /api/summary            → { text: "..." }
POST   /api/summary/regenerate → body: { transcript?: "..." }  ※省略時は最新の文字起こしを使用
```

### 履歴
```
GET    /api/history            → { entries: [{id, timestamp, file, output, model, summary_output}] }
GET    /api/history/{id}       → entry + transcript + summary テキスト
POST   /api/history/{id}/open  → Finderで結果ファイルを開く
```

### フォルダブラウザ
```
GET    /api/browse             → デフォルト候補フォルダ一覧（Desktop/Downloads/Movies/Music/Documents）
GET    /api/browse?path=/Users/xxx/Movies
                               → { path, parent, dirs: [{name, path}], files: [{name, path, size}] }
```
- `parent` は一つ上のフォルダパス（ホームディレクトリなら null）
- `files` は音声・動画のみ（.wav .mp3 .m4a .mp4 .mov .flac .aac .mkv .avi .ogg .wma）
- ファイルを選んだら `POST /api/files` でステージングリストに追加

### SSEリアルタイムイベント
```
GET    /api/events             → text/event-stream
```

**イベント一覧:**
```json
{"type": "ping"}
{"type": "status",          "state": "loading|summarizing", "message": "..."}
{"type": "progress",        "current": 1, "total": 3, "file": "audio.m4a", "step": "transcribing"}
{"type": "progress_pct",    "pct": 45, "message": "文字起こし中... 1/2"}
{"type": "progress_detail", "message": "WAV変換中..."}
{"type": "transcript",      "text": "全文テキスト"}
{"type": "file_error",      "file": "audio.m4a", "message": "エラー内容"}
{"type": "complete",        "elapsed": 123.4, "success": 2, "errors": 0, "errors_detail": []}
{"type": "summary",         "text": "サマリー全文"}
{"type": "summary_complete"}
{"type": "cancelled"}
{"type": "error",           "message": "...", "detail": "..."}
{"type": "history_updated"}
```

---

## UI仕様

### 全体レイアウト（シングルページ）

```
┌─────────────────────────────────────────────────────┐
│  🎙 文字起こしツール                           [設定]  │  ← ヘッダー
├─────────────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────────┐   │
│  │  ここにファイルをドロップ                      │   │  ← ドロップゾーン
│  │  または [ファイルを選択]                       │   │
│  └─────────────────────────────────────────────┘   │
│                                                     │
│  audio1.m4a  [×]                                   │  ← ファイルリスト
│  audio2.wav  [×]                                   │
│                           [クリア]                  │
│                                                     │
│  モデル: [large-v3-turbo ▼]  話者分離[ON]  高速[ON]  │  ← オプション
│                                                     │
│  [▶ 文字起こし開始]              [■ キャンセル]       │  ← アクションボタン
│                                                     │
│  ████████████████░░░░ 45%  文字起こし中... 1/2      │  ← プログレスバー
│  経過: 0:32                                         │
├─────────────────────────────────────────────────────┤
│  [文字起こし]  [サマリー]  [履歴]                     │  ← タブ
│  ┌─────────────────────────────────────────────┐   │
│  │                                             │   │
│  │  [00:00] 話者1:                             │   │  ← 結果エリア
│  │  テキスト内容...                             │   │
│  │                                             │   │
│  └─────────────────────────────────────────────┘   │
│                    [コピー]  [ウィンドウで開く]        │
└─────────────────────────────────────────────────────┘
```

### 各パネルの詳細

**ファイル追加エリア**

```
┌─────────────────────────────────────────────────────────────┐
│  [ここにD&D]  [ファイルを選択]                               │
├─────────────────────────────────────────────────────────────┤
│  候補ファイル  [🔍 検索...]                    [↻ 再スキャン] │
│  🎵 会議録音_2026-06-10.m4a   1:23:45   約15分    [✚]      │
│  🎬 interview.mp4             0:45:00   約8分     [✚ 追加済]│
│  🎵 podcast_ep12.mp3          0:32:10   約6分     [✚]      │
│  ...                                                        │
├─────────────────────────────────────────────────────────────┤
│  追加済み                                        [全クリア]  │
│  🎵 会議録音_2026-06-10.m4a   1:23:45   約15分    [×]      │
│  ─────────────────────────────────────────────────────────  │
│  合計 1:23:45  →  約15分で完了（large-v3-turbo）            │
└─────────────────────────────────────────────────────────────┘
```

① **ドロップゾーン**
- ドラッグ&ドロップ対応（`dragover` / `drop` イベント）
- 複数ファイル対応

② **ファイル候補リスト**（重要：必ず実装すること）
- 履歴タブと同じようなカード/行スタイルのフラットリスト
- ページロード時に `GET /api/browse/scan` を呼び、Desktop・Downloads・Movies・Music・Documentsを自動スキャンした結果を表示
- 更新日時降順で表示（最近使ったファイルが上）
- 各行に: ファイル名、再生時間（duration秒 → `H:MM:SS` 形式）、処理時間概算、追加ボタン（✚）
- サイズ・フォルダ名は省略してシンプルに
- **処理時間概算の計算方法**:
  - `/api/settings` の `speed_factors[選択中モデル]` × `duration秒` = 推定処理秒数
  - 例: duration=5000秒、large-v3-turbo(0.18) → 900秒 → 「約15分」
  - モデル変更時に全行の概算を再計算して更新する
  - 表示形式: `約X分` （1分未満は `約1分以内`）
- 追加済みのファイルはチェックマーク＋グレーアウト
- 拡張子で音声（🎵）・動画（🎬）をアイコン分け
- 検索ボックスでファイル名絞り込み

③ **ファイル選択ダイアログ**
- `<input type="file" accept=".wav,.mp3,.m4a,.mp4,.mov,.flac,.aac,.mkv,.avi">` でクリック起動

**ステージングリスト（追加済みファイル一覧）**
- 各行に: ファイル名、再生時間、処理時間概算（候補リストと同じ計算式）、× ボタン
- リストの下に合計概算を表示:
  ```
  合計 2:08:45  →  約18分で完了（large-v3-turbo）
  ```
- モデル変更時に合計概算も再計算
- [全クリア] ボタン

**オプション行**
- モデル選択: `/api/settings` の `model_choices` でセレクトを生成
- 話者分離トグル（デフォルト ON）
- 高速モードトグル（デフォルト ON）
- 軽量モードチェック（メモリ節約、`low_memory` が true の場合デフォルト ON）

**プログレスバー**
- SSEの `progress_pct.pct` で更新
- 経過時間タイマー（開始時からカウントアップ）
- 処理中は「文字起こし開始」ボタンをグレーアウト、「キャンセル」を有効化

**タブ: 文字起こし**
- SSEの `transcript` イベントで自動更新
- テキストエリア（readonly、縦スクロール）

**タブ: サマリー**
- SSEの `summary` イベントで自動更新
- Markdownをそのまま表示（`<pre>` でOK、またはmarked.jsでレンダリング）
- `summary_enabled` が false の場合は「設定でサマリーをONにしてください」と表示
- [サマリー再生成] ボタン → `POST /api/summary/regenerate`

**タブ: 履歴**
- `GET /api/history` で一覧取得
- 各エントリにタイムスタンプ・ファイル名・モデル表示
- [表示] ボタン → `GET /api/history/{id}` で内容をモーダルまたは結果タブに表示
- [Finderで開く] → `POST /api/history/{id}/open`

**設定モーダル**（ヘッダーの[設定]ボタンで開く）
- サマリーON/OFFトグル
- バックエンド選択（ollama / claude / openai）
- モデル名テキスト入力
- APIキー入力（type="password"）
- **出力形式**: ラジオボタンで選択
  - `txt` — テキスト（デフォルト）
  - `md` — Markdown
  - `docx` — Word文書
- **出力フォルダ**: テキスト入力（空欄 = 入力ファイルと同じ場所）
  - プレースホルダー: `空欄の場合: 入力ファイルと同じフォルダに「文字起こし」フォルダを作成`
  - 出力構造のプレビューを小さく表示:
    ```
    {出力フォルダ}/文字起こし/
      audio1/
        audio1_文字起こし.{形式}
        audio1_サマリー.{形式}
    ```
- [保存] → `POST /api/settings`

---

## デザイン要件

### 方針
固定のデザインシステムを指定するのではなく、以下のリソースと原則を参照して**あなた自身がベストプラクティスを選択・合成**してください。

### 参照リソース
- **uiskills.com** — AIエージェント向けUIのSkill集。コンポーネントパターンを参照すること
- **loops.elorm.xyz** — 実際に使われているLoopのコピペ集。インタラクションパターンを参照すること
- **html2pptx.app/templates/stream-doodle-lightblue** — デザイントーンの参考（フレンドリー・プレイフル・ダイナミック）
- **Stream Doodle Lightblue のデザイン言語**:
  - パレット: light blue `#9ED4F2`、white `#ffffff`、navy `#143A5C`、yellow `#F2B33D`、pale `#E5F4FC`
  - タイポ: Noto Sans JP / Hiragino Sans、Heavy 800 見出し、可読性重視の本文
  - ビジュアル: 手描き風フラットイラスト、角丸カード、わずかな回転装飾、SVGウェーブアンダーライン

### 最低限守ること
- **フォント**: `'Noto Sans JP', 'Hiragino Sans', sans-serif`
- **レスポンシブ**: 最小幅 760px、中央寄せ max-width 960px
- **アクセシビリティ**: ボタンにはfocus ring、カラーコントラスト比 4.5:1 以上
- **日本語UI**: すべてのラベル・メッセージ・エラーは日本語

### 自由裁量
カラーテーマ（ライト/ダーク）、レイアウト構成、コンポーネントのスタイル、アニメーションの種類・強度はすべてあなたの判断に委ねます。上記リソースから最も品質の高いパターンを選んでください。

---

## 実装指示

1. `static/index.html` 1ファイルに HTML + CSS + JS をまとめること（外部CDN可）
2. SSEは `new EventSource('/api/events')` で接続し、全イベントをハンドリング
3. ページロード時に `GET /api/settings` と `GET /api/files` と `GET /api/history` を呼んで初期状態を復元
4. ファイルのD&Dは `dragover` で `preventDefault()` を忘れずに
5. キャンセルボタンは処理中のみ有効、通常時はグレーアウト
6. エラー発生時はトースト通知またはステータスバーに表示
7. コピーボタンは `navigator.clipboard.writeText()` を使用、完了後テキストを一時変更（「✓ コピー済み」）

---

## 参考情報

- セッション履歴: `/Users/matsudo/.claude/projects/-Users-matsudo-dev-new-company/d6779a6b-7c81-4c6c-9ddf-fbe47983975d.jsonl`
- バックエンド実装: `/Users/matsudo/whisper-tool-/server.py`
- 旧CTkアプリ: `/Users/matsudo/whisper-tool-/app.py`（機能の参考に）

---

## 完成条件

- `static/index.html` が存在し、`http://localhost:8080` でアクセスできる
- ファイルをD&Dまたは選択 → 文字起こし開始 → 進捗表示 → 結果タブに表示、の一連の流れが動く
- SSEイベントで進捗・結果がリアルタイムに反映される
- サマリータブ・履歴タブが動作する
- 設定モーダルから保存できる
