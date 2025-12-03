"""/episodes エンドポイント。"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from cocoro_ghost import models, schemas
from cocoro_ghost.db import get_db


router = APIRouter()


@router.get("/episodes", response_model=List[schemas.EpisodeSummary])
async def get_episodes(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    episodes = (
        db.query(models.Episode)
        .order_by(models.Episode.occurred_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return episodes
