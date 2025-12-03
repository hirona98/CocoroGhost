# cocoro_ghost 詳細設計（ドラフト v0.2）

## 1. 概要

- 本書は、CocoroAI のコアコンポーネントである **cocoro_ghost** の詳細設計をまとめたもの。
- 役割:
  - LLM 呼び出し・記憶管理・内的思考（reflection）を担う。
  - REST API を通じて、UI やキャラクター表示コンポーネントから利用される。
- 想定:
  - 一人のユーザー専用システムとして設計し、DB スキーマには `user_id` を持たない。
  - API の `user_id` は初期実装では `"default"` 固定値のみ受け付け、将来マルチユーザー化する場合は互換性を考慮せずスキーマ変更・マイグレーションを行う前提とする。
- 参照ドキュメント:
  - 要件定義: `docs/要件定義.md`
  - プロンプト定義: `docs/cocoro_ghost_prompts.md`
  - API 仕様: `docs/cocoro_ghost_api.md`

---

## 2. プロジェクト構成（新規リポジトリ想定）

### 2.1 ルート構成

- 新規リポジトリ（例: `cocoro_ghost/`）を前提とし、以下のような構成とする。

```text
cocoro_ghost/
  README.md
  pyproject.toml / poetry.lock など
  cocoro_ghost/       # cocoro_ghost 本体の Python パッケージ
  tests/               # テストコード
  docs/                # 要件定義・API仕様・プロンプトなどのドキュメント
```

- `cocoro_ghost/`
  - cocoro_ghost 本体の Python パッケージ。
- `tests/`
  - cocoro_ghost 向けのテストコードを配置。

### 2.2 `cocoro_ghost/` 配下構成

```text
cocoro_ghost/
  __init__.py
  main.py          # FastAPI アプリのエントリポイント
  config.py        # 設定読み込み（トークン、DB、モデル名など）
  db.py            # DB 接続とセッション管理
  models.py        # ORM モデル定義（episodes / persons / episode_persons）
  schemas.py       # API入出力用 Pydantic モデル
  llm_client.py    # LiteLLM ラッパ
  prompts.py       # プロンプト文字列の読み込み・管理
  reflection.py    # reflection 生成と JSON パース処理
  memory.py        # エピソード生成・保存・人物更新ロジック
  api/
    __init__.py
    chat.py        # /chat エンドポイント
    notification.py# /notification エンドポイント
    meta_request.py# /meta_request エンドポイント
    capture.py     # /capture エンドポイント
    episodes.py    # /episodes エンドポイント
    settings.py    # /settings, /presets エンドポイント（設定・プリセット管理）
```

各モジュールの役割は以下の通り。

#### `main.py`

- FastAPI アプリケーションの生成。
- ルーター登録:
  - `/chat`, `/notification`, `/meta_request`, `/capture`, `/episodes`
- 認証ミドルウェア（固定トークンチェック）の組み込み。

#### `config.py`

- 設定の集中管理。
  - API トークン
  - DB 接続文字列（初期は SQLite ファイルパス）
  - 使用する LLM モデル名
  - 埋め込みモデル設定
  - ログレベル など
- 実装イメージ:
  - 設定ファイル（例: `config/ghost.toml`）を読み込むシンプルなクラス／関数。

#### `db.py`

- SQLAlchemy などの ORM を用いて DB エンジンとセッションを管理。
- 初期は SQLite を想定し、ベクタ検索には SQLite 拡張の sqlite-vec を利用する。
- episodes の `episode_embedding` に対する類似検索を sqlite-vec 経由で行い、必要に応じてベクタインデックス用のテーブル／ビューを追加する。

#### `models.py`

- `episodes` テーブル:
  - 要件定義に基づき、エピソードのカラム（source, user_text, image_summary, emotion_label, salience_score など）を定義。
- `persons` テーブル:
  - 人物プロフィール（is_user, name, relation_to_user, status_note, 各種スコアなど）を定義。
- `episode_persons` テーブル:
  - エピソードと人物の多対多関係を表現し、役割（role）などを追加。
- `setting_presets` テーブル:
  - プリセット管理用テーブル。名前付きでLLM設定・振る舞い設定を保存。
  - `name` (UNIQUE), `is_active` (BOOLEAN), LLM設定各項目、振る舞い設定各項目を含む。
  - `is_active = TRUE` は1行のみ（UNIQUEインデックスで保証）。

#### `schemas.py`

- `ChatRequest`, `ChatResponse`
- `NotificationRequest`, `NotificationResponse`
- `MetaRequestRequest`, `MetaRequestResponse`
- `CaptureRequest`, `CaptureResponse`
- `EpisodeSummary` など

API 仕様（`docs/cocoro_ghost_api.md`）に合わせて定義する。

#### `llm_client.py`

- LiteLLM を利用してLLMプロバイダにリクエストを行うラッパ。
- 責務:
  - チャット用 LLM 呼び出し（キャラクターとしての対話）
  - reflection 用 LLM 呼び出し（JSON 生成）
  - 埋め込み生成（テキスト／将来的にはマルチモーダル）
- エラー時は例外を投げ、上位層でログ＋停止処理を行う（フォールバックは行わない方針）。

インターフェース案（Python 疑似コード）:

```python
class LlmClient:
    def generate_reply(
        self,
        system_prompt: str,
        conversation: list[dict],
        temperature: float = 0.7,
    ) -> str:
        """
        パートナーとしての返答を 1 メッセージぶん生成する。
        conversation は {"role": "user|partner|system", "content": "..."} のリストを想定。
        エラー時は例外を送出する。
        """

    def generate_reflection(
        self,
        system_prompt: str,
        context_text: str,
        image_descriptions: list[str] | None = None,
    ) -> dict:
        """
        reflection 用プロンプトテキストを受け取り、JSON 形式の内的思考を生成する。
        戻り値は Python の dict として返す（構造の検証やドメイン構造体への変換は呼び出し元で行う）。
        image_descriptions には、画像要約テキストのリストを入れる想定。
        エラー時は例外を送出する。
        """

    def generate_embedding(
        self,
        texts: list[str],
        images: list[bytes] | None = None,
    ) -> list[list[float]]:
        """
        エピソード用の埋め込みベクトルを生成する。
        初期実装では texts のみを使い、images は未使用でもよい。
        戻り値は、各入力に対応するベクトル（float のリスト）のリストとする。
        エラー時は例外を送出する。
        """

    def generate_image_summary(
        self,
        images: list[bytes],
    ) -> list[str]:
        """
        画像から日本語の要約テキストを生成する。
        /chat でユーザーが画像を送ってきた場合や、/capture での静的要約に利用する。
        戻り値は、各画像に対応する要約文のリストとする。
        エラー時は例外を送出する。
        """
```

#### `prompts.py`

- `docs/cocoro_ghost_prompts.md` に記述したプロンプトをコード側で扱うためのヘルパ。
- 初期案:
  - シンプルに Python の文字列として定義するか、
  - Markdown ファイルから必要なブロックを読み込む関数を用意する。

#### `reflection.py`

- `llm_client.LlmClient` を利用して reflection 用 LLM に問い合わせる高レイヤモジュール。
- 責務:
  - 要件定義・プロンプト定義に基づき、reflection 用プロンプト文字列を組み立てる。
  - LLM から得た JSON 文字列／dict をスキーマに沿って検証し、アプリ内部で扱いやすい構造体（例: `EpisodeReflection`）へ変換する。
- 戻り値のイメージ:
  - `reflection_text`, `emotion_label`, `emotion_intensity`,
    `topic_tags`, `salience_score`, `episode_comment`, `persons[...]`
- JSON パースやバリデーションに失敗した場合はエラーとして扱い、処理を停止する（フォールバックや自動補正は行わない）。

#### `memory.py`

- 主要なビジネスロジックを担当。
- 主な責務:
  - エピソード生成パイプライン:
    - 入力（chat/notification/meta_request/capture）から、
      - 必要に応じて要約を生成
      - reflection を生成
      - 埋め込みを生成
    - episodes / persons / episode_persons へ保存・更新
  - 人物プロフィール更新:
    - reflection の `persons` 情報から、各人物のスコア・status_note を更新。

#### `api/` 各モジュール

- `chat.py`:
  - `/chat` の FastAPI ルート定義。
  - `ChatRequest` を受け取り、`memory` に処理を委譲し、`ChatResponse` を返す。
- `notification.py`:
  - `/notification` のルート。
  - 通知内容を受け取り、エピソード＋ユーザーへの案内文を生成。
- `meta_request.py`:
  - `/meta_request` のルート。
  - 指示＋ペイロードを受け取り、ユーザー向けの説明と感想を生成。
- `capture.py`:
  - `/capture` のルート。
  - 画像パスと context_text を受け取り、要約＋エピソード生成。
- `episodes.py`:
  - `/episodes` のルート。
  - 振り返り用に、簡易なエピソード一覧を返す。

---

## 3. DB スキーマ詳細

### 3.1 `episodes` テーブル

- エピソード（出来事・瞬間）を表すテーブル。

| カラム名           | 型        | 必須 | 説明 |
|--------------------|-----------|------|------|
| id                 | INTEGER   | PK   | エピソードID |
| occurred_at        | DATETIME  | Yes  | エピソード発生時刻（UTC想定） |
| source             | TEXT      | Yes  | 発生源（`chat`, `desktop_capture`, `camera_capture`, `notification`, `meta_request` など） |
| user_text          | TEXT      | No   | ユーザーの発話やテキスト状況 |
| reply_text         | TEXT      | No   | パートナーからユーザーへの返答テキスト（chat/meta_request/notification 等） |
| image_summary      | TEXT      | No   | 画像（デスクトップ／カメラ）からの要約テキスト |
| activity           | TEXT      | No   | おおまかな活動（読書／仕事／ゲーム／移動 等） |
| context_note       | TEXT      | No   | 場所・時間帯・天気などの自由記述 |
| emotion_label      | TEXT      | No   | `joy`, `sadness`, `anger`, `fear`, `neutral` など |
| emotion_intensity  | REAL      | No   | 感情の強さ（0.0〜1.0） |
| topic_tags         | TEXT      | No   | 主なトピックのタグ列（例: `"仕事, 読書, 家族"`。実装上はカンマ区切り or JSON 文字列） |
| reflection_text    | TEXT      | Yes  | 内的思考（reflection）のテキスト |
| reflection_json    | TEXT      | Yes  | reflection の元となる JSON 全体（将来の再解釈や再集計のために保存） |
| salience_score     | REAL      | Yes  | 印象スコア（0.0〜1.0） |
| episode_embedding  | BLOB/TEXT | No   | エピソード埋め込みベクトル（バイナリ or JSON 文字列） |
| raw_desktop_path   | TEXT      | No   | デスクトップ画像のファイルパス（最大72時間有効） |
| raw_camera_path    | TEXT      | No   | カメラ画像のファイルパス（最大72時間有効） |
| created_at         | DATETIME  | Yes  | レコード作成時刻 |
| updated_at         | DATETIME  | Yes  | レコード更新時刻 |

### 3.2 `persons` テーブル

- 登場するすべての人物（ユーザー本人を含む）のプロフィールを表すテーブル。

| カラム名             | 型        | 必須 | 説明 |
|----------------------|-----------|------|------|
| id                   | INTEGER   | PK   | 人物ID |
| is_user              | BOOLEAN   | Yes  | ユーザー本人かどうか |
| name                 | TEXT      | Yes  | 代表的な名前（フルネーム／よく使う呼び名など） |
| aliases              | TEXT      | No   | その他の名前やハンドルネーム（カンマ区切りなどで複数保持） |
| display_name         | TEXT      | No   | キャラクターが呼ぶときの呼び方 |
| relation_to_user     | TEXT      | No   | ユーザーとの関係性（家族／友人／同僚／推し 等） |
| relation_confidence  | REAL      | No   | 関係性の確からしさ（0.0〜1.0） |
| residence            | TEXT      | No   | 居住地や生活拠点 |
| occupation           | TEXT      | No   | 職業・立場 |
| first_seen_at        | DATETIME  | No   | 初登場時刻 |
| last_seen_at         | DATETIME  | No   | 最終登場時刻 |
| mention_count        | INTEGER   | No   | 言及された回数 |
| topic_tags           | TEXT      | No   | その人物に関連する主な話題 |
| status_note          | TEXT      | No   | 現在の状況の要約（仕事・体調・家族構成 等） |
| closeness_score      | REAL      | No   | CocoroAI がその人物をどれくらい身近・親しい存在として感じているか（0.0〜1.0） |
| worry_score          | REAL      | No   | その人物の状態や振る舞いが、ユーザーや生活全体にどれくらい影響しそうか／どれだけ気がかりか（0.0〜1.0） |
| profile_embedding    | BLOB/TEXT | No   | その人物全体像の埋め込みベクトル |
| created_at           | DATETIME  | Yes  | レコード作成時刻 |
| updated_at           | DATETIME  | Yes  | レコード更新時刻 |

### 3.3 `episode_persons` テーブル

- エピソードと人物を紐づける中間テーブル（多対多）。

| カラム名    | 型      | 必須 | 説明 |
|------------|---------|------|------|
| episode_id | INTEGER | Yes  | エピソードID（FK -> episodes.id） |
| person_id  | INTEGER | Yes  | 人物ID（FK -> persons.id） |
| role       | TEXT    | No   | そのエピソードにおける役割（主人公／相手／話題に出ただけ 等） |

- 主キーは `(episode_id, person_id)` の複合主キーとする。

### 3.4 生画像パスとクリーンアップ方針

- `raw_desktop_path` / `raw_camera_path` に保存する生画像パスは、要件定義にある通り最大 72 時間まで有効とする。
- 実装方針:
  - 単純なバッチ／バックグラウンドタスク（例: 5〜10 分おき）で、現在時刻から 72 時間以上前のエピソードを検索し、対応するファイルを削除する。
  - 削除対象ファイルの `raw_desktop_path` / `raw_camera_path` は `NULL` に更新する。
  - 削除処理で例外が発生した場合はログを残し、プロセスを停止させて運用者が原因を確認できるようにする（サイレントなリトライやフォールバックは行わない）。

---

## 4. テスト方針

### 4.1 テスト構成

- ディレクトリ:
  - `tests/`
- テストレベル:
  1. API レベルテスト:
     - FastAPI の TestClient を用いて、各エンドポイントが 200 を返し、
       レスポンス JSON が期待されたスキーマを満たすかを確認。
  2. メモリ・DB ロジックテスト:
     - `memory.py` のエピソード生成・保存が正しく動作するか。
     - reflection の JSON を流し込み、`persons` のスコア更新が期待通りかを確認。
  3. LLM 呼び出しは基本モック:
     - 実際の API 呼び出しは行わず、決め打ちの JSON／テキストを返すテストを中心にする。

### 4.2 テストの進め方

- ステップごとに以下のように追加していく:
  1. `/chat` の最小実装 → `/chat` 用の API テストを追加。
  2. DB スキーマ実装 → シンプルな保存・読み出しテスト。
  3. reflection・memory 実装 → 典型的な入力に対する更新テスト。

---

## 5. 実装ステップ（ロードマップ）

1. **雛形 API 実装**
   - `cocoro_ghost/main.py` と `api/chat.py` を作成し、`/chat` がダミー応答を返すところまで。
2. **DB スキーマ実装**
   - `db.py`, `models.py` を実装し、`episodes` / `persons` / `episode_persons` のテーブル定義を追加。
3. **schemas・config 実装**
   - `schemas.py` に API リクエスト／レスポンスモデルを定義。
   - `config.py` にトークン・DB・モデル設定を集中管理。
4. **LLM クライアント実装**
   - `llm_client.py` で、chat・reflection・埋め込み呼び出しのインターフェースを整備（中身はモックから始めても良い）。
5. **reflection・memory 実装**
   - `reflection.py` と `memory.py` を実装し、`/chat` からエピソード生成まで一連の流れを通す。
6. **他 API 実装**
   - `/notification`, `/meta_request`, `/capture`, `/episodes` のルートと処理を順に追加。
7. **認証・設定・ログ整備**
   - 固定トークンのチェック、設定読み込み、主要イベントのログ出力を整備。

---

## 6. `/chat` フロー詳細

ユーザーとの通常会話を行う `/chat` エンドポイントの処理フローを示す。

1. **API レイヤ（`api/chat.py`）**
   - `POST /chat` で `ChatRequest` を受信:
     - `user_id`
     - `text`
     - `context_hint`
   - 認証トークンを検証。
   - リクエスト内容を `memory.handle_chat(request)` のような関数へ委譲。

2. **メモリレイヤ（`memory.py`）でのフロー**
   1. **コンテキスト収集**
      - 直近のエピソードから、必要な文脈（最近の会話、関連する人物情報など）を取得。
      - ユーザー本人の `persons` プロフィールを読み込み、必要なら会話用のヒントを組み立てる。
   2. **パートナーとしての返答生成**
      - もしリクエストに画像（`image_path`）が含まれている場合:
        - 画像を読み込み、`LlmClient.generate_image_summary([...])` により日本語の要約テキストを生成し、`image_summary` として保持する。
      - 画像の有無にかかわらず、ユーザー発話と `image_summary`（あれば）を背景情報として扱う。
      - `prompts.py` からキャラクター用システムプロンプトを取得。
      - 直近の会話履歴と今回の `text` を `conversation` として構築。
      - `LlmClient.generate_reply(system_prompt, conversation)` を呼び出し、`reply_text` を得る。
   3. **reflection 生成**
      - ユーザー発話、`reply_text`、直近のコンテキストをまとめた `context_text` を組み立てる。
      - 画像がある場合は、その要約（`image_summary`）を `image_descriptions` として渡す。
      - `LlmClient.generate_reflection(system_prompt_reflection, context_text, image_descriptions)` を呼び出し、reflection 用 JSON を取得する。
      - reflection JSON のバリデーションに失敗した場合はエラーとして扱い、処理を停止する。
      - 検証済み JSON から `reflection_text`, `emotion_label`, `emotion_intensity`, `topic_tags`, `salience_score` などを取り出しつつ、元の JSON 全体も `reflection_json` として保持する。
   5. **埋め込み生成**
      - エピソードのためのテキスト（ユーザー発話、`reply_text`, `reflection_text`, `image_summary` など）を連結または適切にまとめた文字列を作成。
      - `LlmClient.generate_embedding([episode_text])` を呼び出し、1 本のベクトルを取得。
   6. **DB への保存・更新**
      - `episodes` テーブルに新しいレコードを作成:
        - `occurred_at`, `source="chat"`, `user_text`, `reply_text`, `image_summary`, `activity`, `context_note`,
          `emotion_label`, `emotion_intensity`, `topic_tags`, `reflection_text`, `reflection_json`, `salience_score`,
          `episode_embedding`, `raw_desktop_path`, `raw_camera_path`（通常は NULL）などを保存。
      - reflection JSON の `persons` 配列を用いて、`persons` テーブルの各人物レコードを更新:
        - `first_seen_at` / `last_seen_at` / `mention_count` の更新
        - `status_note` の更新（必要な場合）
        - 各スコア（`closeness_score`, `worry_score`）への delta 適用
      - `episode_persons` テーブルに、今回のエピソードと関係する人物の紐づけを追加。

3. **レスポンス生成**
   - `memory` モジュールから `reply_text` と `episode_id` を受け取り、`ChatResponse` を構築。
   - API として以下を返す:
     - `reply_text`: パートナーからユーザーへの返答。
     - `episode_id`: 記録されたエピソードの ID。

### 6.1 同期／非同期と直列処理の方針

- ユーザー体験としては「返答と記憶がセットで成立する」ことを重視するが、
  レイテンシを抑えるために以下の方針とする。

- 同期処理（HTTP レスポンスが返るまでに必ず行う）:
  - 画像要約（`generate_image_summary`、画像がある場合）
  - パートナーとしての返答生成（`generate_reply`）
- 非同期処理（HTTP レスポンス返却後にバックグラウンドで行う）:
  - reflection 生成（`generate_reflection`）
  - 埋め込み生成（`generate_embedding`）と episodes / persons / episode_persons の保存・更新

- 直列性の確保:
  - ある `/chat` リクエスト A に対する reflection・記憶処理（3〜4）が完了していない間は、
    次の `/chat` リクエスト B は「受け付けはするが、実際の処理開始は A の 3〜4 完了後」とする。
  - 実装上は、ユーザーごとに 1 本のキュー／ロックで直列化することを想定する。

- 異常時の扱い:
  - reflection や埋め込み生成、episodes / persons / episode_persons への保存処理で例外が発生した場合は、
    その例外を致命的エラーとして扱い、ログ出力後にプロセスを停止させる（ヘルスチェックがあれば NG とし、監視・再起動の対象とする）。
  - すでにユーザーへの `reply_text` を返した後にバックグラウンド処理が失敗した場合でも、
    「返答は行ったが記憶は残せなかった」状態をフォールバックとして許容するのではなく、
    直後にサービス全体をエラー状態とし、新規リクエストは 5xx エラーで失敗させる。
  - 自動リトライや簡易な代替処理は行わず、異常系では必ず停止＋運用者による原因調査を前提とする。

---

## 7. `/notification` フロー詳細

外部システム（メーラーなど）からの通知を受け取り、ユーザーに伝えるメッセージとエピソードを生成する。

1. **API レイヤ（`api/notification.py`）**
   - `POST /notification` で `NotificationRequest` を受信:
     - `source_system`
     - `title`
     - `body`
     - `image_url`（任意）
   - 認証トークンを検証。
   - リクエスト内容を `memory.handle_notification(request)` へ委譲。

2. **メモリレイヤ（`memory.py`）でのフロー**
   1. **通知内容の整理**
      - `source_system`, `title`, `body` から、通知の要約テキストを組み立てる。
      - 通知の種類に応じて、おおまかな `activity`（メール／予定／システム通知 等）を推定しておく。
   2. **画像要約（あれば）**
      - `image_url` から画像が取得できる場合は読み込み、`generate_image_summary` により要約テキストを生成し、通知の背景情報として扱う。
   3. **パートナーとしての返答生成**
      - 通知の要約＋画像要約（あれば）をもとに、ユーザーに伝えるメッセージの骨格を作る。
      - キャラクター用システムプロンプトを用い、`generate_reply` で「どこからの通知か→内容の要約→一言コメント」を含む `speak_text` を生成する。
   4. **reflection 生成（非同期）**
      - 通知の内容・`speak_text`・関連人物（メールの差出人などが分かる場合）をまとめた `context_text` を組み立てる。
      - `generate_reflection` を呼び出し、reflection JSON を取得する。
   5. **埋め込み生成と保存（非同期）**
      - 通知の要約・`speak_text`・`reflection_text` などからエピソードテキストを作成し、`generate_embedding` を呼ぶ。
      - `episodes` テーブルに `source="notification"` でレコードを追加し、必要に応じて差出人などの人物を `persons` / `episode_persons` に反映する。

3. **レスポンス生成**
   - `memory` から `speak_text` と `episode_id` を受け取り、`NotificationResponse` を返す。

同期／非同期の扱いは `/chat` と同様に、ユーザーへの `speak_text` は同期、reflection・記憶処理は非同期（ただし直列化）とする。

---

## 8. `/meta_request` フロー詳細

外部から「こういう説明・振る舞いをしてほしい」というメタ指示を受け取り、ユーザー向けのメッセージとエピソードを生成する。

1. **API レイヤ（`api/meta_request.py`）**
   - `POST /meta_request` で `MetaRequestRequest` を受信:
     - `instruction`
     - `payload_text`
     - `image_url`（任意）
   - 認証トークンを検証。
   - リクエスト内容を `memory.handle_meta_request(request)` へ委譲。

2. **メモリレイヤ（`memory.py`）でのフロー**
   1. **入力情報の整理**
      - `instruction` と `payload_text` から、ユーザーに伝えるべき内容と目的を整理する。
   2. **画像要約（あれば）**
      - `image_url` から画像が取得できる場合は読み込み、`generate_image_summary` により要約テキストを生成し、`payload_text` に付加情報として扱う。
   3. **パートナーとしての返答生成**
      - キャラクター用システムプロンプトと `instruction`＋`payload_text`＋画像要約を組み合わせ、
        ユーザーに対して自然な形で説明・要約・感想を伝える `speak_text` を `generate_reply` で生成する。
      - ユーザーから見ると、外部の指示ではなく、パートナー自身の提案・気づきとして聞こえるようにする。
   4. **reflection 生成（非同期）**
      - `instruction`・`payload_text`・`speak_text` を含む `context_text` を組み立て、`generate_reflection` を呼び出す。
      - その情報がユーザーや人物プロフィールにどう影響しそうかを reflection に含める。
   5. **埋め込み生成と保存（非同期）**
      - 関連テキストからエピソードテキストを作成し、`generate_embedding` を呼び出す。
      - `episodes` テーブルに `source="meta_request"` でレコードを追加する。
      - 必要に応じて、ニュースやトピックに関連する人物を `persons` / `episode_persons` に反映する。

3. **レスポンス生成**
   - `memory` から `speak_text` と `episode_id` を受け取り、`MetaRequestResponse` を返す。

同期／非同期、直列化の方針は `/chat` と同様とする。

---

## 9. 設定（config）設計

### 9.1 設定の読み込み方針

- **ハイブリッド設定管理方式**:
  - **起動設定**: TOMLファイル（`config/setting.toml`）で管理
    - 環境変数には依存せず、アプリ起動に必須の設定のみ記述
  - **動的設定**: SQLiteデータベースで管理（プリセット機能）
    - LLMモデル設定、API Key、プロンプト、振る舞い設定など
    - API経由で動的に変更可能
- `cocoro_ghost.config` モジュールで一元管理し、アプリ内の他の箇所では設定ファイルを直接扱わない。

### 9.2 設定項目

#### TOML設定（起動時必須）

| キー名        | 例                         | 用途 |
|--------------|----------------------------|------|
| `token`      | `"secret-token-123"`       | REST API 認証用の固定トークン |
| `db_url`     | `"sqlite:///./data/ghost.db"` | DB 接続文字列（初期は SQLite） |
| `log_level`  | `"INFO"`                   | ログレベル（DEBUG/INFO/WARN/ERROR） |
| `env`        | `"dev"` / `"prod"`         | 実行環境（開発／本番などの切り替え） |

#### DBプリセット設定（動的変更可能）

| 項目                        | 例                         | 用途 |
|----------------------------|----------------------------|------|
| `llm_api_key`              | `"sk-..."`                 | LLM API キー（プリセット毎） |
| `llm_model`                | `"gemini/gemini-2.5-flash"` | パートナー返答用の LLM モデル名 |
| `reflection_model`         | `"gemini/gemini-2.5-flash"` | reflection 生成用モデル名 |
| `embedding_model`          | `"gemini/gemini-embedding-001"` | 埋め込み生成用モデル名 |
| `embedding_dimension`      | `3072`                     | 埋め込みベクトルの次元数 |
| `image_model`              | `"gemini/gemini-2.5-flash"` | 画像解析用モデル名 |
| `image_timeout_seconds`    | `60`                       | 画像処理タイムアウト |
| `character_prompt`         | `"..."` | キャラクター設定プロンプト |
| `intervention_level`       | `"high"` / `"low"`         | 介入レベル |
| `exclude_keywords`         | `["パスワード", "銀行"]`   | 除外キーワードリスト |
| `similar_episodes_limit`   | `5`                        | 類似エピソード検索上限 |
| `max_chat_queue`           | `10`                       | `/chat` キューの最大長 |

### 9.3 プリセット管理

- **プリセット**: LLM設定と振る舞い設定のセット
  - 名前付きで複数保存可能（例: "default", "work", "casual"）
  - 1つのプリセットを「アクティブ」として選択
  - API経由でプリセットの作成・更新・削除・切り替えが可能

- **初回起動時の動作**:
  - TOMLにLLM設定が記述されている場合、自動的に"default"プリセットを作成
  - 2回目以降はDBのプリセットが優先され、TOMLのLLM設定は無視される

- **プリセット切り替え**:
  - APIで切り替え可能だが、LLMクライアントの再初期化が必要なため**アプリの再起動が必要**

### 9.4 config モジュールの構成

- **Config** (dataclass): TOML起動設定
- **RuntimeConfig** (dataclass): TOML + DBプリセット設定のマージ結果
- **ConfigStore**: ランタイム設定ストア（スレッドセーフ）
- **load_config()**: TOMLファイルから起動設定を読み込み
- **merge_toml_and_preset()**: TOMLとDBプリセットをマージ
- **migrate_toml_to_db_if_needed()**: 初回起動時の自動マイグレーション
