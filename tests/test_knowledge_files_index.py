"""issue #83 第 5 项：`knowledge_files.knowledge_base_id` 要有索引。

PR #81 把「逐库懒加载」换成了 `count_knowledge_files_by_base` 的单条 GROUP BY 聚合，
条数与内存都降下来了，但扫描面没变——过滤与分组都落在 knowledge_base_id 上，
而该列在模型和迁移两处都没有索引，聚合仍然对整个 knowledge_files 表扫一遍。

这里用 SQLite 的 EXPLAIN QUERY PLAN 把「有索引 / 无索引」两种计划并排钉住，
并覆盖迁移侧：已经建好的库要靠 `_ensure_single_column_index` 补，模型上的
index=True 只对 create_all 新建的表生效。
"""

import pytest
from sqlalchemy import create_engine, func, inspect, text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from crud.knowledge_base import count_knowledge_files_by_base
from database import session as db_session
from database.session import Base
from model.models import KnowledgeFile

INDEX_NAME = "ix_knowledge_files_knowledge_base_id"


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_text(_type, _compiler, **_kwargs):
    """knowledge_files.content 是 MySQL 的 LONGTEXT，SQLite 建表时降级成 TEXT。"""
    return "TEXT"


@pytest.fixture()
def sqlite_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


def _seed(session, rows=400):
    """每个知识库若干文件：行数足够多，SQLite 才会倾向于走索引。"""
    session.add_all(
        [
            KnowledgeFile(
                id=index,
                knowledge_base_id=index % 20,
                name=f"文件{index}.txt",
                size=10,
                content="员工考勤管理制度。",
            )
            for index in range(1, rows + 1)
        ]
    )
    session.commit()


def _aggregate_plan(session, engine, kids):
    """按 count_knowledge_files_by_base 的查询形状取执行计划。"""
    statement = (
        session.query(KnowledgeFile.knowledge_base_id, func.count(KnowledgeFile.id))
        .filter(KnowledgeFile.knowledge_base_id.in_(kids))
        .group_by(KnowledgeFile.knowledge_base_id)
    )
    sql = str(statement.statement.compile(engine, compile_kwargs={"literal_binds": True}))
    rows = session.execute(text(f"EXPLAIN QUERY PLAN {sql}")).all()
    return " | ".join(str(row[-1]) for row in rows)


def test_model_declares_the_knowledge_base_id_index(sqlite_engine):
    assert KnowledgeFile.__table__.c.knowledge_base_id.index is True
    assert INDEX_NAME in {index.name for index in KnowledgeFile.__table__.indexes}


def _plan_for_fresh_schema(*, drop_index: bool):
    """在各自独立的库上取计划。

    两次测量必须用两个库：同一个 SQLite 连接在 DROP INDEX 之后仍复用已缓存的
    预处理语句，第二次 EXPLAIN 会拿到旧计划，测出来的不是无索引的真实行为。
    """
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    kids = list(range(20))
    try:
        with Session() as session:
            if drop_index:
                # 在跑任何聚合查询之前摘掉索引，连接上不会留下带索引的计划缓存。
                session.execute(text(f"DROP INDEX {INDEX_NAME}"))
                session.commit()
            _seed(session)
            plan = _aggregate_plan(session, engine, kids)
            # 索引只影响计划，不影响结果：计数照常按知识库分组返回。
            assert count_knowledge_files_by_base(session, kids) == {base_id: 20 for base_id in kids}
        return plan
    finally:
        engine.dispose()


def test_aggregate_uses_the_index_and_falls_back_to_a_scan_without_it():
    """并排给出两种执行计划：这正是「补索引」这一项的验收证据。"""
    with_index_plan = _plan_for_fresh_schema(drop_index=False)
    without_index_plan = _plan_for_fresh_schema(drop_index=True)

    assert INDEX_NAME in with_index_plan
    assert "SCAN knowledge_files" not in with_index_plan
    # 先证红：没有索引时这里就是对整表扫描，也正是 issue 记录的现状。
    assert "SCAN knowledge_files" in without_index_plan


def test_ensure_single_column_index_backfills_an_existing_table(sqlite_engine, monkeypatch):
    """迁移侧：已经建好的库（create_all 不会再跑）要能补上索引，且重复调用安全。"""
    monkeypatch.setattr(db_session, "engine", sqlite_engine)
    with sqlite_engine.begin() as conn:
        conn.execute(text(f"DROP INDEX {INDEX_NAME}"))
    assert (("knowledge_base_id",)) not in {
        tuple(index.get("column_names") or []) for index in inspect(sqlite_engine).get_indexes("knowledge_files")
    }

    db_session._ensure_single_column_index("knowledge_files", "knowledge_base_id", INDEX_NAME)

    assert INDEX_NAME in {index["name"] for index in inspect(sqlite_engine).get_indexes("knowledge_files")}
    # 幂等：第二次调用发现索引已在，不应抛错也不应重复创建。
    db_session._ensure_single_column_index("knowledge_files", "knowledge_base_id", INDEX_NAME)
    assert INDEX_NAME in {index["name"] for index in inspect(sqlite_engine).get_indexes("knowledge_files")}
