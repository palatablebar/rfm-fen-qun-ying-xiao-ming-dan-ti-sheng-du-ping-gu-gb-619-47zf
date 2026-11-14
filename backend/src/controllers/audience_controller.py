"""名单控制器：生成 / 续跑 / 确认都落审计，全部在单事务内完成共享占用。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, audience_service, occupancy_service


def list_audiences(db: Session, **filters) -> dict:
    return audience_service.list_audiences(db, **filters)


def get_audience(db: Session, audience_id: int) -> dict:
    audience = audience_service.get_audience(db, audience_id)
    campaign = audience_service.get_campaign(db, audience.campaign_id)
    payload = audience_service.serialize_audience(audience, campaign)
    # 页面人数与真实占用对账：额外返回台账有效占用数
    payload["locked_members"] = len(occupancy_service.member_ids_for_audience(db, audience.id))
    return payload


def generate(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    campaign = audience_service.get_campaign(db, payload["campaign_id"])
    before = audience_service.list_audiences(db, campaign_id=campaign.id, status="pending", limit=1)["total"]
    audience = audience_service.generate(
        db,
        campaign=campaign,
        budget=payload.get("budget"),
        operator_email=principal["email"],
        note=payload.get("note"),
    )
    resumed = before > 0
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="audience.resume" if resumed else "audience.generate",
        entity="audience",
        entity_id=audience.id,
        detail={
            "audience_no": audience.audience_no,
            "campaign": campaign.code,
            "attempt": audience.attempt,
            "budget": audience.budget,
            "members": audience.member_count,
            "spend": audience.spend,
            "inc_orders": audience.inc_orders,
            "roi": audience.roi,
            "resumed": resumed,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return audience_service.serialize_audience(audience, campaign)


def resume(db: Session, audience_id: int, payload: dict, principal: dict, request: Request) -> dict:
    """确认失败释放后，从已处理扫描位置续跑补人（同一名单，不新增、不重复占用）。"""
    audience = audience_service.get_audience(db, audience_id)
    audience = audience_service.resume_generation(
        db,
        audience,
        operator_email=principal["email"],
        note=(payload or {}).get("note"),
    )
    campaign = audience_service.get_campaign(db, audience.campaign_id)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="audience.resume",
        entity="audience",
        entity_id=audience.id,
        detail={
            "audience_no": audience.audience_no,
            "campaign": campaign.code,
            "attempt": audience.attempt,
            "members": audience.member_count,
            "spend": audience.spend,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return audience_service.serialize_audience(audience, campaign)


def review(db: Session, audience_id: int, payload: dict, principal: dict, request: Request) -> dict:
    audience = audience_service.get_audience(db, audience_id)
    # 确认失败时领域层已用独立事务释放本批占用；此处回滚的只是当前请求尚未提交的改动
    try:
        audience = audience_service.confirm(
            db,
            audience,
            status=payload["status"],
            note=payload.get("note"),
            actor=principal["email"],
        )
    except Exception:
        db.rollback()
        raise
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="audience.confirm",
        entity="audience",
        entity_id=audience.id,
        detail={
            "status": audience.status,
            "audience_no": audience.audience_no,
            "members": audience.member_count,
            "cooldown_committed": audience.lock_phase == "committed",
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    campaign = audience_service.get_campaign(db, audience.campaign_id)
    return audience_service.serialize_audience(audience, campaign)
