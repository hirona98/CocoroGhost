"""
外部 capability `web_access` の adapter。

役割:
- `search` / `open_url` / `extract_structured` を共通契約で実行する。
- 結果を `events(source=autonomy_action)` へ永続化し、WritePlan 連携ジョブを投入する。
"""

from __future__ import annotations

from datetime import datetime, timezone
import html
import re
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from cocoro_ghost import common_utils
from cocoro_ghost.autonomy.capability_adapters.base import (
    AdapterExecutionContext,
    AdapterExecutionOutput,
    CapabilityAdapter,
)
from cocoro_ghost.autonomy.scheduler import enqueue_autonomy_cycle_job
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.memory_models import Event, Job
from cocoro_ghost.worker_constants import JOB_PENDING


def _now_utc_ts() -> int:
    """現在UTC時刻をUNIX秒で返す。"""

    return int(time.time())


def _now_utc_iso() -> str:
    """現在UTC時刻をISO-8601文字列で返す。"""

    return datetime.now(timezone.utc).isoformat()


def _enqueue_write_plan_chain(*, embedding_preset_id: str, embedding_dimension: int, event_id: int) -> None:
    """result event 向けの非同期ジョブチェーンを投入する。"""

    # --- 投入時刻を固定 ---
    now_ts = _now_utc_ts()

    # --- 既存経路と同じ3段チェーンを投入 ---
    with memory_session_scope(str(embedding_preset_id), int(embedding_dimension)) as db:
        db.add(
            Job(
                kind="upsert_event_embedding",
                payload_json=common_utils.json_dumps({"event_id": int(event_id)}),
                status=int(JOB_PENDING),
                run_after=int(now_ts),
                tries=0,
                last_error=None,
                created_at=int(now_ts),
                updated_at=int(now_ts),
            )
        )
        db.add(
            Job(
                kind="upsert_event_assistant_summary",
                payload_json=common_utils.json_dumps({"event_id": int(event_id)}),
                status=int(JOB_PENDING),
                run_after=int(now_ts),
                tries=0,
                last_error=None,
                created_at=int(now_ts),
                updated_at=int(now_ts),
            )
        )
        db.add(
            Job(
                kind="generate_write_plan",
                payload_json=common_utils.json_dumps({"event_id": int(event_id)}),
                status=int(JOB_PENDING),
                run_after=int(now_ts),
                tries=0,
                last_error=None,
                created_at=int(now_ts),
                updated_at=int(now_ts),
            )
        )


def _persist_autonomy_action_event(
    *,
    embedding_preset_id: str,
    embedding_dimension: int,
    summary_text: str,
) -> int:
    """`source=autonomy_action` の result event を永続化する。"""

    # --- イベントを1件保存 ---
    now_ts = _now_utc_ts()
    with memory_session_scope(str(embedding_preset_id), int(embedding_dimension)) as db:
        ev = Event(
            created_at=int(now_ts),
            updated_at=int(now_ts),
            client_id=None,
            source="autonomy_action",
            user_text=str(summary_text or "").strip(),
            assistant_text=None,
            entities_json="[]",
            client_context_json=None,
        )
        db.add(ev)
        db.flush()
        return int(ev.event_id)


def _normalize_text(text: str) -> str:
    """HTML混在テキストを平文へ正規化する。"""

    # --- script/style を削除 ---
    no_script = re.sub(r"<script\b[^>]*>.*?</script>", " ", str(text or ""), flags=re.IGNORECASE | re.DOTALL)
    no_style = re.sub(r"<style\b[^>]*>.*?</style>", " ", no_script, flags=re.IGNORECASE | re.DOTALL)

    # --- タグを除去 ---
    no_tags = re.sub(r"<[^>]+>", " ", no_style)

    # --- HTMLエンティティをデコード ---
    decoded = html.unescape(no_tags)

    # --- 空白を圧縮 ---
    return re.sub(r"\s+", " ", decoded).strip()


def _extract_title_from_html(text: str) -> str:
    """HTML文字列から title を抽出する。"""

    # --- title タグを抽出 ---
    m = re.search(r"<title[^>]*>(.*?)</title>", str(text or ""), flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return _normalize_text(str(m.group(1) or ""))


def _unwrap_duckduckgo_url(url: str) -> str:
    """DuckDuckGo リダイレクトURLを実URLへ戻す。"""

    # --- URLを解析 ---
    parsed = urlparse(str(url or ""))
    if parsed.netloc.lower().endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query or "", keep_blank_values=True)
        uddg = str((qs.get("uddg") or [""])[0] or "")
        if uddg:
            return unquote(uddg)
    return str(url or "")


def _extract_duckduckgo_items(*, html_text: str, top_k: int) -> list[dict[str, Any]]:
    """DuckDuckGo HTMLから検索結果を抽出する。"""

    # --- result__a リンクを抽出 ---
    links = re.findall(
        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        str(html_text or ""),
        flags=re.IGNORECASE | re.DOTALL,
    )

    # --- 重複URLを排除して上位件数へ絞る ---
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for href_raw, title_html in list(links or []):
        # --- URLとタイトルを正規化 ---
        url = _unwrap_duckduckgo_url(str(href_raw or "").strip())
        if not url:
            continue
        if url in seen:
            continue
        seen.add(url)

        title = _normalize_text(str(title_html or ""))
        if not title:
            title = url

        out.append(
            {
                "title": str(title),
                "url": str(url),
                "snippet": "",
                "published_at": None,
            }
        )
        if len(out) >= int(top_k):
            break
    return out


class WebAccessCapabilityAdapter(CapabilityAdapter):
    """`web_access` capability adapter 実装。"""

    @property
    def capability_id(self) -> str:
        """担当 capability_id を返す。"""

        return "web_access"

    def _run_search(
        self,
        *,
        context: AdapterExecutionContext,
        input_payload: dict[str, Any],
        timeout_seconds: int,
    ) -> AdapterExecutionOutput:
        """`search` を実行する。"""

        # --- 入力を正規化 ---
        query = str(input_payload.get("query") or "").strip()
        if not query:
            raise RuntimeError("query is required")
        top_k = int(input_payload.get("top_k"))
        recency_days = input_payload.get("recency_days")
        domains = [str(x).strip() for x in list(input_payload.get("domains") or []) if str(x).strip()]
        locale = str(input_payload.get("locale") or "").strip()
        if not locale:
            raise RuntimeError("locale is required")

        # --- ドメイン制限をクエリへ反映 ---
        query_text = query
        if domains:
            domain_clause = " OR ".join([f"site:{d}" for d in domains])
            query_text = f"{query} ({domain_clause})"

        # --- DuckDuckGo HTML検索を実行 ---
        params = {
            "q": query_text,
            "kl": locale.lower().replace("_", "-"),
        }
        if recency_days is not None:
            params["df"] = "d"
        url = "https://duckduckgo.com/html/"
        with httpx.Client(timeout=float(timeout_seconds), follow_redirects=True) as client:
            resp = client.get(url, params=params)
        if int(resp.status_code) < 200 or int(resp.status_code) >= 300:
            raise RuntimeError(f"search request failed status_code={int(resp.status_code)}")

        # --- 結果抽出 ---
        items = _extract_duckduckgo_items(html_text=str(resp.text or ""), top_k=int(top_k))
        urls = [str(item.get("url") or "") for item in list(items or []) if str(item.get("url") or "")]

        # --- result event を永続化 ---
        summary_text = f"web_access.search query={query} results={len(items)}"
        event_id = _persist_autonomy_action_event(
            embedding_preset_id=str(context.embedding_preset_id),
            embedding_dimension=int(context.embedding_dimension),
            summary_text=str(summary_text),
        )
        _enqueue_write_plan_chain(
            embedding_preset_id=str(context.embedding_preset_id),
            embedding_dimension=int(context.embedding_dimension),
            event_id=int(event_id),
        )
        enqueue_autonomy_cycle_job(
            embedding_preset_id=str(context.embedding_preset_id),
            embedding_dimension=int(context.embedding_dimension),
            trigger_type="event_created",
            event_id=int(event_id),
        )

        # --- 実行結果を返す ---
        return AdapterExecutionOutput(
            result_payload={"items": list(items)},
            effects=[
                {
                    "effect_type": "web.search_result",
                    "query": str(query),
                    "urls": list(urls),
                }
            ],
            meta_json={"event_id": int(event_id)},
        )

    def _run_open_url(
        self,
        *,
        context: AdapterExecutionContext,
        input_payload: dict[str, Any],
        timeout_seconds: int,
    ) -> AdapterExecutionOutput:
        """`open_url` を実行する。"""

        # --- 入力を正規化 ---
        url = str(input_payload.get("url") or "").strip()
        if not url:
            raise RuntimeError("url is required")
        max_chars = int(input_payload.get("max_chars"))

        # --- URL本文を取得 ---
        with httpx.Client(timeout=float(timeout_seconds), follow_redirects=True) as client:
            resp = client.get(str(url))
        if int(resp.status_code) < 200 or int(resp.status_code) >= 300:
            raise RuntimeError(f"open_url request failed status_code={int(resp.status_code)}")

        # --- タイトル/本文を抽出 ---
        html_text = str(resp.text or "")
        title = _extract_title_from_html(html_text)
        text_plain = _normalize_text(html_text)
        if int(max_chars) > 0 and len(text_plain) > int(max_chars):
            text_plain = text_plain[: int(max_chars)]
        fetched_at = _now_utc_iso()
        text_digest = text_plain[:300].strip() or "(empty)"

        # --- result event を永続化 ---
        summary_text = f"web_access.open_url url={url} title={title} chars={len(text_plain)}"
        event_id = _persist_autonomy_action_event(
            embedding_preset_id=str(context.embedding_preset_id),
            embedding_dimension=int(context.embedding_dimension),
            summary_text=str(summary_text),
        )
        _enqueue_write_plan_chain(
            embedding_preset_id=str(context.embedding_preset_id),
            embedding_dimension=int(context.embedding_dimension),
            event_id=int(event_id),
        )
        enqueue_autonomy_cycle_job(
            embedding_preset_id=str(context.embedding_preset_id),
            embedding_dimension=int(context.embedding_dimension),
            trigger_type="event_created",
            event_id=int(event_id),
        )

        # --- 実行結果を返す ---
        return AdapterExecutionOutput(
            result_payload={
                "url": str(url),
                "title": str(title),
                "text": str(text_plain),
                "fetched_at": str(fetched_at),
            },
            effects=[
                {
                    "effect_type": "web.page_opened",
                    "url": str(url),
                    "text_digest": str(text_digest),
                }
            ],
            meta_json={"event_id": int(event_id)},
        )

    def _run_extract_structured(
        self,
        *,
        context: AdapterExecutionContext,
        input_payload: dict[str, Any],
        timeout_seconds: int,
    ) -> AdapterExecutionOutput:
        """`extract_structured` を実行する。"""

        # --- 未使用引数を明示 ---
        _ = timeout_seconds

        # --- 入力を正規化 ---
        source_url = str(input_payload.get("source_url") or "").strip()
        source_text = str(input_payload.get("source_text") or "").strip()
        target_schema_json = input_payload.get("target_schema_json")
        if not source_url:
            raise RuntimeError("source_url is required")
        if not source_text:
            raise RuntimeError("source_text is required")
        if not isinstance(target_schema_json, dict):
            raise RuntimeError("target_schema_json must be object")

        # --- target schema の properties を走査して key:value 形式を抽出 ---
        structured: dict[str, Any] = {}
        properties = target_schema_json.get("properties", {})
        if isinstance(properties, dict):
            for key in list(properties.keys()):
                key_s = str(key or "").strip()
                if not key_s:
                    continue
                pattern = rf"(?im)^\s*{re.escape(key_s)}\s*[:：]\s*(.+)$"
                m = re.search(pattern, source_text)
                if m:
                    structured[key_s] = str(m.group(1) or "").strip()

        # --- 抽出ゼロでも空オブジェクトを返す ---
        keys = sorted([str(k) for k in structured.keys() if str(k)])

        # --- result event を永続化 ---
        summary_text = f"web_access.extract_structured source_url={source_url} keys={','.join(keys)}"
        event_id = _persist_autonomy_action_event(
            embedding_preset_id=str(context.embedding_preset_id),
            embedding_dimension=int(context.embedding_dimension),
            summary_text=str(summary_text),
        )
        _enqueue_write_plan_chain(
            embedding_preset_id=str(context.embedding_preset_id),
            embedding_dimension=int(context.embedding_dimension),
            event_id=int(event_id),
        )
        enqueue_autonomy_cycle_job(
            embedding_preset_id=str(context.embedding_preset_id),
            embedding_dimension=int(context.embedding_dimension),
            trigger_type="event_created",
            event_id=int(event_id),
        )

        # --- 実行結果を返す ---
        return AdapterExecutionOutput(
            result_payload={
                "source_url": str(source_url),
                "structured_data_json": dict(structured),
            },
            effects=[
                {
                    "effect_type": "web.structured_extracted",
                    "source_url": str(source_url),
                    "keys": list(keys),
                }
            ],
            meta_json={"event_id": int(event_id)},
        )

    def execute(
        self,
        *,
        context: AdapterExecutionContext,
        input_payload: dict[str, Any],
        timeout_seconds: int,
    ) -> AdapterExecutionOutput:
        """`web_access` operation を1回実行する。"""

        # --- operation ごとに分岐 ---
        operation = str(context.operation or "").strip()
        if operation == "search":
            return self._run_search(
                context=context,
                input_payload=dict(input_payload or {}),
                timeout_seconds=int(timeout_seconds),
            )
        if operation == "open_url":
            return self._run_open_url(
                context=context,
                input_payload=dict(input_payload or {}),
                timeout_seconds=int(timeout_seconds),
            )
        if operation == "extract_structured":
            return self._run_extract_structured(
                context=context,
                input_payload=dict(input_payload or {}),
                timeout_seconds=int(timeout_seconds),
            )
        raise RuntimeError(f"unsupported operation: {operation}")
