"""
cocoro_ghost API テストスクリプト

使用方法:
    python -X utf8 tests/test_api.py
"""

import base64
import json
import sys
from pathlib import Path

import httpx

BASE_URL = "http://localhost:55601/api"
TOKEN = "cocoro_token"


def get_headers():
    return {"Authorization": f"Bearer {TOKEN}"}


def load_test_image_base64(filename: str) -> str:
    """tmp ディレクトリからテスト画像を読み込んでbase64エンコード"""
    image_path = Path(__file__).parent.parent / "tmp" / filename
    if not image_path.exists():
        print(f"  [SKIP] 画像ファイルが見つかりません: {image_path}")
        return None
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def make_data_uri(base64_data: str, mime_type: str = "image/png") -> str:
    """base64データをdata URI形式に変換"""
    return f"data:{mime_type};base64,{base64_data}"


def test_settings_get():
    """GET /settings - 設定取得テスト"""
    print("\n=== GET /settings ===")
    try:
        r = httpx.get(f"{BASE_URL}/settings", headers=get_headers(), timeout=10)
        print(f"  Status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"  LLM Presets: {len(data.get('llm_preset', []))} 件")
            print(f"  Persona Presets: {len(data.get('persona_preset', []))} 件")
            print("  [OK] 設定取得成功")
            return True
        else:
            print(f"  [NG] エラー: {r.text}")
            return False
    except Exception as e:
        print(f"  [NG] 例外: {e}")
        return False


def test_capture():
    """POST /capture - キャプチャテスト"""
    print("\n=== POST /capture ===")
    base64_data = load_test_image_base64("test_image_1.png")
    if not base64_data:
        return None

    payload = {
        "capture_type": "desktop",
        "image_base64": base64_data,
        "context_text": "テスト用キャプチャ"
    }
    try:
        r = httpx.post(f"{BASE_URL}/capture", headers=get_headers(), json=payload, timeout=30)
        print(f"  Status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"  Unit ID: {data.get('unit_id', 'N/A')}")
            print("  [OK] キャプチャ成功")
            return True
        else:
            print(f"  [NG] エラー: {r.text}")
            return False
    except Exception as e:
        print(f"  [NG] 例外: {e}")
        return False


def parse_sse_events(event_lines: list, data_lines: list) -> dict:
    """SSEイベントをパースして結果を返す
    event_lines: 'event: xxx' の内容リスト
    data_lines: 'data: {...}' の内容リスト
    """
    result = {"tokens": [], "done": None, "error": None}
    for i, data_str in enumerate(data_lines):
        try:
            data = json.loads(data_str)
            # イベントタイプを判定
            event_type = event_lines[i] if i < len(event_lines) else None

            if event_type == "token":
                result["tokens"].append(data.get("text", ""))
            elif event_type == "done":
                result["done"] = data
            elif event_type == "error":
                result["error"] = data
            else:
                # event行がない場合はデータ構造から判定
                if "episode_unit_id" in data and "reply_text" in data:
                    result["done"] = data
                elif "text" in data and "episode_unit_id" not in data:
                    result["tokens"].append(data.get("text", ""))
                elif "message" in data and "code" in data:
                    result["error"] = data
        except json.JSONDecodeError:
            pass
    return result


def test_chat():
    """POST /chat - チャットテスト (SSE)"""
    print("\n=== POST /chat ===")
    payload = {
        "user_text": "こんにちは、これはテストメッセージです",
        "images": []
    }
    try:
        event_lines = []
        data_lines = []
        with httpx.stream(
            "POST",
            f"{BASE_URL}/chat",
            headers=get_headers(),
            json=payload,
            timeout=60
        ) as r:
            print(f"  Status: {r.status_code}")
            if r.status_code != 200:
                print(f"  [NG] エラー: {r.read().decode()}")
                return False
            for line in r.iter_lines():
                if line.startswith("event:"):
                    event_lines.append(line[6:].strip())
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())

        events = parse_sse_events(event_lines, data_lines)

        if events["error"]:
            print(f"  [NG] LLMエラー: {events['error']}")
            return False

        if events["done"]:
            reply_text = events["done"].get("reply_text", "")
            episode_id = events["done"].get("episode_unit_id")
            print(f"  受信イベント数: {len(data_lines)}")
            print(f"  Episode ID: {episode_id}")
            print(f"  応答 ({len(reply_text)}文字): {reply_text[:100]}...")
            if reply_text:
                print("  [OK] チャット成功")
                return True
            else:
                print("  [NG] 応答が空")
                return False
        else:
            print("  [NG] doneイベントなし")
            return False
    except Exception as e:
        print(f"  [NG] 例外: {e}")
        return False


def test_chat_with_image():
    """POST /chat - 画像付きチャットテスト"""
    print("\n=== POST /chat (with image) ===")
    base64_data = load_test_image_base64("test_image_2.png")
    if not base64_data:
        return None

    payload = {
        "user_text": "この画像について教えてください（テスト）",
        "images": [{"type": "data_uri", "base64": base64_data}]
    }
    try:
        event_lines = []
        data_lines = []
        with httpx.stream(
            "POST",
            f"{BASE_URL}/chat",
            headers=get_headers(),
            json=payload,
            timeout=60
        ) as r:
            print(f"  Status: {r.status_code}")
            if r.status_code != 200:
                print(f"  [NG] エラー: {r.read().decode()}")
                return False
            for line in r.iter_lines():
                if line.startswith("event:"):
                    event_lines.append(line[6:].strip())
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())

        events = parse_sse_events(event_lines, data_lines)

        if events["error"]:
            print(f"  [NG] LLMエラー: {events['error']}")
            return False

        if events["done"]:
            reply_text = events["done"].get("reply_text", "")
            episode_id = events["done"].get("episode_unit_id")
            print(f"  受信イベント数: {len(data_lines)}")
            print(f"  Episode ID: {episode_id}")
            print(f"  応答 ({len(reply_text)}文字): {reply_text[:100]}...")
            if reply_text:
                print("  [OK] 画像付きチャット成功")
                return True
            else:
                print("  [NG] 応答が空")
                return False
        else:
            print("  [NG] doneイベントなし")
            return False
    except Exception as e:
        print(f"  [NG] 例外: {e}")
        return False


def test_notification():
    """POST /v1/notification - 通知テスト"""
    print("\n=== POST /v1/notification ===")
    base64_data = load_test_image_base64("test_image_1.png")

    payload = {
        "source_system": "test_system",
        "text": "これはテスト通知です",
    }
    if base64_data:
        payload["images"] = [make_data_uri(base64_data)]
    else:
        payload["images"] = []

    try:
        r = httpx.post(f"{BASE_URL}/v1/notification", headers=get_headers(), json=payload, timeout=30)
        print(f"  Status: {r.status_code}")
        if r.status_code == 204:
            print("  [OK] 通知成功")
            return True
        else:
            print(f"  [NG] エラー: {r.text}")
            return False
    except Exception as e:
        print(f"  [NG] 例外: {e}")
        return False


def test_meta_request():
    """POST /v1/meta_request - メタ要求テスト"""
    print("\n=== POST /v1/meta_request ===")
    base64_data = load_test_image_base64("test_image_3.png")

    payload = {
        "instruction": "これはテストのメタ要求です。ユーザーに音がしたと伝えてください。",
        "payload_text": "",
    }
    if base64_data:
        payload["images"] = [make_data_uri(base64_data)]
    else:
        payload["images"] = []

    try:
        r = httpx.post(f"{BASE_URL}/v1/meta_request", headers=get_headers(), json=payload, timeout=30)
        print(f"  Status: {r.status_code}")
        if r.status_code == 204:
            print("  [OK] メタ要求成功")
            return True
        else:
            print(f"  [NG] エラー: {r.text}")
            return False
    except Exception as e:
        print(f"  [NG] 例外: {e}")
        return False


def main():
    print("=" * 50)
    print("cocoro_ghost API テスト")
    print("=" * 50)

    results = {}

    # 各APIテスト実行
    results["settings"] = test_settings_get()
    results["capture"] = test_capture()
    results["chat"] = test_chat()
    results["chat_image"] = test_chat_with_image()
    results["notification"] = test_notification()
    results["meta_request"] = test_meta_request()

    # 結果サマリー
    print("\n" + "=" * 50)
    print("テスト結果サマリー")
    print("=" * 50)

    passed = 0
    failed = 0
    skipped = 0

    for name, result in results.items():
        if result is True:
            status = "OK"
            passed += 1
        elif result is False:
            status = "NG"
            failed += 1
        else:
            status = "SKIP"
            skipped += 1
        print(f"  {name}: {status}")

    print(f"\n合計: {passed} 成功 / {failed} 失敗 / {skipped} スキップ")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
