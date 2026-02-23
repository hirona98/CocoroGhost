"""
記憶DB（memory_*.db）のORMモデル定義

このモジュールは「出来事ログ（events）」と「更新で育つ状態（state）」を中心に、
改訂履歴（revisions）や文脈グラフ（event_threads/event_links）などを定義する。
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from cocoro_ghost.db import MemoryBase


class Event(MemoryBase):
    """出来事ログ（追記ログ）。

    - 1ターン=1行（user_text + assistant_text）を基本とする
    - 通知/リマインダー/視覚説明なども同様に「出来事」として残す
    - 画像そのものは保持しない（画像の説明テキストだけを残す）
    """

    __tablename__ = "events"

    # --- 主キーと基本メタ ---
    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- 検索対象フラグ ---
    # NOTE:
    # - ユーザーのフィードバックで「関係ない/違う」等が発生した場合に、想起の対象から外すために使う。
    # - ログとしての events 自体は保持し、検索/埋め込み/候補収集から除外する。
    searchable: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # --- 入力の識別（クライアントは単純I/Oなので、サーバ側で文脈を構築する） ---
    client_id: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False)  # chat/notification/reminder/desktop_watch/meta_proactive/vision_detail など

    # --- 本文（ターンの片側だけのイベントもあり得る） ---
    user_text: Mapped[Optional[str]] = mapped_column(Text)
    assistant_text: Mapped[Optional[str]] = mapped_column(Text)
    # --- 画像要約（内部用。画像そのものは保存しない） ---
    # NOTE:
    # - 画像付きチャットでは、画像ごとの要約（最大5件）を JSON 配列で保持する。
    # - UIへ表示しないが、検索と返答生成に効かせるため events に保持する。
    image_summaries_json: Mapped[Optional[str]] = mapped_column(Text)

    # --- about_time（内容がいつの話か） ---
    about_start_ts: Mapped[Optional[int]] = mapped_column(Integer)
    about_end_ts: Mapped[Optional[int]] = mapped_column(Integer)
    about_year_start: Mapped[Optional[int]] = mapped_column(Integer)
    about_year_end: Mapped[Optional[int]] = mapped_column(Integer)
    life_stage: Mapped[Optional[str]] = mapped_column(Text)
    about_time_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # --- 注釈（将来の検索/更新の材料） ---
    entities_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    client_context_json: Mapped[Optional[str]] = mapped_column(Text)


class EventEntity(MemoryBase):
    """イベントのエンティティ索引（検索用）。

    目的:
        - `events.entities_json` は監査/表示用の「スナップショット」として残す。
        - 検索では「正規化キー（type + name_norm）」で素早く関連イベントを引けるように、
          参照テーブルとして `event_entities` を持つ。

    注意:
        - 運用前のためマイグレーションは扱わない（DB作り直し前提）。
        - entity_name_raw は表示/診断用。検索は entity_name_norm を正にする。
    """

    __tablename__ = "event_entities"
    __table_args__ = (
        UniqueConstraint("event_id", "entity_type_norm", "entity_name_norm", name="uq_event_entities_event_type_name"),
    )

    # --- 主キー ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- 紐づけ ---
    event_id: Mapped[int] = mapped_column(ForeignKey("events.event_id", ondelete="CASCADE"), nullable=False)

    # --- エンティティ（正規化） ---
    entity_type_norm: Mapped[str] = mapped_column(Text, nullable=False)  # person/org/place/project/tool
    entity_name_raw: Mapped[str] = mapped_column(Text, nullable=False)
    entity_name_norm: Mapped[str] = mapped_column(Text, nullable=False)

    # --- 品質 ---
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # --- タイムスタンプ ---
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class StateEntity(MemoryBase):
    """状態（state）のエンティティ索引（検索用）。

    目的:
        - stateは「育つ」ため、本文の近くにあるエンティティを索引化しておくと、
          seed→entity→関連state の展開が安定する。
        - 現行は WritePlan に entity が含まれるため、まずは「イベント由来の entity を state へ付与」する。

    注意:
        - 運用前のためマイグレーションは扱わない（DB作り直し前提）。
    """

    __tablename__ = "state_entities"
    __table_args__ = (
        UniqueConstraint("state_id", "entity_type_norm", "entity_name_norm", name="uq_state_entities_state_type_name"),
    )

    # --- 主キー ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- 紐づけ ---
    state_id: Mapped[int] = mapped_column(ForeignKey("state.state_id", ondelete="CASCADE"), nullable=False)

    # --- エンティティ（正規化） ---
    entity_type_norm: Mapped[str] = mapped_column(Text, nullable=False)  # person/org/place/project/tool
    entity_name_raw: Mapped[str] = mapped_column(Text, nullable=False)
    entity_name_norm: Mapped[str] = mapped_column(Text, nullable=False)

    # --- 品質 ---
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # --- タイムスタンプ ---
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class EventLink(MemoryBase):
    """イベント間リンク（文脈グラフの辺）。"""

    __tablename__ = "event_links"
    __table_args__ = (
        UniqueConstraint("from_event_id", "to_event_id", "label", name="uq_event_links_from_to_label"),
    )

    # --- 主キー ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- 関係 ---
    from_event_id: Mapped[int] = mapped_column(ForeignKey("events.event_id", ondelete="CASCADE"), nullable=False)
    to_event_id: Mapped[int] = mapped_column(ForeignKey("events.event_id", ondelete="CASCADE"), nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)  # reply_to/same_topic/caused_by/continuation 等

    # --- 信頼度と根拠 ---
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence_event_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class EventThread(MemoryBase):
    """イベント所属（文脈スレッド）。"""

    __tablename__ = "event_threads"
    __table_args__ = (
        UniqueConstraint("event_id", "thread_key", name="uq_event_threads_event_thread"),
    )

    # --- 主キー ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- 所属 ---
    event_id: Mapped[int] = mapped_column(ForeignKey("events.event_id", ondelete="CASCADE"), nullable=False)
    thread_key: Mapped[str] = mapped_column(Text, nullable=False)

    # --- 信頼度と根拠 ---
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence_event_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class EventAffect(MemoryBase):
    """瞬間的な感情（イベントごと）。

    - VADは v/a/d 各軸 -1.0..+1.0 を前提に保存する
    - 推定/明示のどちらにも対応するため confidence を持つ
    """

    __tablename__ = "event_affects"

    # --- 主キー ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- 紐づけ ---
    event_id: Mapped[int] = mapped_column(ForeignKey("events.event_id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- 表現 ---
    moment_affect_text: Mapped[str] = mapped_column(Text, nullable=False)
    moment_affect_labels_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    # --- 数値（VAD） ---
    vad_v: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    vad_a: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    vad_d: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class EventAssistantSummary(MemoryBase):
    """イベントのアシスタント本文（events.assistant_text）の要約（派生情報）。

    目的:
        - SearchResultPack の「選別」入力を軽量化し、SSE開始までの体感速度を改善する。
        - 会話生成（返答本文）では元の events.* を使い、要約は「選別のための材料」に限定する。

    方針:
        - 1イベントにつき1件（event_id を主キー）として保持する。
        - events.updated_at の値を一緒に保存し、元本文が更新された場合は作り直せるようにする。
        - 運用前のためマイグレーションは扱わない（DBを作り直す前提）。
    """

    __tablename__ = "event_assistant_summaries"

    # --- 主キー（events と 1:1） ---
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.event_id", ondelete="CASCADE"), primary_key=True, autoincrement=False
    )

    # --- 要約本文 ---
    summary_text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # --- 整合性チェック（events の更新時刻） ---
    event_updated_at: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- タイムスタンプ ---
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class State(MemoryBase):
    """更新で育つ状態（単一テーブル）。

    kind:
      - fact/relation/task/summary/long_mood_state など

    注意:
    - 状態は「並存」しうる（期間分割や複数併存）
    - 更新理由と根拠は revisions に残す
    """

    __tablename__ = "state"

    # --- 主キー ---
    state_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- 種別と本文 ---
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)  # ベクトル検索・人間可読の両方に使う
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # --- 最近性/品質 ---
    last_confirmed_at: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # --- 検索対象フラグ ---
    # NOTE:
    # - 誤想起の修正（自動分離）で検索対象から外すために使う。
    # - state自体はDBに保持し、検索/埋め込み/候補収集から除外する。
    searchable: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # --- 並存のための期間 ---
    valid_from_ts: Mapped[Optional[int]] = mapped_column(Integer)
    valid_to_ts: Mapped[Optional[int]] = mapped_column(Integer)

    # --- タイムスタンプ ---
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class UserPreference(MemoryBase):
    """
    ユーザーの「好き/苦手」（確認済み/候補）を保存する。

    目的:
        - 会話で「好き/苦手」を話題にしやすくしつつ、誤断定（架空の好み）を減らす。
        - 「断定して良い好み」を confirmed のみに限定し、発話の根拠を明確にする。

    方針:
        - domain は food/topic/style の3種に固定する（運用前で拡張を急がない）。
        - polarity は like/dislike に固定する。
        - subject_norm は NFKC/空白整形/小文字化で比較キーを安定化する（実体は entity_utils.normalize_entity_name に準拠）。
        - status は現在状態（candidate/confirmed/revoked）を表し、履歴は revisions に残す。
        - 1ユーザー前提のため client_id で分けない。
    """

    __tablename__ = "user_preferences"
    __table_args__ = (
        UniqueConstraint("domain", "subject_norm", "polarity", name="uq_user_preferences_domain_subject_polarity"),
    )

    # --- 主キー ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- 分類 ---
    domain: Mapped[str] = mapped_column(Text, nullable=False)  # food/topic/style
    polarity: Mapped[str] = mapped_column(Text, nullable=False)  # like/dislike

    # --- 対象 ---
    subject_raw: Mapped[str] = mapped_column(Text, nullable=False)
    subject_norm: Mapped[str] = mapped_column(Text, nullable=False)

    # --- 状態 ---
    status: Mapped[str] = mapped_column(Text, nullable=False)  # candidate/confirmed/revoked
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    note: Mapped[Optional[str]] = mapped_column(Text)
    evidence_event_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    # --- 観測時刻（好みの更新/再確認の材料） ---
    first_seen_at: Mapped[int] = mapped_column(Integer, nullable=False)
    last_seen_at: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmed_at: Mapped[Optional[int]] = mapped_column(Integer)
    revoked_at: Mapped[Optional[int]] = mapped_column(Integer)

    # --- タイムスタンプ ---
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class StateLink(MemoryBase):
    """状態間リンク（state↔state の関係）。

    目的:
        - state は「育つノート」なので、state同士の関連（派生/矛盾/補足など）を保存して辿れるようにする。
        - 同期検索では `state_link_expand`（seed→リンク→関連state）として候補を増やせる。

    注意:
        - 関係は「向き付き」で保存する（from_state → to_state）。
          検索では両方向を辿る前提（対称関係は2本張ってもよい）。
    """

    __tablename__ = "state_links"
    __table_args__ = (
        UniqueConstraint("from_state_id", "to_state_id", "label", name="uq_state_links_from_to_label"),
    )

    # --- 主キー ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- 紐づけ（CASCADE） ---
    from_state_id: Mapped[int] = mapped_column(ForeignKey("state.state_id", ondelete="CASCADE"), nullable=False)
    to_state_id: Mapped[int] = mapped_column(ForeignKey("state.state_id", ondelete="CASCADE"), nullable=False)

    # --- 関係 ---
    label: Mapped[str] = mapped_column(Text, nullable=False)  # relates_to/derived_from/contradicts/supports など

    # --- 信頼度と根拠 ---
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence_event_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class Revision(MemoryBase):
    """改訂履歴（状態/派生情報の更新理由を保存）。"""

    __tablename__ = "revisions"

    # --- 主キー ---
    revision_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- 対象 ---
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)  # state/event_links/event_threads/long_mood_state など
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- 差分 ---
    before_json: Mapped[Optional[str]] = mapped_column(Text)
    after_json: Mapped[Optional[str]] = mapped_column(Text)

    # --- 説明責任 ---
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_event_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    # --- タイムスタンプ ---
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class RetrievalRun(MemoryBase):
    """検索実行ログ（観測とデバッグ用）。"""

    __tablename__ = "retrieval_runs"

    # --- 主キー ---
    run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- 紐づけ ---
    event_id: Mapped[int] = mapped_column(ForeignKey("events.event_id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- 主要データ（JSON） ---
    plan_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    candidates_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    selected_json: Mapped[str] = mapped_column(Text, nullable=False, default='{"selected":[]}')


class Job(MemoryBase):
    """非同期処理用ジョブ（簡易キュー）。"""

    __tablename__ = "jobs"

    # --- 主キー ---
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- 種別とペイロード ---
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # --- 実行制御 ---
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    run_after: Mapped[int] = mapped_column(Integer, nullable=False)
    tries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text)

    # --- タイムスタンプ ---
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class ActionDecision(MemoryBase):
    """Deliberation の判断結果（ActionDecision）。

    方針:
        - 1判断 = 1event（source=deliberation_decision）を不変条件にする。
        - decision_outcome / action/defer 必須項目の契約をDB制約で固定する。
    """

    __tablename__ = "action_decisions"
    __table_args__ = (
        CheckConstraint("decision_outcome IN ('do_action','skip','defer')", name="ck_action_decisions_outcome"),
        CheckConstraint(
            "decision_outcome <> 'defer' OR length(trim(COALESCE(defer_reason,''))) > 0",
            name="ck_action_decisions_defer_reason",
        ),
        CheckConstraint(
            "decision_outcome <> 'defer' OR defer_until IS NOT NULL",
            name="ck_action_decisions_defer_until_required",
        ),
        CheckConstraint(
            "decision_outcome <> 'defer' OR next_deliberation_at IS NOT NULL",
            name="ck_action_decisions_next_deliberation_required",
        ),
        CheckConstraint(
            "decision_outcome <> 'defer' OR next_deliberation_at >= defer_until",
            name="ck_action_decisions_defer_order",
        ),
        CheckConstraint(
            "decision_outcome <> 'do_action' OR length(trim(COALESCE(action_type,''))) > 0",
            name="ck_action_decisions_action_type_required_for_do_action",
        ),
        CheckConstraint(
            "decision_outcome <> 'do_action' OR length(trim(COALESCE(action_payload_json,''))) > 0",
            name="ck_action_decisions_action_payload_required_for_do_action",
        ),
    )

    # --- 主キー/イベント対応 ---
    decision_id: Mapped[str] = mapped_column(Text, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.event_id", ondelete="CASCADE"), nullable=False, unique=True)

    # --- トリガ起点 ---
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_ref: Mapped[Optional[str]] = mapped_column(Text)

    # --- 判断本体 ---
    decision_outcome: Mapped[str] = mapped_column(Text, nullable=False)
    action_type: Mapped[Optional[str]] = mapped_column(Text)
    action_payload_json: Mapped[Optional[str]] = mapped_column(Text)
    reason_text: Mapped[Optional[str]] = mapped_column(Text)
    defer_reason: Mapped[Optional[str]] = mapped_column(Text)
    defer_until: Mapped[Optional[int]] = mapped_column(Integer)
    next_deliberation_at: Mapped[Optional[int]] = mapped_column(Integer)

    # --- 監査情報 ---
    persona_influence_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    mood_influence_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    evidence_event_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    evidence_state_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    evidence_goal_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class Goal(MemoryBase):
    """中長期目標（goal）。"""

    __tablename__ = "goals"

    # --- 主キー ---
    goal_id: Mapped[str] = mapped_column(Text, primary_key=True)

    # --- 目標の基本情報 ---
    title: Mapped[str] = mapped_column(Text, nullable=False)
    goal_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    target_condition_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    horizon: Mapped[str] = mapped_column(Text, nullable=False, default="mid")

    # --- タイムスタンプ ---
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class Intent(MemoryBase):
    """Execution 対象の intent。"""

    __tablename__ = "intents"
    __table_args__ = (
        UniqueConstraint("decision_id", name="uq_intents_decision_id"),
        CheckConstraint(
            "status IN ('proposed','queued','running','blocked','done','dropped')",
            name="ck_intents_status",
        ),
        CheckConstraint(
            "status <> 'dropped' OR length(trim(dropped_reason)) > 0",
            name="ck_intents_dropped_reason",
        ),
        CheckConstraint(
            "status <> 'dropped' OR dropped_at IS NOT NULL",
            name="ck_intents_dropped_at",
        ),
        CheckConstraint(
            "length(trim(action_type)) > 0",
            name="ck_intents_action_type_non_empty",
        ),
        CheckConstraint(
            "length(trim(action_payload_json)) > 0",
            name="ck_intents_action_payload_non_empty",
        ),
    )

    # --- 主キー/参照 ---
    intent_id: Mapped[str] = mapped_column(Text, primary_key=True)
    decision_id: Mapped[str] = mapped_column(ForeignKey("action_decisions.decision_id", ondelete="CASCADE"), nullable=False)
    goal_id: Mapped[Optional[str]] = mapped_column(ForeignKey("goals.goal_id", ondelete="SET NULL"))

    # --- 実行定義 ---
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    action_payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    scheduled_at: Mapped[Optional[int]] = mapped_column(Integer)

    # --- 遷移補助 ---
    blocked_reason: Mapped[Optional[str]] = mapped_column(Text)
    dropped_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dropped_at: Mapped[Optional[int]] = mapped_column(Integer)
    last_result_status: Mapped[Optional[str]] = mapped_column(Text)

    # --- タイムスタンプ ---
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class AgentJob(MemoryBase):
    """汎用エージェント委譲ジョブ（別プロセス runner 連携用）。"""

    __tablename__ = "agent_jobs"
    __table_args__ = (
        UniqueConstraint("intent_id", name="uq_agent_jobs_intent_id"),
        CheckConstraint(
            "status IN ('queued','claimed','running','completed','failed','cancelled','timed_out')",
            name="ck_agent_jobs_status",
        ),
        CheckConstraint(
            "status <> 'failed' OR length(trim(COALESCE(error_message,''))) > 0",
            name="ck_agent_jobs_failed_error_message",
        ),
        CheckConstraint(
            "result_status IS NULL OR result_status IN ('success','partial','failed','no_effect')",
            name="ck_agent_jobs_result_status",
        ),
        CheckConstraint(
            "length(trim(backend)) > 0",
            name="ck_agent_jobs_backend_non_empty",
        ),
        CheckConstraint(
            "length(trim(task_instruction)) > 0",
            name="ck_agent_jobs_task_instruction_non_empty",
        ),
    )

    # --- 主キー/参照 ---
    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("intents.intent_id", ondelete="CASCADE"), nullable=False)
    decision_id: Mapped[str] = mapped_column(ForeignKey("action_decisions.decision_id", ondelete="CASCADE"), nullable=False)

    # --- 委譲実行定義 ---
    backend: Mapped[str] = mapped_column(Text, nullable=False)
    task_instruction: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)

    # --- claim/running 管理 ---
    claim_token: Mapped[Optional[str]] = mapped_column(Text)
    runner_id: Mapped[Optional[str]] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    heartbeat_at: Mapped[Optional[int]] = mapped_column(Integer)

    # --- 実行結果 ---
    result_status: Mapped[Optional[str]] = mapped_column(Text)
    result_summary_text: Mapped[Optional[str]] = mapped_column(Text)
    result_details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error_code: Mapped[Optional[str]] = mapped_column(Text)
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    # --- タイムスタンプ ---
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[Optional[int]] = mapped_column(Integer)
    finished_at: Mapped[Optional[int]] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class ActionResult(MemoryBase):
    """Capability 実行結果（ActionResult）。"""

    __tablename__ = "action_results"
    __table_args__ = (
        CheckConstraint("useful_for_recall_hint IN (0,1)", name="ck_action_results_useful_for_recall_hint"),
        CheckConstraint(
            "result_status IN ('success','partial','failed','no_effect')",
            name="ck_action_results_result_status",
        ),
        CheckConstraint("recall_decision IN (-1,0,1)", name="ck_action_results_recall_decision"),
        CheckConstraint(
            "recall_decision = -1 OR recall_decided_at IS NOT NULL",
            name="ck_action_results_recall_decided_at",
        ),
    )

    # --- 主キー/参照 ---
    result_id: Mapped[str] = mapped_column(Text, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.event_id", ondelete="CASCADE"), nullable=False, unique=True)
    intent_id: Mapped[Optional[str]] = mapped_column(ForeignKey("intents.intent_id", ondelete="SET NULL"))
    decision_id: Mapped[Optional[str]] = mapped_column(ForeignKey("action_decisions.decision_id", ondelete="SET NULL"))

    # --- 実行結果 ---
    capability_name: Mapped[Optional[str]] = mapped_column(Text)
    result_status: Mapped[str] = mapped_column(Text, nullable=False)
    result_payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    summary_text: Mapped[Optional[str]] = mapped_column(Text)
    useful_for_recall_hint: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recall_decision: Mapped[int] = mapped_column(Integer, nullable=False, default=-1)
    recall_decided_at: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class WorldModelItem(MemoryBase):
    """世界モデル項目（観測を集約した構造化状態）。"""

    __tablename__ = "world_model_items"
    __table_args__ = (
        UniqueConstraint("item_key", name="uq_world_model_items_item_key"),
        CheckConstraint("observation_count >= 1", name="ck_world_model_items_observation_count"),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="ck_world_model_items_confidence"),
        CheckConstraint("active IN (0,1)", name="ck_world_model_items_active"),
        CheckConstraint("freshness_at >= 0", name="ck_world_model_items_freshness_at"),
        CheckConstraint(
            "length(trim(content_fingerprint)) > 0",
            name="ck_world_model_items_content_fingerprint_non_empty",
        ),
    )

    # --- 主キー/根拠参照 ---
    item_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_event_id: Mapped[Optional[int]] = mapped_column(ForeignKey("events.event_id", ondelete="SET NULL"))
    source_result_id: Mapped[Optional[str]] = mapped_column(ForeignKey("action_results.result_id", ondelete="SET NULL"))

    # --- 構造化内容 ---
    item_key: Mapped[str] = mapped_column(Text, nullable=False)
    observation_class: Mapped[Optional[str]] = mapped_column(Text)
    entity_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    relation_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    location_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    affordance_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    content_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    freshness_at: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class RuntimeSnapshot(MemoryBase):
    """Runtime Blackboard のスナップショット。"""

    __tablename__ = "runtime_snapshots"

    # --- 主キー ---
    snapshot_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- 内容 ---
    snapshot_kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class AutonomyTrigger(MemoryBase):
    """自発行動トリガ（イベント/時刻/heartbeat/policy）。"""

    __tablename__ = "autonomy_triggers"
    __table_args__ = (
        CheckConstraint("status IN ('queued','claimed','done','dropped')", name="ck_autonomy_triggers_status"),
        CheckConstraint(
            "status <> 'dropped' OR length(trim(dropped_reason)) > 0",
            name="ck_autonomy_triggers_dropped_reason",
        ),
        CheckConstraint(
            "status <> 'dropped' OR dropped_at IS NOT NULL",
            name="ck_autonomy_triggers_dropped_at",
        ),
    )

    # --- 主キー/参照 ---
    trigger_id: Mapped[str] = mapped_column(Text, primary_key=True)
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_key: Mapped[str] = mapped_column(Text, nullable=False)
    source_event_id: Mapped[Optional[int]] = mapped_column(ForeignKey("events.event_id", ondelete="SET NULL"))
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # --- 実行状態 ---
    status: Mapped[str] = mapped_column(Text, nullable=False)
    scheduled_at: Mapped[Optional[int]] = mapped_column(Integer)
    claim_token: Mapped[Optional[str]] = mapped_column(Text)
    claimed_at: Mapped[Optional[int]] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    dropped_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dropped_at: Mapped[Optional[int]] = mapped_column(Integer)

    # --- タイムスタンプ ---
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


# --- autonomy_triggers の重複排除（queued/claimed のみ一意） ---
Index(
    "uq_autonomy_triggers_trigger_key_active",
    AutonomyTrigger.trigger_key,
    unique=True,
    sqlite_where=text("status IN ('queued','claimed')"),
)


# --- 主要参照用インデックス（SQLAlchemy create_all 時に作成） ---
Index("idx_action_decisions_created_at", ActionDecision.created_at)
Index("idx_intents_status_scheduled_at", Intent.status, Intent.scheduled_at)
Index("idx_agent_jobs_status_created_at", AgentJob.status, AgentJob.created_at)
Index("idx_agent_jobs_backend_status", AgentJob.backend, AgentJob.status)
Index("idx_agent_jobs_heartbeat_at", AgentJob.heartbeat_at)
Index("idx_action_results_created_at", ActionResult.created_at)
Index("idx_world_model_items_freshness_at", WorldModelItem.freshness_at)
Index("idx_world_model_items_active_confidence", WorldModelItem.active, WorldModelItem.confidence)
Index("idx_runtime_snapshots_created_at", RuntimeSnapshot.created_at)
Index("idx_autonomy_triggers_status_scheduled_at", AutonomyTrigger.status, AutonomyTrigger.scheduled_at)
