"""
汎用エージェント委譲 runner（別プロセス常駐）。

役割:
    - CocoroGhost の /api/control/agent-jobs/* をポーリングして job を claim する。
    - backend ごとの実行プロセス（subprocess）へ自由文 task_instruction を渡す。
    - 完了/失敗を callback で CocoroGhost へ返す。

注意:
    - 権限境界やフォールバック制御は持たない（設計方針）。
    - backend 実行結果は JSON 契約で返す（厳格）。
"""

from __future__ import annotations

import argparse
import json
import logging
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import httpx


logger = logging.getLogger(__name__)


_COMPLETE_RESULT_STATUSES = {"success", "partial", "no_effect"}


@dataclass(frozen=True)
class _ClaimedAgentJob:
    """claim 済み agent_job の runner 内表現。"""

    job_id: str
    claim_token: str
    backend: str
    task_instruction: str
    intent_id: str
    decision_id: str
    created_at: int


@dataclass(frozen=True)
class _BackendExecutionSuccess:
    """backend 実行成功（complete callback 用）。"""

    result_status: str
    summary_text: str
    details_json: dict[str, Any]


@dataclass(frozen=True)
class _BackendExecutionFailure:
    """backend 実行失敗（fail callback 用）。"""

    error_code: str
    error_message: str


def _build_parser() -> argparse.ArgumentParser:
    """CLI 引数パーサを作る。"""

    # --- argparse の基本設定 ---
    parser = argparse.ArgumentParser(description="CocoroGhost agent runner (generic backend subprocess runner)")

    # --- CocoroGhost 接続設定 ---
    parser.add_argument("--base-url", required=True, help="CocoroGhost base URL (e.g. https://127.0.0.1:55601)")
    parser.add_argument("--token", required=True, help="Bearer token for /api/control/*")
    parser.add_argument("--insecure", action="store_true", help="disable TLS certificate verification")

    # --- runner 識別と claim 条件 ---
    parser.add_argument("--runner-id", required=True, help="stable runner id")
    parser.add_argument(
        "--backend",
        action="append",
        default=[],
        help="backend name to claim (repeatable). example: --backend codex",
    )

    # --- backend 実行コマンド定義（name=command string） ---
    parser.add_argument(
        "--backend-command",
        action="append",
        default=[],
        help='backend command mapping in "name=command ..." format (repeatable)',
    )

    # --- ポーリング/タイムアウト設定 ---
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0, help="idle poll interval seconds")
    parser.add_argument("--http-timeout-seconds", type=float, default=30.0, help="HTTP timeout seconds")
    parser.add_argument("--heartbeat-interval-seconds", type=float, default=15.0, help="heartbeat interval seconds")
    parser.add_argument("--task-timeout-seconds", type=float, default=300.0, help="backend subprocess timeout seconds")

    # --- ログ設定 ---
    parser.add_argument("--log-level", default="INFO", help="logging level (DEBUG/INFO/WARNING/ERROR)")
    return parser


def _parse_backend_commands(items: list[str]) -> dict[str, list[str]]:
    """--backend-command の繰り返し引数を dict[name] = argv に変換する。"""

    # --- マッピングを構築 ---
    out: dict[str, list[str]] = {}
    for raw in list(items or []):
        text = str(raw or "").strip()
        if not text:
            continue
        if "=" not in text:
            raise ValueError(f"backend-command must be name=command format: {text}")
        name, command_text = text.split("=", 1)
        backend_name = str(name or "").strip()
        command_norm = str(command_text or "").strip()
        if not backend_name:
            raise ValueError(f"backend-command backend name is empty: {text}")
        if not command_norm:
            raise ValueError(f"backend-command command is empty for backend={backend_name}")
        argv = shlex.split(command_norm)
        if not argv:
            raise ValueError(f"backend-command command parse failed for backend={backend_name}")
        out[str(backend_name)] = list(argv)
    return out


def _auth_headers(token: str) -> dict[str, str]:
    """Bearer 認証ヘッダを返す。"""

    # --- Bearer ヘッダを構築 ---
    return {
        "Authorization": f"Bearer {str(token)}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _claim_jobs(
    *,
    client: httpx.Client,
    base_url: str,
    token: str,
    runner_id: str,
    backends: list[str],
) -> list[_ClaimedAgentJob]:
    """CocoroGhost から agent_job を claim する。"""

    # --- リクエスト payload を構築 ---
    payload = {
        "runner_id": str(runner_id),
        "backends": [str(x) for x in list(backends or [])],
        "limit": 1,
    }

    # --- claim API を呼ぶ ---
    resp = client.post(
        f"{str(base_url).rstrip('/')}/api/control/agent-jobs/claim",
        headers=_auth_headers(token),
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    resp.raise_for_status()
    data = resp.json()

    # --- レスポンスを正規化 ---
    items_raw = data.get("items") if isinstance(data, dict) else []
    if not isinstance(items_raw, list):
        return []
    out: list[_ClaimedAgentJob] = []
    for item in items_raw:
        if not isinstance(item, dict):
            continue
        out.append(
            _ClaimedAgentJob(
                job_id=str(item.get("job_id") or ""),
                claim_token=str(item.get("claim_token") or ""),
                backend=str(item.get("backend") or ""),
                task_instruction=str(item.get("task_instruction") or ""),
                intent_id=str(item.get("intent_id") or ""),
                decision_id=str(item.get("decision_id") or ""),
                created_at=int(item.get("created_at") or 0),
            )
        )
    return out


def _send_heartbeat(
    *,
    client: httpx.Client,
    base_url: str,
    token: str,
    job: _ClaimedAgentJob,
    runner_id: str,
    progress_text: str | None,
) -> None:
    """agent_job heartbeat を送る。"""

    # --- heartbeat payload を構築 ---
    payload = {
        "runner_id": str(runner_id),
        "claim_token": str(job.claim_token),
        "progress_text": (str(progress_text) if progress_text is not None else None),
    }

    # --- heartbeat API を呼ぶ ---
    resp = client.post(
        f"{str(base_url).rstrip('/')}/api/control/agent-jobs/{job.job_id}/heartbeat",
        headers=_auth_headers(token),
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    resp.raise_for_status()


def _send_complete(
    *,
    client: httpx.Client,
    base_url: str,
    token: str,
    job: _ClaimedAgentJob,
    runner_id: str,
    result: _BackendExecutionSuccess,
) -> None:
    """agent_job complete callback を送る。"""

    # --- complete payload を構築 ---
    payload = {
        "runner_id": str(runner_id),
        "claim_token": str(job.claim_token),
        "result_status": str(result.result_status),
        "summary_text": str(result.summary_text),
        "details_json": dict(result.details_json or {}),
    }

    # --- complete API を呼ぶ ---
    resp = client.post(
        f"{str(base_url).rstrip('/')}/api/control/agent-jobs/{job.job_id}/complete",
        headers=_auth_headers(token),
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    resp.raise_for_status()


def _send_fail(
    *,
    client: httpx.Client,
    base_url: str,
    token: str,
    job: _ClaimedAgentJob,
    runner_id: str,
    result: _BackendExecutionFailure,
) -> None:
    """agent_job fail callback を送る。"""

    # --- fail payload を構築 ---
    payload = {
        "runner_id": str(runner_id),
        "claim_token": str(job.claim_token),
        "error_code": str(result.error_code),
        "error_message": str(result.error_message),
    }

    # --- fail API を呼ぶ ---
    resp = client.post(
        f"{str(base_url).rstrip('/')}/api/control/agent-jobs/{job.job_id}/fail",
        headers=_auth_headers(token),
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    resp.raise_for_status()


def _execute_mock_backend(job: _ClaimedAgentJob) -> _BackendExecutionSuccess:
    """組み込み mock backend（疎通確認用）。"""

    # --- summary を作る（自由文の先頭だけ返す） ---
    instruction = str(job.task_instruction or "").strip()
    summary = f"mock backend completed: {instruction[:120]}"
    return _BackendExecutionSuccess(
        result_status="no_effect",
        summary_text=str(summary),
        details_json={
            "backend": "mock",
            "task_instruction": str(instruction),
            "note": "built-in mock backend result",
        },
    )


def _parse_backend_stdout_json(*, stdout_text: str) -> _BackendExecutionSuccess:
    """backend subprocess の stdout JSON を厳格に検証して成功結果へ変換する。"""

    # --- stdout を JSON として読む ---
    try:
        obj = json.loads(str(stdout_text or ""))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("backend stdout is not valid JSON") from exc
    if not isinstance(obj, dict):
        raise RuntimeError("backend stdout JSON must be an object")

    # --- 成功系の必須項目を検証 ---
    result_status = str(obj.get("result_status") or "").strip()
    if result_status not in _COMPLETE_RESULT_STATUSES:
        raise RuntimeError("backend stdout JSON.result_status must be success/partial/no_effect")
    summary_text = str(obj.get("summary_text") or "").strip()
    if not summary_text:
        raise RuntimeError("backend stdout JSON.summary_text is required")
    details_json = obj.get("details_json")
    if details_json is None:
        details_json_obj: dict[str, Any] = {}
    elif isinstance(details_json, dict):
        details_json_obj = dict(details_json)
    else:
        raise RuntimeError("backend stdout JSON.details_json must be an object")

    return _BackendExecutionSuccess(
        result_status=str(result_status),
        summary_text=str(summary_text),
        details_json=details_json_obj,
    )


def _execute_backend_subprocess(
    *,
    job: _ClaimedAgentJob,
    command_argv: list[str],
    heartbeat_interval_seconds: float,
    task_timeout_seconds: float,
    on_heartbeat: callable,
) -> _BackendExecutionSuccess | _BackendExecutionFailure:
    """backend subprocess を実行し、完了または失敗を返す。"""

    # --- backend 入力 JSON を作る（自由文指示はそのまま渡す） ---
    input_obj = {
        "job_id": str(job.job_id),
        "backend": str(job.backend),
        "task_instruction": str(job.task_instruction),
        "intent_id": str(job.intent_id),
        "decision_id": str(job.decision_id),
        "created_at": int(job.created_at),
    }
    input_json = json.dumps(input_obj, ensure_ascii=False)

    # --- subprocess を起動 ---
    try:
        proc = subprocess.Popen(
            list(command_argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as exc:  # noqa: BLE001
        return _BackendExecutionFailure(error_code="backend_spawn_failed", error_message=str(exc))

    # --- 入力JSONを1回だけ stdin に流し込む ---
    try:
        if proc.stdin is not None:
            proc.stdin.write(str(input_json))
            proc.stdin.close()
    except Exception as exc:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return _BackendExecutionFailure(error_code="backend_stdin_failed", error_message=str(exc))

    # --- 入力を渡しつつ、heartbeat 間隔で待つ ---
    started_monotonic = float(time.monotonic())
    stdout_text = ""
    stderr_text = ""
    try:
        while True:
            elapsed = float(time.monotonic()) - float(started_monotonic)
            if elapsed > float(task_timeout_seconds):
                proc.kill()
                try:
                    out_kill, err_kill = proc.communicate(timeout=5.0)
                except Exception:  # noqa: BLE001
                    out_kill, err_kill = "", ""
                stdout_text = str(out_kill or "")
                stderr_text = str(err_kill or "")
                return _BackendExecutionFailure(
                    error_code="backend_timeout",
                    error_message=f"backend subprocess timed out after {int(task_timeout_seconds)}s",
                )

            # --- 定期 heartbeat を送る ---
            on_heartbeat()

            # --- 短い timeout で終了待ち（長時間処理をブロックしない） ---
            try:
                stdout_text, stderr_text = proc.communicate(timeout=max(1.0, float(heartbeat_interval_seconds)))
                break
            except subprocess.TimeoutExpired:
                continue
    except Exception as exc:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return _BackendExecutionFailure(error_code="backend_runner_exception", error_message=str(exc))

    # --- 終了コードを検証 ---
    if int(proc.returncode or 0) != 0:
        err_text = str(stderr_text or "").strip() or str(stdout_text or "").strip() or "backend subprocess failed"
        if len(err_text) > 1000:
            err_text = err_text[:1000]
        return _BackendExecutionFailure(error_code="backend_nonzero_exit", error_message=str(err_text))

    # --- stdout JSON を厳格検証 ---
    try:
        return _parse_backend_stdout_json(stdout_text=str(stdout_text or ""))
    except Exception as exc:  # noqa: BLE001
        return _BackendExecutionFailure(error_code="backend_invalid_output", error_message=str(exc))


def _execute_backend(
    *,
    job: _ClaimedAgentJob,
    backend_commands: dict[str, list[str]],
    heartbeat_interval_seconds: float,
    task_timeout_seconds: float,
    on_heartbeat: callable,
) -> _BackendExecutionSuccess | _BackendExecutionFailure:
    """backend を選択して実行する。"""

    # --- 組み込み mock backend（疎通確認用） ---
    if str(job.backend) == "mock":
        return _execute_mock_backend(job)

    # --- 外部コマンド backend を解決 ---
    command_argv = backend_commands.get(str(job.backend))
    if not command_argv:
        return _BackendExecutionFailure(
            error_code="unsupported_backend",
            error_message=f"unsupported backend: {str(job.backend)}",
        )

    # --- subprocess backend を実行 ---
    return _execute_backend_subprocess(
        job=job,
        command_argv=list(command_argv),
        heartbeat_interval_seconds=float(heartbeat_interval_seconds),
        task_timeout_seconds=float(task_timeout_seconds),
        on_heartbeat=on_heartbeat,
    )


def _run_one_job(
    *,
    client: httpx.Client,
    base_url: str,
    token: str,
    runner_id: str,
    job: _ClaimedAgentJob,
    backend_commands: dict[str, list[str]],
    heartbeat_interval_seconds: float,
    task_timeout_seconds: float,
) -> None:
    """claim 済み agent_job を1件処理する。"""

    # --- 進捗 heartbeat 関数を作る ---
    last_hb_at = {"t": 0.0}

    def _heartbeat() -> None:
        # --- heartbeat 間隔を守って送る ---
        now_mono = float(time.monotonic())
        if (now_mono - float(last_hb_at["t"])) < max(0.5, float(heartbeat_interval_seconds) * 0.9):
            return
        _send_heartbeat(
            client=client,
            base_url=base_url,
            token=token,
            job=job,
            runner_id=runner_id,
            progress_text=None,
        )
        last_hb_at["t"] = float(now_mono)

    # --- 初回 heartbeat を送って running 化する ---
    _send_heartbeat(
        client=client,
        base_url=base_url,
        token=token,
        job=job,
        runner_id=runner_id,
        progress_text="agent job started",
    )
    last_hb_at["t"] = float(time.monotonic())

    # --- backend 実行を行う ---
    result = _execute_backend(
        job=job,
        backend_commands=backend_commands,
        heartbeat_interval_seconds=float(heartbeat_interval_seconds),
        task_timeout_seconds=float(task_timeout_seconds),
        on_heartbeat=_heartbeat,
    )

    # --- callback を送る ---
    if isinstance(result, _BackendExecutionSuccess):
        _send_complete(
            client=client,
            base_url=base_url,
            token=token,
            job=job,
            runner_id=runner_id,
            result=result,
        )
        logger.info(
            "agent job completed job_id=%s backend=%s result_status=%s",
            str(job.job_id),
            str(job.backend),
            str(result.result_status),
        )
        return

    _send_fail(
        client=client,
        base_url=base_url,
        token=token,
        job=job,
        runner_id=runner_id,
        result=result,
    )
    logger.warning(
        "agent job failed job_id=%s backend=%s error_code=%s error=%s",
        str(job.job_id),
        str(job.backend),
        str(result.error_code),
        str(result.error_message),
    )


def run_forever(
    *,
    base_url: str,
    token: str,
    runner_id: str,
    backends: list[str],
    backend_commands: dict[str, list[str]],
    poll_interval_seconds: float,
    http_timeout_seconds: float,
    heartbeat_interval_seconds: float,
    task_timeout_seconds: float,
    verify_tls: bool,
) -> None:
    """agent_runner のメインループ。"""

    # --- HTTP client を1つだけ持つ ---
    with httpx.Client(timeout=float(http_timeout_seconds), verify=bool(verify_tls)) as client:
        while True:
            try:
                # --- claim を試みる ---
                claimed = _claim_jobs(
                    client=client,
                    base_url=base_url,
                    token=token,
                    runner_id=runner_id,
                    backends=backends,
                )
                if not claimed:
                    time.sleep(max(0.1, float(poll_interval_seconds)))
                    continue

                # --- 1件ずつ実行（まずは単純直列） ---
                for job in claimed:
                    logger.info(
                        "agent job claimed job_id=%s backend=%s intent_id=%s",
                        str(job.job_id),
                        str(job.backend),
                        str(job.intent_id),
                    )
                    _run_one_job(
                        client=client,
                        base_url=base_url,
                        token=token,
                        runner_id=runner_id,
                        job=job,
                        backend_commands=backend_commands,
                        heartbeat_interval_seconds=float(heartbeat_interval_seconds),
                        task_timeout_seconds=float(task_timeout_seconds),
                    )
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("agent_runner loop error: %s", str(exc))
                time.sleep(max(0.5, float(poll_interval_seconds)))


def main() -> None:
    """CLI エントリポイント。"""

    # --- 引数を読む ---
    parser = _build_parser()
    args = parser.parse_args()

    # --- ログを初期化 ---
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    # --- backend 一覧を正規化 ---
    backends = [str(x or "").strip() for x in list(args.backend or []) if str(x or "").strip()]
    if not backends:
        parser.error("--backend は最低1つ必要です")

    # --- backend コマンドマップを読む ---
    try:
        backend_commands = _parse_backend_commands(list(args.backend_command or []))
    except ValueError as exc:
        parser.error(str(exc))
        return

    # --- 起動ログ ---
    logger.info(
        "agent_runner start runner_id=%s backends=%s base_url=%s verify_tls=%s",
        str(args.runner_id),
        ",".join(backends),
        str(args.base_url),
        str(not bool(args.insecure)),
    )

    # --- メインループを実行 ---
    run_forever(
        base_url=str(args.base_url),
        token=str(args.token),
        runner_id=str(args.runner_id),
        backends=backends,
        backend_commands=backend_commands,
        poll_interval_seconds=float(args.poll_interval_seconds),
        http_timeout_seconds=float(args.http_timeout_seconds),
        heartbeat_interval_seconds=float(args.heartbeat_interval_seconds),
        task_timeout_seconds=float(args.task_timeout_seconds),
        verify_tls=(not bool(args.insecure)),
    )


if __name__ == "__main__":
    main()
