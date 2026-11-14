"""共享占用流程的 pytest 夹具：SQLite 内存库 + 最小分群/会员/活动数据。

不依赖 PostgreSQL / Redis，只验证领域层与服务层的占用、续跑、确认与对账逻辑。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models import Base
from src.services import audience_service


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, future=True)
    # 让「确认失败独立事务释放」走同一内存库，否则测试里会连到应用默认引擎
    audience_service.set_release_session_factory(factory)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        audience_service.set_release_session_factory(None)
        engine.dispose()
