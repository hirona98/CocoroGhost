"""
SQLite環境チェックスクリプト

このスクリプトはSQLiteの拡張機能サポートとバージョン情報を確認する。
CocoroGhostで使用するベクトル検索拡張が利用可能かを事前に確認するために使用。
"""
import sqlite3


def check_sqlite_capabilities():
    """
    SQLiteの機能をチェックして結果を表示する。
    enable_load_extensionのサポート状況とSQLiteバージョンを確認する。
    """
    # 拡張機能ロードのサポート確認
    if hasattr(sqlite3.Connection, "enable_load_extension"):
        print("enable_load_extension: サポートされています")
    else:
        print("enable_load_extension: サポートされていません")

    # SQLiteバージョン表示
    print(f"sqlite3 version: {sqlite3.sqlite_version}")


if __name__ == "__main__":
    check_sqlite_capabilities()
