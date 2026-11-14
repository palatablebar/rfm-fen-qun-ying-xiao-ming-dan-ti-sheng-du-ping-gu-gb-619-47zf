"""Celery 任务：心跳、每日重算 RFM 并为进行中的活动补一份名单。"""
from __future__ import annotations

import time

from sqlalchemy import select

from src.config.celery_app import TASK_DAILY, TASK_HEARTBEAT, celery_app
from src.config.settings import settings
from src.utils.logger import get_logger

logger = get_logger("tasks")


def _redis():
    import redis

    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


@celery_app.task(name=TASK_HEARTBEAT)
def heartbeat() -> str:
    try:
        _redis().set(f"{settings.app_name}:worker:heartbeat", time.time())
        return "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("heartbeat failed: %s", exc)
        return "error"


@celery_app.task(name=TASK_DAILY)
def daily_refresh() -> dict:
    from src.config.database import session_scope
    from src.models.campaign import Campaign
    from src.services import audit_service, audience_service, segment_service

    with session_scope() as db:
        result = segment_service.recompute(db)
        audit_service.record(
            db,
            actor_email=f"scheduler@{settings.app_name}.io",
            actor_role="admin",
            action="segment.recompute",
            entity="segment",
            entity_id=None,
            detail={"as_of": result["as_of"], "members": result["member_count"], "trigger": "schedule"},
            layer="worker",
        )
        from src.models.audience import Audience

        campaigns = db.scalars(select(Campaign).where(Campaign.status == "active")).all()
        generated = 0
        resumed = 0
        for campaign in campaigns:
            try:
                pending = db.scalars(
                    select(Audience)
                    .where(Audience.campaign_id == campaign.id)
                    .where(Audience.status == "pending")
                    .order_by(Audience.id.desc())
                ).all()
                if pending:
                    # 同一活动只保留一份待确认名单：在原名单上续跑补人，不新增、不重复占用
                    for audience in pending:
                        audience_service.resume_generation(
                            db,
                            audience,
                            operator_email=f"scheduler@{settings.app_name}.io",
                            note="每日定时重算续跑补人",
                        )
                        resumed += 1
                    continue
                audience_service.generate(
                    db,
                    campaign=campaign,
                    budget=campaign.budget,
                    operator_email=f"scheduler@{settings.app_name}.io",
                    note="每日定时重算生成",
                )
                generated += 1
            except Exception as exc:  # noqa: BLE001 - 单个活动失败不影响其它
                logger.warning("audience generate failed for %s: %s", campaign.code, exc)
        logger.info(
            "daily refresh: %s members, %s new audiences, %s resumed",
            result["member_count"],
            generated,
            resumed,
        )
        return {"members": result["member_count"], "audiences": generated, "resumed": resumed}
