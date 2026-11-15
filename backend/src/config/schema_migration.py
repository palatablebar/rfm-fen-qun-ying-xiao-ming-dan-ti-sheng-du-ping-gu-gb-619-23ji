"""轻量结构演进：create_all 只建新表，已存在表的新增列靠这里补。

项目无 Alembic，采用「检查 information_schema → 缺列则 ADD COLUMN」的幂等方式，
PostgreSQL 与 SQLite 均适用；列均为可空/带默认值，不重写历史数据。
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from src.config.database import engine

logger = logging.getLogger("rfmseg.schema")

# 表名 -> [(列名, 列 DDL 片段)]
_MISSING_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "members": [
        ("merged_into_id", "INTEGER NULL REFERENCES members(id) ON DELETE SET NULL"),
        ("merged_at", "TIMESTAMP NULL"),
    ],
}


def ensure_columns() -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _MISSING_COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all 会整体建出含新列的表
            present = {col["name"] for col in inspector.get_columns(table)}
            for name, ddl in columns:
                if name in present:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                logger.info("schema migration: %s.%s added", table, name)
