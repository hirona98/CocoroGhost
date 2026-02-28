"""
ストレージ関連パッケージ。

目的:
    - 設定DB、記憶DB、SQLAlchemyモデル、ベクトル索引を1箇所へ集約する。
    - 永続化層の責務を `cocoro_ghost` 直下から切り離して辿りやすくする。
"""

from __future__ import annotations
