"""共享触达占用端到端测试：跨活动不重复占人、冷却期、确认转正、失败释放续跑、对账一致。

用 SQLite 内存库直接构造会员/分群/活动，覆盖需求的每条硬约束。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from src.constants.enums import ReachLockStatus
from src.models.audience import Audience
from src.models.campaign import Campaign
from src.models.member import Member
from src.models.reach_lock import ReachLock
from src.models.segment import Segment
from src.services import audience_service, occupancy_service
from src.utils.errors import AppError
from src.utils.timeutil import utcnow


# ---- 造数工具 ----

def _make_segments(db, *, per_segment: int = 40) -> None:
    specs = [
        ("champion", "重要价值", 0.080, 8.0, 34.0),
        ("loyal", "重要保持", 0.055, 8.0, 29.0),
        ("new", "新客养成", 0.040, 12.0, 24.0),
        ("hibernating", "沉睡会员", 0.008, 4.0, 20.0),
    ]
    for index, (code, name, rate, coupon, ticket) in enumerate(specs):
        db.add(
            Segment(
                code=code,
                name=name,
                rule_text="test",
                sort_order=index,
                member_count=per_segment,
                purchase_rate=rate,
                avg_ticket=ticket,
                lift=1.0,
                coupon_face=coupon,
                core_share=0.6,
                enabled=True,
                snapshot={"global_purchase_rate": 0.03},
                updated_at=utcnow(),
            )
        )
    db.flush()


def _make_members(db, *, per_segment: int = 40) -> None:
    seq = 0
    for code in ["champion", "loyal", "new", "hibernating"]:
        segment = db.query(Segment).filter(Segment.code == code).one()
        rate = segment.purchase_rate
        for i in range(per_segment):
            seq += 1
            db.add(
                Member(
                    member_no=f"MY{seq:05d}",
                    name=f"会员{seq}",
                    phone=f"138{seq:08d}"[:11],
                    city="上海",
                    tier="gold",
                    joined_on=utcnow().date(),
                    purchase_rate=rate,
                    avg_ticket=segment.avg_ticket,
                    composite_score=100.0 - i * 0.01,
                    segment_code=code,
                    scored_at=utcnow(),
                )
            )
    db.flush()


def _make_campaign(db, code: str, budget: float, *, cooldown_days: int = 30) -> Campaign:
    campaign = Campaign(
        code=code,
        name=code,
        channel="小程序订阅消息",
        budget=budget,
        status="active",
        reach_cost=0.15,
        orders_per_converter=1.4,
        tail_factor=0.6,
        cooldown_days=cooldown_days,
        operator_email="admin@rfmseg.io",
    )
    db.add(campaign)
    db.flush()
    return campaign


def _setup(db, *, per_segment: int = 40, cooldown_days: int = 30):
    _make_segments(db, per_segment=per_segment)
    _make_members(db, per_segment=per_segment)
    return _make_campaign(db, "camp-a", 100000.0, cooldown_days=cooldown_days)


def _locked_member_ids(db, audience_id: int) -> set[int]:
    return set(occupancy_service.member_ids_for_audience(db, audience_id))


# ---- 测试用例 ----

def test_generate_reserves_real_members_and_counts_match(db):
    campaign = _setup(db)
    audience = audience_service.generate(
        db, campaign=campaign, budget=None, operator_email="admin@rfmseg.io"
    )
    db.flush()
    ids = _locked_member_ids(db, audience.id)
    # 页面人数 == 台账真实锁行数 == 各分群实际占用之和
    assert audience.member_count == len(ids)
    assert audience.member_count > 0
    seg_sum = sum(
        row.get("occupied_members", 0)
        for row in audience.allocation
        if row["status"] == "allocated"
    )
    assert seg_sum == audience.member_count
    # 全部为预留占用，尚未转正
    statuses = {lock.status for lock in occupancy_service.locks_for_audience(db, audience.id)}
    assert statuses == {ReachLockStatus.RESERVED.value}
    # 预算不超
    assert audience.spend <= audience.budget + 1e-6


def test_two_campaigns_do_not_share_members(db):
    campaign_a = _setup(db)
    audience_a = audience_service.generate(
        db, campaign=campaign_a, budget=None, operator_email="admin@rfmseg.io"
    )
    campaign_b = _make_campaign(db, "camp-b", 100000.0)
    audience_b = audience_service.generate(
        db, campaign=campaign_b, budget=None, operator_email="admin@rfmseg.io"
    )
    ids_a = _locked_member_ids(db, audience_a.id)
    ids_b = _locked_member_ids(db, audience_b.id)
    # 活动间零重复占人
    assert ids_a.isdisjoint(ids_b)
    # 同一会员在有效占用期内只有一行有效锁
    active_rows = (
        db.query(ReachLock)
        .filter(ReachLock.status.in_([ReachLockStatus.RESERVED.value, ReachLockStatus.CONFIRMED.value]))
        .all()
    )
    member_ids = [row.member_id for row in active_rows]
    assert len(member_ids) == len(set(member_ids))


def test_confirm_switches_locks_to_committed_with_cooldown(db):
    campaign = _setup(db)
    audience = audience_service.generate(
        db, campaign=campaign, budget=None, operator_email="admin@rfmseg.io"
    )
    audience_service.confirm(db, audience, status="confirmed", note=None, actor="analyst@rfmseg.io")
    db.flush()
    assert audience.status == "confirmed"
    assert audience.lock_phase == "committed"
    locks = occupancy_service.locks_for_audience(db, audience.id, active_only=True)
    assert all(lock.status == ReachLockStatus.CONFIRMED.value for lock in locks)
    expected = utcnow() + timedelta(days=campaign.cooldown_days)
    assert all(lock.cooldown_until is not None for lock in locks)
    # 冷却截止落在确认时间 + cooldown_days 附近
    for lock in locks:
        delta = abs((lock.cooldown_until - expected).total_seconds())
        assert delta < 60


def test_cooldown_blocks_reuse_until_expired(db):
    campaign_a = _setup(db, cooldown_days=30)
    audience_a = audience_service.generate(
        db, campaign=campaign_a, budget=None, operator_email="admin@rfmseg.io"
    )
    audience_service.confirm(db, audience_a, status="confirmed", note=None, actor="a@rfmseg.io")
    ids_a = _locked_member_ids(db, audience_a.id)

    campaign_b = _make_campaign(db, "camp-b", 100000.0, cooldown_days=30)
    audience_b = audience_service.generate(
        db, campaign=campaign_b, budget=None, operator_email="admin@rfmseg.io"
    )
    ids_b = _locked_member_ids(db, audience_b.id)
    assert ids_a.isdisjoint(ids_b)

    # 冷却期过后：把 A 的锁冷却时间调到过去，B 续跑/新名单可重新占用这些会员
    for lock in db.query(ReachLock).filter(ReachLock.audience_id == audience_a.id).all():
        lock.cooldown_until = utcnow() - timedelta(days=1)
    db.flush()
    blocking = occupancy_service.active_blocking_member_ids(db)
    assert ids_a.isdisjoint(blocking)


def test_confirm_failure_releases_and_resume_refills_without_duplicates(db):
    # 每群 80 人，A 的预算只够吃一部分，给 B 留出同群的可占用候选（A 占用者冷却中会被跳过）
    campaign_a = _setup(db, per_segment=80)
    campaign_a.budget = 20.0
    db.flush()
    audience_a = audience_service.generate(
        db, campaign=campaign_a, budget=20.0, operator_email="admin@rfmseg.io"
    )
    audience_service.confirm(db, audience_a, status="confirmed", note=None, actor="a@rfmseg.io")
    assert _locked_member_ids(db, audience_a.id), "A 应已确认占用一批会员"

    campaign_b = _make_campaign(db, "camp-b", 100000.0, cooldown_days=30)
    audience_b = audience_service.generate(
        db, campaign=campaign_b, budget=None, operator_email="admin@rfmseg.io"
    )
    original_ids = _locked_member_ids(db, audience_b.id)
    assert original_ids, "B 应已预留一批会员"
    assert original_ids.isdisjoint(_locked_member_ids(db, audience_a.id))

    # 模拟确认失败：整批释放
    released = occupancy_service.release_audience_locks(db, audience_id=audience_b.id)
    db.commit()
    assert released == len(original_ids)
    assert _locked_member_ids(db, audience_b.id) == set()

    # 续跑补人：同一名单号、不新增名单
    before_count = db.query(Audience).count()
    attempt_before = audience_b.attempt
    resumed = audience_service.resume_generation(
        db, audience_b, operator_email="admin@rfmseg.io"
    )
    db.commit()
    assert db.query(Audience).count() == before_count
    assert resumed.id == audience_b.id
    assert resumed.audience_no == audience_b.audience_no
    assert resumed.attempt == attempt_before + 1

    refilled = _locked_member_ids(db, resumed.id)
    # 不与 A 的正式占用重复
    assert refilled.isdisjoint(_locked_member_ids(db, audience_a.id))
    # 人数与台账一致、预算不超
    assert resumed.member_count == len(refilled)
    assert resumed.spend <= resumed.budget + 1e-6


def test_retry_same_campaign_reuses_pending_audience(db):
    campaign = _setup(db)
    first = audience_service.generate(
        db, campaign=campaign, budget=None, operator_email="admin@rfmseg.io"
    )
    # 同活动再次触发生成：不新增名单，在原 pending 名单上续跑
    second = audience_service.generate(
        db, campaign=campaign, budget=None, operator_email="admin@rfmseg.io"
    )
    assert second.id == first.id
    assert second.audience_no == first.audience_no
    pending = db.query(Audience).filter(Audience.campaign_id == campaign.id, Audience.status == "pending").all()
    assert len(pending) == 1


def test_budget_ceiling_never_exceeded(db):
    # 小预算：单人成本至少 0.15，预算只够少数人
    campaign = _setup(db)
    campaign.budget = 5.0
    db.flush()
    audience = audience_service.generate(
        db, campaign=campaign, budget=5.0, operator_email="admin@rfmseg.io"
    )
    assert audience.spend <= 5.0 + 1e-6
    # 逐锁成本之和与名单花费一致
    locks = occupancy_service.locks_for_audience(db, audience.id, active_only=True)
    assert sum(lock.unit_cost for lock in locks) == pytest.approx(audience.spend, abs=0.02)


def test_export_members_equal_lock_rows(db):
    from src.services import export_service

    campaign = _setup(db)
    audience = audience_service.generate(
        db, campaign=campaign, budget=None, operator_email="admin@rfmseg.io"
    )
    csv_bytes = export_service.audience_csv(db, audience.id)
    lines = [line for line in csv_bytes.decode("utf-8-sig").splitlines() if line.strip()]
    # 首行表头 + 每个有效锁一行
    lock_count = len(occupancy_service.member_ids_for_audience(db, audience.id))
    assert len(lines) == lock_count + 1


def test_budget_shrink_releases_surplus_locks(db):
    campaign = _setup(db, per_segment=80)
    audience = audience_service.generate(
        db, campaign=campaign, budget=100.0, operator_email="admin@rfmseg.io"
    )
    assert audience.spend <= 100.0 + 1e-6
    big_spend = audience.spend
    assert big_spend > 0
    # 同活动把预算调小后重试：在原名单上裁剪，花费落到新预算内，不新增名单
    before = db.query(Audience).count()
    shrunk = audience_service.generate(
        db, campaign=campaign, budget=20.0, operator_email="admin@rfmseg.io"
    )
    db.commit()
    assert db.query(Audience).count() == before
    assert shrunk.id == audience.id
    assert shrunk.spend <= 20.0 + 1e-6
    assert shrunk.member_count == len(occupancy_service.member_ids_for_audience(db, shrunk.id))


def test_cooldown_expired_member_can_be_reacquired(db):
    campaign_a = _setup(db, per_segment=80, cooldown_days=30)
    campaign_a.budget = 30.0
    db.flush()
    audience_a = audience_service.generate(
        db, campaign=campaign_a, budget=30.0, operator_email="admin@rfmseg.io"
    )
    audience_service.confirm(db, audience_a, status="confirmed", note=None, actor="a@rfmseg.io")
    ids_a = _locked_member_ids(db, audience_a.id)

    campaign_b = _make_campaign(db, "camp-b", 100000.0, cooldown_days=30)
    audience_b = audience_service.generate(
        db, campaign=campaign_b, budget=None, operator_email="admin@rfmseg.io"
    )
    assert _locked_member_ids(db, audience_b.id).isdisjoint(ids_a)

    # A 的冷却全部过期后，B 续跑可重新占用这些会员
    for lock in db.query(ReachLock).filter(ReachLock.audience_id == audience_a.id).all():
        lock.cooldown_until = utcnow() - timedelta(days=1)
    occupancy_service.release_audience_locks(db, audience_id=audience_b.id, audience=audience_b)
    db.commit()
    resumed = audience_service.resume_generation(db, audience_b, operator_email="admin@rfmseg.io")
    db.commit()
    # 冷却解除后允许跨活动复用，且自身台账人数一致、不超预算
    assert resumed.member_count == len(_locked_member_ids(db, resumed.id))
    assert resumed.spend <= resumed.budget + 1e-6


def test_cannot_confirm_twice(db):
    campaign = _setup(db)
    audience = audience_service.generate(
        db, campaign=campaign, budget=None, operator_email="admin@rfmseg.io"
    )
    audience_service.confirm(db, audience, status="confirmed", note=None, actor="a@rfmseg.io")
    with pytest.raises(AppError) as exc:
        audience_service.confirm(db, audience, status="pushed", note=None, actor="a@rfmseg.io")
    assert exc.value.code == "CONFLICT"


def test_mismatched_audience_releases_on_confirm(db):
    campaign = _setup(db)
    audience = audience_service.generate(
        db, campaign=campaign, budget=None, operator_email="admin@rfmseg.io"
    )
    # 人为篡改名单人数字段，制造与台账不一致
    audience.member_count = audience.member_count + 99
    db.commit()
    with pytest.raises(AppError) as exc:
        audience_service.confirm(db, audience, status="confirmed", note=None, actor="a@rfmseg.io")
    assert exc.value.code == "CONFLICT"
    # 失败后本批占用已释放
    assert _locked_member_ids(db, audience.id) == set()
