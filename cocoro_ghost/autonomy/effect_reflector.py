"""
自律ループの反省（Reflect）で使う world model 反映。

役割:
- ActionResult.effects を `wm_*` へ反映する。
- loop_runtime 本体へ capability 固有処理を持ち込まない。
"""

from __future__ import annotations

from typing import Any

from cocoro_ghost.autonomy.world_model_store import WorldModelStore


def reflect_effects_into_world_model(
    *,
    store: WorldModelStore,
    observation_id: int,
    effects: list[dict[str, Any]],
    evidence_event_ids: list[int],
    trigger_type: str,
) -> None:
    """effect 群を world model へ反映する。"""

    # --- effect を順に処理 ---
    for effect in list(effects or []):
        if not isinstance(effect, dict):
            continue

        # --- URL候補を収集する ---
        urls: list[str] = []
        url_single = str(effect.get("url") or "").strip()
        if url_single:
            urls.append(url_single)
        for item in list(effect.get("urls") or []):
            url_item = str(item or "").strip()
            if url_item:
                urls.append(url_item)
        urls = sorted(set(urls))

        # --- URLを web_resource entity として保存 ---
        entity_ids_by_url: dict[str, int] = {}
        for url in list(urls or []):
            entity_id = store.upsert_entity(
                entity_key=f"web_resource:{url}",
                entity_type="web_resource",
                name=str(url),
                value_json={"url": str(url)},
                confidence=0.8,
            )
            entity_ids_by_url[str(url)] = int(entity_id)
            store.upsert_link(
                link_type="evidence_for",
                from_type="observation",
                from_id=str(int(observation_id)),
                to_type="entity",
                to_id=str(int(entity_id)),
                confidence=0.8,
                evidence_event_ids=list(evidence_event_ids),
            )

        # --- query + urls を belief として保存 ---
        query = str(effect.get("query") or "").strip()
        if query and urls:
            store.add_belief(
                subject_entity_id=None,
                predicate="web.search.query",
                object_text=str(query),
                value_json={
                    "urls": list(urls),
                    "trigger_type": str(trigger_type),
                },
                confidence=0.7,
                source_type="action_result",
                evidence_event_ids=list(evidence_event_ids),
            )

        # --- text_digest を URL entity に紐付ける ---
        text_digest = str(effect.get("text_digest") or "").strip()
        if text_digest and url_single:
            store.add_belief(
                subject_entity_id=entity_ids_by_url.get(str(url_single)),
                predicate="web.page.digest",
                object_text=str(text_digest),
                value_json={"url": str(url_single)},
                confidence=0.6,
                source_type="action_result",
                evidence_event_ids=list(evidence_event_ids),
            )

        # --- structured keys を belief に保存 ---
        keys = [str(k).strip() for k in list(effect.get("keys") or []) if str(k).strip()]
        source_url = str(effect.get("source_url") or "").strip()
        if keys and source_url:
            store.add_belief(
                subject_entity_id=entity_ids_by_url.get(str(source_url)),
                predicate="web.structured.keys",
                object_text=",".join(sorted(set(keys))),
                value_json={"source_url": str(source_url)},
                confidence=0.65,
                source_type="action_result",
                evidence_event_ids=list(evidence_event_ids),
            )

