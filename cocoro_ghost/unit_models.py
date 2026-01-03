"""
Unitベース記憶のORMモデル

記憶データベース（memory_*.db）のテーブル定義。
Unit（記憶本体）、各種Payload（Episode/Fact/Summary等）、
Entity（エンティティ）、Job（ジョブ）などのモデルを含む。
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Float, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from cocoro_ghost.db import UnitBase


class Unit(UnitBase):
    """
    Unit本体（記憶ユニットの共通メタ情報）。

    すべての記憶種別に共通するメタ情報を保持する。
    具体的な内容は各PayloadテーブルにFKで紐づく。
    """
    __tablename__ = "units"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)  # Unit固有ID
    kind: Mapped[int] = mapped_column(Integer, nullable=False)  # 種別（UnitKind）
    occurred_at: Mapped[Optional[int]] = mapped_column(Integer)  # 発生日時（UNIXタイムスタンプ）
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)  # 作成日時
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)  # 更新日時

    source: Mapped[Optional[str]] = mapped_column(Text)  # ソース識別子（chat/notification等）
    state: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 状態（UnitState）
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)  # 確信度（0.0-1.0）
    salience: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # 顕著性スコア
    sensitivity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 秘匿度（Sensitivity）
    pin: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # ピン留め（0:なし, 1:あり）

    topic_tags: Mapped[Optional[str]] = mapped_column(Text)  # トピックタグ（JSON配列）
    persona_affect_label: Mapped[Optional[str]] = mapped_column(Text)  # AI人格感情ラベル
    persona_affect_intensity: Mapped[Optional[float]] = mapped_column(Float)  # 感情強度


class PayloadEpisode(UnitBase):
    """
    Episode（対話エピソード）のペイロード。

    ユーザーとの対話内容を保持する。
    """
    __tablename__ = "payload_episode"

    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"), primary_key=True)  # 親UnitID
    input_text: Mapped[Optional[str]] = mapped_column(Text)  # 入力本文
    reply_text: Mapped[Optional[str]] = mapped_column(Text)  # AI返答
    image_summary: Mapped[Optional[str]] = mapped_column(Text)  # 画像の説明（添付画像がある場合）
    context_note: Mapped[Optional[str]] = mapped_column(Text)  # コンテキスト補足
    reflection_json: Mapped[Optional[str]] = mapped_column(Text)  # 反射（LLM生成JSON）


class PayloadFact(UnitBase):
    """
    Fact（三つ組の知識）のペイロード。

    主語-述語-目的語の形式で知識を保持する。
    """
    __tablename__ = "payload_fact"

    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"), primary_key=True)  # 親UnitID
    subject_entity_id: Mapped[Optional[int]] = mapped_column(ForeignKey("entities.id"))  # 主語エンティティID
    predicate: Mapped[str] = mapped_column(Text, nullable=False)  # 述語（関係）
    object_text: Mapped[Optional[str]] = mapped_column(Text)  # 目的語テキスト
    object_entity_id: Mapped[Optional[int]] = mapped_column(ForeignKey("entities.id"))  # 目的語エンティティID
    valid_from: Mapped[Optional[int]] = mapped_column(Integer)  # 有効期間開始
    valid_to: Mapped[Optional[int]] = mapped_column(Integer)  # 有効期間終了
    evidence_unit_ids_json: Mapped[str] = mapped_column(Text, nullable=False)  # 根拠UnitIDリスト（JSON）


class PayloadSummary(UnitBase):
    """
    Summary（要約）のペイロード。

    日次・週次などの期間要約を保持する。
    """
    __tablename__ = "payload_summary"

    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"), primary_key=True)  # 親UnitID
    # 固定Enumではなく自由ラベル（将来のスコープ追加に耐える）
    scope_label: Mapped[str] = mapped_column(Text, nullable=False)  # スコープラベル（daily/weekly等）
    scope_key: Mapped[str] = mapped_column(Text, nullable=False)  # スコープキー（日付等）
    range_start: Mapped[Optional[int]] = mapped_column(Integer)  # 対象範囲開始
    range_end: Mapped[Optional[int]] = mapped_column(Integer)  # 対象範囲終了
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)  # 要約テキスト
    summary_json: Mapped[Optional[str]] = mapped_column(Text)  # 構造化要約（JSON）


class PayloadLoop(UnitBase):
    """
    Loop（未解決ループ）のペイロード。

    未解決のタスクや気掛かりを保持する。
    """
    __tablename__ = "payload_loop"

    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"), primary_key=True)  # 親UnitID
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)  # 有効期限（UNIXタイムスタンプ）
    due_at: Mapped[Optional[int]] = mapped_column(Integer)  # 期限（UNIXタイムスタンプ）
    loop_text: Mapped[str] = mapped_column(Text, nullable=False)  # ループ内容


class Entity(UnitBase):
    """
    エンティティ（人/場所/話題など）のマスタ。

    会話に登場する固有名詞や概念を管理する。
    """
    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)  # エンティティID
    # 固定Enumをやめ、自由なラベル + rolesで扱う（PERSONA_ANCHORの人物用途）
    type_label: Mapped[Optional[str]] = mapped_column(Text)  # 種別ラベル（person/place等）
    name: Mapped[str] = mapped_column(Text, nullable=False)  # 名前
    normalized: Mapped[Optional[str]] = mapped_column(Text)  # 正規化名
    roles_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # 役割リスト（JSON）
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)  # 作成日時
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)  # 更新日時


class EntityAlias(UnitBase):
    """
    エンティティの別名。

    同一人物・概念の異なる呼び方を管理する。
    """
    __tablename__ = "entity_aliases"

    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    )  # 親エンティティID
    alias: Mapped[str] = mapped_column(Text, primary_key=True)  # 別名


class UnitEntity(UnitBase):
    """
    UnitとEntityの関連を表す中間テーブル。

    Unit内でどのエンティティが言及されているかを管理する。
    """
    __tablename__ = "unit_entities"

    unit_id: Mapped[int] = mapped_column(
        ForeignKey("units.id", ondelete="CASCADE"),
        primary_key=True,
    )  # UnitID
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    )  # エンティティID
    role: Mapped[int] = mapped_column(Integer, primary_key=True)  # 関係種別（EntityRole）
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)  # 重み


class Edge(UnitBase):
    """
    エンティティ間の関係（グラフエッジ）。

    エンティティ同士の関係性を有向グラフとして管理する。
    """
    __tablename__ = "edges"

    src_entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    )  # 始点エンティティID
    # 固定Enumではなく自由ラベル（"friend"/"likes"/"mentor" など）
    relation_label: Mapped[str] = mapped_column(Text, primary_key=True)  # 関係ラベル
    dst_entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    )  # 終点エンティティID
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)  # 関係の強さ
    first_seen_at: Mapped[Optional[int]] = mapped_column(Integer)  # 初出日時
    last_seen_at: Mapped[Optional[int]] = mapped_column(Integer)  # 最終確認日時
    evidence_unit_id: Mapped[Optional[int]] = mapped_column(ForeignKey("units.id"))  # 根拠UnitID


class UnitVersion(UnitBase):
    """
    Unitのバージョン履歴。

    Unitの変更履歴を追跡し、差分理由やハッシュを記録する。
    """
    __tablename__ = "unit_versions"

    unit_id: Mapped[int] = mapped_column(
        ForeignKey("units.id", ondelete="CASCADE"),
        primary_key=True,
    )  # UnitID
    version: Mapped[int] = mapped_column(Integer, primary_key=True)  # バージョン番号
    parent_version: Mapped[Optional[int]] = mapped_column(Integer)  # 親バージョン
    patch_reason: Mapped[Optional[str]] = mapped_column(Text)  # 変更理由
    payload_hash: Mapped[Optional[str]] = mapped_column(Text)  # ペイロードハッシュ
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)  # 作成日時


class Job(UnitBase):
    """
    非同期処理用ジョブ。

    Workerが処理するバックグラウンドジョブを管理する。
    """
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)  # ジョブID
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # ジョブ種別
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)  # ペイロード（JSON）
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 状態（JobStatus）
    run_after: Mapped[int] = mapped_column(Integer, nullable=False)  # 実行可能日時
    tries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 試行回数
    last_error: Mapped[Optional[str]] = mapped_column(Text)  # 最終エラー
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)  # 作成日時
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)  # 更新日時
