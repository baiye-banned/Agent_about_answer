"""一次性把历史知识库/知识文件回填给某个用户。

issue #9 修复后，`knowledge_bases` / `knowledge_files` 通过 `user_id` 判断归属；
历史上没有归属字段的数据 `user_id` 为 NULL，接口对它们一律按 404 处理（fail-closed），
所以升级后需要用本脚本把它们指给一个用户。

用法：

    python scripts/backfill_knowledge_owner.py --user-id 3            # 默认 dry-run，只打印计划
    python scripts/backfill_knowledge_owner.py --user-id 3 --apply    # 真正写库
    python scripts/backfill_knowledge_owner.py --user-id 3 --apply --rename-conflicts

冲突：回填后同一用户下不允许同名知识库；如果目标用户已经有同名知识库，
默认拒绝写入并在计划里列出，加 --rename-conflicts 会在原名后追加 `-<知识库ID>` 后写入。
"""
import argparse
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from model.models import KnowledgeBase, KnowledgeFile, User  # noqa: E402


def _unique_name(name: str, kid: int, taken: set[str]) -> str:
    candidate = f"{name}-{kid}"
    suffix = 2
    while candidate in taken:
        candidate = f"{name}-{kid}-{suffix}"
        suffix += 1
    return candidate


def build_plan(db, user_id: int, *, rename_conflicts: bool = False) -> dict:
    """计算回填计划，不修改任何数据。"""
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise SystemExit(f"用户 id={user_id} 不存在")

    bases = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.user_id.is_(None))
        .order_by(KnowledgeBase.id.asc())
        .all()
    )
    base_ids = [base.id for base in bases]

    owned_names = {base.name for base in db.query(KnowledgeBase).filter_by(user_id=user_id).all()}
    renames: dict[int, str] = {}
    conflicts: list[dict] = []
    for base in bases:
        if base.name not in owned_names:
            owned_names.add(base.name)
            continue
        conflicts.append({"id": base.id, "name": base.name})
        if rename_conflicts:
            new_name = _unique_name(base.name, base.id, owned_names)
            owned_names.add(new_name)
            renames[base.id] = new_name

    files = []
    if base_ids:
        files = (
            db.query(KnowledgeFile)
            .filter(KnowledgeFile.user_id.is_(None), KnowledgeFile.knowledge_base_id.in_(base_ids))
            .order_by(KnowledgeFile.id.asc())
            .all()
        )

    file_ids = {file_entry.id for file_entry in files}
    skipped_files = [
        {"id": file_entry.id, "knowledge_base_id": file_entry.knowledge_base_id}
        for file_entry in db.query(KnowledgeFile).filter(KnowledgeFile.user_id.is_(None)).all()
        if file_entry.id not in file_ids
    ]

    return {
        "user_id": user_id,
        "username": user.username,
        "knowledge_bases": [{"id": base.id, "name": base.name} for base in bases],
        "knowledge_files": [{"id": file_entry.id, "name": file_entry.name} for file_entry in files],
        "rename_conflicts": renames,
        "conflicts": conflicts,
        "skipped_files": skipped_files,
    }


def backfill(db, user_id: int, *, apply: bool = False, rename_conflicts: bool = False) -> dict:
    """按归属用户回填 NULL 归属的知识库与知识文件，返回执行结果。"""
    plan = build_plan(db, user_id, rename_conflicts=rename_conflicts)
    plan["applied"] = False

    if plan["conflicts"] and not rename_conflicts:
        db.rollback()
        plan["error"] = "目标用户已存在同名知识库，请加 --rename-conflicts 后重试"
        return plan

    if not apply:
        db.rollback()
        return plan

    for base in plan["knowledge_bases"]:
        entry = db.query(KnowledgeBase).filter_by(id=base["id"]).first()
        entry.user_id = user_id
        if base["id"] in plan["rename_conflicts"]:
            entry.name = plan["rename_conflicts"][base["id"]]
    for file_entry in plan["knowledge_files"]:
        entry = db.query(KnowledgeFile).filter_by(id=file_entry["id"]).first()
        entry.user_id = user_id
    db.commit()

    plan["applied"] = True
    return plan


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="回填知识库/知识文件的历史归属用户")
    parser.add_argument("--user-id", type=int, required=True, help="把 NULL 归属的数据指给这个用户")
    parser.add_argument("--apply", action="store_true", help="真正写库；不传则只打印计划（dry-run）")
    parser.add_argument(
        "--rename-conflicts",
        action="store_true",
        help="目标用户已有同名知识库时，给历史知识库加 `-<ID>` 后缀后回填",
    )
    args = parser.parse_args(argv)

    from database.session import SessionLocal

    db = SessionLocal()
    try:
        plan = backfill(
            db,
            args.user_id,
            apply=args.apply,
            rename_conflicts=args.rename_conflicts,
        )
    finally:
        db.close()

    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if plan.get("error"):
        return 1
    if not plan["applied"]:
        print("dry-run：未修改任何数据，确认无误后加 --apply 执行。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
