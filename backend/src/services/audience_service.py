"""活动与名单：算法 B 编排 + 跨活动共享触达占用。

两条链路：
- 预览（preview_allocation）：只读算法 B 的预算试算，不落库、不占人；
- 生成（generate/resume_generation）：按试算优先级在共享占用台账上抢占真实会员，
  先写 reserved 预留占用；页面人数/预算/导出全部回算自真实占用行。
- 确认（confirm）：一次性把 reserved 切换为 confirmed 正式占用（带冷却期）；
  确认失败则整批释放，重试从名单记录的扫描游标续跑补人，不新增名单、不重复占用。
"""
from __future__ import annotations

import math

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import AUDIENCE_STATUS_LABELS, CAMPAIGN_STATUS_LABELS
from src.constants.limits import (
    BUDGET_EPS,
    DEFAULT_ORDERS_PER_CONVERTER,
    DEFAULT_TAIL_FACTOR,
)
from src.models.audience import Audience
from src.models.campaign import Campaign
from src.models.reach_lock import ReachLock
from src.services import lift_service, occupancy_service, segment_service
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_date, iso_utc, local_now, utcnow


def serialize_campaign(campaign: Campaign) -> dict:
    return {
        "id": campaign.id,
        "code": campaign.code,
        "name": campaign.name,
        "channel": campaign.channel,
        "budget": round(campaign.budget or 0.0, 2),
        "status": campaign.status,
        "status_label": CAMPAIGN_STATUS_LABELS.get(campaign.status, campaign.status),
        "starts_on": iso_date(campaign.starts_on),
        "ends_on": iso_date(campaign.ends_on),
        "reach_cost": round(campaign.reach_cost or 0.0, 4),
        "orders_per_converter": round(campaign.orders_per_converter or 0.0, 4),
        "tail_factor": round(campaign.tail_factor or 0.0, 4),
        "cooldown_days": int(campaign.cooldown_days or 0),
        "target_segments": campaign.target_segments or [],
        "note": campaign.note,
        "operator_email": campaign.operator_email,
        "created_at": iso_utc(campaign.created_at),
    }


def list_campaigns(db: Session) -> dict:
    rows = db.scalars(select(Campaign).order_by(Campaign.id.desc())).all()
    return {"items": [serialize_campaign(row) for row in rows], "total": len(rows)}


def get_campaign(db: Session, campaign_id: int) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise not_found("campaign", campaign_id)
    return campaign


def create_campaign(db: Session, payload: dict, operator_email: str, seed_key: str | None = None) -> Campaign:
    exists = db.scalar(select(Campaign).where(Campaign.code == payload["code"]))
    if exists is not None:
        raise AppError("CONFLICT", 409, {"code": payload["code"]})
    campaign = Campaign(
        code=payload["code"],
        name=payload["name"],
        channel=payload.get("channel") or "小程序订阅消息",
        budget=round(float(payload["budget"]), 2),
        status=payload.get("status") or "draft",
        starts_on=_parse_date(payload.get("starts_on")),
        ends_on=_parse_date(payload.get("ends_on")),
        cooldown_days=int(payload.get("cooldown_days") or 30),
        note=payload.get("note"),
        operator_email=operator_email,
        seed_key=seed_key,
    )
    db.add(campaign)
    db.flush()
    return campaign


def _parse_date(text: str | None):
    if not text:
        return None
    from datetime import date

    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise AppError("VALIDATION", 422, {"reason": "日期格式应为 YYYY-MM-DD", "value": text}) from None


def preview_allocation(db: Session, campaign: Campaign, budget: float | None = None) -> dict:
    """算法 B 预览：读当前分群快照 + 活动参数，算出预算分配（不落库、不占人）。"""
    segments = segment_service.segments_for_allocation(db)
    if not segments:
        raise AppError("VALIDATION", 409, {"reason": "还没有分群快照，请先重算 RFM"})
    meta = segment_service.list_segments(db)
    result = lift_service.allocate(
        segments,
        budget=float(budget if budget is not None else campaign.budget),
        global_rate=meta.get("global_purchase_rate") or None,
        reach_cost=campaign.reach_cost,
        orders_per_converter=campaign.orders_per_converter,
        tail_factor=campaign.tail_factor,
    )
    result["campaign"] = serialize_campaign(campaign)
    result["as_of"] = meta.get("as_of")
    result["segments_basis"] = [
        {
            "code": item["code"],
            "name": item["name"],
            "rule_text": item.get("rule_text"),
            "share": item.get("share"),
            "avg_r_days": item.get("avg_r_days"),
        }
        for item in meta["items"]
    ]
    return result


def _next_audience_no(db: Session) -> str:
    prefix = f"A{local_now():%Y%m%d}"
    count = db.scalar(select(func.count()).select_from(Audience).where(Audience.audience_no.like(f"{prefix}%"))) or 0
    return f"{prefix}{count + 1:02d}"


def _round(value: float, digits: int = 2) -> float:
    return round(float(value) + 0.0, digits)


def _recompute_segment_metrics(
    row: dict,
    *,
    actual_count: int,
    spend: float,
    global_rate: float,
    orders_per_converter: float,
    tail_factor: float,
) -> dict:
    """按真实占用人数重算单群增量/ROI（口径与 lift_service 完全一致）。"""
    member_count = int(row.get("member_count") or 0)
    core_cap = int(math.ceil(member_count * float(row.get("core_share") or 0.6)))
    core_allocated = min(actual_count, core_cap)
    tail_allocated = max(0, actual_count - core_cap)
    effective_people = core_allocated + tail_allocated * tail_factor
    delta = max(0.0, float(row.get("_rate", row.get("purchase_rate") or 0.0)) - global_rate)
    inc_converters = effective_people * delta
    inc_orders = int(round(inc_converters * orders_per_converter))
    inc_gmv = _round(inc_orders * float(row.get("avg_ticket") or 0.0))
    roi = _round(inc_gmv / spend, 2) if spend > 0 else 0.0
    return {
        "allocated": actual_count,
        "budget": _round(spend),
        "inc_orders": inc_orders,
        "inc_gmv": inc_gmv,
        "roi": roi,
        "core_allocated": core_allocated,
        "tail_allocated": tail_allocated,
        "effective_people": _round(effective_people),
        "inc_converters": _round(inc_converters),
    }


def _apply_occupancy(
    db: Session,
    *,
    audience: Audience,
    campaign: Campaign,
    plan: dict,
    existing_locks: list[ReachLock] | None = None,
) -> dict:
    """按试算计划在共享台账上抢占会员，并把真实结果回写到名单分配与汇总字段。

    existing_locks 非空表示续跑：已占用的行保留，只对差额从 scan_cursor 续扫补人。
    """
    occupancy_service.release_expired_reservations(db)
    blocked = occupancy_service.active_blocking_member_ids(db)

    plan_rows = {row["segment_code"]: row for row in plan["segments"]}
    global_rate = float(plan.get("global_rate") or 0.0)
    tail_factor = float(campaign.tail_factor or DEFAULT_TAIL_FACTOR)
    orders_per_converter = float(campaign.orders_per_converter or DEFAULT_ORDERS_PER_CONVERTER)
    budget_cap = float(plan.get("budget") or campaign.budget)

    # 续跑基线：本名单已有的有效占用（按分群聚合）
    cursor: dict[str, int] = dict(audience.scan_cursor or {})
    owned_by_segment: dict[str, list[ReachLock]] = {}
    for lock in existing_locks or []:
        owned_by_segment.setdefault(lock.segment_code, []).append(lock)

    # 预算下调时先全局裁剪已有占用，保证续跑后总花费绝不超过新预算上限
    all_owned = [lock for locks in owned_by_segment.values() for lock in locks]
    owned_total_spend = _round(sum(lock.unit_cost or 0.0 for lock in all_owned))
    if owned_total_spend > budget_cap + BUDGET_EPS and all_owned:
        # 按单人成本从高到低估算可保留人数，逐行回退直到花费落进预算
        ordered_desc = sorted(all_owned, key=lambda item: (-(item.unit_cost or 0.0), -item.id))
        keep_locks: list[ReachLock] = []
        running = 0.0
        for lock in sorted(all_owned, key=lambda item: item.id):  # 先按占用顺序保留低成本前缀
            if running + (lock.unit_cost or 0.0) <= budget_cap + BUDGET_EPS:
                keep_locks.append(lock)
                running = _round(running + (lock.unit_cost or 0.0))
        keep_ids = {lock.id for lock in keep_locks}
        trimmed_segments: set[str] = set()
        for lock in ordered_desc:
            if lock.id not in keep_ids:
                lock.status = "released"
                lock.released_at = utcnow()
                lock.cooldown_until = None
                trimmed_segments.add(lock.segment_code)
        db.flush()
        owned_by_segment = {}
        for lock in keep_locks:
            owned_by_segment.setdefault(lock.segment_code, []).append(lock)
        # 被裁剪分群的游标归零，续跑可重新评估被释放的会员
        for code in trimmed_segments:
            cursor[code] = 0

    # 自己已占用的会员不构成自我阻挡
    for locks in owned_by_segment.values():
        for lock in locks:
            blocked.discard(lock.member_id)

    result_rows: list[dict] = []
    total_count = 0
    total_spend = 0.0
    total_inc_orders = 0
    total_inc_gmv = 0.0
    total_blocked = 0
    priority = 0

    for row in plan["segments"]:
        code = row["segment_code"]
        clean = {key: value for key, value in row.items() if not key.startswith("_")}
        rate = float(plan_rows[code].get("_rate") or row.get("purchase_rate") or 0.0)
        unit_cost = float(plan_rows[code].get("_unit_cost") or (campaign.reach_cost + rate * float(row.get("coupon_face") or 0.0)))

        if row.get("status") != "allocated":
            # lift≤1 / 试算即预算不足的群：不占人，原样保留说明
            clean.update(
                {
                    "allocated": 0,
                    "budget": 0.0,
                    "inc_orders": 0,
                    "inc_gmv": 0.0,
                    "roi": 0.0,
                    "core_allocated": 0,
                    "tail_allocated": 0,
                    "occupied_members": 0,
                    "blocked_members": 0,
                }
            )
            result_rows.append(clean)
            continue

        priority += 1
        target = int(row.get("allocated") or 0)
        owned = owned_by_segment.get(code, [])
        owned_count = len(owned)
        owned_spend = _round(sum(lock.unit_cost or 0.0 for lock in owned))
        need = max(0, target - owned_count)
        budget_remaining = budget_cap - total_spend - owned_spend

        if need > 0:
            outcome = occupancy_service.reserve_members(
                db,
                audience_id=audience.id,
                campaign=campaign,
                segment_code=code,
                target_count=need,
                budget_remaining=max(0.0, budget_remaining),
                unit_cost=unit_cost,
                scan_offset=int(cursor.get(code, 0)),
                blocked_ids=blocked,
            )
            cursor[code] = outcome["cursor"]
            total_blocked += outcome["blocked"]
            new_count = outcome["count"]
            new_spend = outcome["spend"]
        else:
            new_count = 0
            new_spend = 0.0

        actual_count = owned_count + new_count
        segment_spend = _round(owned_spend + new_spend)
        metrics = _recompute_segment_metrics(
            row,
            actual_count=actual_count,
            spend=segment_spend,
            global_rate=global_rate,
            orders_per_converter=orders_per_converter,
            tail_factor=tail_factor,
        )
        shortfall = target - actual_count
        reason = row.get("reason") or ""
        if shortfall > 0:
            reason = (
                f"{reason}；较试算少 {shortfall} 人（被其它活动占用/冷却中或受预算限制），"
                f"本名单实际覆盖 {actual_count} 人"
            ).strip("；")
        clean.update(metrics)
        clean.update(
            {
                "priority": priority,
                "status": "allocated" if actual_count > 0 else "deferred",
                "reason": reason,
                "unit_cost": _round(unit_cost, 4),
                "occupied_members": actual_count,
                "blocked_members": shortfall,
            }
        )
        result_rows.append(clean)
        total_count += actual_count
        total_spend = _round(total_spend + segment_spend)
        total_inc_orders += metrics["inc_orders"]
        total_inc_gmv = _round(total_inc_gmv + metrics["inc_gmv"])

    if total_spend > budget_cap + BUDGET_EPS:
        # 兜底层：理论上不会触发（逐人预算校验），触发即视为占用异常
        raise AppError("CONFLICT", 409, {"reason": "占用超出活动预算，已中止", "spend": total_spend, "budget": budget_cap})

    audience.scan_cursor = cursor
    audience.allocation = result_rows
    audience.member_count = total_count
    audience.spend = total_spend
    audience.budget = _round(budget_cap)
    audience.inc_orders = total_inc_orders
    audience.inc_gmv = total_inc_gmv
    audience.roi = _round(total_inc_gmv / total_spend, 2) if total_spend > 0 else 0.0
    audience.global_rate = _round(global_rate, 6)
    db.flush()
    return {
        "member_count": total_count,
        "spend": total_spend,
        "blocked": total_blocked,
        "segments": result_rows,
    }


def generate(
    db: Session,
    *,
    campaign: Campaign,
    budget: float | None,
    operator_email: str,
    note: str | None = None,
    seed_key: str | None = None,
) -> Audience:
    if seed_key:
        existing = db.scalar(select(Audience).where(Audience.seed_key == seed_key))
        if existing is not None:
            return existing

    # 同一活动重试不新增名单：已有待确认（含已释放待续跑）名单则在原名单上续跑
    reusable = db.scalar(
        select(Audience)
        .where(Audience.campaign_id == campaign.id)
        .where(Audience.status == "pending")
        .order_by(Audience.id.desc())
        .limit(1)
    )
    if reusable is not None:
        return resume_generation(
            db, reusable, operator_email=operator_email, note=note, budget=budget
        )

    plan = preview_allocation(db, campaign, budget)
    if not plan["totals"]["audience"]:
        raise AppError("VALIDATION", 409, {"reason": "当前预算下没有可分群可投，请提高预算或调整券面额"})

    audience = Audience(
        audience_no=_next_audience_no(db),
        campaign_id=campaign.id,
        status="pending",
        lock_phase="reserved",
        scan_cursor={},
        attempt=1,
        budget=plan["budget"],
        member_count=0,
        spend=0.0,
        global_rate=plan["global_rate"],
        allocation=plan["segments"],
        note=note or plan["log"],
        operator_email=operator_email,
        seed_key=seed_key,
    )
    db.add(audience)
    db.flush()
    _apply_occupancy(db, audience=audience, campaign=campaign, plan=plan, existing_locks=[])
    audience.note = note or _build_note(audience, plan["log"], resumed=False)
    db.flush()
    return audience


def resume_generation(
    db: Session,
    audience: Audience,
    *,
    operator_email: str,
    note: str | None = None,
    budget: float | None = None,
) -> Audience:
    """确认失败释放后从已处理位置续跑补人：同一名单号、不新增、不重复占用。

    budget 显式传入时按新预算重算试算并更新名单预算（重试允许调整预算）。
    """
    if audience.status != "pending":
        raise AppError("CONFLICT", 409, {"reason": "只有待确认名单可以续跑补人", "audience_no": audience.audience_no})
    campaign = get_campaign(db, audience.campaign_id)
    effective_budget = float(budget) if budget is not None else float(audience.budget)
    plan = preview_allocation(db, campaign, effective_budget)
    if not plan["totals"]["audience"]:
        raise AppError("VALIDATION", 409, {"reason": "当前预算下没有可分群可投，请提高预算或调整券面额"})
    audience.budget = plan["budget"]
    existing = occupancy_service.locks_for_audience(db, audience.id, active_only=True)
    # 上一批已被整批释放（确认失败）：清空扫描游标，从已处理位置重新抢占，避免漏人
    if not existing:
        cursor_reset = {key: 0 for key in (audience.scan_cursor or {})}
        audience.scan_cursor = cursor_reset
    audience.attempt = int(audience.attempt or 0) + 1
    audience.operator_email = operator_email
    _apply_occupancy(
        db,
        audience=audience,
        campaign=campaign,
        plan=plan,
        existing_locks=existing,
    )
    if note:
        audience.note = note
    else:
        audience.note = _build_note(audience, plan["log"], resumed=True)
    db.flush()
    return audience


def _build_note(audience: Audience, plan_log: str, *, resumed: bool) -> str:
    prefix = f"第 {audience.attempt} 轮占用（续跑补人）：" if resumed else ""
    return f"{prefix}{plan_log}；共享占用实际覆盖 {audience.member_count} 人，花费 ¥{audience.spend:,.2f}"


def serialize_audience(audience: Audience, campaign: Campaign | None = None) -> dict:
    return {
        "id": audience.id,
        "audience_no": audience.audience_no,
        "campaign_id": audience.campaign_id,
        "campaign_name": campaign.name if campaign else None,
        "campaign_code": campaign.code if campaign else None,
        "status": audience.status,
        "status_label": AUDIENCE_STATUS_LABELS.get(audience.status, audience.status),
        "lock_phase": audience.lock_phase,
        "attempt": audience.attempt or 0,
        "budget": round(audience.budget or 0.0, 2),
        "budget_remaining": round((audience.budget or 0.0) - (audience.spend or 0.0), 2),
        "member_count": audience.member_count,
        "spend": round(audience.spend or 0.0, 2),
        "inc_orders": audience.inc_orders,
        "inc_gmv": round(audience.inc_gmv or 0.0, 2),
        "roi": round(audience.roi or 0.0, 2),
        "global_rate": round(audience.global_rate or 0.0, 6),
        "allocation": audience.allocation or [],
        "note": audience.note,
        "operator_email": audience.operator_email,
        "confirmed_by": audience.confirmed_by,
        "confirmed_at": iso_utc(audience.confirmed_at),
        "created_at": iso_utc(audience.created_at),
    }


def list_audiences(
    db: Session,
    *,
    campaign_id: int | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    stmt = select(Audience)
    count_stmt = select(func.count()).select_from(Audience)
    if campaign_id:
        stmt = stmt.where(Audience.campaign_id == campaign_id)
        count_stmt = count_stmt.where(Audience.campaign_id == campaign_id)
    if status:
        stmt = stmt.where(Audience.status == status)
        count_stmt = count_stmt.where(Audience.status == status)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(Audience.id.desc()).limit(limit).offset(offset)).all()
    campaigns = {item.id: item for item in db.scalars(select(Campaign)).all()}
    items = []
    for row in rows:
        payload = serialize_audience(row, campaigns.get(row.campaign_id))
        # 页面人数与真实占用台账对账
        payload["locked_members"] = len(occupancy_service.member_ids_for_audience(db, row.id))
        items.append(payload)
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def get_audience(db: Session, audience_id: int) -> Audience:
    audience = db.get(Audience, audience_id)
    if audience is None:
        raise not_found("audience", audience_id)
    return audience


def confirm(db: Session, audience: Audience, *, status: str, note: str | None, actor: str) -> Audience:
    """确认/推送：一次性把预留占用转正。占用与名单不一致时整批释放并报错（可续跑）。

    失败路径的释放用独立事务提交，保证「确认失败 → 本批占用已释放」不被外层回滚撤销。
    """
    if status not in {"confirmed", "pushed"}:
        raise AppError("VALIDATION", 422, {"reason": "只能确认或推送名单"})
    if audience.status != "pending":
        raise AppError("CONFLICT", 409, {"reason": "名单已确认，不能重复确认", "audience_no": audience.audience_no})

    campaign = get_campaign(db, audience.campaign_id)
    locks = occupancy_service.locks_for_audience(db, audience.id, active_only=True)
    # 一致性校验：台账真实人数/花费必须与名单字段一致，否则不放行确认
    real_count = len(locks)
    real_spend = _round(sum(lock.unit_cost or 0.0 for lock in locks))
    mismatch = real_count != audience.member_count or abs(real_spend - (audience.spend or 0.0)) > BUDGET_EPS
    if mismatch or real_count == 0:
        _release_in_new_transaction(audience.id)
        reason = (
            "名单与共享占用台账不一致，已释放本批占用，请续跑补人后重新确认"
            if mismatch
            else "名单没有有效占用会员，请先续跑补人"
        )
        raise AppError(
            "CONFLICT",
            409,
            {
                "reason": reason,
                "audience_no": audience.audience_no,
                "expected_members": audience.member_count,
                "actual_members": real_count,
            },
        )

    committed = occupancy_service.commit_audience_locks(
        db, audience_id=audience.id, cooldown_days=int(campaign.cooldown_days or 0)
    )
    if committed != real_count:
        # 转正不完整视为确认失败：独立事务整批释放，保持可续跑
        _release_in_new_transaction(audience.id)
        raise AppError("INTERNAL", 500, {"reason": "占用转正不完整，已释放本批占用，请重试"})

    audience.status = status
    audience.lock_phase = "committed"
    audience.confirmed_by = actor
    audience.confirmed_at = utcnow()
    if note:
        audience.note = note
    db.flush()
    return audience


def _release_in_new_transaction(audience_id: int) -> None:
    """用独立会话提交释放，避免外层事务回滚把「释放本批占用」一并撤销。

    独立会话默认绑定应用引擎；测试可通过 set_release_session_factory 注入同引擎工厂。
    同时清空扫描游标，保证续跑从已处理位置重新评估被释放的会员。
    """
    from src.config.database import SessionLocal  # noqa: WPS433

    factory = _RELEASE_SESSION_FACTORY or SessionLocal
    own_session = factory()
    try:
        occupancy_service.release_audience_locks(own_session, audience_id=audience_id)
        stale = own_session.get(Audience, audience_id)
        if stale is not None:
            stale.attempt = int(stale.attempt or 0) + 1
            stale.scan_cursor = {}
        own_session.commit()
    except Exception:  # noqa: BLE001 - 释放失败不应掩盖原始确认错误
        own_session.rollback()
    finally:
        own_session.close()


# 测试/特殊部署可覆盖的独立会话工厂（生产留空，使用应用默认 SessionLocal）
_RELEASE_SESSION_FACTORY = None


def set_release_session_factory(factory) -> None:
    global _RELEASE_SESSION_FACTORY
    _RELEASE_SESSION_FACTORY = factory
