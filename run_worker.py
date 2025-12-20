"""cocoro_ghost Worker 起動スクリプト。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


# プロジェクトルートを PYTHONPATH に追加
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="cocoro_ghost worker runner")
    parser.add_argument("--memory-id", dest="memory_id", default=None, help="override target memory_id (UUID recommended)")
    parser.add_argument(
        "--periodic-interval-seconds",
        dest="periodic_interval_seconds",
        type=float,
        default=30.0,
        help="cron無し定期enqueueの判定間隔（0以下で無効）",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    from cocoro_ghost.config import (
        ConfigStore,
        build_runtime_config,
        load_config,
        set_global_config_store,
    )
    from cocoro_ghost.db import (
        ensure_initial_settings,
        init_memory_db,
        init_settings_db,
        load_active_embedding_preset,
        load_active_contract_preset,
        load_active_llm_preset,
        load_active_persona_preset,
        load_global_settings,
        settings_session_scope,
    )
    from cocoro_ghost.llm_client import LlmClient
    from cocoro_ghost.logging_config import setup_logging
    from cocoro_ghost.worker import run_forever

    toml_config = load_config()
    setup_logging(toml_config.log_level)

    init_settings_db()
    with settings_session_scope() as session:
        ensure_initial_settings(session, toml_config)

    with settings_session_scope() as session:
        global_settings = load_global_settings(session)
        llm_preset = load_active_llm_preset(session)
        embedding_preset = load_active_embedding_preset(session)
        persona_preset = load_active_persona_preset(session)
        contract_preset = load_active_contract_preset(session)

        runtime_config = build_runtime_config(
            toml_config,
            global_settings,
            llm_preset,
            embedding_preset,
            persona_preset,
            contract_preset,
        )

        # セッション外でも参照できるようにデタッチして保持する
        session.expunge(global_settings)
        session.expunge(llm_preset)
        session.expunge(embedding_preset)
        session.expunge(persona_preset)
        session.expunge(contract_preset)
        config_store = ConfigStore(
            toml_config,
            runtime_config,
            global_settings,
            llm_preset,
            embedding_preset,
            persona_preset,
            contract_preset,
        )

    set_global_config_store(config_store)

    memory_id = str(args.memory_id).strip() if args.memory_id is not None else runtime_config.memory_id
    init_memory_db(memory_id, runtime_config.embedding_dimension)

    llm_client = LlmClient(
        model=runtime_config.llm_model,
        embedding_model=runtime_config.embedding_model,
        embedding_api_key=runtime_config.embedding_api_key,
        image_model=runtime_config.image_model,
        api_key=runtime_config.llm_api_key,
        llm_base_url=runtime_config.llm_base_url,
        embedding_base_url=runtime_config.embedding_base_url,
        image_llm_base_url=runtime_config.image_llm_base_url,
        image_model_api_key=runtime_config.image_model_api_key,
        reasoning_effort=runtime_config.reasoning_effort,
        max_tokens=runtime_config.max_tokens,
        max_tokens_vision=runtime_config.max_tokens_vision,
        image_timeout_seconds=runtime_config.image_timeout_seconds,
    )

    run_forever(
        memory_id=memory_id,
        embedding_dimension=runtime_config.embedding_dimension,
        llm_client=llm_client,
        periodic_interval_seconds=float(args.periodic_interval_seconds),
    )


if __name__ == "__main__":
    main()
