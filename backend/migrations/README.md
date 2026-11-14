# 数据库迁移说明

本项目用 SQLAlchemy `Base.metadata.create_all()` 在 backend 启动时建表（见 `src/config/app.py`），
因此首次 `docker compose up` 会自动建好表，不需要额外执行迁移。

- `001_core_tables.sql`：核心表的人类可读 DDL 参考（PostgreSQL 语法），用于 reviewer 对照字段；
  运行时不执行它。
- 已存在库的新增列/新表由启动时的 `src/config/schema_patch.py` 幂等补齐（inspector 检查缺失列后
  `ALTER TABLE ADD COLUMN`，方言无关），新表仍由 `create_all` 建出。当前补丁：
  `campaigns.cooldown_days`、`audiences.{scan_cursor,lock_phase,attempt}`、新表 `member_reach_locks`。
- 后续如果要做更复杂的字段变更，建议引入 Alembic，并在本目录按 `00x_<描述>.py` 递增。
- seed 数据只走 `backend/scripts/seed_demo.py`，不要写进 SQL。
