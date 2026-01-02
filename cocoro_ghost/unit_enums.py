"""
Unitベース記憶のEnum定義

記憶システムで使用する各種列挙型を定義する。
UnitKind（記憶種別）、UnitState（ライフサイクル状態）、
Sensitivity（秘匿度）、JobStatus（ジョブ状態）などを含む。
"""

from __future__ import annotations

from enum import IntEnum


class UnitKind(IntEnum):
    """
    Unitの種類を表す列挙型。

    記憶ユニットの種別を定義する。
    """
    EPISODE = 1   # 対話エピソード（会話ログ）
    FACT = 2      # ファクト（三つ組の知識）
    SUMMARY = 3   # 要約（日次/週次など）
    LOOP = 7      # ループ（未解決タスク/気掛かり）


class UnitState(IntEnum):
    """
    Unitのライフサイクル状態を表す列挙型。

    記憶の成熟度・処理状態を示す。
    """
    RAW = 0          # 未処理（生データ）
    VALIDATED = 1    # 検証済み
    CONSOLIDATED = 2 # 統合済み
    ARCHIVED = 3     # アーカイブ（非アクティブ）


class Sensitivity(IntEnum):
    """
    秘匿度を表す列挙型。

    値が高いほど外部公開を避けるべき情報を示す。
    """
    NORMAL = 0   # 通常（公開可能）
    PRIVATE = 1  # プライベート（限定公開）
    SECRET = 2   # 秘密（非公開）


class EntityRole(IntEnum):
    """
    UnitとEntityの関係を表す列挙型。

    Unit内でエンティティがどのように参照されているかを示す。
    """
    MENTIONED = 1  # 言及（会話中に登場）


class JobStatus(IntEnum):
    """
    ジョブの実行状態を表す列挙型。

    Workerが処理する非同期ジョブの状態を示す。
    """
    QUEUED = 0   # キュー待ち
    RUNNING = 1  # 実行中
    DONE = 2     # 完了
    FAILED = 3   # 失敗
