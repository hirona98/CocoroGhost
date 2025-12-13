"""cocoro_ghost Worker 起動スクリプト"""

import sys
from pathlib import Path

# プロジェクトルートをPYTHONPATHに追加
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))


def main() -> None:
    from cocoro_ghost.config import ConfigStore, build_runtime_config, load_config, set_global_config_store
    from cocoro_ghost.db import (
        ensure_initial_settings,
        init_memory_db,
        init_settings_db,
        load_active_character_preset,
        load_active_llm_preset,
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
        character_preset = load_active_character_preset(session)
        runtime_config = build_runtime_config(toml_config, global_settings, llm_preset, character_preset)

        session.expunge(global_settings)
        session.expunge(llm_preset)
        session.expunge(character_preset)
        config_store = ConfigStore(toml_config, runtime_config, global_settings, llm_preset, character_preset)

    set_global_config_store(config_store)
    init_memory_db(runtime_config.memory_id, runtime_config.embedding_dimension)

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
        memory_id=runtime_config.memory_id,
        embedding_dimension=runtime_config.embedding_dimension,
        llm_client=llm_client,
    )


if __name__ == "__main__":
    main()

