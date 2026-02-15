"""
記憶DB（memory_*.db）のORMモデル定義

このモジュールは「出来事ログ（events）」と「更新で育つ状態（state）」を中心に、
改訂履歴（revisions）や文脈グラフ（event_threads/event_links）などを定義する。
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Float, ForeignKey, Integer, Text, UniqueConstraint
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
    source: Mapped[str] = mapped_column(Text, nullable=False)  # chat/notification/reminder/desktop_watch/meta_proactive/vision_detail/autonomy_action など

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


class WmEntity(MemoryBase):
    """階層型世界モデルのエンティティ。"""

    __tablename__ = "wm_entities"
    __table_args__ = (
        UniqueConstraint("entity_key", name="uq_wm_entities_entity_key"),
    )

    # --- 主キー ---
    entity_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- 同一性 ---
    entity_key: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)

    # --- 付帯情報 ---
    value_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # --- タイムスタンプ ---
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class WmRelation(MemoryBase):
    """階層型世界モデルのエンティティ間関係。"""

    __tablename__ = "wm_relations"
    __table_args__ = (
        UniqueConstraint("from_entity_id", "to_entity_id", "relation_type", name="uq_wm_relations_from_to_type"),
    )

    # --- 主キー ---
    relation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- 関係 ---
    from_entity_id: Mapped[int] = mapped_column(ForeignKey("wm_entities.entity_id", ondelete="CASCADE"), nullable=False)
    to_entity_id: Mapped[int] = mapped_column(ForeignKey("wm_entities.entity_id", ondelete="CASCADE"), nullable=False)
    relation_type: Mapped[str] = mapped_column(Text, nullable=False)

    # --- 品質 ---
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence_event_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    # --- タイムスタンプ ---
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class WmBelief(MemoryBase):
    """階層型世界モデルの命題（belief）。"""

    __tablename__ = "wm_beliefs"

    # --- 主キー ---
    belief_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- 主体と命題 ---
    subject_entity_id: Mapped[Optional[int]] = mapped_column(ForeignKey("wm_entities.entity_id", ondelete="SET NULL"))
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    object_text: Mapped[str] = mapped_column(Text, nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # --- 品質 ---
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    source_type: Mapped[str] = mapped_column(Text, nullable=False, default="observation")

    # --- 適用期間 ---
    valid_from_ts: Mapped[Optional[int]] = mapped_column(Integer)
    valid_to_ts: Mapped[Optional[int]] = mapped_column(Integer)

    # --- 根拠 ---
    evidence_event_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    # --- タイムスタンプ ---
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class WmStrategicGoal(MemoryBase):
    """戦略層の目標（StrategicGoal）。"""

    __tablename__ = "wm_strategic_goals"

    # --- 主キー ---
    goal_id: Mapped[str] = mapped_column(Text, primary_key=True, autoincrement=False)

    # --- 本体 ---
    title: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    horizon_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_criteria_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    constraints_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")

    # --- タイムスタンプ ---
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class WmActionTicket(MemoryBase):
    """戦術層が発行する実行単位（ActionTicket）。"""

    __tablename__ = "wm_action_tickets"

    # --- 主キー ---
    ticket_id: Mapped[str] = mapped_column(Text, primary_key=True, autoincrement=False)

    # --- 紐づけ ---
    goal_id: Mapped[Optional[str]] = mapped_column(ForeignKey("wm_strategic_goals.goal_id", ondelete="SET NULL"))

    # --- 実行情報 ---
    capability_id: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    input_payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    preconditions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    expected_effect_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    verify_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    reason_code: Mapped[Optional[str]] = mapped_column(Text)

    # --- 時刻 ---
    issued_at: Mapped[int] = mapped_column(Integer, nullable=False)
    deadline_at: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class WmActionResult(MemoryBase):
    """実行結果（ActionResult）。"""

    __tablename__ = "wm_action_results"

    # --- 主キー ---
    result_id: Mapped[str] = mapped_column(Text, primary_key=True, autoincrement=False)

    # --- 紐づけ ---
    ticket_id: Mapped[Optional[str]] = mapped_column(ForeignKey("wm_action_tickets.ticket_id", ondelete="SET NULL"))

    # --- 結果 ---
    status: Mapped[str] = mapped_column(Text, nullable=False)
    observations_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    effects_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    reason_code: Mapped[Optional[str]] = mapped_column(Text)

    # --- 時刻 ---
    finished_at: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class WmObservation(MemoryBase):
    """観測入力の正規化ログ。"""

    __tablename__ = "wm_observations"

    # --- 主キー ---
    observation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- 観測本体 ---
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[Optional[str]] = mapped_column(Text)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # --- タイムスタンプ ---
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class WmLink(MemoryBase):
    """World Model 内の汎用リンク。"""

    __tablename__ = "wm_links"
    __table_args__ = (
        UniqueConstraint("link_type", "from_type", "from_id", "to_type", "to_id", name="uq_wm_links_route"),
    )

    # --- 主キー ---
    link_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- 関係 ---
    link_type: Mapped[str] = mapped_column(Text, nullable=False)
    from_type: Mapped[str] = mapped_column(Text, nullable=False)
    from_id: Mapped[str] = mapped_column(Text, nullable=False)
    to_type: Mapped[str] = mapped_column(Text, nullable=False)
    to_id: Mapped[str] = mapped_column(Text, nullable=False)

    # --- 品質 ---
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence_event_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    # --- タイムスタンプ ---
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class WmCapability(MemoryBase):
    """CapabilityDescriptor のヘッダ情報。"""

    __tablename__ = "wm_capabilities"

    # --- 主キー ---
    capability_id: Mapped[str] = mapped_column(Text, primary_key=True, autoincrement=False)

    # --- 表示/制御 ---
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    version: Mapped[str] = mapped_column(Text, nullable=False, default="1")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # --- タイムスタンプ ---
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class WmCapabilityOperation(MemoryBase):
    """Capability ごとの operation 定義。"""

    __tablename__ = "wm_capability_operations"
    __table_args__ = (
        UniqueConstraint("capability_id", "operation", name="uq_wm_capability_operations_capability_operation"),
    )

    # --- 主キー ---
    operation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- 紐づけ ---
    capability_id: Mapped[str] = mapped_column(ForeignKey("wm_capabilities.capability_id", ondelete="CASCADE"), nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)

    # --- 契約 ---
    input_schema_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    result_schema_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    effect_schema_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

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
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)

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
