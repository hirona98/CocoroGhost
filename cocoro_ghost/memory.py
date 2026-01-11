"""
記憶・チャット処理

このモジュールは CocoroGhost の中心として、以下を担う。

- `/api/chat`（SSE）: 出来事ログ（events）作成 → 検索 → 返答ストリーム → 非同期更新のキック
- `/api/v2/notification`: 通知を出来事ログとして保存し、イベントストリームへ配信
- `/api/v2/meta-request`: 外部要求は保存せず、能動メッセージの「結果」だけを保存（events.source="meta_proactive"）
- `/api/v2/vision/capture-response`: 画像を保存せず、詳細な画像説明テキストを出来事ログとして保存
"""

from __future__ import annotations

import base64
import concurrent.futures
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Generator

from fastapi import BackgroundTasks
from sqlalchemy import text

from cocoro_ghost import event_stream, schemas
from cocoro_ghost.config import ConfigStore
from cocoro_ghost.db import memory_session_scope
from cocoro_ghost.db import search_similar_item_ids
from cocoro_ghost.llm_debug import log_llm_payload, normalize_llm_log_level
from cocoro_ghost.llm_client import LlmClient, LlmRequestPurpose
from cocoro_ghost.memory_models import Event, EventAffect, EventLink, EventThread, Job, RetrievalRun, State
from cocoro_ghost import vision_bridge
from cocoro_ghost.time_utils import format_iso8601_local


logger = logging.getLogger(__name__)
_warned_memory_disabled = False


_VEC_KIND_EVENT = 1
_VEC_KIND_STATE = 2
_VEC_KIND_EVENT_AFFECT = 3
_VEC_ID_STRIDE = 10_000_000_000

_JOB_PENDING = 0


def _now_utc_ts() -> int:
    """現在時刻（UTC）をUNIX秒で返す。"""
    return int(time.time())


def _json_dumps(payload: Any) -> str:
    """DB保存向けにJSONを安定した形式でダンプする（日本語保持）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _fts_or_query(terms: list[str]) -> str:
    """
    FTS5 MATCH 用のORクエリを作る。

    目的:
        - 入力文字列に記号や空白が混ざっても検索が破綻しにくくする。
        - trigram FTS の「広め検索」を実現する。

    方針:
        - 各語をダブルクォートで囲む（フレーズ扱い）
        - ダブルクォートは "" にエスケープする
        - 空/短すぎる語は捨てる
    """
    cleaned: list[str] = []
    for t in terms:
        s = str(t or "").replace("\n", " ").replace("\r", " ").strip()
        if not s:
            continue
        if len(s) < 2:
            continue
        s = s.replace('"', '""')
        cleaned.append(f'"{s}"')
    if not cleaned:
        # NOTE: MATCH が空になるとエラーになる可能性があるため、最低限の語を返す。
        seed = str(terms[0] if terms else "_").replace("\n", " ").replace("\r", " ").strip()
        seed = seed.replace('"', '""')
        return f'"{seed}"'
    # NOTE: 長すぎるORは重くなるため、上位だけ使う。
    return " OR ".join(cleaned[:8])


def _vec_item_id(kind: int, entity_id: int) -> int:
    """vec_items の item_id を決定する（kind + entity_id の名前空間衝突を避ける）。"""
    return int(kind) * int(_VEC_ID_STRIDE) + int(entity_id)


def _vec_entity_id(item_id: int) -> int:
    """vec_items の item_id から entity_id を復元する。"""
    return int(item_id) % int(_VEC_ID_STRIDE)


def _first_choice_content(resp: Any) -> str:
    """LiteLLMのレスポンスから最初のchoiceのcontentを取り出す。"""
    try:
        return str(resp["choices"][0]["message"]["content"] or "")
    except Exception:  # noqa: BLE001
        try:
            return str(resp.choices[0].message.content or "")
        except Exception:  # noqa: BLE001
            return ""


def _parse_first_json_object(text: str) -> dict[str, Any] | None:
    """LLM出力から最初のJSONオブジェクトを抽出してdictとして返す。"""
    s = str(text or "").strip()
    if not s:
        return None

    # --- llm_client の内部ユーティリティで抽出/修復する ---
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


def _sse(event: str, data: dict) -> str:
    """SSEの1イベントを文字列化する。"""
    return f"event: {event}\n" + f"data: {_json_dumps(data)}\n\n"


class _UserVisibleReplySanitizer:
    """ユーザーに見せる本文から、内部コンテキストの混入を除去する。"""

    # NOTE: 改行が出ないモデルでもストリーミング体感を維持するための最小送信単位。
    _STREAM_FLUSH_THRESHOLD_CHARS = 64

    def __init__(self) -> None:
        # --- feed()で改行が来るまで保留する末尾（行未確定） ---
        self._pending: str = ""

        # --- 内部ブロックをスキップ中かどうか ---
        self._skip_mode: bool = False

        # --- スキップ開始後に、空行以外を1行でも捨てたか ---
        self._skipped_any_line_in_block: bool = False

    def feed(self, text: str) -> str:
        """差分テキストを取り込み、ユーザーに送ってよいテキストだけ返す。"""

        # --- 空は即返す ---
        if not text:
            return ""

        # --- バッファへ追加 ---
        self._pending += text

        # --- 改行単位で確定処理 ---
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

        # --- 改行が来ないモデルでも、体感速度（ストリーミング）を維持する ---
        # 方針:
        # - 内部用タグ（"<" 始まり）の混入を避けるため、"<" を含む場合は保留する。
        # - それ以外は一定文字数で分割して送る（行単位以外でも流れるようにする）。
        if not self._skip_mode and self._pending and "<" not in self._pending:
            # NOTE: 小さすぎると無駄なイベントが増えるため、ほどよい粒度で送る。
            if len(self._pending) >= int(self._STREAM_FLUSH_THRESHOLD_CHARS):
                out_parts.append(self._pending)
                self._pending = ""
        return "".join(out_parts)

    def flush(self) -> str:
        """末尾（改行が無い行）を確定し、送ってよいテキストだけ返す。"""
        if not self._pending:
            return ""
        tail = self._process_line(self._pending)
        self._pending = ""
        return tail

    def _process_line(self, line: str) -> str:
        """1行分を処理し、送信する場合はそのまま返す。"""
        stripped_line = line.rstrip("\n").rstrip("\r").strip()

        # --- 内部ブロックの終端検出 ---
        if self._skip_mode:
            if stripped_line:
                self._skipped_any_line_in_block = True
                return ""
            if self._skipped_any_line_in_block:
                self._skip_mode = False
                self._skipped_any_line_in_block = False
            return ""

        # --- 内部っぽい行が来たら、その行から次の空行まで捨てる ---
        if self._is_internal_line(stripped_line):
            self._skip_mode = True
            self._skipped_any_line_in_block = False
            return ""

        return line

    def _is_internal_line(self, stripped_line: str) -> bool:
        """内部用の制御行/見出しに見えるかを判定する。"""
        if not stripped_line:
            return False

        # --- 内部コンテキスト開始タグ ---
        if stripped_line == "<<INTERNAL_CONTEXT>>":
            return True

        # --- 明示的な内部見出し ---
        if stripped_line.startswith("<<<") and stripped_line.endswith(">>>"):
            return True

        return False


def _search_plan_system_prompt() -> str:
    """SearchPlan生成用のsystem promptを返す。"""
    return "\n".join(
        [
            "あなたは会話用の記憶検索計画（SearchPlan）を作る。",
            "出力はJSONオブジェクトのみ（前後に説明文やコードフェンスは禁止）。",
            "",
            "目的:",
            "- ユーザー入力に対して「最近の連想」か「全期間の目的検索」かを選び、候補収集の方針を固定する",
            "",
            "出力スキーマ（型が重要）:",
            "{",
            '  "mode": "associative_recent|targeted_broad|explicit_about_time",',
            '  "queries": ["string"],',
            '  "time_hint": {',
            '    "about_year_start": null,',
            '    "about_year_end": null,',
            '    "life_stage_hint": ""',
            "  },",
            '  "diversify": {"by": ["life_stage", "about_year_bucket"], "per_bucket": 5},',
            '  "limits": {"max_candidates": 200, "max_selected": 12}',
            "}",
            "",
            "ルール:",
            "- 日本語の会話前提。queries は短い検索語（固有名詞/話題/型番など）を1〜5個。",
            "- 指示語だけで検索語が作れない場合は、queries=[ユーザー入力そのまま] でよい。",
            "- about_year_start/about_year_end は整数（年）か null。文字列の年（\"2018\"）は出さない。",
            "- life_stage_hint は elementary|middle|high|university|work|unknown のどれか。分からなければ \"\"。",
            "",
            "mode の選び方:",
            "- 直近の続き/指示語が多い/\"さっき\" など → associative_recent",
            "- \"昔\" \"子供の頃\" \"学生の頃\" など（全期間から探したい） → targeted_broad",
            "- 年や時期が明示（例: 2018年、高校の頃） → explicit_about_time（time_hintも埋める）",
            "",
            "注意: 迷ったら associative_recent。",
        ]
    ).strip()


def _selection_system_prompt() -> str:
    """SearchResultPack生成（選別）用のsystem promptを返す。"""
    return "\n".join(
        [
            "あなたは会話のために、候補記憶から必要なものだけを選び、SearchResultPackを作る。",
            "出力はJSONオブジェクトのみ（前後に説明文やコードフェンスは禁止）。",
            "",
            "入力: user_input, plan, candidates（event/state/event_affect）。",
            "目的: ユーザー入力に答えるのに必要な記憶だけを最大 max_selected 件まで選ぶ（ノイズは捨てる）。",
            "",
            "選び方（品質）:",
            "- まずは state（fact/relation/task/summary）を優先し、足りない分を event（具体エピソード）で補う。",
            "- 同じ内容の重複は代表1件に寄せる（近縁が多いのは仕様だが、採用は絞る）。",
            "- mode=associative_recent では最近性を優先する。",
            "- mode=targeted_broad/explicit_about_time では期間/ライフステージの偏りを避ける。",
            "- event_affect は内部用。必要なら少数だけ（トーン調整用）。",
            "",
            "重要（出力の厳格さ）:",
            "- selected の各要素は、必ず次のキーを全て含める: type, event_id, state_id, affect_id, why, snippet",
            "- type は event|state|event_affect のいずれか。",
            "- event_id/state_id/affect_id はDBの主キー。入力の candidates に存在するIDのみを使い、絶対に作り出さない。",
            "- type=event の場合: event_id>0, state_id=0, affect_id=0",
            "- type=state の場合: state_id>0, event_id=0, affect_id=0",
            "- type=event_affect の場合: affect_id>0, event_id=0, state_id=0",
            "- 選べない場合は selected を空配列にする（形だけ埋めてはいけない）。",
            "",
            "出力スキーマ（概略）:",
            "{",
            '  "selected": [',
            "    {",
            '      "type": "event|state|event_affect",',
            '      "event_id": 0,',
            '      "state_id": 0,',
            '      "affect_id": 0,',
            '      "why": "短い理由",',
            '      "snippet": "短い抜粋（必要なら）"',
            "    }",
            "  ]",
            "}",
            "",
            "注意:",
            "- why は短く具体的に（会話にどう効くか）。snippet は短い抜粋（不要なら空文字列でよい）。",
            "- event_affect（内心）は内部用。本文にそのまま出さない前提で、返答の雰囲気調整に使う。",
        ]
    ).strip()


def _reply_system_prompt(*, persona_text: str, addon_text: str) -> str:
    """返答生成用のsystem promptを組み立てる。"""
    parts: list[str] = []

    # --- 内部コンテキスト露出防止 ---
    parts.append(
        "\n".join(
            [
                "重要: <<INTERNAL_CONTEXT>> で始まるメッセージは内部用。本文に出力しない。",
                "- 内部用JSONや内部見出し（<<<...>>>）を本文に出力しない。",
                "- 本文は日本語で、ユーザーに向けた会話の発話（セリフ）として自然に成立する文章を書く。",
                "- 返答は「ユーザーに向けて発話されたセリフ」のみ。モノローグ、心理描写、括弧書きの内心は一切書かない。",
                "",
                "内部コンテキストを以下のように使うこと:",
                "- TimeContext（now/last_chat_created_at/gap_seconds）: 実際の経過時間（gap_seconds）との整合性を保つ。",
                "- LongMoodState（背景の長期感情）: JSONやVAD数値は出さず、雰囲気や言葉選びに反映する。",
                "- SearchResultPack（思い出した候補記憶）: 必要な範囲だけを会話に自然に織り込む（IDやキー名は出さない）。",
                "- event_affect（瞬間感情/内心）: 内部用。本文に一切出さない。「（内心：...）」のような括弧書きの心情描写も禁止。雰囲気や言葉選びにだけ反映する。",
                "",
                "記憶を以下のように使うこと:",
                "- SearchResultPack/直近会話に無い事実は断定しない。推測するなら「たぶん/覚えてる限り」などで不確実さを明示する。",
                "- 重要情報が欠ける場合は、自然に質問して埋める。",
                "- 矛盾が見える場合は断定せず、どちらが正しいかユーザーに確認する。",
                "",
                "人間としての振る舞い（重要）:",
                "- 正しさや効率よりも、その場の感情や共感を優先する。",
                "- 記憶（SearchResultPack）は、データベースの検索結果としてではなく、脳裏に浮かんだ「思い出」として語る。",
                "- 全てを知っている必要はない。分からないことは素直に分からないと言ったり、興味を持って聞き返したりする。",
                "- ユーザーの体調や気分の変化には敏感に反応し、用件よりも優先して気遣う言葉をかける。",
                "",
                "視点・口調:",
                "- 一人称/二人称/口調は persona の指定を最優先する（指定が無い場合は一人称=私、二人称=あなた）。",
                "- 自分を三人称（「このアシスタント」など）で呼ばない。",
                "- システム/DB/検索/プロンプト/モデル/トークン等の内部実装には触れない（ユーザーが明示的に求めた場合を除く）。",
            ]
        ).strip()
    )

    # --- ペルソナ（ユーザー編集） ---
    # NOTE: 行末（CRLF/LF）の揺れは暗黙的キャッシュの阻害になり得るため、ここで正規化する。
    pt = str(persona_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    at = str(addon_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if pt or at:
        parts.append("\n".join([x for x in [pt, at] if x]).strip())

    return "\n\n".join([p for p in parts if p]).strip()


@dataclass(frozen=True)
class _CandidateItem:
    """候補アイテム（イベント/状態/感情）。"""

    type: str  # event/state/event_affect
    id: int
    rank_ts: int
    meta: dict[str, Any]
    hit_sources: list[str]


class MemoryManager:
    """記憶操作の窓口（API層から呼ばれる）。"""

    def __init__(self, *, llm_client: LlmClient, config_store: ConfigStore) -> None:
        self.llm_client = llm_client
        self.config_store = config_store

    def _load_recent_chat_dialog_messages(
        self,
        *,
        embedding_preset_id: str,
        embedding_dimension: int,
        client_id: str,
        exclude_event_id: int,
        max_turn_events: int,
    ) -> list[dict[str, str]]:
        """
        直近のチャット会話（短期コンテキスト）を messages 形式で返す。

        注意:
            - クライアントは単純I/Oなので、サーバ側で直近会話を付与して会話の安定性を上げる。
            - ここは「会話の流れ」を補助する目的。検索（記憶）は別経路（SearchResultPack）。
            - with を抜けても安全なように、ORMを返さず dict だけ返す。
        """
        cid = str(client_id or "").strip()
        if not cid:
            return []

        # --- 直近ターン数（短期コンテキスト） ---
        # NOTE: max_turns_window は常に設定される前提（欠損フォールバックはしない）。
        n = int(max_turn_events)
        if n <= 0:
            return []
        n = max(1, n)

        # --- 1イベント=1ターン（user_text + assistant_text）を想定 ---
        rows: list[tuple[int, str, str]] = []
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            q = (
                db.query(Event.event_id, Event.user_text, Event.assistant_text)
                .filter(Event.source == "chat")
                .filter(Event.client_id == cid)
                .filter(Event.event_id != int(exclude_event_id))
                # assistant_text が無いターン（作成途中）は除外する
                .filter(Event.assistant_text.isnot(None))
                .order_by(Event.event_id.desc())
                .limit(int(n))
            )
            for r in q.all():
                if not r:
                    continue
                eid = int(r[0] or 0)
                ut = str(r[1] or "")
                at = str(r[2] or "")
                rows.append((eid, ut, at))

        # --- 新しい順で取っているので、会話としては古い順に並べ直す ---
        rows.reverse()

        out: list[dict[str, str]] = []
        # NOTE: メッセージは切り詰めず、そのまま送る。
        for _, ut, at in rows:
            if str(ut or "").strip():
                out.append({"role": "user", "content": str(ut)})
            if str(at or "").strip():
                out.append({"role": "assistant", "content": str(at)})
        return out

    def _load_long_mood_state_snapshot(
        self, *, embedding_preset_id: str, embedding_dimension: int
    ) -> dict[str, Any] | None:
        """
        長期気分（state.kind="long_mood_state"）の最新スナップショットを返す。

        目的:
            - 返答生成で「背景の気分」を安定して参照できるようにする（SearchResultPackの選別に依存しない）。

        注意:
            - 1ユーザー前提（client_id は端末識別）のため、ここでは client_id で分けない。
            - with を抜けても安全なように、ORMを返さず dict だけ返す。
        """
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            st = (
                db.query(State)
                .filter(State.kind == "long_mood_state")
                .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
                .first()
            )
            if st is None:
                return None

            # --- payload_json を dict として扱えるようにする ---
            payload_raw = str(st.payload_json or "").strip()
            payload_obj: Any
            if payload_raw:
                try:
                    payload_obj = json.loads(payload_raw)
                except Exception:  # noqa: BLE001
                    payload_obj = {"_raw": payload_raw}
            else:
                payload_obj = {}

            # --- VAD を取り出せる場合は、見やすい形で別キーにする ---
            vad: dict[str, float] | None = None
            if isinstance(payload_obj, dict):
                if all(k in payload_obj for k in ["v", "a", "d"]):
                    try:
                        vad = {
                            "v": float(payload_obj.get("v")),  # type: ignore[arg-type]
                            "a": float(payload_obj.get("a")),  # type: ignore[arg-type]
                            "d": float(payload_obj.get("d")),  # type: ignore[arg-type]
                        }
                    except Exception:  # noqa: BLE001
                        vad = None
                elif isinstance(payload_obj.get("vad"), dict):
                    vv = payload_obj.get("vad")
                    if isinstance(vv, dict) and all(k in vv for k in ["v", "a", "d"]):
                        try:
                            vad = {
                                "v": float(vv.get("v")),  # type: ignore[arg-type]
                                "a": float(vv.get("a")),  # type: ignore[arg-type]
                                "d": float(vv.get("d")),  # type: ignore[arg-type]
                            }
                        except Exception:  # noqa: BLE001
                            vad = None

            return {
                "state_id": int(st.state_id),
                "kind": str(st.kind),
                "body_text": str(st.body_text),
                "payload": payload_obj,
                "vad": vad,
                "confidence": float(st.confidence),
                "salience": float(st.salience),
                "last_confirmed_at": format_iso8601_local(int(st.last_confirmed_at)),
                "valid_from_ts": (
                    format_iso8601_local(int(st.valid_from_ts)) if st.valid_from_ts is not None else None
                ),
                "valid_to_ts": format_iso8601_local(int(st.valid_to_ts)) if st.valid_to_ts is not None else None,
            }

    def _llm_io_loggers(self) -> tuple[logging.Logger, logging.Logger]:
        """LLM I/O ログの出力先ロガー（console/file）を返す。"""
        return (logging.getLogger("cocoro_ghost.llm_io.console"), logging.getLogger("cocoro_ghost.llm_io.file"))

    def _llm_log_level(self) -> str:
        """TOML設定に基づく LLMログレベル（DEBUG/INFO/OFF）を返す。"""
        return normalize_llm_log_level(self.config_store.toml_config.llm_log_level)

    def _llm_log_limits(self) -> tuple[int, int, int, int]:
        """TOML設定に基づく LLMログの文字数上限を返す。"""
        tc = self.config_store.toml_config
        return (
            int(tc.llm_log_console_max_chars),
            int(tc.llm_log_file_max_chars),
            int(tc.llm_log_console_value_max_chars),
            int(tc.llm_log_file_value_max_chars),
        )

    def _log_retrieval_debug(self, label: str, payload: Any) -> None:
        """検索（候補収集）まわりのデバッグ情報を LLM I/O ログへ出力する。"""

        # --- LLMログレベル（TOML）に従う ---
        llm_log_level = self._llm_log_level()
        if llm_log_level != "DEBUG":
            return

        # --- 出力先（console/file） ---
        console_logger, file_logger = self._llm_io_loggers()

        # --- トリミングはTOML設定に従う ---
        console_max_chars, file_max_chars, console_max_value_chars, file_max_value_chars = self._llm_log_limits()
        log_llm_payload(
            console_logger,
            label,
            payload,
            llm_log_level=llm_log_level,
            max_chars=int(console_max_chars),
            max_value_chars=int(console_max_value_chars),
        )
        log_llm_payload(
            file_logger,
            label,
            payload,
            llm_log_level=llm_log_level,
            max_chars=int(file_max_chars),
            max_value_chars=int(file_max_value_chars),
        )

    def _log_llm_stream_receive_complete(
        self,
        *,
        purpose: str,
        finish_reason: str,
        content: str,
        elapsed_ms: int,
    ) -> None:
        """ストリーミング応答の「受信完了」を LLM I/O ログへ出力する。"""

        # --- LLMログレベル（TOML）に従う ---
        llm_log_level = self._llm_log_level()
        if llm_log_level == "OFF":
            return

        # --- 出力先（console/file） ---
        console_logger, file_logger = self._llm_io_loggers()

        # --- 受信メタ（INFO） ---
        msg = "LLM response 受信 %s kind=chat stream=%s finish_reason=%s chars=%s ms=%s"
        args = (str(purpose), True, str(finish_reason or ""), len(content or ""), int(elapsed_ms))
        console_logger.info(msg, *args)
        file_logger.info(msg, *args)

        # --- 本文（DEBUGのみ） ---
        console_max_chars, file_max_chars, console_max_value_chars, file_max_value_chars = self._llm_log_limits()
        payload = {"finish_reason": str(finish_reason or ""), "content": str(content or "")}
        log_llm_payload(
            console_logger,
            "LLM response (chat)",
            payload,
            llm_log_level=llm_log_level,
            max_chars=int(console_max_chars),
            max_value_chars=int(console_max_value_chars),
        )
        log_llm_payload(
            file_logger,
            "LLM response (chat)",
            payload,
            llm_log_level=llm_log_level,
            max_chars=int(file_max_chars),
            max_value_chars=int(file_max_value_chars),
        )

    def stream_chat(self, request: schemas.ChatRequest, background_tasks: BackgroundTasks) -> Generator[str, None, None]:
        """チャットをSSEで返す（出来事ログ作成→検索→ストリーム→非同期更新）。"""

        # --- 設定を取得 ---
        cfg = self.config_store.config
        embedding_preset_id = str(request.embedding_preset_id or cfg.embedding_preset_id).strip()
        embedding_dimension = int(cfg.embedding_dimension)

        # --- 記憶が無効なら、ベクトル索引/状態更新が育たない（初回だけ強くログする） ---
        global _warned_memory_disabled
        if not bool(self.config_store.memory_enabled) and not bool(_warned_memory_disabled):
            _warned_memory_disabled = True
            logger.warning("memory_enabled=false のため、非同期ジョブ（埋め込み/状態更新）は実行されません")

        # --- 入力を正規化 ---
        client_id = str(request.client_id or "").strip()
        input_text = str(request.input_text or "").strip()
        if not input_text:
            yield _sse("error", {"message": "input_text must not be empty", "code": "invalid_request"})
            return

        now_ts = _now_utc_ts()
        last_chat_created_at_ts: int | None = None

        # --- eventsを作成（ターン単位） ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            # --- 1) events を作る（assistant_text は後で埋める） ---
            ev = Event(
                created_at=now_ts,
                updated_at=now_ts,
                client_id=client_id,
                source="chat",
                user_text=input_text,
                assistant_text=None,
                entities_json="[]",
                client_context_json=_json_dumps(request.client_context) if request.client_context is not None else None,
            )
            db.add(ev)
            db.flush()
            event_id = int(ev.event_id)

            # --- 2) reply_to（同じclient_idの直前チャット）を軽量に仮置きする ---
            prev = (
                db.query(Event.event_id, Event.created_at)
                .filter(Event.client_id == client_id)
                .filter(Event.source == "chat")
                .filter(Event.event_id != event_id)
                # assistant_text が無いターン（作成途中）は除外する
                .filter(Event.assistant_text.isnot(None))
                .order_by(Event.event_id.desc())
                .first()
            )
            if prev is not None and int(prev[0] or 0) > 0:
                # NOTE: 文脈グラフの本更新は非同期で行う。ここでは reply_to だけを即時に張る。
                from cocoro_ghost.memory_models import EventLink  # noqa: PLC0415

                db.add(
                    EventLink(
                        from_event_id=event_id,
                        to_event_id=int(prev[0]),
                        label="reply_to",
                        confidence=1.0,
                        evidence_event_ids_json="[]",
                        created_at=now_ts,
                    )
                )
                last_chat_created_at_ts = int(prev[1] or 0) if int(prev[1] or 0) > 0 else None

        # --- 3) 先行: 埋め込み取得（input_text のみ。SearchPlan生成と重ねて待ちを削る） ---
        # NOTE:
        # - 段階化（追加クエリの追い埋め込み）はしない（シンプル優先）。
        # - 先行埋め込みは vector_all で使い、文字n-gram側は SearchPlan.queries を使える。
        vector_embedding_future: concurrent.futures.Future[list[Any]] | None = None
        pre_ex: concurrent.futures.ThreadPoolExecutor | None = None
        try:
            pre_ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            vector_embedding_future = pre_ex.submit(
                self.llm_client.generate_embedding,
                [str(input_text)],
                purpose=LlmRequestPurpose.SYNC_RETRIEVAL_QUERY_EMBEDDING,
            )
        finally:
            # NOTE: 送信済みタスクは継続する。ここで待たない（体感速度優先）。
            try:
                if pre_ex is not None:
                    pre_ex.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass

        # --- 4) SearchPlan（LLM） ---
        plan_obj: dict[str, Any] = {
            "mode": "associative_recent",
            "queries": [input_text],
            "time_hint": {"about_year_start": None, "about_year_end": None, "life_stage_hint": ""},
            "diversify": {"by": ["life_stage", "about_year_bucket"], "per_bucket": 5},
            "limits": {"max_candidates": 200, "max_selected": 12},
        }
        try:
            resp = self.llm_client.generate_json_response(
                system_prompt=_search_plan_system_prompt(),
                input_text=input_text,
                purpose=LlmRequestPurpose.SYNC_SEARCH_PLAN,
                max_tokens=500,
            )
            obj = _parse_first_json_object(_first_choice_content(resp))
            if obj is not None:
                plan_obj = obj
        except Exception as exc:  # noqa: BLE001
            logger.warning("SearchPlan generation failed; fallback to default", exc_info=exc)

        # --- 5) 候補収集（取りこぼし防止優先・可能なものは並列） ---
        candidates: list[_CandidateItem] = self._collect_candidates(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
            input_text=input_text,
            plan_obj=plan_obj,
            vector_embedding_future=vector_embedding_future,
        )

        # --- 5) retrieval_runs を記録（plan + candidate統計） ---
        run_id: int | None = None
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            rr = RetrievalRun(
                event_id=int(event_id),
                created_at=now_ts,
                plan_json=_json_dumps(plan_obj),
                candidates_json=_json_dumps(
                    {
                        "counts": {
                            "total": len(candidates),
                            "event": sum(1 for c in candidates if c.type == "event"),
                            "state": sum(1 for c in candidates if c.type == "state"),
                            "event_affect": sum(1 for c in candidates if c.type == "event_affect"),
                        },
                        "sources": {f"{c.type}:{c.id}": c.hit_sources for c in candidates},
                    }
                ),
                selected_json="{}",
                timings_json="{}",
                errors_json="{}",
            )
            db.add(rr)
            db.flush()
            run_id = int(rr.run_id)

        # --- 6) 選別（LLM → SearchResultPack） ---
        search_result_pack: dict[str, Any] = {"selected": []}
        try:
            selection_input = _json_dumps(
                {
                    "user_input": input_text,
                    "plan": plan_obj,
                    "candidates": [dict(c.meta) | {"hit_sources": c.hit_sources} for c in candidates],
                }
            )
            resp = self.llm_client.generate_json_response(
                system_prompt=_selection_system_prompt(),
                input_text=selection_input,
                purpose=LlmRequestPurpose.SYNC_SEARCH_SELECT,
                max_tokens=1500,
            )
            obj = _parse_first_json_object(_first_choice_content(resp))
            if obj is not None:
                search_result_pack = obj
        except Exception as exc:  # noqa: BLE001
            logger.warning("SearchResultPack selection failed; fallback to empty", exc_info=exc)

        # --- 7) retrieval_runs を更新（selected_json） ---
        if run_id is not None:
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                db.execute(
                    text("UPDATE retrieval_runs SET selected_json=:v WHERE run_id=:id"),
                    {"v": _json_dumps(search_result_pack), "id": int(run_id)},
                )

        # --- 8) 返答をSSEで生成（SearchResultPackを内部注入） ---
        system_prompt = _reply_system_prompt(persona_text=cfg.persona_text, addon_text=cfg.addon_text)
        gap_seconds: int | None = None
        if last_chat_created_at_ts is not None and int(last_chat_created_at_ts) > 0:
            gap_seconds = int(now_ts) - int(last_chat_created_at_ts)
            if gap_seconds < 0:
                gap_seconds = 0

        internal_context = _json_dumps(
            {
                "TimeContext": {
                    "now": format_iso8601_local(int(now_ts)),
                    "last_chat_created_at": (
                        format_iso8601_local(int(last_chat_created_at_ts))
                        if last_chat_created_at_ts is not None and int(last_chat_created_at_ts) > 0
                        else None
                    ),
                    "gap_seconds": (int(gap_seconds) if gap_seconds is not None else None),
                },
                "LongMoodState": self._load_long_mood_state_snapshot(
                    embedding_preset_id=embedding_preset_id,
                    embedding_dimension=embedding_dimension,
                ),
                "SearchResultPack": self._inflate_search_result_pack(candidates, search_result_pack),
            }
        )
        # --- 直近会話（短期コンテキスト）を付与して会話の安定性を上げる ---
        # NOTE:
        # - 記憶（長期）は SearchResultPack で注入する。
        # - 直近会話は「文脈の流れ（指示・口調・直前の合意）」のために常に少量入れる。
        # - max_turns_window は LLM プリセット（設定UI）側の値を使う（常に存在する前提）。
        recent_dialog = self._load_recent_chat_dialog_messages(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            client_id=client_id,
            exclude_event_id=int(event_id),
            max_turn_events=int(cfg.max_turns_window),
        )
        # --- 暗黙的キャッシュ（プロンプトキャッシュ）を効かせやすくする ---
        # NOTE:
        # - 「先頭側が同じほどキャッシュが効きやすい」前提で、system直後に固定ヘッダを置く。
        # - SearchResultPack/TimeContext はターンごとに変化しやすいので末尾側に寄せる。
        # - 直近会話は短期文脈として必要だが、先頭が揺れやすいので固定ヘッダの後ろへ置く。
        conversation: list[dict[str, str]] = []
        conversation.append(
            {
                "role": "assistant",
                "content": "\n".join(
                    [
                        "<<INTERNAL_CONTEXT>>",
                        "このメッセージは固定ヘッダ。本文に出力しない。",
                    ]
                ),
            }
        )
        conversation.extend(recent_dialog)
        conversation.append({"role": "assistant", "content": f"<<INTERNAL_CONTEXT>>\n{internal_context}"})
        conversation.append({"role": "user", "content": input_text})

        reply_text = ""
        finish_reason = ""
        sanitizer = _UserVisibleReplySanitizer()
        stream_failed = False
        stream_start = time.perf_counter()
        try:
            resp_stream = self.llm_client.generate_reply_response(
                system_prompt=system_prompt,
                conversation=conversation,
                purpose=LlmRequestPurpose.SYNC_CONVERSATION,
                stream=True,
            )
            for delta in self.llm_client.stream_delta_chunks(resp_stream):
                if delta.finish_reason:
                    finish_reason = str(delta.finish_reason)
                if not delta.text:
                    continue
                safe = sanitizer.feed(delta.text)
                if safe:
                    reply_text += safe
                    yield _sse("token", {"text": safe})
            tail = sanitizer.flush()
            if tail:
                reply_text += tail
                yield _sse("token", {"text": tail})
        except Exception as exc:  # noqa: BLE001
            stream_failed = True
            logger.error("chat stream failed", exc_info=exc)
            yield _sse("error", {"message": str(exc), "code": "llm_stream_failed"})
            return
        finally:
            # --- ストリーミング受信ログ（切断や例外時でも、可能な限り出す） ---
            if not stream_failed:
                elapsed_ms = int((time.perf_counter() - stream_start) * 1000)
                self._log_llm_stream_receive_complete(
                    purpose=LlmRequestPurpose.SYNC_CONVERSATION,
                    finish_reason=str(finish_reason or ""),
                    content=str(reply_text or ""),
                    elapsed_ms=int(elapsed_ms),
                )

        # --- 9) events を更新（assistant_text） ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            db.execute(
                text("UPDATE events SET assistant_text=:t, updated_at=:u WHERE event_id=:id"),
                {"t": reply_text, "u": _now_utc_ts(), "id": int(event_id)},
            )

        # --- 10) 非同期: 埋め込み更新ジョブを積む（次ターンで効く） ---
        background_tasks.add_task(
            self._enqueue_event_embedding_job,
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 10.5) 非同期: 記憶更新（WritePlan） ---
        # NOTE:
        # - 返答とは別に「状態/感情/文脈/要約」を育てる。
        # - 体感速度を壊さないため、同期では行わない。
        background_tasks.add_task(
            self._enqueue_write_plan_job,
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 11) SSE完了 ---
        yield _sse("done", {"event_id": int(event_id), "reply_text": reply_text, "usage": {"finish_reason": finish_reason}})

    def handle_notification(self, request: schemas.NotificationRequest, *, background_tasks: BackgroundTasks) -> None:
        """通知を受け取り、出来事ログとして保存し、イベントとして配信する。"""

        # --- 設定 ---
        cfg = self.config_store.config
        embedding_preset_id = str(cfg.embedding_preset_id).strip()
        embedding_dimension = int(cfg.embedding_dimension)
        now_ts = _now_utc_ts()

        # --- 入力 ---
        source_system = str(request.source_system or "").strip()
        text_in = str(request.text or "").strip()

        # --- 画像は保存しない（必要なら詳細説明を別イベントで残す） ---
        # NOTE: 今回は通知自体の処理を優先し、画像説明は扱わない。

        # --- LLMで人格メッセージを生成 ---
        system_prompt = _reply_system_prompt(persona_text=cfg.persona_text, addon_text=cfg.addon_text)
        user_prompt = "\n".join(
            [
                f"通知: {source_system}",
                text_in,
            ]
        ).strip()

        resp = self.llm_client.generate_reply_response(
            system_prompt=system_prompt,
            conversation=[{"role": "user", "content": user_prompt}],
            purpose=LlmRequestPurpose.SYNC_NOTIFICATION,
            stream=False,
        )
        message = _first_choice_content(resp).strip()

        # --- events に保存 ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            ev = Event(
                created_at=now_ts,
                updated_at=now_ts,
                client_id=None,
                source="notification",
                user_text=user_prompt,
                assistant_text=message,
                entities_json="[]",
                client_context_json=None,
            )
            db.add(ev)
            db.flush()
            event_id = int(ev.event_id)

        # --- events/stream へ配信 ---
        event_stream.publish(
            type="notification",
            event_id=int(event_id),
            data={
                "system_text": f"[{source_system}] {text_in}",
                "message": message,
            },
            target_client_id=None,
            bufferable=True,
        )

        # --- 非同期: 埋め込み更新 ---
        background_tasks.add_task(
            self._enqueue_event_embedding_job,
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 非同期: 記憶更新（WritePlan） ---
        background_tasks.add_task(
            self._enqueue_write_plan_job,
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

    def handle_meta_request(self, request: schemas.MetaRequestRequest, *, background_tasks: BackgroundTasks) -> None:
        """メタ依頼を受け取り、外部要求は保存せず、能動メッセージの結果だけを保存する。"""

        # --- 設定 ---
        cfg = self.config_store.config
        embedding_preset_id = str(cfg.embedding_preset_id).strip()
        embedding_dimension = int(cfg.embedding_dimension)
        now_ts = _now_utc_ts()

        # --- 入力（永続化しない） ---
        instruction = str(request.instruction or "").strip()
        payload_text = str(request.payload_text or "").strip()

        # --- LLMで能動メッセージを生成（外部要求だとは悟らせない） ---
        system_prompt = _reply_system_prompt(persona_text=cfg.persona_text, addon_text=cfg.addon_text)
        user_prompt = "\n".join(
            [
                "あなたは今、自分から話しかけたい話題を思いついた。",
                "次の材料を踏まえて、自然にユーザーへ話しかける短いメッセージを作る。",
                "",
                "材料:",
                instruction,
                payload_text,
            ]
        ).strip()

        resp = self.llm_client.generate_reply_response(
            system_prompt=system_prompt,
            conversation=[{"role": "user", "content": user_prompt}],
            purpose=LlmRequestPurpose.SYNC_META_REQUEST,
            stream=False,
        )
        message = _first_choice_content(resp).strip()

        # --- events に保存（sourceで区別） ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            ev = Event(
                created_at=now_ts,
                updated_at=now_ts,
                client_id=None,
                source="meta_proactive",
                user_text=None,
                assistant_text=message,
                entities_json="[]",
                client_context_json=None,
            )
            db.add(ev)
            db.flush()
            event_id = int(ev.event_id)

        # --- events/stream へ配信 ---
        event_stream.publish(
            type="meta-request",
            event_id=int(event_id),
            data={"message": message},
            target_client_id=None,
            bufferable=True,
        )

        # --- 非同期: 埋め込み更新 ---
        background_tasks.add_task(
            self._enqueue_event_embedding_job,
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 非同期: 記憶更新（WritePlan） ---
        background_tasks.add_task(
            self._enqueue_write_plan_job,
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

    def handle_vision_capture_response(self, request: schemas.VisionCaptureResponseV2Request) -> None:
        """視覚のcapture-responseを受け取り、待機中の要求へ紐づけ、画像説明を出来事ログへ保存する。"""

        # --- まずは待機中の要求へ紐づける（タイムアウト済みなら何もしない） ---
        ok = vision_bridge.fulfill_capture_response(
            vision_bridge.VisionCaptureResponse(
                request_id=str(request.request_id),
                client_id=str(request.client_id),
                images=list(request.images or []),
                client_context=(request.client_context if request.client_context is not None else None),
                error=(str(request.error).strip() if request.error is not None else None),
            )
        )
        if not ok:
            return

        # --- 画像は保存しない。images があれば詳細説明テキストを生成して events に残す ---
        if not request.images:
            return
        if request.error is not None:
            return

        # --- data URI -> bytes ---
        images_bytes: list[bytes] = []
        for s in list(request.images or []):
            b64 = schemas.data_uri_image_to_base64(s)
            images_bytes.append(base64.b64decode(b64))

        # --- LLMで詳細説明 ---
        descriptions = self.llm_client.generate_image_summary(
            images_bytes,
            purpose=LlmRequestPurpose.SYNC_IMAGE_DETAIL,
        )
        detail_text = "\n\n".join([d.strip() for d in descriptions if str(d or "").strip()]).strip()
        if not detail_text:
            return

        # --- events に保存（画像は保持しない） ---
        cfg = self.config_store.config
        embedding_preset_id = str(cfg.embedding_preset_id).strip()
        embedding_dimension = int(cfg.embedding_dimension)
        now_ts = _now_utc_ts()
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            ev = Event(
                created_at=now_ts,
                updated_at=now_ts,
                client_id=str(request.client_id),
                source="vision_detail",
                user_text=detail_text,
                assistant_text=None,
                entities_json="[]",
                client_context_json=_json_dumps(request.client_context) if request.client_context is not None else None,
            )
            db.add(ev)
            db.flush()
            event_id = int(ev.event_id)

        # NOTE: vision_detail はUIへ通知する必要がないため events/stream へは配信しない。

        # --- 非同期: 埋め込み更新（次ターン以降で参照できる） ---
        self._enqueue_event_embedding_job(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 非同期: 記憶更新（WritePlan） ---
        self._enqueue_write_plan_job(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

    def run_reminder_once(self, *, reminder_id: str, target_client_id: str, hhmm: str, content: str) -> None:
        """リマインダーを1件発火し、イベントストリームへ配信する。"""

        # --- 設定 ---
        cfg = self.config_store.config
        embedding_preset_id = str(cfg.embedding_preset_id).strip()
        embedding_dimension = int(cfg.embedding_dimension)
        now_ts = _now_utc_ts()

        # --- LLMで短い文面を生成 ---
        system_prompt = _reply_system_prompt(persona_text=cfg.persona_text, addon_text=cfg.addon_text)
        user_prompt = "\n".join(
            [
                "リマインダーを発火する。",
                "必須: 50文字以内で、時刻を含めて自然に言う。",
                "",
                f"時刻: {hhmm}",
                f"内容: {content}",
            ]
        ).strip()

        resp = self.llm_client.generate_reply_response(
            system_prompt=system_prompt,
            conversation=[{"role": "user", "content": user_prompt}],
            purpose=LlmRequestPurpose.SYNC_REMINDER,
            stream=False,
        )
        message = _first_choice_content(resp).strip()

        # --- events に保存 ---
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            ev = Event(
                created_at=now_ts,
                updated_at=now_ts,
                client_id=None,
                source="reminder",
                user_text=str(content),
                assistant_text=message,
                entities_json="[]",
                client_context_json=None,
            )
            db.add(ev)
            db.flush()
            event_id = int(ev.event_id)

        # --- events/stream へ配信（バッファしない） ---
        event_stream.publish(
            type="reminder",
            event_id=int(event_id),
            data={
                "reminder_id": str(reminder_id),
                "hhmm": str(hhmm),
                "message": message,
            },
            target_client_id=str(target_client_id),
            bufferable=False,
        )

        # --- 非同期: 埋め込み更新 ---
        self._enqueue_event_embedding_job(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 非同期: 記憶更新（WritePlan） ---
        self._enqueue_write_plan_job(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

    def run_desktop_watch_once(self, *, target_client_id: str) -> str:
        """デスクトップウォッチを1回実行する（結果コードを返す）。"""

        # --- 設定 ---
        cfg = self.config_store.config
        embedding_preset_id = str(cfg.embedding_preset_id).strip()
        embedding_dimension = int(cfg.embedding_dimension)

        # --- 視覚要求（命令） ---
        resp = vision_bridge.request_capture_and_wait(
            target_client_id=str(target_client_id),
            source="desktop",
            purpose="desktop_watch",
            timeout_seconds=5.0,
            timeout_ms=5000,
        )
        if resp is None:
            return "skipped_idle"
        if vision_bridge.is_capture_skipped_idle(resp.error):
            return "skipped_idle"
        if vision_bridge.is_capture_skipped_excluded_window_title(resp.error):
            return "skipped_excluded_window_title"
        if resp.error is not None:
            return "failed"
        if not resp.images:
            return "failed"

        # --- 画像説明（詳細） ---
        images_bytes: list[bytes] = []
        for s in list(resp.images or []):
            b64 = schemas.data_uri_image_to_base64(s)
            images_bytes.append(base64.b64decode(b64))
        descriptions = self.llm_client.generate_image_summary(
            images_bytes,
            purpose=LlmRequestPurpose.SYNC_IMAGE_SUMMARY_DESKTOP_WATCH,
        )
        detail_text = "\n\n".join([d.strip() for d in descriptions if str(d or "").strip()]).strip()

        # --- LLMで人格コメントを生成 ---
        system_prompt = _reply_system_prompt(persona_text=cfg.persona_text, addon_text=cfg.addon_text)
        user_prompt = "\n".join(
            [
                "デスクトップの様子を見て、自然に短いコメントを言う。",
                "",
                "<<INTERNAL_CONTEXT>>",
                "画像の詳細説明（内部用。本文に出力しない）:",
                detail_text,
                "",
                "コメントを一言で。",
            ]
        ).strip()
        resp2 = self.llm_client.generate_reply_response(
            system_prompt=system_prompt,
            conversation=[{"role": "user", "content": user_prompt}],
            purpose=LlmRequestPurpose.SYNC_DESKTOP_WATCH,
            stream=False,
        )
        message = _first_choice_content(resp2).strip()

        # --- events に保存（画像は保持しない） ---
        now_ts = _now_utc_ts()
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            ev = Event(
                created_at=now_ts,
                updated_at=now_ts,
                client_id=str(target_client_id),
                source="desktop_watch",
                user_text=detail_text,
                assistant_text=message,
                entities_json="[]",
                client_context_json=_json_dumps(resp.client_context) if resp.client_context is not None else None,
            )
            db.add(ev)
            db.flush()
            event_id = int(ev.event_id)

        # --- events/stream へ配信（リアルタイム性を優先してバッファしない） ---
        event_stream.publish(
            type="desktop_watch",
            event_id=int(event_id),
            data={
                "system_text": "[desktop_watch]",
                "message": message,
            },
            target_client_id=str(target_client_id),
            bufferable=False,
        )

        # --- 埋め込み更新 ---
        self._enqueue_event_embedding_job(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )

        # --- 非同期: 記憶更新（WritePlan） ---
        self._enqueue_write_plan_job(
            embedding_preset_id=embedding_preset_id,
            embedding_dimension=embedding_dimension,
            event_id=int(event_id),
        )
        return "ok"

    def _enqueue_event_embedding_job(self, *, embedding_preset_id: str, embedding_dimension: int, event_id: int) -> None:
        """出来事ログの埋め込み更新ジョブを積む（次ターンで効かせる）。"""

        # --- memory_enabled が無効なら何もしない ---
        if not bool(self.config_store.memory_enabled):
            return

        now_ts = _now_utc_ts()
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            db.add(
                Job(
                    kind="upsert_event_embedding",
                    payload_json=_json_dumps({"event_id": int(event_id)}),
                    status=int(_JOB_PENDING),
                    run_after=int(now_ts),
                    tries=0,
                    last_error=None,
                    created_at=int(now_ts),
                    updated_at=int(now_ts),
                )
            )

    def _enqueue_write_plan_job(self, *, embedding_preset_id: str, embedding_dimension: int, event_id: int) -> None:
        """記憶更新のための WritePlan 生成ジョブを積む。"""

        # --- memory_enabled が無効なら何もしない ---
        if not bool(self.config_store.memory_enabled):
            return

        now_ts = _now_utc_ts()
        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            db.add(
                Job(
                    kind="generate_write_plan",
                    payload_json=_json_dumps({"event_id": int(event_id)}),
                    status=int(_JOB_PENDING),
                    run_after=int(now_ts),
                    tries=0,
                    last_error=None,
                    created_at=int(now_ts),
                    updated_at=int(now_ts),
                )
            )

    def _collect_candidates(
        self,
        *,
        embedding_preset_id: str,
        embedding_dimension: int,
        event_id: int,
        input_text: str,
        plan_obj: dict[str, Any],
        vector_embedding_future: concurrent.futures.Future[list[Any]] | None = None,
    ) -> list[_CandidateItem]:
        """
        候補収集（取りこぼし防止優先・可能なものは並列）。

        Args:
            embedding_preset_id: 埋め込みプリセットID。
            embedding_dimension: 埋め込み次元。
            event_id: 現在ターンの event_id（除外用）。
            input_text: ユーザー入力。
            plan_obj: SearchPlan（文字n-gram/期間補助/上限制御などに使う）。
            vector_embedding_future:
                先行して開始した「input_text のみ」の埋め込み取得結果。
                - これが渡された場合、vector_all は原則この結果を使う（SearchPlanのqueriesは使わない）。
                - 目的: SearchPlan生成と埋め込み取得を重ねて体感を上げ、vec候補の欠落を減らす。
        """

        # --- 上限 ---
        limits = plan_obj.get("limits") if isinstance(plan_obj, dict) else None
        max_candidates = 200
        if isinstance(limits, dict) and isinstance(limits.get("max_candidates"), (int, float)):
            max_candidates = int(limits.get("max_candidates") or 200)
        max_candidates = max(1, min(400, max_candidates))

        # --- 並列候補収集（タイムアウトで全体が破綻しない） ---
        # NOTE:
        # - セッションはスレッドセーフではないため、各タスクで個別に開く。
        # - 遅い経路（特に embedding）があっても、全体を止めない。
        sources_by_key: dict[tuple[str, int], set[str]] = {}

        # --- 検索語（SearchPlan）を正として、複数クエリで広めに拾う ---
        queries_raw = plan_obj.get("queries") if isinstance(plan_obj, dict) else None
        query_texts: list[str] = [str(input_text or "").strip()]
        if isinstance(queries_raw, list):
            for q in queries_raw:
                qq = str(q or "").strip()
                if not qq:
                    continue
                if qq not in query_texts:
                    query_texts.append(qq)
        query_texts = [q for q in query_texts if q]

        # --- ベクトル検索は「input_text のみ」で先行埋め込みを使えるようにする ---
        # NOTE:
        # - 段階化（追加クエリの追い埋め込み）はしない（シンプル優先）。
        # - SearchPlan.queries は文字n-gram側の補助としては使えるが、vec側は input_text を正にする。
        vector_query_texts: list[str] = [str(input_text or "").strip()]
        vector_query_texts = [q for q in vector_query_texts if q]

        def add_sources(keys: list[tuple[str, int]], label: str) -> None:
            for t, i in keys:
                if not t or int(i) <= 0:
                    continue
                k = (str(t), int(i))
                s = sources_by_key.get(k)
                if s is None:
                    s = set()
                    sources_by_key[k] = s
                s.add(str(label))

        def task_recent_events() -> list[tuple[str, int]]:
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                rows = (
                    db.query(Event.event_id)
                    .filter(Event.event_id != int(event_id))
                    .order_by(Event.created_at.desc(), Event.event_id.desc())
                    .limit(50)
                    .all()
                )
                return [("event", int(r[0])) for r in rows if r and r[0] is not None]

        def task_trigram_events() -> list[tuple[str, int]]:
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                q = _fts_or_query(query_texts)
                rows = db.execute(
                    text(
                        """
                        SELECT rowid AS event_id
                        FROM events_fts
                        WHERE events_fts MATCH :q
                          AND rowid != :event_id
                        ORDER BY rowid DESC
                        LIMIT 80
                        """
                    ),
                    {"q": str(q), "event_id": int(event_id)},
                ).fetchall()
                return [("event", int(r[0])) for r in rows if r and r[0] is not None]

        def task_reply_chain_events() -> list[tuple[str, int]]:
            # --- reply_to の連鎖を辿る（軽量な文脈復元） ---
            out: list[tuple[str, int]] = []
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                cur = int(event_id)
                for _ in range(20):
                    link = (
                        db.query(EventLink)
                        .filter(EventLink.from_event_id == int(cur))
                        .filter(EventLink.label == "reply_to")
                        .order_by(EventLink.id.desc())
                        .first()
                    )
                    if link is None:
                        break
                    prev_id = int(link.to_event_id)
                    if prev_id <= 0:
                        break
                    out.append(("event", int(prev_id)))
                    cur = prev_id
            return out

        def task_context_threads_events() -> list[tuple[str, int]]:
            # --- 文脈スレッド（event_threads）から、同一文脈のイベントを拾う ---
            # NOTE:
            # - 現在ターン（event_id）は同期直後で thread 付与が無いことが多い。
            # - そのため reply_to 連鎖（直近の流れ）を「種」にして thread_key を集める。
            out: list[tuple[str, int]] = []
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                # --- 種（seed）: reply_to 連鎖の先頭側（短め） ---
                seed_event_ids: list[int] = []
                cur = int(event_id)
                for _ in range(8):
                    link = (
                        db.query(EventLink.to_event_id)
                        .filter(EventLink.from_event_id == int(cur))
                        .filter(EventLink.label == "reply_to")
                        .order_by(EventLink.id.desc())
                        .first()
                    )
                    if link is None:
                        break
                    prev_id = int(link[0] or 0)
                    if prev_id <= 0:
                        break
                    seed_event_ids.append(int(prev_id))
                    cur = int(prev_id)

                if not seed_event_ids:
                    return []

                # --- thread_key を集める（重くしない） ---
                rows = (
                    db.query(EventThread.thread_key)
                    .filter(EventThread.event_id.in_([int(x) for x in seed_event_ids]))
                    .order_by(EventThread.id.desc())
                    .limit(16)
                    .all()
                )
                thread_keys: list[str] = []
                seen: set[str] = set()
                for r in rows:
                    tk = str(r[0] or "").strip()
                    if not tk:
                        continue
                    if tk in seen:
                        continue
                    seen.add(tk)
                    thread_keys.append(tk)

                if not thread_keys:
                    return []

                # --- 同一threadのイベントを拾う（最近順） ---
                rows2 = (
                    db.query(EventThread.event_id)
                    .filter(EventThread.thread_key.in_([str(x) for x in thread_keys]))
                    .order_by(EventThread.event_id.desc())
                    .limit(80)
                    .all()
                )
                for r in rows2:
                    if not r or r[0] is None:
                        continue
                    eid = int(r[0] or 0)
                    if eid <= 0 or int(eid) == int(event_id):
                        continue
                    out.append(("event", int(eid)))
            return out

        def task_context_links_events() -> list[tuple[str, int]]:
            # --- 文脈リンク（event_links）から、同一話題/因果/継続を拾う ---
            # NOTE:
            # - reply_to は別経路で辿るので、ここでは reply_to 以外を対象にする。
            out: list[tuple[str, int]] = []
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                # --- 種（seed）: reply_to 連鎖の先頭側（短め） ---
                seed_event_ids: list[int] = []
                cur = int(event_id)
                for _ in range(8):
                    link = (
                        db.query(EventLink.to_event_id)
                        .filter(EventLink.from_event_id == int(cur))
                        .filter(EventLink.label == "reply_to")
                        .order_by(EventLink.id.desc())
                        .first()
                    )
                    if link is None:
                        break
                    prev_id = int(link[0] or 0)
                    if prev_id <= 0:
                        break
                    seed_event_ids.append(int(prev_id))
                    cur = int(prev_id)

                if not seed_event_ids:
                    return []

                seed_set = {int(x) for x in seed_event_ids if int(x) > 0}
                if not seed_set:
                    return []

                # --- 同一話題/因果/継続を拾う ---
                labels = ["same_topic", "caused_by", "continuation"]
                rows = (
                    db.query(EventLink.from_event_id, EventLink.to_event_id)
                    .filter(
                        (EventLink.from_event_id.in_([int(x) for x in seed_set]))
                        | (EventLink.to_event_id.in_([int(x) for x in seed_set]))
                    )
                    .filter(EventLink.label.in_([str(x) for x in labels]))
                    .order_by(EventLink.id.desc())
                    .limit(80)
                    .all()
                )
                for r in rows:
                    if not r:
                        continue
                    a = int(r[0] or 0)
                    b = int(r[1] or 0)
                    if a <= 0 or b <= 0:
                        continue
                    other = b if a in seed_set else a if b in seed_set else 0
                    if other <= 0 or int(other) == int(event_id):
                        continue
                    out.append(("event", int(other)))
            return out

        def task_recent_states() -> list[tuple[str, int]]:
            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                rows = (
                    db.query(State.state_id)
                    .order_by(State.last_confirmed_at.desc(), State.state_id.desc())
                    .limit(40)
                    .all()
                )
                return [("state", int(r[0])) for r in rows if r and r[0] is not None]

        def task_about_time_events() -> list[tuple[str, int]]:
            # --- about_time / life_stage で拾う（全期間横断向け） ---
            mode = str(plan_obj.get("mode") or "").strip()
            time_hint = plan_obj.get("time_hint") if isinstance(plan_obj.get("time_hint"), dict) else {}
            y0 = time_hint.get("about_year_start")
            y1 = time_hint.get("about_year_end")
            life = str(time_hint.get("life_stage_hint") or "").strip()

            # NOTE: 明示ヒントが無いなら空で返す（ノイズを増やさない）
            if not y0 and not y1 and not life:
                return []

            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                q = db.query(Event.event_id)
                if y0 or y1:
                    ys = int(y0) if isinstance(y0, (int, float)) and int(y0) > 0 else None
                    ye = int(y1) if isinstance(y1, (int, float)) and int(y1) > 0 else None
                    if ys is not None and ye is None:
                        ye = ys
                    if ye is not None and ys is None:
                        ys = ye
                    if ys is not None and ye is not None:
                        q = q.filter(Event.about_year_start.isnot(None)).filter(Event.about_year_end.isnot(None))
                        q = q.filter(Event.about_year_start <= int(ye)).filter(Event.about_year_end >= int(ys))
                if life:
                    q = q.filter(Event.life_stage == str(life))
                limit = 120 if mode == "explicit_about_time" else 80
                rows = q.order_by(Event.created_at.desc(), Event.event_id.desc()).limit(int(limit)).all()
                return [("event", int(r[0])) for r in rows if r and r[0] is not None]

        def task_vector_all() -> tuple[list[tuple[str, int]], dict[str, Any]]:
            # --- 類似検索の件数（k）を設定から決める ---
            # NOTE:
            # - embedding_preset.similar_episodes_limit は「類似イベント（episodes）の上限」を表す。
            # - 今回は vec 側は input_text を正とし、総量が暴れないように per-query に割り当てる。
            # - state / event_affect は従来の比率（60:40:20）を踏襲し、events を基準に派生させる。
            cfg = self.config_store.config
            total_event_k = max(1, min(200, int(cfg.similar_episodes_limit)))
            qn = max(1, len(vector_query_texts))

            per_query_event_k = int(math.ceil(float(total_event_k) / float(qn)))
            total_state_k = int(math.ceil(float(total_event_k) * (2.0 / 3.0)))
            total_affect_k = int(math.ceil(float(total_event_k) * (1.0 / 3.0)))
            per_query_state_k = max(1, int(math.ceil(float(total_state_k) / float(qn))))
            per_query_affect_k = max(1, int(math.ceil(float(total_affect_k) / float(qn))))

            # --- embedding を用意する（重いので、可能なら先行開始した結果を使う） ---
            # NOTE:
            # - 先行埋め込みがあれば、SearchPlan生成（同期）と埋め込み取得を重ねて待ちを削る。
            # - 先行埋め込みが無い場合のみ、ここで取得する。
            embeddings: list[Any]
            embedding_source: str = "generated_in_vector_all"
            if vector_embedding_future is not None:
                # --- vec_all タスク内での待ち時間を抑える（SQLite検索の時間を確保） ---
                budget_seconds = 2.0
                started = time.perf_counter()
                remaining = max(0.0, float(budget_seconds) - float(time.perf_counter() - started))
                embeddings = vector_embedding_future.result(timeout=float(remaining))
                embedding_source = "precomputed_input_only"
            else:
                embeddings = self.llm_client.generate_embedding(
                    [str(x) for x in vector_query_texts],
                    purpose=LlmRequestPurpose.SYNC_RETRIEVAL_QUERY_EMBEDDING,
                )

            # --- mode によって最近性フィルタを使う ---
            mode = str(plan_obj.get("mode") or "").strip()
            rank_range = None
            if mode == "associative_recent":
                today_day = int(_now_utc_ts()) // 86400
                rank_range = (int(today_day) - 90, int(today_day) + 1)

            with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
                out: list[tuple[str, int]] = []
                dbg: dict[str, Any] = {
                    "embedding_preset_id": str(embedding_preset_id),
                    "memory_enabled": bool(self.config_store.memory_enabled),
                    "mode": str(mode),
                    "rank_day_range": (list(rank_range) if rank_range is not None else None),
                    "vector_query_texts": list(vector_query_texts),
                    "trigram_query_texts": list(query_texts),
                    "embedding_source": str(embedding_source),
                    "similar_episodes_limit": int(total_event_k),
                    "k_per_query": {
                        "event": int(per_query_event_k),
                        "state": int(per_query_state_k),
                        "event_affect": int(per_query_affect_k),
                    },
                    "vec_items_counts": {},
                    "hits": {"event": [], "state": [], "event_affect": []},
                }

                # --- vec_items の状況（育っていない時の診断用） ---
                # NOTE: vec_items が空だと search_similar_item_ids の結果も空になりやすい。
                try:
                    total = db.execute(text("SELECT COUNT(*) FROM vec_items")).scalar()
                    c_event = db.execute(text("SELECT COUNT(*) FROM vec_items WHERE kind=:k"), {"k": int(_VEC_KIND_EVENT)}).scalar()
                    c_state = db.execute(text("SELECT COUNT(*) FROM vec_items WHERE kind=:k"), {"k": int(_VEC_KIND_STATE)}).scalar()
                    c_aff = db.execute(
                        text("SELECT COUNT(*) FROM vec_items WHERE kind=:k"),
                        {"k": int(_VEC_KIND_EVENT_AFFECT)},
                    ).scalar()
                    dbg["vec_items_counts"] = {
                        "total": int(total or 0),
                        "event": int(c_event or 0),
                        "state": int(c_state or 0),
                        "event_affect": int(c_aff or 0),
                    }
                except Exception:  # noqa: BLE001
                    dbg["vec_items_counts"] = {"error": "count failed"}

                # --- 各クエリ埋め込みで拾ったヒットを統合する ---
                for q_text, q_emb in zip(vector_query_texts, embeddings, strict=False):
                    q_label = str(q_text)[:60]

                    rows_e = search_similar_item_ids(
                        db,
                        query_embedding=q_emb,
                        k=int(per_query_event_k),
                        kind=int(_VEC_KIND_EVENT),
                        rank_day_range=rank_range,
                        active_only=True,
                    )
                    for r in rows_e:
                        if not r or r[0] is None:
                            continue
                        item_id = int(r[0])
                        distance = float(r[1]) if len(r) > 1 and r[1] is not None else None
                        event_id2 = _vec_entity_id(item_id)
                        if int(event_id2) == int(event_id):
                            continue
                        out.append(("event", int(event_id2)))
                        dbg["hits"]["event"].append(
                            {"event_id": int(event_id2), "distance": distance, "item_id": int(item_id), "q": q_label}
                        )

                    rows_s = search_similar_item_ids(
                        db,
                        query_embedding=q_emb,
                        k=int(per_query_state_k),
                        kind=int(_VEC_KIND_STATE),
                        rank_day_range=None,
                        active_only=True,
                    )
                    for r in rows_s:
                        if not r or r[0] is None:
                            continue
                        item_id = int(r[0])
                        distance = float(r[1]) if len(r) > 1 and r[1] is not None else None
                        state_id2 = _vec_entity_id(item_id)
                        out.append(("state", int(state_id2)))
                        dbg["hits"]["state"].append(
                            {"state_id": int(state_id2), "distance": distance, "item_id": int(item_id), "q": q_label}
                        )

                    rows_a = search_similar_item_ids(
                        db,
                        query_embedding=q_emb,
                        k=int(per_query_affect_k),
                        kind=int(_VEC_KIND_EVENT_AFFECT),
                        rank_day_range=None,
                        active_only=True,
                    )
                    for r in rows_a:
                        if not r or r[0] is None:
                            continue
                        item_id = int(r[0])
                        distance = float(r[1]) if len(r) > 1 and r[1] is not None else None
                        affect_id2 = _vec_entity_id(item_id)
                        out.append(("event_affect", int(affect_id2)))
                        dbg["hits"]["event_affect"].append(
                            {"affect_id": int(affect_id2), "distance": distance, "item_id": int(item_id), "q": q_label}
                        )

                return out, dbg

        # --- 並列実行（遅い経路があっても全体が破綻しない） ---
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futures = {
                "recent_events": ex.submit(task_recent_events),
                "trigram_events": ex.submit(task_trigram_events),
                "reply_chain": ex.submit(task_reply_chain_events),
                "context_threads": ex.submit(task_context_threads_events),
                "context_links": ex.submit(task_context_links_events),
                "recent_states": ex.submit(task_recent_states),
                "about_time": ex.submit(task_about_time_events),
                "vector_all": ex.submit(task_vector_all),
            }

            timeouts = {
                "recent_events": 0.25,
                "trigram_events": 0.6,
                "reply_chain": 0.2,
                "context_threads": 0.35,
                "context_links": 0.35,
                "recent_states": 0.25,
                "about_time": 0.4,
                "vector_all": 2.2,
            }

            vector_debug: dict[str, Any] | None = None
            for label, fut in futures.items():
                try:
                    result = fut.result(timeout=float(timeouts.get(label, 0.5)))
                    if label == "vector_all":
                        keys, vector_debug = result
                    else:
                        keys = result
                    add_sources([(str(t), int(i)) for (t, i) in keys], label=str(label))
                except Exception as exc:  # noqa: BLE001
                    if label == "vector_all":
                        vector_debug = {"error": f"vector_all failed or timed out: {type(exc).__name__}: {exc}"}
                    continue

        # --- レコードをまとめて引く（ORMのDetachedを避けるため、候補のdict化までセッション内で行う） ---
        keys_all = sorted([k for k in sources_by_key.keys() if not (k[0] == "event" and int(k[1]) == int(event_id))])
        if not keys_all:
            return []

        with memory_session_scope(embedding_preset_id, embedding_dimension) as db:
            event_ids = [int(i) for (t, i) in keys_all if t == "event"]
            state_ids = [int(i) for (t, i) in keys_all if t == "state"]
            affect_ids = [int(i) for (t, i) in keys_all if t == "event_affect"]

            events = db.query(Event).filter(Event.event_id.in_(event_ids)).all() if event_ids else []
            states = db.query(State).filter(State.state_id.in_(state_ids)).all() if state_ids else []
            affects = db.query(EventAffect).filter(EventAffect.id.in_(affect_ids)).all() if affect_ids else []

            by_event_id = {int(r.event_id): r for r in events}
            by_state_id = {int(r.state_id): r for r in states}
            by_affect_id = {int(r.id): r for r in affects}

            # --- event_threads（文脈スレッド）をイベントメタへ添える ---
            # NOTE:
            # - thread_key は「なんの流れだっけ？」のヒントとして有効。
            # - 同期の選別入力へ含めることで、選別の安定性を上げる。
            threads_by_event_id: dict[int, list[str]] = {}
            if event_ids:
                rows = (
                    db.query(EventThread.event_id, EventThread.thread_key)
                    .filter(EventThread.event_id.in_([int(x) for x in event_ids]))
                    .order_by(EventThread.id.desc())
                    .all()
                )
                for r in rows:
                    if not r:
                        continue
                    eid = int(r[0] or 0)
                    tk = str(r[1] or "").strip()
                    if eid <= 0 or not tk:
                        continue
                    lst = threads_by_event_id.get(eid)
                    if lst is None:
                        lst = []
                        threads_by_event_id[eid] = lst
                    if tk not in lst:
                        lst.append(tk)

            affect_event_ids = sorted({int(a.event_id) for a in affects if a and a.event_id is not None})
            affect_events = db.query(Event).filter(Event.event_id.in_(affect_event_ids)).all() if affect_event_ids else []
            by_affect_event_id = {int(r.event_id): r for r in affect_events}

            out: list[_CandidateItem] = []
            for t, i in keys_all:
                hit_sources = sorted(list(sources_by_key.get((t, int(i))) or set()))

                if t == "event":
                    r = by_event_id.get(int(i))
                    if r is None:
                        continue
                    out.append(
                        _CandidateItem(
                            type="event",
                            id=int(i),
                            rank_ts=int(r.created_at),
                            meta={
                                "type": "event",
                                "event_id": int(r.event_id),
                                "created_at": format_iso8601_local(int(r.created_at)),
                                "source": str(r.source),
                                "thread_keys": threads_by_event_id.get(int(r.event_id), []),
                                "user_text": str(r.user_text or "")[:800],
                                "assistant_text": str(r.assistant_text or "")[:800],
                                "about_time": {
                                    "about_year_start": r.about_year_start,
                                    "about_year_end": r.about_year_end,
                                    "life_stage": r.life_stage,
                                    "confidence": float(r.about_time_confidence),
                                },
                            },
                            hit_sources=hit_sources,
                        )
                    )
                elif t == "state":
                    s = by_state_id.get(int(i))
                    if s is None:
                        continue
                    # --- long_mood_state は背景として別途注入するため、候補（SearchResultPack）には入れない ---
                    if str(s.kind) == "long_mood_state":
                        continue
                    out.append(
                        _CandidateItem(
                            type="state",
                            id=int(i),
                            rank_ts=int(s.last_confirmed_at),
	                            meta={
	                                "type": "state",
	                                "state_id": int(s.state_id),
	                                "kind": str(s.kind),
	                                "body_text": str(s.body_text)[:900],
	                                "payload_json": str(s.payload_json)[:1200],
	                                "last_confirmed_at": format_iso8601_local(int(s.last_confirmed_at)),
	                                "valid_from_ts": (
	                                    format_iso8601_local(int(s.valid_from_ts)) if s.valid_from_ts is not None else None
	                                ),
	                                "valid_to_ts": (
	                                    format_iso8601_local(int(s.valid_to_ts)) if s.valid_to_ts is not None else None
	                                ),
	                            },
	                            hit_sources=hit_sources,
	                        )
	                    )
                elif t == "event_affect":
                    a = by_affect_id.get(int(i))
                    if a is None:
                        continue
                    ev2 = by_affect_event_id.get(int(a.event_id))
                    event_created_at = int(ev2.created_at) if ev2 is not None else None
                    out.append(
                        _CandidateItem(
                            type="event_affect",
                            id=int(i),
                            rank_ts=int(a.created_at),
                            meta={
                                "type": "event_affect",
                                "affect_id": int(a.id),
                                "event_id": int(a.event_id),
                                "created_at": format_iso8601_local(int(a.created_at)),
                                "event_created_at": (
                                    format_iso8601_local(int(event_created_at)) if event_created_at is not None else None
                                ),
                                "moment_affect_text": str(a.moment_affect_text or "")[:600],
                                "inner_thought_text": (
                                    str(a.inner_thought_text)[:600] if a.inner_thought_text is not None else None
                                ),
                                "vad": {"v": float(a.vad_v), "a": float(a.vad_a), "d": float(a.vad_d)},
                                "confidence": float(a.confidence),
                            },
                            hit_sources=hit_sources,
                        )
                    )

        # --- デバッグ: 埋め込みDB（vec_items）由来の候補を表示する ---
        # NOTE:
        # - ここはLLMへ投げる情報の一部なので、LLM I/O ログ（DEBUG）へ揃えて出す。
        # - vector_all の経路でヒットした候補を中心に表示する（取りこぼしと重複の確認用）。
        vector_loaded_preview = [c.meta for c in out if "vector_all" in (c.hit_sources or [])][:30]
        if vector_debug is not None:
            self._log_retrieval_debug(
                "（（埋め込みDBから候補取得））",
                {
                    "query_text": str(input_text),
                    "trigram_query_texts": list(query_texts),
                    "vector_query_texts": list(vector_query_texts),
                    "mode": str(plan_obj.get("mode") or ""),
                    "vector_debug": vector_debug,
                    "loaded_preview": vector_loaded_preview,
                },
            )

        out = sorted(out, key=lambda x: (len(x.hit_sources), int(x.rank_ts), int(x.id)), reverse=True)
        return out[:max_candidates]

    def _inflate_search_result_pack(self, candidates: list[_CandidateItem], pack: dict[str, Any]) -> dict[str, Any]:
        """選別結果に候補詳細を埋め、返答生成へ渡しやすい形へ整形する。"""

        # --- 入力を正規化 ---
        if not isinstance(pack, dict):
            return {"selected": []}

        selected = pack.get("selected") if isinstance(pack.get("selected"), list) else []

        # --- 候補辞書（type:id -> meta） ---
        by_key: dict[str, dict[str, Any]] = {}
        for c in candidates:
            key = f"{str(c.type)}:{int(c.id)}"
            by_key[key] = dict(c.meta) | {"hit_sources": list(c.hit_sources)}

        out_selected: list[dict[str, Any]] = []
        for s in selected:
            if not isinstance(s, dict):
                continue

            # --- type が無い場合は event_id の有無で推定する（LLMの揺れ吸収） ---
            t = str(s.get("type") or "").strip()
            if not t:
                if int(s.get("event_id") or 0) > 0:
                    t = "event"
                elif int(s.get("state_id") or 0) > 0:
                    t = "state"
                elif int(s.get("affect_id") or 0) > 0:
                    t = "event_affect"
                else:
                    continue

            # --- typeごとにキーを決める ---
            key: str | None = None
            if t == "event":
                eid = int(s.get("event_id") or 0)
                if eid > 0:
                    key = f"event:{eid}"
            elif t == "state":
                sid = int(s.get("state_id") or 0)
                if sid > 0:
                    key = f"state:{sid}"
            elif t == "event_affect":
                aid = int(s.get("affect_id") or 0)
                if aid > 0:
                    key = f"event_affect:{aid}"

            if key is None:
                continue
            item = by_key.get(key)
            if item is None:
                continue

            out_selected.append(
                {
                    "type": str(t),
                    "why": str(s.get("why") or "").strip(),
                    "snippet": str(s.get("snippet") or "").strip(),
                    "item": item,
                }
            )

        return {"selected": out_selected}
