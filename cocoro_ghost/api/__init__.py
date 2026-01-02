"""
API ルーター群

cocoro_ghost の REST API エンドポイントを定義するルーターモジュール群。
FastAPI の APIRouter を使用して各エンドポイントを実装する。

含まれるルーター:
- chat: チャットAPI（SSEストリーミング）
- notification: 外部通知受付API
- meta-request: メタ要求（能動メッセージ）API
- settings: 設定取得/更新API
- admin: 管理API（Unit操作等）
- persona_mood: AI人格感情状態API
- events: イベントSSEストリーム
- logs: ログSSEストリーム
"""
