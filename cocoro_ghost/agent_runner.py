"""
汎用エージェント委譲 runner（別プロセス常駐）。

役割:
    - CocoroGhost の /api/control/agent-jobs/* をポーリングして job を claim する。
    - backend ごとの実行プロセス（subprocess）へ自由文 task_instruction を渡す。
    - 完了/失敗を callback で CocoroGhost へ返す。

注意:
    - 権限境界やフォールバック制御は持たない（設計方針）。
    - backend 実行は「固定コマンド + task_instruction を末尾引数で渡す」方式。
    - backend の標準出力はプレーンテキストとして扱う。
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
    parser.add_argument("--token", required=True, help="Bearer token for /api/control/* and /api/settings")
    parser.add_argument("--insecure", action="store_true", help="disable TLS certificate verification")

    # --- runner 識別と claim 条件 ---
    parser.add_argument("--runner-id", required=True, help="stable runner id")
    parser.add_argument(
        "--backend",
        action="append",
        default=[],
        help="backend name to claim (repeatable). example: --backend gmini",
    )

    # --- ポーリング/タイムアウト設定 ---
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0, help="idle poll interval seconds")
    parser.add_argument("--http-timeout-seconds", type=float, default=30.0, help="HTTP timeout seconds")
    parser.add_argument("--heartbeat-interval-seconds", type=float, default=15.0, help="heartbeat interval seconds")
    parser.add_argument("--task-timeout-seconds", type=float, default=300.0, help="backend subprocess timeout seconds")

    # --- ログ設定 ---
    parser.add_argument("--log-level", default="INFO", help="logging level (DEBUG/INFO/WARNING/ERROR)")
    return parser

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


def _parse_command_argv(*, backend: str, command_text: str) -> list[str]:
    """DB設定のコマンド文字列を argv に変換する。"""

    # --- コマンド文字列を検証して分割する ---
    command_norm = str(command_text or "").strip()
    if not command_norm:
        raise RuntimeError(f"backend command is empty for backend={str(backend)}")
    argv = shlex.split(command_norm)
    if not argv:
        raise RuntimeError(f"backend command parse failed for backend={str(backend)}")
    return [str(x) for x in list(argv)]


def _fetch_backend_command_argv_from_settings(
    *,
    client: httpx.Client,
    base_url: str,
    token: str,
    backend: str,
) -> list[str]:
    """Ghost の /api/settings から backend 実行コマンドを取得して argv に変換する。"""

    # --- 現在は backend=gmini のみ設定DBから取得する ---
    backend_norm = str(backend or "").strip()
    if backend_norm != "gmini":
        raise RuntimeError(f"unsupported backend: {backend_norm}")

    # --- 全設定を取得し、gmini 実行コマンドを読む ---
    resp = client.get(
        f"{str(base_url).rstrip('/')}/api/settings",
        headers=_auth_headers(token),
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("settings response is not an object")

    command_text = str(data.get("agent_backend_gmini_command") or "").strip()
    return _parse_command_argv(backend=backend_norm, command_text=command_text)


def _build_backend_success_from_stdout_text(*, stdout_text: str) -> _BackendExecutionSuccess:
    """backend subprocess の標準出力テキストを success 結果へ変換する。"""

    # --- 標準出力テキストを成功結果として扱う（空はエラー） ---
    summary_text = str(stdout_text or "").strip()
    if not summary_text:
        raise RuntimeError("backend stdout is empty")

    return _BackendExecutionSuccess(
        result_status="success",
        summary_text=str(summary_text),
        details_json={},
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

    # --- 実行argvを構築（task_instruction は末尾引数として渡す） ---
    argv = [str(x) for x in list(command_argv or [])]
    argv.append(str(job.task_instruction))

    # --- subprocess を起動 ---
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as exc:  # noqa: BLE001
        return _BackendExecutionFailure(error_code="backend_spawn_failed", error_message=str(exc))

    # --- 実行中は heartbeat 間隔で待つ ---
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

    # --- stdout テキストを成功結果へ変換 ---
    try:
        return _build_backend_success_from_stdout_text(stdout_text=str(stdout_text or ""))
    except Exception as exc:  # noqa: BLE001
        return _BackendExecutionFailure(error_code="backend_invalid_output", error_message=str(exc))


def _execute_backend(
    *,
    client: httpx.Client,
    base_url: str,
    token: str,
    job: _ClaimedAgentJob,
    heartbeat_interval_seconds: float,
    task_timeout_seconds: float,
    on_heartbeat: callable,
) -> _BackendExecutionSuccess | _BackendExecutionFailure:
    """backend を選択して実行する。"""

    # --- 組み込み mock backend（疎通確認用） ---
    if str(job.backend) == "mock":
        return _execute_mock_backend(job)

    # --- 設定DBから backend 実行コマンドを解決 ---
    try:
        command_argv = _fetch_backend_command_argv_from_settings(
            client=client,
            base_url=base_url,
            token=token,
            backend=str(job.backend),
        )
    except Exception as exc:  # noqa: BLE001
        return _BackendExecutionFailure(error_code="backend_not_configured", error_message=str(exc))

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
        client=client,
        base_url=base_url,
        token=token,
        job=job,
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
        poll_interval_seconds=float(args.poll_interval_seconds),
        http_timeout_seconds=float(args.http_timeout_seconds),
        heartbeat_interval_seconds=float(args.heartbeat_interval_seconds),
        task_timeout_seconds=float(args.task_timeout_seconds),
        verify_tls=(not bool(args.insecure)),
    )


if __name__ == "__main__":
    main()
