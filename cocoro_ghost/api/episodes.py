"""/episodes エンドポイント。"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from cocoro_ghost import models, schemas
from cocoro_ghost.deps import get_memory_db_dep


router = APIRouter()


@router.get("/episodes", response_model=List[schemas.EpisodeSummary])
async def get_episodes(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_memory_db_dep),
):
    """エピソード一覧を取得（記憶DBから）。"""
    episodes = (
        db.query(models.Episode)
        .order_by(models.Episode.occurred_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        schemas.EpisodeSummary(
            id=e.id,
            occurred_at=e.occurred_at,
            source=e.source,
            user_text=e.user_text,
            reply_text=e.reply_text,
            emotion_label=e.emotion_label,
            salience_score=e.salience_score or 0.0,
        )
        for e in episodes
    ]
