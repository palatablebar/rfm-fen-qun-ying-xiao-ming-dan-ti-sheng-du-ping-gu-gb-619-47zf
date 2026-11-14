"""共享触达占用台账：跨活动的会员级占用，保证同一会员同一时间只被一份名单占用。

状态机（ReachLockStatus）：
  reserved  —— 生成名单时按优先级抢占的预留占用（名单 pending），不立即转正；
  confirmed —— 名单确认/推送时一次性切换的正式占用，按 cooldown_until 对其它活动生效；
  released  —— 确认失败或预留超时后释放，本批占用全部归还，会员可被重新占用。

同一会员只允许存在一行有效占用（reserved/confirmed），由部分唯一索引在库内兜底，
应用层在事务内先刷新有效占用集合再插入，避免并发双占。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow

# 部分唯一索引条件：仅有效占用参与唯一约束（released 历史行不冲突）
_ACTIVE_LOCK_CONDITION = text("status IN ('reserved', 'confirmed')")


class ReachLock(Base):
    __tablename__ = "member_reach_locks"
    __table_args__ = (
        # 有效占用（reserved/confirmed）同一会员唯一；released 行保留用于审计，不参与唯一约束
        Index(
            "ix_reach_lock_active_member",
            "member_id",
            unique=True,
            sqlite_where=_ACTIVE_LOCK_CONDITION,
            postgresql_where=_ACTIVE_LOCK_CONDITION,
        ),
        Index("ix_reach_lock_audience", "audience_id"),
        Index("ix_reach_lock_campaign", "campaign_id"),
        Index("ix_reach_lock_status_cooldown", "status", "cooldown_until"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"))
    audience_id: Mapped[int] = mapped_column(ForeignKey("audiences.id", ondelete="CASCADE"))
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    segment_code: Mapped[str] = mapped_column(String(20), default="", index=True)
    status: Mapped[str] = mapped_column(String(16), default="reserved", index=True)
    # 占用有效期截止：reserved = 预留 TTL；confirmed = 确认时间 + 活动冷却期
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 该会员在本名单分群口径下的单人成本（触达 + 期望券核销），落库后预算可逐行对账
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    reserved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
