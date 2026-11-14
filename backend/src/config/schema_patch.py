"""轻量级幂等 schema 补丁：create_all 只建新表，不会给已存在的表补列。

项目未引入 Alembic（见 migrations/README.md），这里用 inspector 检查缺失列后补 ADD COLUMN，
方言无关（PostgreSQL / SQLite 均可），启动时执行一次，重复执行无副作用。
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from src.config.database import engine

logger = logging.getLogger("rfmseg.schema")

# 表名 -> [(列名, 列 DDL 片段)]
_MISSING_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "campaigns": [
        ("cooldown_days", "INTEGER NOT NULL DEFAULT 30"),
    ],
    "audiences": [
        ("scan_cursor", "JSON"),
        ("lock_phase", "VARCHAR(16) NOT NULL DEFAULT 'reserved'"),
        ("attempt", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def ensure_schema() -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _MISSING_COLUMNS.items():
            if table not in existing_tables:
                continue  # 新库由 create_all 一次性建出，无需补列
            present = {column["name"] for column in inspector.get_columns(table)}
            for name, ddl in columns:
                if name in present:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                logger.info("schema patch: %s.%s added", table, name)
