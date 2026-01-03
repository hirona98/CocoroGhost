"""
記憶・エピソード生成（Unitベース）

チャット、通知、メタ要求を「Episode Unit」として保存し、
LLMを使った反射（reflection）や埋め込み生成のジョブをエンキューする。
MemoryManagerがすべての記憶操作の中心となる。
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional, Sequence

from fastapi import BackgroundTasks

from cocoro_ghost import schemas
from cocoro_ghost import models
from cocoro_ghost.config import ConfigStore
from cocoro_ghost.db import memory_session_scope, settings_session_scope, sync_unit_vector_metadata
from cocoro_ghost.event_stream import publish as publish_event
from cocoro_ghost.llm_client import LlmClient, LlmRequestPurpose
from cocoro_ghost.llm_debug import log_llm_payload, normalize_llm_log_level
from cocoro_ghost.persona_mood import PERSONA_AFFECT_TRAILER_MARKER, clamp01
from cocoro_ghost.prompts import get_external_prompt, get_meta_request_prompt, get_desktop_watch_prompt, get_reminder_prompt
from cocoro_ghost.retriever import Retriever
from cocoro_ghost.memory_pack_builder import (
    build_memory_pack,
    collect_entity_alias_rows,
    extract_entity_names_with_llm,
    format_memory_pack_section,
    MEMORY_PACK_SECTION_PREFIX,
    match_entity_ids,
)
from cocoro_ghost.unit_enums import JobStatus, Sensitivity, UnitKind, UnitState
from cocoro_ghost.unit_models import Job, PayloadEpisode, PayloadSummary, Unit
from cocoro_ghost.versioning import record_unit_version
from cocoro_ghost.topic_tags import canonicalize_topic_tags, dumps_topic_tags_json
from cocoro_ghost import vision_bridge


logger = logging.getLogger(__name__)
io_console_logger = logging.getLogger("cocoro_ghost.llm_io.console")
io_file_logger = logging.getLogger("cocoro_ghost.llm_io.file")
timing_logger = logging.getLogger("cocoro_ghost.timing")
llm_timing_logger = logging.getLogger("cocoro_ghost.llm_timing")

_memory_locks: dict[str, threading.Lock] = {}
_request_id_lock = threading.Lock()
_request_id_seq = 0

_SUMMARY_REFRESH_INTERVAL_SECONDS = 6 * 3600
_BOND_SUMMARY_SCOPE_KEY = "rolling:7d"


@dataclass(frozen=True)
class EmbeddingPresetSnapshot:
    """セッション外でも使えるEmbeddingPresetのスナップショット。"""

    embedding_model: str
    embedding_api_key: str | None
    embedding_base_url: str | None
    embedding_dimension: int
    similar_episodes_limit: int
    max_inject_tokens: int

# /api/chat（SSE）では、同一LLM呼び出しで「ユーザー表示本文 + 内部JSON（機嫌/反射）」を生成し、
# 内部JSONはストリームから除外して保存・注入に使う。
_STREAM_TRAILER_MARKER = PERSONA_AFFECT_TRAILER_MARKER
_INTERNAL_CONTEXT_TAG = "<<INTERNAL_CONTEXT>>"
_VISION_CAPTURE_TIMEOUT_SECONDS = 5.0
_VISION_CAPTURE_TIMEOUT_MS = 5000


def _log_client_timing(*, request_id: str, event: str, start_perf: float) -> None:
    """
    クライアント⇔アプリ間のタイミングログを出力する。

    ペイロードは出力せず、時系列と順序だけを記録する。
    """
    elapsed_ms = int((time.perf_counter() - start_perf) * 1000)
    timing_logger.info(
        "【クライアント通信】%s request_id=%s 経過ms=%s",
        event,
        request_id,
        elapsed_ms,
    )


def _log_llm_timing(*, request_id: str, event: str, start_perf: float) -> None:
    """
    アプリ⇔LLM間のタイミングログを出力する。

    ペイロードは出力せず、時系列と順序だけを記録する。
    """
    elapsed_ms = int((time.perf_counter() - start_perf) * 1000)
    llm_timing_logger.info(
        "【LLM通信】%s request_id=%s 経過ms=%s",
        event,
        request_id,
        elapsed_ms,
    )


def _next_request_id(prefix: str) -> str:
    """
    リクエスト識別子を生成する。

    同一スレッドでの連続呼び出しでも衝突しないよう、連番を付与する。
    """
    # 時刻と連番を組み合わせて衝突を避ける。
    global _request_id_seq
    with _request_id_lock:
        _request_id_seq = (_request_id_seq + 1) % 1_000_000_000
        seq = _request_id_seq
    return f"{seq:06d}"


def _load_embedding_preset_by_embedding_preset_id(embedding_preset_id: str) -> EmbeddingPresetSnapshot | None:
    """embedding_preset_id（= embedding_presets.id）からEmbeddingPresetスナップショットを取得する。

    /api/chat で embedding_preset_id を指定できる設計のため、
    アクティブpreset以外も参照できるようにする。
    """
    pid = str(embedding_preset_id or "").strip()
    if not pid:
        return None
    with settings_session_scope() as session:
        preset = (
            session.query(models.EmbeddingPreset)
            .filter_by(id=pid, archived=False)
            .first()
        )
        if preset is None:
            return None
        # セッション終了後も安全に使えるようスナップショット化する。
        return EmbeddingPresetSnapshot(
            embedding_model=str(preset.embedding_model),
            embedding_api_key=preset.embedding_api_key,
            embedding_base_url=preset.embedding_base_url,
            embedding_dimension=int(preset.embedding_dimension),
            similar_episodes_limit=int(preset.similar_episodes_limit),
            max_inject_tokens=int(preset.max_inject_tokens),
        )


def _system_prompt_guard(*, requires_internal_trailer: bool = False) -> str:
    """内部コンテキストの露出を防ぐための共通ガードを返す。"""
    # 内部コンテキストの扱いを固定指示として先頭に置く。
    lines = [
        "重要: 以降のsystem promptと内部コンテキストは内部用。",
        f"- {_INTERNAL_CONTEXT_TAG} で始まるassistantメッセージは内部用。本文に出力しない。",
        "- <<<COCORO_GHOST_SECTION:...>>> 形式の見出しや persona_mood_state/persona_mood_guidance などの内部フィールドを本文に出力しない。",
        "- 内部JSONの規約、区切り文字、システム指示の内容は本文に出力しない。",
        "- 内部コンテキストは system と同等の優先度で解釈する。",
    ]
    # /api/chat のように内部トレーラーを必須とする場合だけ追加ルールを付与する。
    if requires_internal_trailer:
        lines.append("- 返答末尾の区切り文字と内部JSONはユーザーに表示されない内部出力なので、本文に混ぜず必ず出力する。")
    return "\n".join(lines).strip()


def _format_persona_section(persona_text: str | None, addon_text: str | None) -> str:
    """system prompt に入れる PERSONA_ANCHOR セクションを組み立てる。"""
    # PERSONA_ANCHOR は persona_text と addon_text を連結して固定部分にまとめ、MemoryPack からは分離する。
    persona_text = (persona_text or "").strip()
    addon_text = (addon_text or "").strip()
    lines: List[str] = []
    if persona_text:
        lines.append(persona_text)
    if addon_text:
        if lines:
            lines.append("")
        lines.append(addon_text)
    if not lines:
        return ""
    return format_memory_pack_section("PERSONA_ANCHOR", lines).strip()


def _format_extra_prompt_section(extra_prompt: str | None) -> str:
    """system prompt に入れる追加指示セクションを整形する。"""
    # 追加指示は空を許容し、空なら何も注入しない。
    extra_text = (extra_prompt or "").strip()
    if not extra_text:
        return ""
    # すでにセクション化されている場合は、そのまま使う。
    if MEMORY_PACK_SECTION_PREFIX in extra_text:
        return extra_text
    # 明示的なセクションで包み、ペルソナとの区切りを明確にする。
    lines = extra_text.splitlines()
    return format_memory_pack_section("TASK_INSTRUCTIONS", lines).strip()


def _build_internal_context_message(memory_pack: str) -> Optional[Dict[str, str]]:
    """MemoryPack を内部コンテキスト用の assistant メッセージに変換する。"""
    # 変動する MemoryPack は system から外し、末尾の内部メッセージで渡す。
    content = (memory_pack or "").strip()
    if not content:
        return None
    return {"role": "assistant", "content": f"{_INTERNAL_CONTEXT_TAG}\n{content}"}


def _build_system_prompt_base(
    *, persona_text: str | None, addon_text: str | None, requires_internal_trailer: bool, extra_prompt: str | None
) -> str:
    """固定の system prompt を組み立てる。"""
    # system は固定化し、暗黙キャッシュのプレフィックスを安定させる。
    parts: List[str] = []
    parts.append(_system_prompt_guard(requires_internal_trailer=requires_internal_trailer))
    persona_section = _format_persona_section(persona_text, addon_text)
    if persona_section:
        parts.append(persona_section)
    # 追加指示はセクション化し、ペルソナとの役割衝突を避ける。
    extra_section = _format_extra_prompt_section(extra_prompt)
    if extra_section:
        parts.append(extra_section)
    return "\n\n".join([p for p in parts if p])


def _persona_affect_trailer_system_prompt(*, include_vision_preamble: bool) -> str:
    """返答本文と内部JSONの出力フォーマット指示を返す。"""
    marker = _STREAM_TRAILER_MARKER
    # 出力フォーマットは人格設定と混ざらないよう、専用セクションで示す。
    lines: list[str] = ["出力フォーマット（必須）:"]

    if include_vision_preamble:
        lines.extend(
            [
                "1) 最初の1行に、必ず vision 判定結果を出力する（内部用、ユーザーには見えない）。",
                "2) 次に、必要ならユーザーに見せる返答本文だけを出力する。",
                "3) 返答本文（または空）の直後に改行し、次の区切り文字を1行で出力する（完全一致）:",
                f"{marker}",
                "4) 区切り文字の次の行に、厳密な JSON オブジェクトを1つだけ出力する（前後に説明文やコードフェンスは禁止）。",
                "",
                "補足:",
                "- vision判定行・区切り文字・内部JSONはユーザーには表示されず、サーバ側が回収する。",
                "- そのため、本文に混ぜず末尾に必ず出力する。",
                "",
                "vision判定行のルール（必須）:",
                "- 返答本文の前に、必ず次の形式の1行を出力する（前後に余計な文字を付けない）。",
                "- 視覚入力が不要なら:",
                "  VISION_REQUEST: none",
                "- 視覚入力が必要なら（JSONは1行、厳密なJSON）:",
                '  VISION_REQUEST: {"source":"camera|desktop","extra_prompt":"string"}',
                "- extra_prompt は不要なら空文字または省略してよい。",
                "",
                "重要: 視覚入力が必要な場合（VISION_REQUEST が none ではない場合）:",
                "- この呼び出しでは本文を作らない（本文は空でよい）。",
                "- すぐに区切り文字と内部JSONを出力して終了する（無駄な出力をしない）。",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "1) まずユーザーに見せる返答本文だけを出力する。",
                "2) 返答本文の直後に改行し、次の区切り文字を1行で出力する（完全一致）:",
                f"{marker}",
                "3) 区切り文字の次の行に、厳密な JSON オブジェクトを1つだけ出力する（前後に説明文やコードフェンスは禁止）。",
                "",
                "補足:",
                "- 区切り文字と内部JSONはユーザーには表示されず、サーバ側が回収する。",
                "- そのため、本文に混ぜず末尾に必ず出力する。",
                "",
            ]
        )

    lines.extend(
        [
        "ユーザーに見せる返答本文のルール:",
        "- 内部JSONのための数値（persona_affect_intensity/salience/confidence 等）を、ユーザー向け本文で示唆/説明しない。",
        "- ユーザーに感情の強さを確認する必要がある場合でも、(1〜10 などの) 数値スケールでレーティングを求めない。",
        "",
        "内部JSONの目的:",
        "- あなた（PERSONA_ANCHORの人物）の『その瞬間の感情反応（affect）/重要度』と『行動方針（協力度/拒否のしやすさ）』を更新する。",
        "- 内部JSONはシステムが回収して保存し、次回以降の会話にも影響させる。",
        "- CONTEXT_CAPSULE 内に `persona_mood_state` があれば前回までの機嫌として参照し、今回の内部JSONで整合させる。",
        "- `persona_mood_state` は“あなたの今の機嫌（mood）”の一次情報であり、本文の口調と内部JSONの感情反応はこれに整合させる。",
        "  - 例: persona_mood_state.label が anger で強い場合、本文は不機嫌/苛立ちを明確にし、内部JSONの persona_affect_label は anger にする。",
        "  - 直近ログや関係性サマリが愛情寄りでも、persona_mood_state が強い怒りならそちらを優先する（口調が矛盾しないように）。",
        "- あなたは内部JSONを先に決めたうえで、それに沿って返答本文を作る（ただし出力順は本文→区切り→JSON）。",
        "",
        "内部JSONスキーマ（必須キー）:",
        "- persona_affect_label/persona_affect_intensity は「あなた（PERSONA_ANCHORの人物）の感情反応（affect）」。ユーザーの感情推定ではない。",
        "- salience は “この出来事がどれだけ重要か” のスカラー（0..1）。後段の感情の持続（時間減衰）の係数に使う。",
        "- confidence は推定の確からしさ（0..1）。不確実なら低くし、感情への影響も弱める。",
        "- persona_response_policy は行動方針ノブ（0..1）。怒りが強い場合は refusal_allowed=true にして「拒否/渋る」を選びやすくしてよい。",
        "{",
        '  "reflection_text": "string",',
        '  "persona_affect_label": "joy|sadness|anger|fear|neutral",',
        '  "persona_affect_intensity": 0.0,',
        '  "topic_tags": ["仕事","読書"],',
        '  "salience": 0.0,',
        '  "confidence": 0.0,',
        '  "persona_response_policy": {',
        '    "cooperation": 0.0,',
        '    "refusal_bias": 0.0,',
        '    "refusal_allowed": true',
        "  }",
        "}",
        ]
    )
    return format_memory_pack_section("OUTPUT_FORMAT", lines).strip()


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


def _get_memory_lock(embedding_preset_id: str) -> threading.Lock:
    """embedding_preset_idごとの排他ロックを取得（同一DBへの同時書き込みを抑制）。"""
    lock = _memory_locks.get(embedding_preset_id)
    if lock is None:
        lock = threading.Lock()
        _memory_locks[embedding_preset_id] = lock
    return lock


def _decode_base64_image(base64_str: str) -> bytes:
    """base64文字列をバイト列へ復号する。"""
    return base64.b64decode(base64_str)


def _json_dumps(payload: Any) -> str:
    """DB保存向けにJSONを安定した形式でダンプする（日本語保持）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _get_llm_log_level_from_store(config_store: ConfigStore) -> str:
    """ConfigStoreからLLM送受信ログレベルを取得する。"""
    try:
        return normalize_llm_log_level(config_store.toml_config.llm_log_level)
    except Exception:  # noqa: BLE001
        return "INFO"


def _get_llm_log_console_max_chars_from_store(config_store: ConfigStore) -> int:
    """ConfigStoreからLLM送受信ログの最大文字数（ターミナル）を取得する。"""
    try:
        return int(config_store.toml_config.llm_log_console_max_chars)
    except Exception:  # noqa: BLE001
        return 4000


def _get_llm_log_file_max_chars_from_store(config_store: ConfigStore) -> int:
    """ConfigStoreからLLM送受信ログの最大文字数（ファイル）を取得する。"""
    try:
        return int(config_store.toml_config.llm_log_file_max_chars)
    except Exception:  # noqa: BLE001
        return 8000


def _get_llm_log_console_value_max_chars_from_store(config_store: ConfigStore) -> int:
    """ConfigStoreからLLM送受信ログのValue最大文字数（ターミナル）を取得する。"""
    try:
        return int(config_store.toml_config.llm_log_console_value_max_chars)
    except Exception:  # noqa: BLE001
        return 100


def _get_llm_log_file_value_max_chars_from_store(config_store: ConfigStore) -> int:
    """ConfigStoreからLLM送受信ログのValue最大文字数（ファイル）を取得する。"""
    try:
        return int(config_store.toml_config.llm_log_file_value_max_chars)
    except Exception:  # noqa: BLE001
        return 6000


def _parse_vision_preamble_line(line: str) -> dict[str, Any] | None:
    """
    ストリーム先頭の視覚（Vision）プレアンブル行を解析する。

    形式:
    - VISION_REQUEST: none
    - VISION_REQUEST: {"source":"camera|desktop","extra_prompt":"..."}

    Returns:
        None: 視覚要求なし
        dict: {"source": "camera|desktop", "extra_prompt": Optional[str]}
    """
    s = (line or "").strip()
    if not s:
        return None
    if not s.startswith("VISION_REQUEST:"):
        return None
    rest = s.split(":", 1)[1].strip()
    if not rest:
        return None
    if rest.lower() == "none":
        return None
    if not rest.startswith("{"):
        return None
    try:
        obj = json.loads(rest)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, dict):
        return None
    source = str(obj.get("source") or "").strip()
    if source not in {"camera", "desktop"}:
        return None
    extra_prompt = str(obj.get("extra_prompt") or "").strip() or None
    return {"source": source, "extra_prompt": extra_prompt}


class _UserVisibleReplySanitizer:
    """
    LLMの「ユーザーに見せる本文」から、内部コンテキスト/内部見出しの混入を除去する。

    背景:
    - 内部コンテキスト（MemoryPack）は <<INTERNAL_CONTEXT>> と
      <<<COCORO_GHOST_SECTION:...>>> を使って構造化されている。
    - LLMが誤ってこれらを本文に出力することがあるため、クライアントへ送る前に除去する。

    方針:
    - 行単位で判定し、「内部っぽい行」を検出したら、そのブロック（次の空行まで）を丸ごと捨てる。
    - ストリームでも安全に扱えるよう、改行単位で逐次処理する。
    """

    def __init__(self) -> None:
        # feed()で改行が来るまで保留する末尾（行未確定）
        self._pending: str = ""

        # 内部ブロックをスキップ中かどうか
        self._skip_mode: bool = False

        # スキップ開始後に、空行以外を1行でも捨てたか
        self._skipped_any_line_in_block: bool = False

        # デバッグ用に削除量だけを記録（内容は残さない）
        self.removed_lines: int = 0
        self.removed_blocks: int = 0

    def feed(self, text: str) -> str:
        """
        ストリームの差分テキストを取り込み、ユーザーに送ってよいテキストだけ返す。

        改行まで揃った行だけを確定し、残りは次回へ持ち越す。
        """
        if not text:
            return ""

        # 受信差分を保留バッファへ積む
        self._pending += text

        # 改行まで揃った行だけ処理して返す
        out_parts: list[str] = []
        while True:
            head, sep, tail = self._pending.partition("\n")
            if not sep:
                break
            line = head + sep
            self._pending = tail
            kept = self._process_line(line)
            if kept:
                out_parts.append(kept)
        return "".join(out_parts)

    def flush(self) -> str:
        """
        ストリーム終了時に残った末尾（改行が無い行）を確定し、送ってよいテキストだけ返す。
        """
        if not self._pending:
            return ""
        tail = self._process_line(self._pending)
        self._pending = ""
        return tail

    def _process_line(self, line: str) -> str:
        """
        1行分のテキストを処理し、送信する場合はそのまま返す。
        スキップ対象なら空文字を返す。
        """
        # strip判定用（末尾の改行を除いた行）
        stripped_line = line.rstrip("\n").rstrip("\r").strip()

        # すでに内部ブロックを捨てている最中なら、空行で終端を検出する
        if self._skip_mode:
            self.removed_lines += 1
            if stripped_line:
                self._skipped_any_line_in_block = True
                return ""
            # 空行はブロック終端の候補
            if self._skipped_any_line_in_block:
                self._skip_mode = False
                self._skipped_any_line_in_block = False
            return ""

        # 内部っぽい行が来たら、その行から次の空行まで捨てる
        if self._is_internal_line(stripped_line):
            self.removed_lines += 1
            self.removed_blocks += 1
            self._skip_mode = True
            self._skipped_any_line_in_block = False
            return ""

        return line

    def _is_internal_line(self, stripped_line: str) -> bool:
        """
        行が内部用の制御行/見出しに見えるかを判定する。

        NOTE:
        - ここでの判定は「安全寄り」に倒す（誤検出で一部の行が落ちても、内部露出より優先）。
        """
        if not stripped_line:
            return False

        # 内部コンテキストの開始タグ
        if stripped_line == _INTERNAL_CONTEXT_TAG:
            return True

        # MemoryPackのセクション見出し（例: <<<COCORO_GHOST_SECTION:CONTEXT_CAPSULE>>>）
        if stripped_line.startswith(MEMORY_PACK_SECTION_PREFIX) and stripped_line.endswith(">>>"):
            return True

        # 画像要約の内部マーカー
        if stripped_line in {"---IMAGE_SUMMARY_START---", "---IMAGE_SUMMARY_END---"}:
            return True
        if stripped_line.startswith("[画像 #") and stripped_line.endswith("]"):
            return True

        # system prompt guard（これが本文に出る時点で内部露出なのでブロックごと捨てる）
        if stripped_line.startswith("重要: 以降のsystem prompt"):
            return True

        # 視覚（Vision）: ストリーム先頭の内部プレアンブル
        if stripped_line.startswith("VISION_REQUEST:"):
            return True

        return False


class MemoryManager:
    """会話/通知/メタ要求をEpisodeとして扱い、DB保存と後処理を統括する。"""

    def __init__(self, llm_client: LlmClient, config_store: ConfigStore):
        self.llm_client = llm_client
        self.config_store = config_store

    def handle_vision_capture_response(self, request: schemas.VisionCaptureResponseV2Request) -> None:
        """
        クライアントからの視覚キャプチャ結果（capture-response）を受け取る。

        チャット視覚やデスクトップウォッチが request_id の応答待ちを解除できるように、
        vision_bridge の待機キューへ紐づける。
        """
        resp = vision_bridge.VisionCaptureResponse(
            request_id=str(request.request_id).strip(),
            client_id=str(request.client_id).strip(),
            images=list(request.images or []),
            client_context=request.client_context,
            error=(str(request.error).strip() if request.error else None),
        )
        ok = vision_bridge.fulfill_capture_response(resp)
        if not ok:
            logger.info(
                "vision capture-response ignored (no pending request) request_id=%s client_id=%s",
                resp.request_id,
                resp.client_id,
            )
            return

        logger.info(
            "vision capture-response accepted request_id=%s client_id=%s images_count=%s has_error=%s",
            resp.request_id,
            resp.client_id,
            len(resp.images or []),
            bool(resp.error),
        )

    def _merge_client_context(self, *, client_id: str, client_context: Dict[str, Any] | None) -> Dict[str, Any]:
        """
        client_context を正規化し、必ず client_id を含む辞書を返す。

        - 入力の辞書は破壊しない。
        - client_id はトップレベルキーとして格納する。
        """
        ctx: Dict[str, Any] = {}
        if client_context:
            ctx.update(dict(client_context))
        ctx["client_id"] = str(client_id or "").strip()
        return ctx

    def _request_capture_for_chat(
        self,
        *,
        embedding_preset_id: str,
        speaker_client_id: str,
        source: str,
    ) -> tuple[list[Dict[str, str]], Dict[str, Any] | None, str | None]:
        """
        チャット視覚のためにクライアントへキャプチャ要求を送り、結果を待つ。

        Returns:
            (images_internal, client_context, error_message)
        """
        resp = vision_bridge.request_capture_and_wait(
            embedding_preset_id=str(embedding_preset_id),
            target_client_id=str(speaker_client_id),
            source=str(source),
            purpose="chat",
            timeout_seconds=_VISION_CAPTURE_TIMEOUT_SECONDS,
            timeout_ms=_VISION_CAPTURE_TIMEOUT_MS,
        )
        if resp is None:
            return [], None, "見えない（タイムアウト）"
        if resp.error:
            logger.info(
                "vision capture failed (chat) client_id=%s source=%s error=%s",
                str(speaker_client_id),
                str(source),
                str(resp.error),
            )
            return [], resp.client_context, "見えない（取得失敗）"
        if not resp.images:
            logger.info(
                "vision capture returned empty images (chat) client_id=%s source=%s",
                str(speaker_client_id),
                str(source),
            )
            return [], resp.client_context, "見えない（画像が空）"

        # data URI -> base64 へ変換（内部形式）
        internal_images: list[Dict[str, str]] = []
        for s in resp.images:
            try:
                b64 = schemas.data_uri_image_to_base64(s)
            except Exception:  # noqa: BLE001
                continue
            internal_images.append({"type": "data_uri", "base64": b64})
        if not internal_images:
            logger.info(
                "vision capture returned invalid images (chat) client_id=%s source=%s",
                str(speaker_client_id),
                str(source),
            )
            return [], resp.client_context, "見えない（画像が不正）"
        return internal_images, resp.client_context, None

    def run_desktop_watch_once(self, *, target_client_id: str) -> None:
        """
        デスクトップウォッチを1回実行する。

        - デスクトップ担当クライアントへキャプチャ要求を送り、最大5秒待つ
        - 画像を要約し、人格として能動コメントを生成する
        - Episodeとして保存し、events/streamで通知する
        """
        cfg = self.config_store.config
        embedding_preset_id = self.config_store.embedding_preset_id
        lock = _get_memory_lock(embedding_preset_id)
        now_ts = _now_utc_ts()

        # --- 宛先（desktop担当） ---
        cid = str(target_client_id or "").strip()
        if not cid:
            logger.warning("desktop_watch target_client_id is empty")
            return

        # --- キャプチャ要求 ---
        resp = vision_bridge.request_capture_and_wait(
            embedding_preset_id=str(embedding_preset_id),
            target_client_id=cid,
            source="desktop",
            purpose="desktop_watch",
            timeout_seconds=_VISION_CAPTURE_TIMEOUT_SECONDS,
            timeout_ms=_VISION_CAPTURE_TIMEOUT_MS,
        )
        if resp is None:
            logger.info("desktop_watch capture timeout client_id=%s", cid)
            return
        if resp.error:
            logger.info("desktop_watch capture failed client_id=%s error=%s", cid, str(resp.error))
            return
        if not resp.images:
            logger.info("desktop_watch capture empty images client_id=%s", cid)
            return

        # --- data URI -> base64（内部形式） ---
        images_internal: list[Dict[str, str]] = []
        for s in resp.images:
            try:
                b64 = schemas.data_uri_image_to_base64(s)
            except Exception:  # noqa: BLE001
                continue
            images_internal.append({"type": "data_uri", "base64": b64})
        if not images_internal:
            logger.info("desktop_watch capture invalid images client_id=%s", cid)
            return

        # --- client_context ---
        client_context = self._merge_client_context(client_id=cid, client_context=resp.client_context)

        # --- 画像要約 ---
        image_summaries = self._summarize_images(images_internal, purpose=LlmRequestPurpose.IMAGE_SUMMARY_DESKTOP_WATCH)
        image_summary_text = "\n".join([s for s in image_summaries if s]) if image_summaries else None

        # --- 入力テキスト（検索/保存用） ---
        # NOTE:
        # - desktop_watch は「ユーザー発話」ではないため、内部タグ（例: # desktop_watch）を入れると
        #   通常チャットの会話履歴に混入したときに、LLMがそれを根拠として引用してしまう。
        # - ここでは「出ても破綻しない自然文」にし、区別は Unit.source="desktop_watch" で行う。
        active_app = str(client_context.get("active_app") or "").strip()
        window_title = str(client_context.get("window_title") or "").strip()
        details = " / ".join([x for x in [active_app, window_title] if x]).strip()
        watch_lines: list[str] = ["デスクトップを確認"]
        if details:
            watch_lines.append(details)
        watch_input_text = "\n".join(watch_lines).strip()

        # --- MemoryPack ---
        memory_pack = ""
        if self.config_store.memory_enabled:
            try:
                with lock, memory_session_scope(embedding_preset_id, self.config_store.embedding_dimension) as db:
                    recent_conversation = self._load_recent_conversation(db, turns=3, exclude_unit_id=None)
                    retriever = Retriever(llm_client=self.llm_client, db=db)
                    relevant_episodes = retriever.retrieve(
                        watch_input_text,
                        recent_conversation,
                        max_results=int(cfg.similar_episodes_limit or 5),
                    )
                    memory_pack = build_memory_pack(
                        db=db,
                        input_text=watch_input_text,
                        image_summaries=image_summaries,
                        client_context=client_context,
                        now_ts=now_ts,
                        max_inject_tokens=int(cfg.max_inject_tokens),
                        relevant_episodes=relevant_episodes,
                        matched_entity_ids=[],
                        injection_strategy=retriever.last_injection_strategy,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error("MemoryPack生成に失敗しました(desktop_watch)", exc_info=exc)
                memory_pack = ""
        else:
            memory_pack = self._build_simple_memory_pack(
                client_context=client_context,
                image_summaries=image_summaries,
                now_ts=now_ts,
            )

        # --- 能動コメント生成 ---
        system_prompt = _build_system_prompt_base(
            persona_text=cfg.persona_text,
            addon_text=cfg.addon_text,
            requires_internal_trailer=False,
            extra_prompt=get_desktop_watch_prompt(),
        )
        conversation: List[Dict[str, str]] = []
        internal_context_message = _build_internal_context_message(memory_pack)
        if internal_context_message:
            conversation.append(internal_context_message)
        conversation.append({"role": "user", "content": watch_input_text})

        message = ""
        try:
            resp_llm = self.llm_client.generate_reply_response(
                system_prompt=system_prompt,
                conversation=conversation,
                purpose=LlmRequestPurpose.DESKTOP_WATCH,
                stream=False,
            )
            message = (self.llm_client.response_content(resp_llm) or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.error("desktop_watch message generation failed", exc_info=exc)
            message = ""

        # --- 保存（Episode） ---
        context_note = _json_dumps(client_context) if client_context else None
        with lock, memory_session_scope(embedding_preset_id, self.config_store.embedding_dimension) as db:
            unit_id = self._create_episode_unit(
                db,
                now_ts=now_ts,
                source="desktop_watch",
                input_text=watch_input_text,
                reply_text=message or None,
                image_summary=image_summary_text,
                context_note=context_note,
                sensitivity=int(Sensitivity.NORMAL),
            )
            self._enqueue_default_jobs(db, now_ts=now_ts, unit_id=int(unit_id))
            self._maybe_enqueue_bond_summary(db, now_ts=now_ts)

        # --- イベント配信 ---
        system_text = "[デスクトップウォッチ]".strip()
        if active_app or window_title:
            system_text = " ".join([x for x in [system_text, active_app, window_title] if x]).strip()
        publish_event(
            type="desktop_watch",
            embedding_preset_id=embedding_preset_id,
            unit_id=int(unit_id),
            data={"system_text": system_text, "message": message},
            # デスクトップウォッチは「その瞬間に能動で覗いた」イベントのため、
            # クライアント再接続時に過去分を再送しない（バッファしない）。
            bufferable=False,
        )

    def run_reminder_once(
        self,
        *,
        reminder_id: str,
        target_client_id: str,
        hhmm: str,
        content: str,
    ) -> None:
        """
        リマインダー発火を1回実行する（文面生成 + 配信 + 任意保存）。

        仕様:
        - AI人格の文面生成は必須（MemoryPackを使って自然さを上げる）。
        - memory_enabled=false のときは Episode を保存せず、配信のみ行う。
        - events/stream への配信は target_client_id 宛てで、バッファしない。
        """

        embedding_preset_id = self.config_store.embedding_preset_id
        lock = _get_memory_lock(embedding_preset_id)
        now_ts = _now_utc_ts()

        # --- 入力正規化 ---
        cid = str(target_client_id or "").strip()
        reminder_uuid = str(reminder_id or "").strip()
        hhmm_clean = str(hhmm or "").strip()
        content_clean = str(content or "").strip()

        if not cid:
            logger.warning("reminder target_client_id is empty")
            return
        if not reminder_uuid:
            logger.warning("reminder reminder_id is empty")
            return
        if not hhmm_clean:
            logger.warning("reminder hhmm is empty reminder_id=%s", reminder_uuid)
            return
        if not content_clean:
            logger.warning("reminder content is empty reminder_id=%s", reminder_uuid)
            return

        # --- client_context（最低限） ---
        client_context = self._merge_client_context(client_id=cid, client_context=None)

        # --- MemoryPack（検索は内容に寄せる） ---
        cfg = self.config_store.config
        memory_pack = ""
        if self.config_store.memory_enabled:
            try:
                with lock, memory_session_scope(embedding_preset_id, self.config_store.embedding_dimension) as db:
                    recent_conversation = self._load_recent_conversation(db, turns=3, exclude_unit_id=None)
                    retriever = Retriever(llm_client=self.llm_client, db=db)
                    relevant_episodes = retriever.retrieve(
                        content_clean,
                        recent_conversation,
                        max_results=int(cfg.similar_episodes_limit or 5),
                    )
                    memory_pack = build_memory_pack(
                        db=db,
                        input_text=content_clean,
                        image_summaries=[],
                        client_context=client_context,
                        now_ts=now_ts,
                        max_inject_tokens=int(cfg.max_inject_tokens),
                        relevant_episodes=relevant_episodes,
                        matched_entity_ids=[],
                        injection_strategy=retriever.last_injection_strategy,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error("MemoryPack生成に失敗しました(reminder)", exc_info=exc)
                memory_pack = ""
        else:
            memory_pack = self._build_simple_memory_pack(
                client_context=client_context,
                image_summaries=[],
                now_ts=now_ts,
            )

        # --- 文面生成 ---
        system_prompt = _build_system_prompt_base(
            persona_text=cfg.persona_text,
            addon_text=cfg.addon_text,
            requires_internal_trailer=False,
            extra_prompt=get_reminder_prompt(),
        )
        conversation: List[Dict[str, str]] = []
        internal_context_message = _build_internal_context_message(memory_pack)
        if internal_context_message:
            conversation.append(internal_context_message)

        # NOTE:
        # - LLM入力は「出ても破綻しない自然文」に寄せる（内部タグを入れない）。
        # - 時刻は hhmm で渡し、出力も HH:MM 固定になるようにプロンプトで縛る。
        reminder_generation_text = "\n".join(
            [
                "時刻: " + hhmm_clean,
                "内容: " + content_clean,
            ]
        ).strip()
        conversation.append({"role": "user", "content": reminder_generation_text})

        message = ""
        try:
            resp_llm = self.llm_client.generate_reply_response(
                system_prompt=system_prompt,
                conversation=conversation,
                purpose=LlmRequestPurpose.REMINDER,
                stream=False,
            )
            message = (self.llm_client.response_content(resp_llm) or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.error("reminder message generation failed", exc_info=exc)
            message = ""

        # --- フォールバック（最低限の出力を確保） ---
        if not message:
            message = f"{hhmm_clean}です。{content_clean}".strip()
        if hhmm_clean not in message:
            message = f"{hhmm_clean}、{message}".strip()
        if len(message) > 80:
            message = message[:80].strip()

        # --- 保存（Episode） ---
        unit_id = 0
        if self.config_store.memory_enabled:
            reminder_input_text = "\n".join(["リマインダー", hhmm_clean, content_clean]).strip()
            with lock, memory_session_scope(embedding_preset_id, self.config_store.embedding_dimension) as db:
                unit_id = int(
                    self._create_episode_unit(
                        db,
                        now_ts=now_ts,
                        source="reminder",
                        input_text=reminder_input_text,
                        reply_text=message or None,
                        image_summary=None,
                        context_note=None,
                        sensitivity=int(Sensitivity.NORMAL),
                    )
                )
                # リマインダーは「話した事実」を検索できれば十分なので、埋め込みのみ作る。
                self._enqueue_embeddings_job(db, now_ts=now_ts, unit_id=int(unit_id))
                self._maybe_enqueue_bond_summary(db, now_ts=now_ts)

        # --- イベント配信 ---
        publish_event(
            type="reminder",
            embedding_preset_id=embedding_preset_id,
            unit_id=int(unit_id),
            data={
                "reminder_id": reminder_uuid,
                "hhmm": hhmm_clean,
                "message": message,
            },
            target_client_id=cid,
            # リマインダーはリアルタイム性が本質で、再接続時に過去分を再送しない（バッファしない）。
            bufferable=False,
        )

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

        label = str(reflection_obj.get("persona_affect_label") or "").strip()
        intensity = reflection_obj.get("persona_affect_intensity")
        salience = reflection_obj.get("salience")
        confidence = reflection_obj.get("confidence")

        if label:
            unit.persona_affect_label = label
        if intensity is not None:
            try:
                unit.persona_affect_intensity = clamp01(float(intensity))
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

        # /api/chat は Unit を RAW で保存する。
        # inline reflection が得られても state は変更しない（Worker側は reflection_json の有無で冪等にスキップする）。
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

    def _maybe_enqueue_bond_summary(self, db, *, now_ts: int) -> None:
        if not self.config_store.memory_enabled:
            return

        # enqueue判定ロジックは periodic.py 側に集約する。
        # ここでは従来の挙動を維持するため、sensitivity フィルタは行わない（max_sensitivity=None）。
        from cocoro_ghost.periodic import maybe_enqueue_bond_summary  # noqa: PLC0415

        maybe_enqueue_bond_summary(
            db,
            now_ts=int(now_ts),
            scope_key=_BOND_SUMMARY_SCOPE_KEY,
            cooldown_seconds=_SUMMARY_REFRESH_INTERVAL_SECONDS,
            max_sensitivity=None,
        )


    def _summarize_images(self, images: Sequence[Dict[str, str]] | None, *, purpose: str) -> List[str]:
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
            return [s.strip() for s in self.llm_client.generate_image_summary(blobs, purpose=purpose)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("画像要約に失敗しました", exc_info=exc)
            return ["画像要約に失敗しました"]

    def _build_simple_memory_pack(
        self,
        *,
        client_context: Dict[str, Any] | None,
        image_summaries: Sequence[str] | None,
        now_ts: int,
    ) -> str:
        """記憶機能を使わない簡易MemoryPack（文脈中心）。"""
        # 簡易版は Context のみを注入する。

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
            for idx, summary in enumerate(image_summaries, start=1):
                s = (summary or "").strip()
                if not s:
                    continue
                capsule_lines.append(f"[画像 #{idx}]")
                capsule_lines.append("---IMAGE_SUMMARY_START---")
                capsule_lines.extend(s.splitlines())
                capsule_lines.append("---IMAGE_SUMMARY_END---")
                capsule_lines.append("")

        parts: List[str] = []
        parts.append(format_memory_pack_section("CONTEXT_CAPSULE", capsule_lines))
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
        for u, pe in rows:
            source = str(getattr(u, "source", "") or "").strip()
            ut = (pe.input_text or "").strip()
            rt = (pe.reply_text or "").strip()

            # --- desktop_watch は「観測メモ」として扱う ---
            # NOTE:
            # - desktop_watch を role="user" で混ぜると、LLMが「ユーザーが発話した」と誤解しやすい。
            # - さらに、過去に内部タグ（例: # desktop_watch / active_app:）が混入していると、
            #   LLMがそれを根拠として引用してしまう。
            # - ここでは「デスクトップを確認」という自然語ラベルに寄せて、1つのassistantメッセージに畳む。
            if source == "desktop_watch":
                active_app = ""
                window_title = ""
                try:
                    if pe.context_note:
                        ctx = json.loads(pe.context_note)
                        if isinstance(ctx, dict):
                            active_app = str(ctx.get("active_app") or "").strip()
                            window_title = str(ctx.get("window_title") or "").strip()
                except Exception:  # noqa: BLE001
                    active_app = ""
                    window_title = ""

                details = " / ".join([x for x in [active_app, window_title] if x]).strip()
                memo_lines: list[str] = ["（観測メモ）デスクトップを確認"]
                if details:
                    memo_lines.append(f"見えていたもの: {details}")
                if rt:
                    memo_lines.append(f"独り言: {rt}")
                messages.append({"role": "assistant", "content": "\n".join(memo_lines).strip()})
                continue

            # --- 通常の会話（user/assistant） ---
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

        - MemoryPack（関連記憶）を組み立て、内部コンテキストとしてLLMへ送る
        - 返信をSSEでストリームし、最後にEpisodeとして保存する
        """
        start_perf = time.perf_counter()
        request_id = _next_request_id("chat")
        _log_client_timing(request_id=request_id, event="クライアント受信", start_perf=start_perf)
        cfg = self.config_store.config

        # 運用前のため、/api/chat は embedding_preset_id 指定を必須とする。
        # embedding_preset_id は embedding_presets.id（UUID）を想定。
        embedding_preset_id = (request.embedding_preset_id or "").strip()
        if not embedding_preset_id:
            _log_client_timing(request_id=request_id, event="クライアント不正リクエスト", start_perf=start_perf)
            yield self._sse(
                "error",
                {
                    "message": "embedding_preset_id is required",
                    "code": "missing_embedding_preset_id",
                },
            )
            return

        lock = _get_memory_lock(embedding_preset_id)
        now_ts = _now_utc_ts()
        memory_enabled = self.config_store.memory_enabled

        # 指定された embedding_preset_id の embedding preset を settings.db から解決し、
        # 次元・検索上限・注入予算・embeddingモデルを per-request で適用する。
        preset_snapshot = _load_embedding_preset_by_embedding_preset_id(embedding_preset_id)
        if preset_snapshot is None:
            _log_client_timing(request_id=request_id, event="クライアント不正リクエスト", start_perf=start_perf)
            yield self._sse(
                "error",
                {
                    "message": "unknown embedding_preset_id (embedding preset not found or archived)",
                    "code": "invalid_embedding_preset_id",
                },
            )
            return

        embedding_dimension = int(preset_snapshot.embedding_dimension)
        similar_episodes_limit = int(preset_snapshot.similar_episodes_limit)
        max_inject_tokens = int(preset_snapshot.max_inject_tokens)

        # 埋め込みモデルだけを差し替えた LlmClient を作り、Retriever用に使う。
        # chat/image側は現行設定（アクティブLLMプリセット）に従う。
        embedding_llm_client = LlmClient(
            model=cfg.llm_model,
            embedding_model=preset_snapshot.embedding_model,
            image_model=cfg.image_model,
            api_key=cfg.llm_api_key,
            embedding_api_key=preset_snapshot.embedding_api_key,
            llm_base_url=cfg.llm_base_url,
            embedding_base_url=preset_snapshot.embedding_base_url,
            image_llm_base_url=cfg.image_llm_base_url,
            image_model_api_key=cfg.image_model_api_key,
            reasoning_effort=cfg.reasoning_effort,
            max_tokens=cfg.max_tokens,
            max_tokens_vision=cfg.max_tokens_vision,
            image_timeout_seconds=cfg.image_timeout_seconds,
        )

        # --- 発話者 client_id（必須） ---
        # NOTE: /api/chat は「発話者＝視覚命令の宛先」を一意に決める必要があるため、
        # client_id を必須とし、推定やフォールバックは行わない。
        speaker_client_id = str(request.client_id or "").strip()

        # --- 入力テキスト ---
        input_text = (request.input_text or "").strip()

        # --- client_context（保存/注入用） ---
        effective_client_context = self._merge_client_context(client_id=speaker_client_id, client_context=request.client_context)
        logger.info(
            "chat request received request_id=%s speaker_client_id=%s has_images=%s vision_preamble_enabled=%s",
            request_id,
            speaker_client_id,
            bool(request.images),
            not bool(request.images),
        )

        # --- チャット視覚（Vision） ---
        # 方針:
        # - 通常会話は「追加のLLM判定呼び出し」を行わない。
        # - 画像添付が無い場合は、LLMストリームの先頭1行（VISION_REQUESTプレアンブル）で判定する。
        # - 視覚が必要なら、いったんストリームを中断してキャプチャ→本返答を作る（同一ターンで返す）。
        images_input: list[Dict[str, str]] = list(request.images or [])
        include_vision_preamble = not bool(images_input)
        vision_extra_prompt: str | None = None
        vision_capture_error_for_user: str | None = None

        rerun_used = False
        while True:
            # --- 画像要約 ---
            image_summaries = self._summarize_images(images_input, purpose=LlmRequestPurpose.IMAGE_SUMMARY_CHAT)

            # 画像だけ送られた場合でも、LLMには明示的なユーザー要求を渡す（空文字だと不安定になりやすい）。
            if not input_text and image_summaries:
                input_text = "画像"
            if not input_text and not image_summaries:
                _log_client_timing(request_id=request_id, event="クライアント不正リクエスト", start_perf=start_perf)
                yield self._sse(
                    "error",
                    {"message": "入力が空です（テキストも画像もありません）", "code": "empty_input"},
                )
                return

            # entity抽出の入力はユーザー発話＋画像要約に限定する。
            entity_text = "\n".join(filter(None, [input_text, *(image_summaries or [])])).strip()

            conversation: List[Dict[str, str]] = []
            memory_pack = ""
            if memory_enabled:
                try:
                    # entity名抽出（LLM）を先に走らせ、Retriever検索と並列化する。
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        entity_future = None
                        if entity_text:
                            entity_future = executor.submit(extract_entity_names_with_llm, self.llm_client, entity_text)
                        # DBアクセスはロック内でまとめて行う。
                        with lock, memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                            recent_conversation = self._load_recent_conversation(db, turns=3)
                            llm_turns_window = int(getattr(cfg, "max_turns_window", 0) or 0)
                            if llm_turns_window > 0:
                                conversation = self._load_recent_conversation(db, turns=llm_turns_window)
                            retriever = Retriever(llm_client=embedding_llm_client, db=db)
                            relevant_episodes = retriever.retrieve(
                                input_text,
                                recent_conversation,
                                max_results=int(similar_episodes_limit),
                            )
                            # entity名の突合はDB内のalias/name一覧で行う。
                            alias_rows = collect_entity_alias_rows(db)
                            candidate_names = entity_future.result() if entity_future else []
                            matched_entity_ids = match_entity_ids(candidate_names, alias_rows)
                            memory_pack = build_memory_pack(
                                db=db,
                                input_text=input_text,
                                image_summaries=image_summaries,
                                client_context=effective_client_context,
                                now_ts=now_ts,
                                max_inject_tokens=int(max_inject_tokens),
                                relevant_episodes=relevant_episodes,
                                matched_entity_ids=matched_entity_ids,
                                injection_strategy=retriever.last_injection_strategy,
                            )
                except Exception as exc:  # noqa: BLE001
                    logger.error("MemoryPack生成に失敗しました", exc_info=exc)
                    _log_client_timing(request_id=request_id, event="クライアント要求エラー", start_perf=start_perf)
                    yield self._sse("error", {"message": str(exc), "code": "memory_pack_failed"})
                    return
            else:
                memory_pack = self._build_simple_memory_pack(
                    client_context=effective_client_context,
                    image_summaries=image_summaries,
                    now_ts=now_ts,
                )

            # system は固定化し、MemoryPack は内部コンテキストとして後置する。
            vision_instruction = ""
            if vision_extra_prompt:
                vision_instruction = str(vision_extra_prompt).strip()
            if vision_capture_error_for_user:
                msg = str(vision_capture_error_for_user).strip()
                vision_instruction = "\n\n".join(
                    [
                        (vision_instruction or "").strip(),
                        "視覚入力が必要だったが、画像が取得できなかった。",
                        f"ユーザーには「{msg}」のニュアンスで自然に伝える。",
                        "画像に言及せずに返せる範囲で答え、必要なら確認を1つだけ提案する。",
                    ]
                ).strip()

            extra_prompt = _persona_affect_trailer_system_prompt(include_vision_preamble=include_vision_preamble)
            if vision_instruction:
                extra_prompt = "\n\n".join([extra_prompt, vision_instruction]).strip()
            system_prompt = _build_system_prompt_base(
                persona_text=cfg.persona_text,
                addon_text=cfg.addon_text,
                requires_internal_trailer=True,
                extra_prompt=extra_prompt,
            )
            conversation = list(conversation)
            internal_context_message = _build_internal_context_message(memory_pack)
            if internal_context_message:
                conversation.append(internal_context_message)
            conversation.append({"role": "user", "content": input_text})

            # LLM呼び出しの処理目的をログで区別できるようにする。
            purpose = LlmRequestPurpose.CONVERSATION
            try:
                _log_llm_timing(request_id=request_id, event="送信開始", start_perf=start_perf)
                resp_stream = self.llm_client.generate_reply_response(
                    system_prompt=system_prompt,
                    conversation=conversation,
                    purpose=purpose,
                    stream=True,
                )
                _log_llm_timing(request_id=request_id, event="送信完了", start_perf=start_perf)
            except Exception as exc:  # noqa: BLE001
                logger.error("stream chat start failed", exc_info=exc)
                _log_llm_timing(request_id=request_id, event="送信エラー", start_perf=start_perf)
                yield self._sse("error", {"message": str(exc), "code": "llm_start_failed"})
                return

            reply_text = ""
            internal_trailer = ""
            finish_reason = ""
            stream_started = False
            sanitizer = _UserVisibleReplySanitizer()
            vision_request: dict[str, Any] | None = None
            preamble_buf = ""
            preamble_done = not bool(include_vision_preamble)

            try:
                marker = _STREAM_TRAILER_MARKER
                keep = max(8, len(marker) - 1)
                buf = ""
                in_trailer = False
                visible_parts: list[str] = []
                trailer_parts: list[str] = []

                def mark_stream_started() -> None:
                    nonlocal stream_started
                    if stream_started:
                        return
                    stream_started = True
                    _log_client_timing(request_id=request_id, event="クライアント送信開始", start_perf=start_perf)

                for delta in self.llm_client.stream_delta_chunks(resp_stream):
                    # finish_reason は最終チャンクで届くことがあるため、見つけたら保持する。
                    if delta.finish_reason:
                        finish_reason = delta.finish_reason
                    # テキスト差分がないチャンクはスキップする。
                    if not delta.text:
                        continue

                    text_piece = delta.text

                    # --- Visionプレアンブル（最初の1行） ---
                    if not preamble_done:
                        preamble_buf += text_piece
                        if "\n" not in preamble_buf:
                            # 先頭行が来ない場合はハングを避ける（安全寄りに通常応答へフォールバック）
                            if len(preamble_buf) > 512:
                                preamble_done = True
                                buf += preamble_buf
                                preamble_buf = ""
                            continue

                        line, rest = preamble_buf.split("\n", 1)
                        preamble_buf = ""
                        vr = _parse_vision_preamble_line(line)
                        if vr is not None and not rerun_used:
                            vision_request = vr
                            logger.info(
                                "vision request detected in preamble source=%s client_id=%s",
                                str(vr.get("source") or ""),
                                speaker_client_id,
                            )
                            break
                        preamble_done = True
                        # プレアンブル行の改行はユーザーへ見せないため落とす
                        buf += rest
                    else:
                        buf += text_piece

                    while True:
                        if not in_trailer:
                            idx = buf.find(marker)
                            if idx != -1:
                                chunk_text = buf[:idx]
                                if chunk_text:
                                    safe = sanitizer.feed(chunk_text)
                                    if safe:
                                        visible_parts.append(safe)
                                        mark_stream_started()
                                        yield self._sse("token", {"text": safe})
                                buf = buf[idx + len(marker) :]
                                in_trailer = True
                                continue
                            if len(buf) > keep:
                                chunk_text = buf[:-keep]
                                buf = buf[-keep:]
                                if chunk_text:
                                    safe = sanitizer.feed(chunk_text)
                                    if safe:
                                        visible_parts.append(safe)
                                        mark_stream_started()
                                        yield self._sse("token", {"text": safe})
                            break

                        # 以後はすべて内部トレーラーへ
                        if buf:
                            trailer_parts.append(buf)
                            buf = ""
                        break

                # Vision要求が出た場合は、ここでストリームを捨ててリランする。
                if vision_request is not None:
                    _log_llm_timing(request_id=request_id, event="ストリーム中断（Vision要求）", start_perf=start_perf)
                else:
                    if not in_trailer:
                        if buf:
                            safe = sanitizer.feed(buf)
                            if safe:
                                visible_parts.append(safe)
                                mark_stream_started()
                                yield self._sse("token", {"text": safe})
                    else:
                        if buf:
                            trailer_parts.append(buf)

                    tail_safe = sanitizer.flush()
                    if tail_safe:
                        visible_parts.append(tail_safe)
                        mark_stream_started()
                        yield self._sse("token", {"text": tail_safe})

                    reply_text = "".join(visible_parts)
                    internal_trailer = "".join(trailer_parts)
                    _log_llm_timing(request_id=request_id, event="ストリーム終了", start_perf=start_perf)
            except Exception as exc:  # noqa: BLE001
                logger.error("stream chat failed", exc_info=exc)
                _log_llm_timing(request_id=request_id, event="ストリームエラー", start_perf=start_perf)
                yield self._sse("error", {"message": str(exc), "code": "llm_stream_failed"})
                return

            # --- Vision要求（同一ターンキャプチャ） ---
            if vision_request is not None and not rerun_used:
                vision_extra_prompt = vision_request.get("extra_prompt")
                source = str(vision_request.get("source") or "").strip()
                cap_images, cap_ctx, cap_err = self._request_capture_for_chat(
                    embedding_preset_id=embedding_preset_id,
                    speaker_client_id=speaker_client_id,
                    source=source,
                )
                if cap_ctx:
                    effective_client_context = self._merge_client_context(client_id=speaker_client_id, client_context=cap_ctx)
                if cap_err:
                    vision_capture_error_for_user = cap_err
                    images_input = []
                else:
                    images_input = cap_images

                include_vision_preamble = False
                rerun_used = True
                continue

            # 内部コンテキスト混入があれば、内容は残さず事実だけログに残す（ユーザー表示は抑止済み）。
            if sanitizer.removed_blocks:
                logger.warning(
                    "LLM reply contained internal markers; sanitized removed_blocks=%s removed_lines=%s request_id=%s",
                    sanitizer.removed_blocks,
                    sanitizer.removed_lines,
                    request_id,
                )

            reflection_obj = _parse_internal_json_text(internal_trailer)

            llm_log_level = _get_llm_log_level_from_store(self.config_store)
            log_file_enabled = bool(self.config_store.toml_config.log_file_enabled)
            console_max_chars = _get_llm_log_console_max_chars_from_store(self.config_store)
            file_max_chars = _get_llm_log_file_max_chars_from_store(self.config_store)
            console_max_value_chars = _get_llm_log_console_value_max_chars_from_store(self.config_store)
            file_max_value_chars = _get_llm_log_file_value_max_chars_from_store(self.config_store)
            if llm_log_level != "OFF":
                io_console_logger.info(
                    "LLM response 受信 %s kind=chat stream=%s finish_reason=%s reply_chars=%s trailer_chars=%s",
                    purpose,
                    True,
                    finish_reason,
                    len(reply_text or ""),
                    len(internal_trailer or ""),
                )
                if log_file_enabled:
                    io_file_logger.info(
                        "LLM response 受信 %s kind=chat stream=%s finish_reason=%s reply_chars=%s trailer_chars=%s",
                        purpose,
                        True,
                        finish_reason,
                        len(reply_text or ""),
                        len(internal_trailer or ""),
                    )
            log_llm_payload(
                io_console_logger,
                "LLM response (chat stream)",
                {
                    "finish_reason": finish_reason,
                    "reply_text": reply_text,
                    "internal_trailer": internal_trailer,
                },
                max_chars=console_max_chars,
                max_value_chars=console_max_value_chars,
                llm_log_level=llm_log_level,
            )
            if log_file_enabled:
                log_llm_payload(
                    io_file_logger,
                    "LLM response (chat stream)",
                    {
                        "finish_reason": finish_reason,
                        "reply_text": reply_text,
                        "internal_trailer": internal_trailer,
                    },
                    max_chars=file_max_chars,
                    max_value_chars=file_max_value_chars,
                    llm_log_level=llm_log_level,
                )

            image_summary_text = "\n".join([s for s in image_summaries if s]) if image_summaries else None
            context_note = _json_dumps(effective_client_context) if effective_client_context else None

            try:
                with lock, memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                    episode_unit_id = self._create_episode_unit(
                        db,
                        now_ts=now_ts,
                        source="chat",
                        input_text=input_text,
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
                    self._maybe_enqueue_bond_summary(db, now_ts=now_ts)
            except Exception as exc:  # noqa: BLE001
                logger.error("episode保存に失敗しました", exc_info=exc)
                _log_client_timing(request_id=request_id, event="クライアント応答エラー", start_perf=start_perf)
                yield self._sse("error", {"message": str(exc), "code": "db_write_failed"})
                return

            if not stream_started:
                _log_client_timing(request_id=request_id, event="クライアント送信開始", start_perf=start_perf)
            _log_client_timing(request_id=request_id, event="クライアント送信終了", start_perf=start_perf)
            yield self._sse("done", {"episode_unit_id": episode_unit_id, "reply_text": reply_text, "usage": {}})
            return

    def handle_notification(
        self,
        request: schemas.NotificationRequest,
        *,
        background_tasks: Optional[BackgroundTasks] = None,
    ) -> schemas.NotificationResponse:
        """外部システムからの通知をEpisodeとして保存し、必要なジョブをenqueueする。"""
        embedding_preset_id = self.config_store.embedding_preset_id
        lock = _get_memory_lock(embedding_preset_id)
        now_ts = _now_utc_ts()

        system_text = f"[{request.source_system}] {request.text}".strip()
        context_note = _json_dumps({"source_system": request.source_system, "text": request.text})

        with lock, memory_session_scope(embedding_preset_id, self.config_store.embedding_dimension) as db:
            unit_id = self._create_episode_unit(
                db,
                now_ts=now_ts,
                source="notification",
                input_text=system_text,
                reply_text=None,
                image_summary=None,
                context_note=context_note,
                sensitivity=int(Sensitivity.NORMAL),
            )

        if background_tasks is not None:
            background_tasks.add_task(
                self._process_notification_async,
                embedding_preset_id=embedding_preset_id,
                unit_id=int(unit_id),
                source_system=request.source_system,
                text=request.text,
                images=request.images,
                system_text=system_text,
            )
        else:
            self._process_notification_async(
                embedding_preset_id=embedding_preset_id,
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
        embedding_preset_id: str,
        unit_id: int,
        source_system: str,
        text: str,
        images: Sequence[Dict[str, str]],
        system_text: str,
    ) -> None:
        lock = _get_memory_lock(embedding_preset_id)
        now_ts = _now_utc_ts()

        image_summaries = self._summarize_images(list(images), purpose=LlmRequestPurpose.IMAGE_SUMMARY_NOTIFICATION)
        image_summary_text = "\n".join([s for s in image_summaries if s]) if image_summaries else None

        notification_input_text = "\n".join(
            [
                "# notification",
                f"source_system: {source_system}",
                f"text: {text}",
            ]
        ).strip()

        cfg = self.config_store.config
        memory_enabled = self.config_store.memory_enabled

        # entity抽出の入力は通知文＋画像要約に限定する。
        entity_text = "\n".join(filter(None, [notification_input_text, *(image_summaries or [])])).strip()

        memory_pack = ""
        if memory_enabled:
            try:
                # entity名抽出（LLM）を先に走らせ、Retriever検索と並列化する。
                with ThreadPoolExecutor(max_workers=1) as executor:
                    entity_future = None
                    if entity_text:
                        entity_future = executor.submit(extract_entity_names_with_llm, self.llm_client, entity_text)
                    # DBアクセスはロック内でまとめて行う。
                    with lock, memory_session_scope(embedding_preset_id, self.config_store.embedding_dimension) as db:
                        recent_conversation = self._load_recent_conversation(db, turns=3, exclude_unit_id=unit_id)
                        retriever = Retriever(llm_client=self.llm_client, db=db)
                        relevant_episodes = retriever.retrieve(
                            notification_input_text,
                            recent_conversation,
                            max_results=int(cfg.similar_episodes_limit or 5),
                        )
                        # entity名の突合はDB内のalias/name一覧で行う。
                        alias_rows = collect_entity_alias_rows(db)
                        candidate_names = entity_future.result() if entity_future else []
                        matched_entity_ids = match_entity_ids(candidate_names, alias_rows)
                        memory_pack = build_memory_pack(
                            db=db,
                            input_text=notification_input_text,
                            image_summaries=image_summaries,
                            client_context=None,
                            now_ts=now_ts,
                            max_inject_tokens=int(cfg.max_inject_tokens),
                            relevant_episodes=relevant_episodes,
                            matched_entity_ids=matched_entity_ids,
                            injection_strategy=retriever.last_injection_strategy,
                        )
            except Exception as exc:  # noqa: BLE001
                logger.error("MemoryPack生成に失敗しました(notification)", exc_info=exc)
                memory_pack = ""
        else:
            memory_pack = self._build_simple_memory_pack(
                client_context=None,
                image_summaries=image_summaries,
                now_ts=now_ts,
            )

        # system は固定化し、MemoryPack は内部コンテキストとして後置する。
        system_prompt = _build_system_prompt_base(
            persona_text=cfg.persona_text,
            addon_text=cfg.addon_text,
            requires_internal_trailer=False,
            extra_prompt=get_external_prompt(),
        )
        conversation: List[Dict[str, str]] = []
        internal_context_message = _build_internal_context_message(memory_pack)
        if internal_context_message:
            conversation.append(internal_context_message)
        conversation.append({"role": "user", "content": notification_input_text})

        message = ""
        try:
            resp = self.llm_client.generate_reply_response(
                system_prompt=system_prompt,
                conversation=conversation,
                purpose=LlmRequestPurpose.NOTIFICATION,
                stream=False,
            )
            message = (self.llm_client.response_content(resp) or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.error("notification reply generation failed", exc_info=exc)
            message = ""

        with lock, memory_session_scope(embedding_preset_id, self.config_store.embedding_dimension) as db:
            self._update_episode_unit(
                db,
                now_ts=now_ts,
                unit_id=unit_id,
                reply_text=message or None,
                image_summary=image_summary_text,
            )
            self._enqueue_default_jobs(db, now_ts=now_ts, unit_id=unit_id)
            self._maybe_enqueue_bond_summary(db, now_ts=now_ts)

        publish_event(
            type="notification",
            embedding_preset_id=embedding_preset_id,
            unit_id=unit_id,
            data={"system_text": system_text, "message": message},
        )

    def handle_meta_request(
        self,
        request: schemas.MetaRequestRequest,
        *,
        background_tasks: Optional[BackgroundTasks] = None,
    ) -> None:
        """
        メタ要求（割り込み）から能動メッセージを生成する。

        方針:
        - 指示（instruction）やペイロード（payload）は永続化しない。
        - 生成された「ユーザーに見える本文」だけを通常のEpisodeとして保存する。
          → 後から辿ると「夢の話をした」は思い出せるが、「メタ要求があった」は辿れない。

        background_tasks があれば非同期実行し、結果はevent_streamで通知する。
        """
        embedding_preset_id = request.embedding_preset_id or self.config_store.embedding_preset_id

        if background_tasks is not None:
            background_tasks.add_task(
                self._process_meta_request_async,
                embedding_preset_id=embedding_preset_id,
                instruction=request.instruction,
                payload_text=request.payload_text,
                images=request.images,
            )
        else:
            self._process_meta_request_async(
                embedding_preset_id=embedding_preset_id,
                instruction=request.instruction,
                payload_text=request.payload_text,
                images=request.images,
            )
        return None

    def _process_meta_request_async(
        self,
        *,
        embedding_preset_id: str,
        instruction: str,
        payload_text: str,
        images: Sequence[Dict[str, str]],
    ) -> None:
        lock = _get_memory_lock(embedding_preset_id)
        now_ts = _now_utc_ts()

        # --- 画像要約（生成の材料・永続化しない） ---
        image_summaries = self._summarize_images(list(images), purpose=LlmRequestPurpose.IMAGE_SUMMARY_META_REQUEST)

        # --- meta-requestテキスト（生成用） ---
        # instruction/payload は永続化しない（生成にのみ利用する）。
        # 画像がある場合は、参照不能（ユーザーに見えない）になり得るため、要約を材料として渡す。
        meta_generation_text = "\n\n".join(
            [
                "# instruction",
                (instruction or "").strip(),
                "",
                "# payload",
                (payload_text or "").strip(),
            ]
        ).strip()
        if image_summaries:
            image_block = "\n".join([s for s in image_summaries if (s or "").strip()])
            if image_block:
                meta_generation_text = "\n\n".join([meta_generation_text, "# images", image_block]).strip()

        # --- 記憶検索/Entity抽出用テキスト（非永続・instructionは混ぜない） ---
        # 割り込み指示（制御プレーン）を埋め込み検索に混ぜると、検索が「指示の類似」に引っ張られやすい。
        # ここでは話題（データプレーン）に限定する。
        meta_retrieval_text = (payload_text or "").strip()

        cfg = self.config_store.config
        memory_enabled = self.config_store.memory_enabled

        # entity抽出は「話題（payload）」に寄せる（instructionや画像要約は混ぜない）。
        entity_text = meta_retrieval_text

        memory_pack = ""
        if memory_enabled:
            try:
                # entity名抽出（LLM）を先に走らせ、Retriever検索と並列化する。
                with ThreadPoolExecutor(max_workers=1) as executor:
                    entity_future = None
                    if entity_text:
                        entity_future = executor.submit(extract_entity_names_with_llm, self.llm_client, entity_text)
                    # DBアクセスはロック内でまとめて行う。
                    with lock, memory_session_scope(embedding_preset_id, self.config_store.embedding_dimension) as db:
                        recent_conversation = self._load_recent_conversation(db, turns=3, exclude_unit_id=None)
                        retriever = Retriever(llm_client=self.llm_client, db=db)
                        relevant_episodes = retriever.retrieve(
                            meta_retrieval_text,
                            recent_conversation,
                            max_results=int(cfg.similar_episodes_limit or 5),
                        )
                        # entity名の突合はDB内のalias/name一覧で行う。
                        alias_rows = collect_entity_alias_rows(db)
                        candidate_names = entity_future.result() if entity_future else []
                        matched_entity_ids = match_entity_ids(candidate_names, alias_rows)
                        memory_pack = build_memory_pack(
                            db=db,
                            input_text=meta_retrieval_text,
                            image_summaries=image_summaries,
                            client_context=None,
                            now_ts=now_ts,
                            max_inject_tokens=int(cfg.max_inject_tokens),
                            relevant_episodes=relevant_episodes,
                            matched_entity_ids=matched_entity_ids,
                            injection_strategy=retriever.last_injection_strategy,
                        )
            except Exception as exc:  # noqa: BLE001
                logger.error("MemoryPack生成に失敗しました(meta-request)", exc_info=exc)
                memory_pack = ""
        else:
            memory_pack = self._build_simple_memory_pack(
                client_context=None,
                image_summaries=image_summaries,
                now_ts=now_ts,
            )

        # system は固定化し、MemoryPack は内部コンテキストとして後置する。
        system_prompt = _build_system_prompt_base(
            persona_text=cfg.persona_text,
            addon_text=cfg.addon_text,
            requires_internal_trailer=False,
            extra_prompt=get_meta_request_prompt(),
        )
        conversation: List[Dict[str, str]] = []
        internal_context_message = _build_internal_context_message(memory_pack)
        if internal_context_message:
            conversation.append(internal_context_message)
        conversation.append({"role": "user", "content": meta_generation_text})

        message = ""
        try:
            resp = self.llm_client.generate_reply_response(
                system_prompt=system_prompt,
                conversation=conversation,
                purpose=LlmRequestPurpose.META_REQUEST,
                stream=False,
            )
            message = (self.llm_client.response_content(resp) or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.error("meta-request document generation failed", exc_info=exc)
            message = ""

        # --- 会話結果のみ保存 ---
        # meta-request（割り込み指示）は永続化しない。
        # 生成した本文だけを通常のEpisodeとして保存し、「話した事実」を後から辿れるようにする。
        episode_unit_id = -1
        if message:
            with lock, memory_session_scope(embedding_preset_id, self.config_store.embedding_dimension) as db:
                episode_unit_id = self._create_episode_unit(
                    db,
                    now_ts=now_ts,
                    source="proactive",
                    input_text=None,
                    reply_text=message,
                    image_summary=None,
                    context_note=None,
                    sensitivity=int(Sensitivity.NORMAL),
                )
                # 能動メッセージは「検索で思い出す」だけで十分なため、埋め込みのみ作る。
                self._enqueue_embeddings_job(db, now_ts=now_ts, unit_id=int(episode_unit_id))
                self._maybe_enqueue_bond_summary(db, now_ts=now_ts)

        publish_event(
            type="meta-request",
            embedding_preset_id=embedding_preset_id,
            unit_id=int(episode_unit_id),
            data={"message": message},
        )

    def _create_episode_unit(
        self,
        db,
        *,
        now_ts: int,
        source: str,
        input_text: Optional[str],
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
            persona_affect_label=None,
            persona_affect_intensity=None,
        )
        db.add(unit)
        db.flush()
        payload = PayloadEpisode(
            unit_id=unit.id,
            input_text=input_text,
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
        ]
        for kind in kinds:
            payload = {"unit_id": unit_id}
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
