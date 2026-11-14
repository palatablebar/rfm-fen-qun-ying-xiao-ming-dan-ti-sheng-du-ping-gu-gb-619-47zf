"""跨活动共享触达占用服务（member_reach_locks 的唯一写入口）。

设计目标（对应需求）：
1. 按分群提升度优先级占用「冷却期外」的会员 —— 候选顺序与导出口径一致
   （综合分降序、id 升序），已被其它活动有效占用或处于冷却期内的会员跳过；
2. 生成名单只做 reserved 预留占用，确认时一次性切换为 confirmed 正式占用；
3. 确认失败（或预留超时）整批释放，之后从名单记录的扫描游标续跑补人，
   同一活动重试不新增名单、不重复占用；
4. 页面人数 / 预算 / 导出全部以本台账的真实会员行为准，活动间不重复占人、不超预算。

并发：整个生成/确认流程在单个数据库事务内执行；同一会员的有效占用由
部分唯一索引在库内兜底，应用层插入前刷新有效占用集合，抢占冲突即跳过该会员选下一个。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import ReachLockStatus
from src.constants.limits import RESERVATION_TTL_HOURS
from src.models.campaign import Campaign
from src.models.member import Member
from src.models.reach_lock import ReachLock
from src.utils.timeutil import utcnow

_ACTIVE = (ReachLockStatus.RESERVED.value, ReachLockStatus.CONFIRMED.value)


def release_expired_reservations(db: Session) -> int:
    """清场：超时未确认的预留 + 冷却期已过的正式占用都标记释放，会员回到可占用池。

    释放历史行保留（released）用于审计；部分唯一索引只约束有效占用，释放后即可被重新抢占。
    """
    now = utcnow()
    rows = db.scalars(
        select(ReachLock).where(
            ReachLock.status.in_(list(_ACTIVE)),
            ReachLock.cooldown_until.is_not(None),
            ReachLock.cooldown_until < now,
        )
    ).all()
    for row in rows:
        row.status = ReachLockStatus.RELEASED.value
        row.released_at = now
        row.cooldown_until = None
    if rows:
        db.flush()
    return len(rows)


def active_blocking_member_ids(db: Session) -> set[int]:
    """当前仍会阻止占用的会员 id：reserved/confirmed 且在有效期内。

    - reserved 未超时：其它活动不能抢（同批事务内的行由本流程自行管理）；
    - confirmed 且 cooldown_until 未到：冷却期内的正式占用；
    - released、confirmed 已过冷却期：不阻止。
    """
    now = utcnow()
    rows = db.execute(
        select(ReachLock.member_id).where(
            ReachLock.status.in_(list(_ACTIVE)),
            ReachLock.cooldown_until.is_not(None),
            ReachLock.cooldown_until >= now,
        )
    ).all()
    return {int(row[0]) for row in rows}


def reserve_members(
    db: Session,
    *,
    audience_id: int,
    campaign: Campaign,
    segment_code: str,
    target_count: int,
    budget_remaining: float,
    unit_cost: float,
    scan_offset: int,
    blocked_ids: set[int],
) -> dict:
    """在单个分群内按优先级抢占会员，直到补足 target_count、预算不足或候选耗尽。

    返回实际占用结果（含新游标、本群花费、被挡人数）。所有行先写 reserved。
    占用前先刷新库内有效占用集合做成员级去重（应用事务 + 部分唯一索引双重兜底），
    不使用 SAVEPOINT，避免回滚导致同事务 ORM 对象过期。
    """
    reservation_deadline = utcnow() + timedelta(hours=RESERVATION_TTL_HOURS)
    reserved: list[ReachLock] = []
    blocked_here = 0
    cursor = scan_offset
    spend = 0.0
    # 每轮拉取的候选窗口；跳过的人也要推进游标，避免续跑重复扫描
    page_size = max(500, target_count * 2)
    budget_exhausted = False

    while len(reserved) < target_count and not budget_exhausted:
        rows = db.scalars(
            select(Member)
            .where(Member.segment_code == segment_code)
            .order_by(Member.composite_score.desc().nullslast(), Member.id)
            .offset(cursor)
            .limit(page_size)
        ).all()
        if not rows:
            break
        # 刷新库内当前有效占用，兜住本事务外并发提交的占用
        db_active = active_blocking_member_ids(db)
        for member in rows:
            cursor += 1
            if member.id in blocked_ids or member.id in db_active:
                blocked_here += 1
                blocked_ids.add(member.id)
                continue
            # 预算硬约束：再占一个就超预算则停止；该会员未消费，游标回退一位，续跑可重新评估
            if unit_cost > 0 and spend + unit_cost > budget_remaining + 1e-6:
                cursor -= 1
                budget_exhausted = True
                break
            lock = ReachLock(
                member_id=member.id,
                audience_id=audience_id,
                campaign_id=campaign.id,
                segment_code=segment_code,
                status=ReachLockStatus.RESERVED.value,
                cooldown_until=reservation_deadline,
                unit_cost=round(unit_cost, 4),
                reserved_at=utcnow(),
            )
            db.add(lock)
            db.flush()
            reserved.append(lock)
            blocked_ids.add(member.id)
            db_active.add(member.id)
            spend = round(spend + unit_cost, 4)
            if len(reserved) >= target_count:
                break

    return {
        "locks": reserved,
        "member_ids": [lock.member_id for lock in reserved],
        "count": len(reserved),
        "spend": round(spend, 2),
        "cursor": cursor,
        "blocked": blocked_here,
    }


def locks_for_audience(db: Session, audience_id: int, *, active_only: bool = False) -> list[ReachLock]:
    stmt = select(ReachLock).where(ReachLock.audience_id == audience_id)
    if active_only:
        stmt = stmt.where(ReachLock.status != ReachLockStatus.RELEASED.value)
    return db.scalars(stmt.order_by(ReachLock.segment_code, ReachLock.id)).all()


def member_ids_for_audience(db: Session, audience_id: int) -> list[int]:
    """名单的真实会员（有效占用），导出与页面人数的唯一来源。"""
    rows = db.execute(
        select(ReachLock.member_id)
        .where(ReachLock.audience_id == audience_id, ReachLock.status.in_(list(_ACTIVE)))
        .order_by(ReachLock.segment_code, ReachLock.id)
    ).all()
    return [int(row[0]) for row in rows]


def commit_audience_locks(db: Session, *, audience_id: int, cooldown_days: int) -> int:
    """确认时一次性把本名单的 reserved 占用切换为 confirmed 正式占用。"""
    now = utcnow()
    cooldown_until = now + timedelta(days=max(0, int(cooldown_days)))
    rows = db.scalars(
        select(ReachLock).where(
            ReachLock.audience_id == audience_id,
            ReachLock.status == ReachLockStatus.RESERVED.value,
        )
    ).all()
    for row in rows:
        row.status = ReachLockStatus.CONFIRMED.value
        row.confirmed_at = now
        row.cooldown_until = cooldown_until
    db.flush()
    return len(rows)


def release_audience_locks(db: Session, *, audience_id: int, audience=None) -> int:
    """确认失败/作废：释放本名单全部有效占用，会员回到可占用池。

    传入 audience ORM 对象时同步清空扫描游标，保证续跑从已处理位置重新抢占。
    """
    now = utcnow()
    rows = db.scalars(
        select(ReachLock).where(
            ReachLock.audience_id == audience_id,
            ReachLock.status.in_(list(_ACTIVE)),
        )
    ).all()
    for row in rows:
        row.status = ReachLockStatus.RELEASED.value
        row.released_at = now
        row.cooldown_until = None
    if audience is not None and hasattr(audience, "scan_cursor"):
        audience.scan_cursor = {}
    db.flush()
    return len(rows)


def active_lock_counts(db: Session) -> dict:
    """总览用：当前有效预留 / 正式占用 / 冷却中人数（跨活动去重，即锁行数）。"""
    now = utcnow()
    reserved = confirmed = cooling = 0
    for status, cooldown_until in db.execute(select(ReachLock.status, ReachLock.cooldown_until)).all():
        if status == ReachLockStatus.RELEASED.value:
            continue
        if status == ReachLockStatus.RESERVED.value and cooldown_until and cooldown_until >= now:
            reserved += 1
        elif status == ReachLockStatus.CONFIRMED.value:
            confirmed += 1
            if cooldown_until and cooldown_until >= now:
                cooling += 1
    return {"reserved": reserved, "confirmed": confirmed, "cooling": cooling}
