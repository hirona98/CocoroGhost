# cocoro_ghost 実装計画書

## 概要

本ドキュメントは、cocoro_ghostの実装を進めるための詳細な計画書です。
要件定義、詳細設計、API仕様、プロンプト定義の各ドキュメントに基づき、実装の優先順位と具体的な作業手順を定義します。

---

## 実装方針

### 基本原則

1. **フォールバック処理は実装しない**
   - エラーは停止させ、ログに記録する
   - サイレントな失敗や自動リカバリは行わない

2. **後方互換性は維持しない**
   - 必要に応じて非互換な変更を行う
   - スキーマ変更時は再計算・マイグレーションを前提とする

3. **シンプルさ最優先**
   - 過度な抽象化や将来のための準備コードは書かない
   - 必要になったときに拡張する

4. **異常系では停止**
   - LLM呼び出し失敗、DB保存失敗などは即座にエラーとする
   - プロセスを停止させ、監視・再起動は外部システムに任せる

---

## 実装フェーズ

### フェーズ1: 基盤構築（プロジェクト初期化〜基本API）

#### 1.1 プロジェクト構造の作成

**ディレクトリ構成:**
```
cocoro_ghost/
  README.md
  pyproject.toml
  requirements.txt
  config/
    ghost.toml
  cocoro_ghost/
    __init__.py
    main.py
    config.py
    db.py
    models.py
    schemas.py
    llm_client.py
    prompts.py
    reflection.py
    memory.py
    api/
      __init__.py
      chat.py
      notification.py
      meta_request.py
      capture.py
      episodes.py
  tests/
    __init__.py
    test_api.py
    test_memory.py
  data/
    (SQLiteデータベースファイルの格納先)
  images/
    desktop/
    camera/
    (生画像の一時保存先)
```

**必要なパッケージ:**
```
fastapi
uvicorn[standard]
sqlalchemy
litellm
pydantic
python-multipart
httpx
tomli
sqlite-vec
```

**作業内容:**
- [ ] ディレクトリ構造を作成
- [ ] pyproject.toml または requirements.txt を作成
- [ ] README.mdに基本的な説明を記載

#### 1.2 設定管理（config.py）

**実装内容:**
- 設定ファイル（config/ghost.toml）の読み込み
- 必須項目のバリデーション（起動時にチェック）
- 設定項目:
  - `token`: API認証用トークン
  - `db_url`: SQLite接続文字列
  - `llm_model`: チャット用LLMモデル名
  - `reflection_model`: reflection用モデル名
  - `embedding_model`: 埋め込みモデル名
  - `log_level`: ログレベル
  - `env`: 実行環境（dev/prod）
  - `max_chat_queue`: チャットキューの最大長

**config/ghost.toml サンプル:**
```toml
token = "your-secret-token-here"
db_url = "sqlite:///./data/ghost.db"
llm_model = "gpt-4"
reflection_model = "gpt-4"
embedding_model = "text-embedding-ada-002"
log_level = "INFO"
env = "dev"
max_chat_queue = 10
```

**作業内容:**
- [ ] config.pyモジュールを実装
- [ ] 設定ファイルの読み込みロジック
- [ ] 必須項目チェック（なければ起動時エラー）
- [ ] config/ghost.toml.exampleを作成

#### 1.3 データベーススキーマ（db.py, models.py）

**db.py 実装内容:**
- SQLAlchemyエンジンの初期化
- セッション管理
- sqlite-vecの初期化とベクタ検索サポート

**models.py テーブル定義:**

1. **episodes テーブル**
   - id: INTEGER (PK)
   - occurred_at: DATETIME
   - source: TEXT (chat/desktop_capture/camera_capture/notification/meta_request)
   - user_text: TEXT (nullable)
   - reply_text: TEXT (nullable)
   - image_summary: TEXT (nullable)
   - activity: TEXT (nullable)
   - context_note: TEXT (nullable)
   - emotion_label: TEXT (nullable)
   - emotion_intensity: REAL (nullable)
   - topic_tags: TEXT (nullable、カンマ区切り)
   - reflection_text: TEXT
   - reflection_json: TEXT (JSON文字列として保存)
   - salience_score: REAL
   - episode_embedding: BLOB (nullable)
   - raw_desktop_path: TEXT (nullable)
   - raw_camera_path: TEXT (nullable)
   - created_at: DATETIME
   - updated_at: DATETIME

2. **persons テーブル**
   - id: INTEGER (PK)
   - is_user: BOOLEAN
   - name: TEXT
   - aliases: TEXT (nullable)
   - display_name: TEXT (nullable)
   - relation_to_user: TEXT (nullable)
   - relation_confidence: REAL (nullable)
   - residence: TEXT (nullable)
   - occupation: TEXT (nullable)
   - first_seen_at: DATETIME (nullable)
   - last_seen_at: DATETIME (nullable)
   - mention_count: INTEGER (nullable)
   - topic_tags: TEXT (nullable)
   - status_note: TEXT (nullable)
   - closeness_score: REAL (nullable)
   - worry_score: REAL (nullable)
   - profile_embedding: BLOB (nullable)
   - created_at: DATETIME
   - updated_at: DATETIME

3. **episode_persons テーブル**
   - episode_id: INTEGER (FK -> episodes.id)
   - person_id: INTEGER (FK -> persons.id)
   - role: TEXT (nullable)
   - PRIMARY KEY (episode_id, person_id)

**作業内容:**
- [ ] db.pyでSQLAlchemyエンジンとセッション管理を実装
- [ ] models.pyで3つのテーブルのORMモデルを定義
- [ ] sqlite-vecの初期化ロジック
- [ ] テーブル作成スクリプト（初回起動時）
- [ ] ベクタインデックスの作成処理

#### 1.4 APIスキーマ（schemas.py）

**実装内容:**
Pydanticモデルで各APIのリクエスト/レスポンススキーマを定義

1. **ChatRequest / ChatResponse**
```python
class ChatRequest(BaseModel):
    user_id: str = "default"
    text: str
    context_hint: Optional[str] = None
    image_path: Optional[str] = None

class ChatResponse(BaseModel):
    reply_text: str
    episode_id: int
```

2. **NotificationRequest / NotificationResponse**
```python
class NotificationRequest(BaseModel):
    source_system: str
    title: str
    body: str
    image_url: Optional[str] = None

class NotificationResponse(BaseModel):
    speak_text: str
    episode_id: int
```

3. **MetaRequestRequest / MetaRequestResponse**
```python
class MetaRequestRequest(BaseModel):
    instruction: str
    payload_text: str
    image_url: Optional[str] = None

class MetaRequestResponse(BaseModel):
    speak_text: str
    episode_id: int
```

4. **CaptureRequest / CaptureResponse**
```python
class CaptureRequest(BaseModel):
    capture_type: str  # "desktop" or "camera"
    image_path: str
    context_text: Optional[str] = None

class CaptureResponse(BaseModel):
    episode_id: int
    stored: bool
```

5. **EpisodeSummary**
```python
class EpisodeSummary(BaseModel):
    id: int
    occurred_at: datetime
    source: str
    user_text: Optional[str]
    reply_text: Optional[str]
    emotion_label: Optional[str]
    salience_score: float
```

**作業内容:**
- [ ] schemas.pyに全てのリクエスト/レスポンスモデルを定義
- [ ] バリデーションルールを設定

#### 1.5 FastAPIアプリ雛形（main.py）

**実装内容:**
- FastAPIアプリケーションの生成
- 認証ミドルウェア（固定トークンチェック）
- ルーター登録の準備
- 起動時の初期化処理
- エラーハンドリング

**main.py 構造:**
```python
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from cocoro_ghost import config, db

app = FastAPI(title="cocoro_ghost API")
security = HTTPBearer()
cfg = config.load_config()

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != cfg.token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token"
        )
    return credentials.credentials

@app.on_event("startup")
async def startup():
    # DB初期化
    db.init_db(cfg.db_url)

@app.get("/")
async def root():
    return {"message": "cocoro_ghost API is running"}

# ルーター登録は後のフェーズで追加
```

**作業内容:**
- [ ] main.pyの雛形を実装
- [ ] 認証ミドルウェアを実装
- [ ] 起動時のDB初期化処理
- [ ] ヘルスチェック用エンドポイント（/healthなど）

---

### フェーズ2: LLMクライアントとプロンプト

#### 2.1 プロンプト管理（prompts.py）

**実装内容:**
- キャラクター用システムプロンプト
- Reflection用システムプロンプト
- 通知/メタ要求用システムプロンプト
- プロンプトテンプレートの管理

**prompts.py 構造:**
```python
# docs/cocoro_ghost_prompts.mdの内容をコードに落とし込む

CHARACTER_SYSTEM_PROMPT = """
あなたは「CocoroAI」と呼ばれる、一人のユーザー専用のパートナーAIです。
...
"""

REFLECTION_SYSTEM_PROMPT = """
あなたは cocoro_ghost の「内的思考（reflection）」モジュールです。
...
"""

NOTIFICATION_SYSTEM_PROMPT = """
あなたは CocoroAI という、一人のユーザー専用のパートナーAIです。
...
"""

def get_character_prompt() -> str:
    return CHARACTER_SYSTEM_PROMPT

def get_reflection_prompt() -> str:
    return REFLECTION_SYSTEM_PROMPT

def get_notification_prompt() -> str:
    return NOTIFICATION_SYSTEM_PROMPT
```

**作業内容:**
- [ ] prompts.pyにプロンプト文字列を実装
- [ ] docs/cocoro_ghost_prompts.mdの内容を移植
- [ ] プロンプト取得関数を実装

#### 2.2 LLMクライアント（llm_client.py）

**実装内容:**
LiteLLMを使ったLLMとの通信ラッパー

**llm_client.py インターフェース:**
```python
from typing import List, Dict, Optional
import litellm

class LlmClient:
    def __init__(self, model: str, reflection_model: str, embedding_model: str):
        self.model = model
        self.reflection_model = reflection_model
        self.embedding_model = embedding_model

    def generate_reply(
        self,
        system_prompt: str,
        conversation: List[Dict[str, str]],
        temperature: float = 0.7,
    ) -> str:
        """
        パートナーとしての返答を1メッセージ生成する。
        エラー時は例外を送出する。
        """
        pass

    def generate_reflection(
        self,
        system_prompt: str,
        context_text: str,
        image_descriptions: Optional[List[str]] = None,
    ) -> dict:
        """
        reflection用プロンプトを受け取り、JSON形式の内的思考を生成する。
        エラー時は例外を送出する。
        """
        pass

    def generate_embedding(
        self,
        texts: List[str],
        images: Optional[List[bytes]] = None,
    ) -> List[List[float]]:
        """
        エピソード用の埋め込みベクトルを生成する。
        エラー時は例外を送出する。
        """
        pass

    def generate_image_summary(
        self,
        images: List[bytes],
    ) -> List[str]:
        """
        画像から日本語の要約テキストを生成する。
        エラー時は例外を送出する。
        """
        pass
```

**作業内容:**
- [ ] llm_client.pyの基本クラスを実装
- [ ] generate_replyメソッドの実装
- [ ] generate_reflectionメソッドの実装（JSON生成）
- [ ] generate_embeddingメソッドの実装
- [ ] generate_image_summaryメソッドの実装
- [ ] エラーハンドリング（例外を適切に送出）
- [ ] LiteLLMの設定とAPI呼び出し

---

### フェーズ3: Reflectionとメモリ管理

#### 3.1 Reflection処理（reflection.py）

**実装内容:**
- LLMClientを使ってreflectionを生成
- JSON検証とパース
- 構造体への変換

**reflection.py 構造:**
```python
from typing import List, Dict, Optional
from dataclasses import dataclass
from cocoro_ghost.llm_client import LlmClient
import json

@dataclass
class PersonUpdate:
    name: str
    is_user: bool
    relation_update_note: Optional[str]
    status_update_note: Optional[str]
    closeness_delta: float
    worry_delta: float

@dataclass
class EpisodeReflection:
    reflection_text: str
    emotion_label: str
    emotion_intensity: float
    topic_tags: List[str]
    salience_score: float
    episode_comment: str
    persons: List[PersonUpdate]
    raw_json: str  # 元のJSON全体を保持

def generate_reflection(
    llm_client: LlmClient,
    context_text: str,
    image_descriptions: Optional[List[str]] = None,
) -> EpisodeReflection:
    """
    reflectionを生成し、構造化されたオブジェクトとして返す。
    JSONパース失敗時は例外を送出する。
    """
    pass
```

**作業内容:**
- [ ] reflection.pyモジュールを実装
- [ ] EpisodeReflectionデータクラスの定義
- [ ] generate_reflection関数の実装
- [ ] JSON検証ロジック（スキーマチェック）
- [ ] パース失敗時のエラーハンドリング

#### 3.2 メモリ管理（memory.py）

**実装内容:**
- エピソード生成パイプライン
- 人物プロフィール更新
- データベースへの保存

**memory.py 主要関数:**
```python
from cocoro_ghost.llm_client import LlmClient
from cocoro_ghost.reflection import EpisodeReflection, generate_reflection
from cocoro_ghost import models, schemas
from sqlalchemy.orm import Session

class MemoryManager:
    def __init__(self, llm_client: LlmClient):
        self.llm_client = llm_client

    def handle_chat(
        self,
        db: Session,
        request: schemas.ChatRequest,
    ) -> schemas.ChatResponse:
        """
        /chat エンドポイントの処理ロジック。
        1. コンテキスト収集
        2. 返答生成（同期）
        3. reflection生成（非同期）
        4. 埋め込み生成と保存（非同期）
        """
        pass

    def handle_notification(
        self,
        db: Session,
        request: schemas.NotificationRequest,
    ) -> schemas.NotificationResponse:
        """
        /notification エンドポイントの処理ロジック。
        """
        pass

    def handle_meta_request(
        self,
        db: Session,
        request: schemas.MetaRequestRequest,
    ) -> schemas.MetaRequestResponse:
        """
        /meta_request エンドポイントの処理ロジック。
        """
        pass

    def handle_capture(
        self,
        db: Session,
        request: schemas.CaptureRequest,
    ) -> schemas.CaptureResponse:
        """
        /capture エンドポイントの処理ロジック。
        """
        pass

    def _update_persons(
        self,
        db: Session,
        episode_id: int,
        persons_data: List[PersonUpdate],
    ):
        """
        reflectionのpersons情報からpersonsテーブルを更新。
        """
        pass

    def _create_episode(
        self,
        db: Session,
        occurred_at: datetime,
        source: str,
        user_text: Optional[str],
        reply_text: Optional[str],
        reflection: EpisodeReflection,
        embedding: List[float],
        **kwargs
    ) -> int:
        """
        episodesテーブルにレコードを作成し、episode_idを返す。
        """
        pass
```

**作業内容:**
- [ ] memory.pyモジュールを実装
- [ ] MemoryManagerクラスの基本構造
- [ ] handle_chatメソッドの実装
- [ ] handle_notificationメソッドの実装
- [ ] handle_meta_requestメソッドの実装
- [ ] handle_captureメソッドの実装
- [ ] _update_personsメソッドの実装
- [ ] _create_episodeメソッドの実装
- [ ] 同期/非同期処理の実装（FastAPIのBackgroundTasksを使用）
- [ ] 直列化キュー（ユーザーごとのロック）の実装

---

### フェーズ4: APIエンドポイント実装

#### 4.1 /chat エンドポイント（api/chat.py）

**実装内容:**
```python
from fastapi import APIRouter, Depends
from cocoro_ghost import schemas
from cocoro_ghost.memory import MemoryManager
from cocoro_ghost.db import get_db
from sqlalchemy.orm import Session

router = APIRouter()

@router.post("/chat", response_model=schemas.ChatResponse)
async def chat(
    request: schemas.ChatRequest,
    db: Session = Depends(get_db),
    memory_manager: MemoryManager = Depends(get_memory_manager),
):
    """
    通常会話エンドポイント。
    """
    return memory_manager.handle_chat(db, request)
```

**作業内容:**
- [ ] api/chat.pyを実装
- [ ] /chatエンドポイントの定義
- [ ] 認証の適用
- [ ] リクエスト処理の委譲

#### 4.2 /notification エンドポイント（api/notification.py）

**作業内容:**
- [ ] api/notification.pyを実装
- [ ] /notificationエンドポイントの定義
- [ ] 認証の適用

#### 4.3 /meta_request エンドポイント（api/meta_request.py）

**作業内容:**
- [ ] api/meta_request.pyを実装
- [ ] /meta_requestエンドポイントの定義
- [ ] 認証の適用

#### 4.4 /capture エンドポイント（api/capture.py）

**作業内容:**
- [ ] api/capture.pyを実装
- [ ] /captureエンドポイントの定義
- [ ] 認証の適用

#### 4.5 /episodes エンドポイント（api/episodes.py）

**実装内容:**
```python
from fastapi import APIRouter, Depends, Query
from cocoro_ghost import schemas
from cocoro_ghost.db import get_db
from cocoro_ghost import models
from sqlalchemy.orm import Session
from typing import List

router = APIRouter()

@router.get("/episodes", response_model=List[schemas.EpisodeSummary])
async def get_episodes(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """
    エピソード一覧取得エンドポイント。
    """
    episodes = db.query(models.Episode)\
        .order_by(models.Episode.occurred_at.desc())\
        .offset(offset)\
        .limit(limit)\
        .all()
    return episodes
```

**作業内容:**
- [ ] api/episodes.pyを実装
- [ ] /episodesエンドポイントの定義
- [ ] 認証の適用
- [ ] ページネーション実装

#### 4.6 main.pyへのルーター登録

**作業内容:**
- [ ] main.pyに各APIルーターを登録
- [ ] 認証依存関係の設定

---

### フェーズ5: バックグラウンド処理と補助機能

#### 5.1 生画像クリーンアップ

**実装内容:**
- 72時間以上前の画像ファイルを削除するバックグラウンドタスク
- 定期実行（5〜10分おき）

**cleanup.py（新規）:**
```python
from datetime import datetime, timedelta
from cocoro_ghost import models
from cocoro_ghost.db import get_db
import os

def cleanup_old_images():
    """
    72時間以上前のエピソードの生画像を削除する。
    """
    db = next(get_db())
    cutoff_time = datetime.utcnow() - timedelta(hours=72)

    old_episodes = db.query(models.Episode)\
        .filter(models.Episode.occurred_at < cutoff_time)\
        .all()

    for episode in old_episodes:
        if episode.raw_desktop_path and os.path.exists(episode.raw_desktop_path):
            os.remove(episode.raw_desktop_path)
            episode.raw_desktop_path = None

        if episode.raw_camera_path and os.path.exists(episode.raw_camera_path):
            os.remove(episode.raw_camera_path)
            episode.raw_camera_path = None

    db.commit()
```

**作業内容:**
- [ ] cleanup.pyモジュールを実装
- [ ] cleanup_old_images関数の実装
- [ ] FastAPIのBackgroundTasksまたはAPSchedulerで定期実行
- [ ] エラーハンドリング（削除失敗時は停止）

#### 5.2 ロギング設定

**実装内容:**
- Python標準loggingの設定
- 主要イベントのログ記録
  - API呼び出し
  - LLM呼び出し（開始/終了/エラー）
  - エピソード作成
  - 人物プロフィール更新
  - エラー発生時のスタックトレース

**作業内容:**
- [ ] logging設定をconfig.pyまたは独立したlogging_config.pyに実装
- [ ] 各モジュールでロガーを設定
- [ ] 適切なログレベルでメッセージを記録
- [ ] ログファイルのローテーション設定

#### 5.3 ベクタ検索実装

**実装内容:**
- sqlite-vecを使ったエピソードの類似検索
- コンテキスト収集時に関連エピソードを取得

**db.py 追加:**
```python
def search_similar_episodes(
    db: Session,
    query_embedding: List[float],
    limit: int = 5,
) -> List[models.Episode]:
    """
    与えられた埋め込みベクトルに類似するエピソードを検索する。
    """
    # sqlite-vecを使った類似検索
    pass
```

**作業内容:**
- [ ] sqlite-vecの初期化とテーブル作成
- [ ] ベクタインデックスの構築
- [ ] 類似検索関数の実装
- [ ] memory.pyのコンテキスト収集で類似検索を利用

---

### フェーズ6: テストとデバッグ

#### 6.1 ユニットテスト

**tests/test_api.py:**
- 各APIエンドポイントのテスト
- 認証テスト
- リクエスト/レスポンスの検証

**tests/test_memory.py:**
- エピソード生成テスト
- 人物更新テスト
- reflection処理テスト

**tests/test_llm_client.py:**
- LLM呼び出しのモックテスト

**作業内容:**
- [ ] pytestの設定
- [ ] FastAPIのTestClientを使ったAPIテスト
- [ ] LLM呼び出しのモック化
- [ ] DBのテスト用フィクスチャ作成
- [ ] 主要な処理フローのテストケース作成

#### 6.2 統合テスト

**実装内容:**
- 実際のLLMを使ったエンドツーエンドテスト
- 複数のエピソード作成と検索のテスト

**作業内容:**
- [ ] 統合テストスクリプトの作成
- [ ] テストデータの準備
- [ ] 実行手順のドキュメント化

---

### フェーズ7: デプロイと運用準備

#### 7.1 Docker化（オプション）

**Dockerfile:**
```dockerfile
FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "cocoro_ghost.main:app", "--host", "0.0.0.0", "--port", "55601"]
```

**docker-compose.yml:**
```yaml
version: '3.8'
services:
  cocoro_ghost:
    build: .
    ports:
      - "55601:55601"
    volumes:
      - ./data:/app/data
      - ./images:/app/images
      - ./config:/app/config
    environment:
      - PYTHONUNBUFFERED=1
```

**作業内容:**
- [ ] Dockerfileの作成
- [ ] docker-compose.ymlの作成
- [ ] イメージビルドとテスト

#### 7.2 起動スクリプト

**run.sh:**
```bash
#!/bin/bash
python -X utf8 -m uvicorn cocoro_ghost.main:app --host 0.0.0.0 --port 55601
```

**作業内容:**
- [ ] 起動スクリプトの作成
- [ ] 環境変数設定スクリプト
- [ ] サービス登録用のsystemdユニットファイル（Linux）

#### 7.3 ドキュメント整備

**README.md更新:**
- セットアップ手順
- 設定方法
- API使用例
- トラブルシューティング

**作業内容:**
- [ ] README.mdの更新
- [ ] API使用例の追加
- [ ] 設定ファイルのサンプル
- [ ] よくある質問（FAQ）

---

## 実装優先順位

### 高優先度（MVP: Minimum Viable Product）

1. フェーズ1: 基盤構築
   - プロジェクト構造、設定、DBスキーマ、基本API
2. フェーズ2: LLMクライアントとプロンプト
3. フェーズ3: Reflectionとメモリ管理
4. フェーズ4.1: /chat エンドポイント

**目標: /chatエンドポイントが動作し、エピソードが記録される**

### 中優先度

1. フェーズ4.2〜4.5: 残りのAPIエンドポイント
   - /notification, /meta_request, /capture, /episodes
2. フェーズ5.1: 生画像クリーンアップ
3. フェーズ5.2: ロギング設定

**目標: 全APIが動作し、基本的な運用が可能**

### 低優先度（後から追加可能）

1. フェーズ5.3: ベクタ検索実装
2. フェーズ6: テストとデバッグ
3. フェーズ7: デプロイと運用準備

**目標: 品質向上と運用効率化**

---

## 注意事項

### 開発時の注意点

1. **Python UTF-8モード必須**
   - 実行時は必ず `python -X utf8` を使用

2. **SQLiteの同時アクセス**
   - 書き込みが競合しないよう、適切なロック機構を実装

3. **LLM APIのレート制限**
   - 必要に応じてリトライやバックオフを実装
   - ただし、失敗時は最終的にエラーで停止

4. **画像ファイルの管理**
   - パスは絶対パスで管理
   - 存在チェックを適切に行う

5. **JSON生成の不安定性**
   - LLMが正しいJSONを返さない可能性を考慮
   - パース失敗時は即座にエラーとする

### セキュリティ考慮事項

1. **認証トークンの管理**
   - 設定ファイルに平文で保存（リポジトリにはコミットしない）
   - .gitignoreにconfig/ghost.tomlを追加

2. **パストラバーサル対策**
   - 画像パスの検証
   - 許可されたディレクトリ外へのアクセスを防ぐ

3. **SQLインジェクション対策**
   - ORMを使用することで自動的に対策される
   - 生SQLを書く場合はパラメータバインディングを使用

---

## 完了条件

### MVP完了条件

- [ ] /chatエンドポイントが動作する
- [ ] ユーザー発話に対して返答が生成される
- [ ] エピソードがDBに保存される
- [ ] reflectionが正しく生成される
- [ ] 人物プロフィールが更新される
- [ ] 基本的なエラーハンドリングが実装されている

### 全体完了条件

- [ ] 5つのAPIエンドポイント全てが動作する
- [ ] ベクタ検索が実装されている
- [ ] 生画像のクリーンアップが動作する
- [ ] ログが適切に記録される
- [ ] 基本的なテストがパスする
- [ ] ドキュメントが整備されている
- [ ] デプロイ可能な状態になっている

---

## 次のステップ

実装計画を確認し、フェーズ1から順に実装を開始します。

1. **プロジェクト構造の作成**: ディレクトリとファイルの雛形を作成
2. **設定管理の実装**: config.pyとconfig/ghost.toml
3. **データベーススキーマの実装**: db.pyとmodels.py
4. **FastAPI雛形の実装**: main.pyとヘルスチェックエンドポイント

各フェーズの実装が完了したら、次のフェーズに進みます。
