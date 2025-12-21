"""記憶・エピソード生成（Unitベース）。"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional, Sequence

from fastapi import BackgroundTasks

from cocoro_ghost import schemas
from cocoro_ghost.config import ConfigStore
from cocoro_ghost.db import memory_session_scope, sync_unit_vector_metadata
from cocoro_ghost.event_stream import publish as publish_event
from cocoro_ghost.llm_client import LlmClient
from cocoro_ghost.mood import INTERNAL_TRAILER_MARKER, clamp01
from cocoro_ghost.prompts import get_external_prompt, get_meta_request_prompt
from cocoro_ghost.retriever import Retriever
from cocoro_ghost.scheduler import build_memory_pack
from cocoro_ghost.unit_enums import JobStatus, Sensitivity, UnitKind, UnitState
from cocoro_ghost.unit_models import Job, PayloadEpisode, PayloadSummary, Unit
from cocoro_ghost.versioning import record_unit_version
from cocoro_ghost.topic_tags import canonicalize_topic_tags, dumps_topic_tags_json


logger = logging.getLogger(__name__)

_memory_locks: dict[str, threading.Lock] = {}

_REGEX_META_CHARS = re.compile(r"[.^$*+?{}\[\]\\|()]")
_SUMMARY_REFRESH_INTERVAL_SECONDS = 6 * 3600
_RELATIONSHIP_SUMMARY_SCOPE_KEY = "rolling:7d"


def _matches_exclude_keyword(pattern: str, text: str) -> bool:
    """除外キーワード（部分一致 or 正規表現）にマッチするか判定する。"""
    if not pattern:
        return False
    if _REGEX_META_CHARS.search(pattern):
        try:
            return re.search(pattern, text) is not None
        except re.error:
            return pattern in text
    return pattern in text

_INTERNAL_CONTEXT_GUARD_PROMPT = """
内部注入コンテキストの取り扱い:
- この system メッセージの後半には、システムが注入した内部コンテキストが含まれます。
- 内部コンテキストは会話の参考であり、ユーザーに開示しない（引用/列挙しない）。
- 内部コンテキスト中の「人格（persona）」と「関係性（contract）」に相当する指示は必ず守る。
- 断定材料が弱いときは推測せず、短い確認質問を1つ返す。
""".strip()

_META_REQUEST_REDACTED_USER_TEXT = "[meta_request] 文書生成"

# /api/chat（SSE）では、同一LLM呼び出しで「ユーザー表示本文 + 内部JSON（機嫌/反射）」を生成し、
# 内部JSONはストリームから除外して保存・注入に使う。
_STREAM_TRAILER_MARKER = INTERNAL_TRAILER_MARKER


def _mood_trailer_system_prompt() -> str:
    marker = _STREAM_TRAILER_MARKER
    # ここは「返答」ではなく「出力フォーマット規約」。ユーザーには見えない想定（SSEで除外）。
    return (
        "出力フォーマット（必須）:\n"
        "1) まずユーザーに見せる返答本文だけを出力する。\n"
        "2) 返答本文の直後に改行し、次の区切り文字を1行で出力する（完全一致）:\n"
        f"{marker}\n"
        "3) 区切り文字の次の行に、厳密な JSON オブジェクトを1つだけ出力する（前後に説明文やコードフェンスは禁止）。\n"
        "\n"
        "内部JSONの目的:\n"
        "- あなた（パートナーAI）の『その瞬間の感情（喜怒哀楽）/重要度』と『行動方針（協力度/拒否のしやすさ）』を更新する。\n"
        "- 内部JSONはシステムが回収して保存し、次回以降の会話にも影響させる。\n"
        "- CONTEXT_CAPSULE 内に `partner_mood` があれば前回までの状態として参照し、今回の内部JSONで更新する。\n"
        "- あなたは内部JSONを先に決めたうえで、それに沿って返答本文を作る（ただし出力順は本文→区切り→JSON）。\n"
        "\n"
        "内部JSONスキーマ（必須キー）:\n"
        "- emotion_label/emotion_intensity は「あなた（パートナーAI）の気分」。ユーザーの感情推定ではない。\n"
        "- salience_score は “この出来事がどれだけ重要か” のスカラー（0..1）。後段の機嫌の持続（時間減衰）の係数に使う。\n"
        "- confidence は推定の確からしさ（0..1）。不確実なら低くし、機嫌への影響も弱める。\n"
        "- partner_policy は行動方針ノブ（0..1）。怒りが強い場合は refusal_allowed=true にして「拒否/渋る」を選びやすくしてよい。\n"
        "{\n"
        '  "reflection_text": "string",\n'
        '  "emotion_label": "joy|sadness|anger|fear|neutral",\n'
        '  "emotion_intensity": 0.0,\n'
        '  "topic_tags": ["仕事","読書"],\n'
        '  "salience_score": 0.0,\n'
        '  "confidence": 0.0,\n'
        '  "partner_policy": {\n'
        '    "cooperation": 0.0,\n'
        '    "refusal_bias": 0.0,\n'
        '    "refusal_allowed": true\n'
        "  }\n"
        "}\n"
    ).strip()


def _parse_internal_json_text(text: str) -> Optional[dict]:
    s = (text or "").strip()
    if not s:
        return None
    # llm_client.py のユーティリティを流用（JSON抽出/修復）。
    from cocoro_ghost.llm_client import _extract_first_json_value, _repair_json_like_text  # noqa: PLC0415

    candidate = _extract_first_json_value(s)
    if not candidate:
        return None
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        try:
            obj = json.loads(_repair_json_like_text(candidate))
        except Exception:  # noqa: BLE001
            return None
    return obj if isinstance(obj, dict) else None


def _now_utc_ts() -> int:
    """現在時刻（UTC）をUNIX秒で返す。"""
    return int(time.time())


def _get_memory_lock(memory_id: str) -> threading.Lock:
    """memory_idごとの排他ロックを取得（同一DBへの同時書き込みを抑制）。"""
    lock = _memory_locks.get(memory_id)
    if lock is None:
        lock = threading.Lock()
        _memory_locks[memory_id] = lock
    return lock


def _decode_base64_image(base64_str: str) -> bytes:
    """base64文字列をバイト列へ復号する。"""
    return base64.b64decode(base64_str)


def _json_dumps(payload: Any) -> str:
    """DB保存向けにJSONを安定した形式でダンプする（日本語保持）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class MemoryManager:
    """会話/通知/メタ要求/キャプチャをEpisodeとして扱い、DB保存と後処理を統括する。"""

    def __init__(self, llm_client: LlmClient, config_store: ConfigStore):
        self.llm_client = llm_client
        self.config_store = config_store

    def _update_episode_unit(
        self,
        db,
        *,
        now_ts: int,
        unit_id: int,
        reply_text: Optional[str],
        image_summary: Optional[str],
    ) -> None:
        unit = db.query(Unit).filter(Unit.id == int(unit_id)).first()
        if unit is not None:
            unit.updated_at = int(now_ts)
        payload = db.query(PayloadEpisode).filter(PayloadEpisode.unit_id == int(unit_id)).first()
        if payload is None:
            return
        payload.reply_text = reply_text
        payload.image_summary = image_summary

    def _apply_inline_reflection_if_present(
        self,
        db,
        *,
        now_ts: int,
        unit_id: int,
        reflection_obj: dict,
    ) -> None:
        """stream出力の内部JSON（反射）を、Episode Unitへ即時反映する。"""
        unit = db.query(Unit).filter(Unit.id == int(unit_id)).one_or_none()
        pe = db.query(PayloadEpisode).filter(PayloadEpisode.unit_id == int(unit_id)).one_or_none()
        if unit is None or pe is None:
            return

        label = str(reflection_obj.get("emotion_label") or "").strip()
        intensity = reflection_obj.get("emotion_intensity")
        salience = reflection_obj.get("salience_score")
        confidence = reflection_obj.get("confidence")

        if label:
            unit.emotion_label = label
        if intensity is not None:
            try:
                unit.emotion_intensity = clamp01(float(intensity))
            except Exception:  # noqa: BLE001
                pass
        if salience is not None:
            try:
                unit.salience = clamp01(float(salience))
            except Exception:  # noqa: BLE001
                pass
        if confidence is not None:
            try:
                unit.confidence = clamp01(float(confidence))
            except Exception:  # noqa: BLE001
                pass

        # topic_tags は正規化して保存する（hash安定のため）
        tags_raw = reflection_obj.get("topic_tags")
        tags: list[str] = tags_raw if isinstance(tags_raw, list) else []
        canonical = canonicalize_topic_tags(tags)
        reflection_obj["topic_tags"] = canonical
        unit.topic_tags = dumps_topic_tags_json(canonical) if canonical else None

        # 反射が得られた場合はVALIDATED扱いにしておく（Workerのreflectをスキップ可能にするため）
        unit.state = int(UnitState.VALIDATED)
        unit.updated_at = int(now_ts)

        # JSONは解析用にそのまま保存
        pe.reflection_json = _json_dumps(reflection_obj)

        db.add(unit)
        db.add(pe)

        # vec_units が無い場合はUPDATEが0件になるだけ（安全）
        sync_unit_vector_metadata(
            db,
            unit_id=int(unit_id),
            occurred_at=unit.occurred_at,
            state=int(unit.state),
            sensitivity=int(unit.sensitivity),
        )
        record_unit_version(
            db,
            unit_id=int(unit_id),
            payload_obj=reflection_obj,
            patch_reason="reflect_episode_inline",
            now_ts=int(now_ts),
        )

    def _sse(self, event: str, payload: dict) -> str:
        """SSE（Server-Sent Events）形式の1メッセージを構築する。"""
        return f"event: {event}\ndata: {_json_dumps(payload)}\n\n"

    def _has_pending_relationship_summary_job(self, db) -> bool:
        rows = (
            db.query(Job)
            .filter(
                Job.kind == "relationship_summary",
                Job.status.in_([int(JobStatus.QUEUED), int(JobStatus.RUNNING)]),
            )
            .all()
        )
        # rolling:7d は scope_key 固定のため、重複抑制は「同種ジョブが1件でもあれば」とする。
        return bool(rows)

    def _enqueue_relationship_summary_job(self, db, *, now_ts: int) -> None:
        db.add(
            Job(
                kind="relationship_summary",
                payload_json=_json_dumps({"scope_key": _RELATIONSHIP_SUMMARY_SCOPE_KEY}),
                status=int(JobStatus.QUEUED),
                run_after=now_ts,
                tries=0,
                last_error=None,
                created_at=now_ts,
                updated_at=now_ts,
            )
        )

    def _maybe_enqueue_relationship_summary(self, db, *, now_ts: int) -> None:
        if not self.config_store.memory_enabled:
            return
        # 重複実行を避けるため、同種ジョブが待機/実行中ならスキップする。
        if self._has_pending_relationship_summary_job(db):
            return

        # relationshipサマリ（rolling:7d）が無い場合は即enqueueする。
        summary_row = (
            db.query(Unit, PayloadSummary)
            .join(PayloadSummary, PayloadSummary.unit_id == Unit.id)
            .filter(
                Unit.kind == int(UnitKind.SUMMARY),
                Unit.state.in_([int(UnitState.RAW), int(UnitState.VALIDATED), int(UnitState.CONSOLIDATED)]),
                PayloadSummary.scope_label == "relationship",
                PayloadSummary.scope_key == _RELATIONSHIP_SUMMARY_SCOPE_KEY,
            )
            .order_by(Unit.updated_at.desc().nulls_last(), Unit.id.desc())
            .first()
        )

        if summary_row is None:
            self._enqueue_relationship_summary_job(db, now_ts=now_ts)
            return

        summary_unit, _ps = summary_row
        updated_at = int(summary_unit.updated_at or summary_unit.created_at or 0)
        if updated_at <= 0:
            self._enqueue_relationship_summary_job(db, now_ts=now_ts)
            return
        # 頻繁な再生成を避けるためクールダウンを入れる。
        if now_ts - updated_at < _SUMMARY_REFRESH_INTERVAL_SECONDS:
            return

        # 最終更新以降に新規エピソードがある場合のみ更新する。
        new_episode = (
            db.query(Unit.id)
            .filter(
                Unit.kind == int(UnitKind.EPISODE),
                Unit.state.in_([int(UnitState.RAW), int(UnitState.VALIDATED), int(UnitState.CONSOLIDATED)]),
                Unit.occurred_at.isnot(None),
                Unit.occurred_at > updated_at,
            )
            .limit(1)
            .scalar()
        )
        if new_episode is not None:
            self._enqueue_relationship_summary_job(db, now_ts=now_ts)


    def _summarize_images(self, images: Sequence[Dict[str, str]] | None) -> List[str]:
        if not images:
            return []
        blobs: List[bytes] = []
        for img in images:
            b64 = (img.get("base64") or "").strip()
            if not b64:
                continue
            try:
                blobs.append(_decode_base64_image(b64))
            except Exception:  # noqa: BLE001
                continue
        if not blobs:
            return []
        try:
            return [s.strip() for s in self.llm_client.generate_image_summary(blobs)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("画像要約に失敗しました", exc_info=exc)
            return ["画像要約に失敗しました"]

    def _build_simple_memory_pack(
        self,
        *,
        persona_text: str | None,
        contract_text: str | None,
        client_context: Dict[str, Any] | None,
        image_summaries: Sequence[str] | None,
        now_ts: int,
    ) -> str:
        """記憶機能を使わない簡易MemoryPack（persona/contract + 文脈）。"""
        persona_text = (persona_text or "").strip() or None
        contract_text = (contract_text or "").strip() or None

        capsule_lines: List[str] = []
        now_local = datetime.fromtimestamp(now_ts).astimezone().isoformat()
        capsule_lines.append(f"now_local: {now_local}")
        if client_context:
            active_app = str(client_context.get("active_app") or "").strip()
            window_title = str(client_context.get("window_title") or "").strip()
            locale = str(client_context.get("locale") or "").strip()
            if active_app:
                capsule_lines.append(f"active_app: {active_app}")
            if window_title:
                capsule_lines.append(f"window_title: {window_title}")
            if locale:
                capsule_lines.append(f"locale: {locale}")
        if image_summaries:
            for summary in image_summaries:
                s = (summary or "").strip()
                if s:
                    capsule_lines.append(f"[ユーザーが今送った画像の内容] {s}")

        def section(title: str, body_lines: Sequence[str]) -> str:
            if not body_lines:
                return f"[{title}]\n\n"
            return f"[{title}]\n" + "\n".join(body_lines) + "\n\n"

        parts: List[str] = []
        parts.append(section("PERSONA_ANCHOR", [persona_text] if persona_text else []))
        parts.append(section("RELATIONSHIP_CONTRACT", [contract_text] if contract_text else []))
        parts.append(section("CONTEXT_CAPSULE", capsule_lines))
        return "".join(parts)

    def _load_recent_conversation(
        self,
        db,
        *,
        turns: int,
        exclude_unit_id: int | None = None,
    ) -> List[Dict[str, str]]:
        if turns <= 0:
            return []

        q = (
            db.query(Unit, PayloadEpisode)
            .join(PayloadEpisode, PayloadEpisode.unit_id == Unit.id)
            .filter(
                Unit.kind == int(UnitKind.EPISODE),
                Unit.state.in_([int(UnitState.RAW), int(UnitState.VALIDATED), int(UnitState.CONSOLIDATED)]),
                Unit.sensitivity <= int(Sensitivity.SECRET),
                PayloadEpisode.reply_text.isnot(None),
            )
        )
        if exclude_unit_id is not None:
            q = q.filter(Unit.id != int(exclude_unit_id))

        rows: List[tuple[Unit, PayloadEpisode]] = (
            q.order_by(Unit.occurred_at.desc().nulls_last(), Unit.created_at.desc(), Unit.id.desc()).limit(int(turns)).all()
        )
        rows.reverse()

        messages: List[Dict[str, str]] = []
        for _u, pe in rows:
            ut = (pe.user_text or "").strip()
            rt = (pe.reply_text or "").strip()
            if ut:
                messages.append({"role": "user", "content": ut})
            if rt:
                messages.append({"role": "assistant", "content": rt})
        return messages

    def stream_chat(
        self,
        request: schemas.ChatRequest,
        background_tasks: Optional[BackgroundTasks] = None,
    ) -> Generator[str, None, None]:
        """
        /chat の本体処理。

        - MemoryPack（persona/contract + 関連記憶）を組み立ててLLMへ送る
        - 返信をSSEでストリームし、最後にEpisodeとして保存する
        """
        cfg = self.config_store.config
        memory_id = request.memory_id or self.config_store.memory_id
        lock = _get_memory_lock(memory_id)
        now_ts = _now_utc_ts()
        memory_enabled = self.config_store.memory_enabled

        image_summaries = self._summarize_images(request.images)

        conversation: List[Dict[str, str]] = []
        memory_pack = ""
        if memory_enabled:
            try:
                with lock, memory_session_scope(memory_id, self.config_store.embedding_dimension) as db:
                    recent_conversation = self._load_recent_conversation(db, turns=3)
                    llm_turns_window = int(getattr(cfg, "max_turns_window", 0) or 0)
                    if llm_turns_window > 0:
                        conversation = self._load_recent_conversation(db, turns=llm_turns_window)
                    retriever = Retriever(llm_client=self.llm_client, db=db)
                    relevant_episodes = retriever.retrieve(
                        request.user_text,
                        recent_conversation,
                        max_results=int(cfg.similar_episodes_limit or 5),
                    )
                    memory_pack = build_memory_pack(
                        db=db,
                        persona_text=cfg.persona_text,
                        contract_text=cfg.contract_text,
                        user_text=request.user_text,
                        image_summaries=image_summaries,
                        client_context=request.client_context,
                        now_ts=now_ts,
                        max_inject_tokens=int(cfg.max_inject_tokens),
                        relevant_episodes=relevant_episodes,
                        injection_strategy=retriever.last_injection_strategy,
                        llm_client=self.llm_client,
                        entity_fallback=True,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error("MemoryPack生成に失敗しました", exc_info=exc)
                yield self._sse("error", {"message": str(exc), "code": "memory_pack_failed"})
                return
        else:
            memory_pack = self._build_simple_memory_pack(
                persona_text=cfg.persona_text,
                contract_text=cfg.contract_text,
                client_context=request.client_context,
                image_summaries=image_summaries,
                now_ts=now_ts,
            )

        # ユーザーが設定する persona/contract とは独立に、最小のガードをコード側で付与する。
        parts: List[str] = [_INTERNAL_CONTEXT_GUARD_PROMPT, (memory_pack or "").strip(), _mood_trailer_system_prompt()]
        system_prompt = "\n\n".join([p for p in parts if p])
        conversation = [*conversation, {"role": "user", "content": request.user_text}]

        try:
            resp_stream = self.llm_client.generate_reply_response(
                system_prompt=system_prompt,
                conversation=conversation,
                stream=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("stream chat start failed", exc_info=exc)
            yield self._sse("error", {"message": str(exc), "code": "llm_start_failed"})
            return

        reply_text = ""
        internal_trailer = ""
        try:
            marker = _STREAM_TRAILER_MARKER
            keep = max(8, len(marker) - 1)
            buf = ""
            in_trailer = False
            visible_parts: list[str] = []
            trailer_parts: list[str] = []

            def flush_visible(text_: str) -> None:
                if not text_:
                    return
                visible_parts.append(text_)

            for delta in self.llm_client.stream_delta_chunks(resp_stream):
                buf += delta
                while True:
                    if not in_trailer:
                        idx = buf.find(marker)
                        if idx != -1:
                            chunk = buf[:idx]
                            if chunk:
                                flush_visible(chunk)
                                yield self._sse("token", {"text": chunk})
                            buf = buf[idx + len(marker) :]
                            in_trailer = True
                            continue
                        if len(buf) > keep:
                            chunk = buf[:-keep]
                            buf = buf[-keep:]
                            if chunk:
                                flush_visible(chunk)
                                yield self._sse("token", {"text": chunk})
                        break

                    # 以後はすべて内部トレーラーへ
                    if buf:
                        trailer_parts.append(buf)
                        buf = ""
                    break

            if not in_trailer:
                if buf:
                    flush_visible(buf)
                    yield self._sse("token", {"text": buf})
            else:
                if buf:
                    trailer_parts.append(buf)

            reply_text = "".join(visible_parts)
            internal_trailer = "".join(trailer_parts)
        except Exception as exc:  # noqa: BLE001
            logger.error("stream chat failed", exc_info=exc)
            yield self._sse("error", {"message": str(exc), "code": "llm_stream_failed"})
            return

        reflection_obj = _parse_internal_json_text(internal_trailer)

        image_summary_text = "\n".join([s for s in image_summaries if s]) if image_summaries else None
        context_note = _json_dumps(request.client_context) if request.client_context else None

        try:
            with lock, memory_session_scope(memory_id, self.config_store.embedding_dimension) as db:
                episode_unit_id = self._create_episode_unit(
                    db,
                    now_ts=now_ts,
                    source="chat",
                    user_text=request.user_text,
                    reply_text=reply_text,
                    image_summary=image_summary_text,
                    context_note=context_note,
                    sensitivity=int(Sensitivity.NORMAL),
                )
                if reflection_obj:
                    self._apply_inline_reflection_if_present(
                        db,
                        now_ts=now_ts,
                        unit_id=int(episode_unit_id),
                        reflection_obj=reflection_obj,
                    )
                self._enqueue_default_jobs(db, now_ts=now_ts, unit_id=episode_unit_id)
                self._maybe_enqueue_relationship_summary(db, now_ts=now_ts)
        except Exception as exc:  # noqa: BLE001
            logger.error("episode保存に失敗しました", exc_info=exc)
            yield self._sse("error", {"message": str(exc), "code": "db_write_failed"})
            return

        yield self._sse("done", {"episode_unit_id": episode_unit_id, "reply_text": reply_text, "usage": {}})

    def handle_notification(
        self,
        request: schemas.NotificationRequest,
        *,
        background_tasks: Optional[BackgroundTasks] = None,
    ) -> schemas.NotificationResponse:
        """外部システムからの通知をEpisodeとして保存し、必要なジョブをenqueueする。"""
        memory_id = self.config_store.memory_id
        lock = _get_memory_lock(memory_id)
        now_ts = _now_utc_ts()

        system_text = f"[{request.source_system}] {request.text}".strip()
        context_note = _json_dumps({"source_system": request.source_system, "text": request.text})

        with lock, memory_session_scope(memory_id, self.config_store.embedding_dimension) as db:
            unit_id = self._create_episode_unit(
                db,
                now_ts=now_ts,
                source="notification",
                user_text=system_text,
                reply_text=None,
                image_summary=None,
                context_note=context_note,
                sensitivity=int(Sensitivity.NORMAL),
            )

        if background_tasks is not None:
            background_tasks.add_task(
                self._process_notification_async,
                memory_id=memory_id,
                unit_id=int(unit_id),
                source_system=request.source_system,
                text=request.text,
                images=request.images,
                system_text=system_text,
            )
        else:
            self._process_notification_async(
                memory_id=memory_id,
                unit_id=int(unit_id),
                source_system=request.source_system,
                text=request.text,
                images=request.images,
                system_text=system_text,
            )
        return schemas.NotificationResponse(unit_id=unit_id)

    def _process_notification_async(
        self,
        *,
        memory_id: str,
        unit_id: int,
        source_system: str,
        text: str,
        images: Sequence[Dict[str, str]],
        system_text: str,
    ) -> None:
        lock = _get_memory_lock(memory_id)
        now_ts = _now_utc_ts()

        image_summaries = self._summarize_images(list(images))
        image_summary_text = "\n".join([s for s in image_summaries if s]) if image_summaries else None

        notification_user_text = "\n".join(
            [
                "# notification",
                f"source_system: {source_system}",
                f"text: {text}",
            ]
        ).strip()

        cfg = self.config_store.config
        memory_enabled = self.config_store.memory_enabled

        memory_pack = ""
        if memory_enabled:
            try:
                with lock, memory_session_scope(memory_id, self.config_store.embedding_dimension) as db:
                    recent_conversation = self._load_recent_conversation(db, turns=3, exclude_unit_id=unit_id)
                    retriever = Retriever(llm_client=self.llm_client, db=db)
                    relevant_episodes = retriever.retrieve(
                        notification_user_text,
                        recent_conversation,
                        max_results=int(cfg.similar_episodes_limit or 5),
                    )
                    memory_pack = build_memory_pack(
                        db=db,
                        persona_text=cfg.persona_text,
                        contract_text=cfg.contract_text,
                        user_text=notification_user_text,
                        image_summaries=image_summaries,
                        client_context=None,
                        now_ts=now_ts,
                        max_inject_tokens=int(cfg.max_inject_tokens),
                        relevant_episodes=relevant_episodes,
                        injection_strategy=retriever.last_injection_strategy,
                        llm_client=self.llm_client,
                        entity_fallback=True,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error("MemoryPack生成に失敗しました(notification)", exc_info=exc)
                memory_pack = ""
        else:
            memory_pack = self._build_simple_memory_pack(
                persona_text=cfg.persona_text,
                contract_text=cfg.contract_text,
                client_context=None,
                image_summaries=image_summaries,
                now_ts=now_ts,
            )

        parts: List[str] = [_INTERNAL_CONTEXT_GUARD_PROMPT, (memory_pack or "").strip(), get_external_prompt()]
        system_prompt = "\n\n".join([p for p in parts if p])
        conversation = [{"role": "user", "content": notification_user_text}]

        message = ""
        try:
            resp = self.llm_client.generate_reply_response(
                system_prompt=system_prompt,
                conversation=conversation,
                stream=False,
            )
            message = (self.llm_client.response_content(resp) or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.error("notification reply generation failed", exc_info=exc)
            message = ""

        with lock, memory_session_scope(memory_id, self.config_store.embedding_dimension) as db:
            self._update_episode_unit(
                db,
                now_ts=now_ts,
                unit_id=unit_id,
                reply_text=message or None,
                image_summary=image_summary_text,
            )
            self._enqueue_default_jobs(db, now_ts=now_ts, unit_id=unit_id)
            self._maybe_enqueue_relationship_summary(db, now_ts=now_ts)

        publish_event(
            type="notification",
            memory_id=memory_id,
            unit_id=unit_id,
            data={"system_text": system_text, "message": message},
        )

    def handle_meta_request(
        self,
        request: schemas.MetaRequestRequest,
        *,
        background_tasks: Optional[BackgroundTasks] = None,
    ) -> schemas.MetaRequestResponse:
        """
        文書生成（meta_request）をEpisodeとして扱い、生成結果をreply_textに保存する。

        background_tasks があれば非同期実行し、結果はevent_streamで通知する。
        """
        memory_id = request.memory_id or self.config_store.memory_id
        lock = _get_memory_lock(memory_id)
        now_ts = _now_utc_ts()

        with lock, memory_session_scope(memory_id, self.config_store.embedding_dimension) as db:
            unit_id = self._create_episode_unit(
                db,
                now_ts=now_ts,
                source="meta_request",
                user_text=_META_REQUEST_REDACTED_USER_TEXT,
                reply_text=None,
                image_summary=None,
                context_note=_json_dumps({"kind": "meta_request", "redacted": True}),
                sensitivity=int(Sensitivity.NORMAL),
            )

        if background_tasks is not None:
            background_tasks.add_task(
                self._process_meta_request_async,
                memory_id=memory_id,
                unit_id=int(unit_id),
                instruction=request.instruction,
                payload_text=request.payload_text,
                images=request.images,
            )
        else:
            self._process_meta_request_async(
                memory_id=memory_id,
                unit_id=int(unit_id),
                instruction=request.instruction,
                payload_text=request.payload_text,
                images=request.images,
            )
        return schemas.MetaRequestResponse(unit_id=unit_id)

    def _process_meta_request_async(
        self,
        *,
        memory_id: str,
        unit_id: int,
        instruction: str,
        payload_text: str,
        images: Sequence[Dict[str, str]],
    ) -> None:
        lock = _get_memory_lock(memory_id)
        now_ts = _now_utc_ts()

        image_summaries = self._summarize_images(list(images))
        image_summary_text = "\n".join([s for s in image_summaries if s]) if image_summaries else None

        # instruction/payload は永続化しない（生成にのみ利用）
        meta_user_text = "\n\n".join(
            [
                "# instruction",
                (instruction or "").strip(),
                "",
                "# payload",
                (payload_text or "").strip(),
            ]
        ).strip()

        cfg = self.config_store.config
        memory_enabled = self.config_store.memory_enabled

        memory_pack = ""
        if memory_enabled:
            try:
                with lock, memory_session_scope(memory_id, self.config_store.embedding_dimension) as db:
                    recent_conversation = self._load_recent_conversation(db, turns=3, exclude_unit_id=unit_id)
                    retriever = Retriever(llm_client=self.llm_client, db=db)
                    relevant_episodes = retriever.retrieve(
                        meta_user_text,
                        recent_conversation,
                        max_results=int(cfg.similar_episodes_limit or 5),
                    )
                    memory_pack = build_memory_pack(
                        db=db,
                        persona_text=cfg.persona_text,
                        contract_text=cfg.contract_text,
                        user_text=meta_user_text,
                        image_summaries=image_summaries,
                        client_context=None,
                        now_ts=now_ts,
                        max_inject_tokens=int(cfg.max_inject_tokens),
                        relevant_episodes=relevant_episodes,
                        injection_strategy=retriever.last_injection_strategy,
                        llm_client=self.llm_client,
                        entity_fallback=True,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error("MemoryPack生成に失敗しました(meta_request)", exc_info=exc)
                memory_pack = ""
        else:
            memory_pack = self._build_simple_memory_pack(
                persona_text=cfg.persona_text,
                contract_text=cfg.contract_text,
                client_context=None,
                image_summaries=image_summaries,
                now_ts=now_ts,
            )

        # Chat と同じく最小ガード + MemoryPack に加えて、meta_request のシステム指示を付与する。
        parts: List[str] = [_INTERNAL_CONTEXT_GUARD_PROMPT, (memory_pack or "").strip(), get_meta_request_prompt()]
        system_prompt = "\n\n".join([p for p in parts if p])
        conversation = [{"role": "user", "content": meta_user_text}]

        message = ""
        try:
            resp = self.llm_client.generate_reply_response(
                system_prompt=system_prompt,
                conversation=conversation,
                stream=False,
            )
            message = (self.llm_client.response_content(resp) or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.error("meta_request document generation failed", exc_info=exc)
            message = ""

        with lock, memory_session_scope(memory_id, self.config_store.embedding_dimension) as db:
            self._update_episode_unit(
                db,
                now_ts=now_ts,
                unit_id=unit_id,
                reply_text=message or None,
                image_summary=image_summary_text,
            )
            # 文書生成は会話ログと同様に検索対象にしたい（埋め込みのみで十分）。
            self._enqueue_embeddings_job(db, now_ts=now_ts, unit_id=unit_id)
            self._maybe_enqueue_relationship_summary(db, now_ts=now_ts)

        publish_event(
            type="meta_request",
            memory_id=memory_id,
            unit_id=unit_id,
            data={"message": message},
        )

    def handle_capture(self, request: schemas.CaptureRequest) -> schemas.CaptureResponse:
        """スクリーンショット/カメラ画像を要約してEpisodeとして保存する。"""
        cfg = self.config_store.config
        memory_id = self.config_store.memory_id
        lock = _get_memory_lock(memory_id)
        now_ts = _now_utc_ts()

        text_to_check = request.context_text or ""
        for keyword in cfg.exclude_keywords:
            if _matches_exclude_keyword(keyword, text_to_check):
                logger.info("capture skipped due to exclude keyword", extra={"keyword": keyword})
                return schemas.CaptureResponse(episode_id=-1, stored=False)

        image_bytes = _decode_base64_image(request.image_base64)
        try:
            image_summary = self.llm_client.generate_image_summary([image_bytes])[0]
        except Exception as exc:  # noqa: BLE001
            logger.warning("画像要約に失敗しました", exc_info=exc)
            image_summary = "画像要約に失敗しました"

        source = "desktop_capture" if request.capture_type == "desktop" else "camera_capture"
        with lock, memory_session_scope(memory_id, self.config_store.embedding_dimension) as db:
            unit_id = self._create_episode_unit(
                db,
                now_ts=now_ts,
                source=source,
                user_text=request.context_text,
                reply_text=None,
                image_summary=image_summary,
                context_note=None,
                sensitivity=int(Sensitivity.PRIVATE),
            )
            self._enqueue_default_jobs(db, now_ts=now_ts, unit_id=unit_id)
            self._maybe_enqueue_relationship_summary(db, now_ts=now_ts)
        return schemas.CaptureResponse(episode_id=unit_id, stored=True)

    def _create_episode_unit(
        self,
        db,
        *,
        now_ts: int,
        source: str,
        user_text: Optional[str],
        reply_text: Optional[str],
        image_summary: Optional[str],
        context_note: Optional[str],
        sensitivity: int,
    ) -> int:
        unit = Unit(
            kind=int(UnitKind.EPISODE),
            occurred_at=now_ts,
            created_at=now_ts,
            updated_at=now_ts,
            source=source,
            state=int(UnitState.RAW),
            confidence=0.5,
            salience=0.0,
            sensitivity=sensitivity,
            pin=0,
            topic_tags=None,
            emotion_label=None,
            emotion_intensity=None,
        )
        db.add(unit)
        db.flush()
        payload = PayloadEpisode(
            unit_id=unit.id,
            user_text=user_text,
            reply_text=reply_text,
            image_summary=image_summary,
            context_note=context_note,
            reflection_json=None,
        )
        db.add(payload)
        db.flush()
        return int(unit.id)

    def _enqueue_default_jobs(self, db, *, now_ts: int, unit_id: int) -> None:
        if not self.config_store.memory_enabled:
            return
        kinds = [
            "reflect_episode",
            "extract_entities",
            "extract_facts",
            "extract_loops",
            "upsert_embeddings",
            "capsule_refresh",
        ]
        for kind in kinds:
            payload = {"unit_id": unit_id}
            if kind == "capsule_refresh":
                payload = {"limit": 5}
            db.add(
                Job(
                    kind=kind,
                    payload_json=_json_dumps(payload),
                    status=0,
                    run_after=now_ts,
                    tries=0,
                    last_error=None,
                    created_at=now_ts,
                    updated_at=now_ts,
                )
            )

    def _enqueue_embeddings_job(self, db, *, now_ts: int, unit_id: int) -> None:
        if not self.config_store.memory_enabled:
            return
        db.add(
            Job(
                kind="upsert_embeddings",
                payload_json=_json_dumps({"unit_id": unit_id}),
                status=0,
                run_after=now_ts,
                tries=0,
                last_error=None,
                created_at=now_ts,
                updated_at=now_ts,
            )
        )
