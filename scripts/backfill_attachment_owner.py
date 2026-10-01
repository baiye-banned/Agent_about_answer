"""一次性给历史聊天附件键补种登记行（issue #233）。

`chat_attachment_uploads` 是附件归属的唯一凭据：删会话时只有「这把键是我铸的」才允许
签发 DELETE。issue #233 之前，发送成功会把登记行**物理删掉**，于是升级后这些键在表里
查无此行——属主未知，按 deny-by-default 一律拒签，删会话不再回收它们。本脚本从
「`messages.attachments` 里的键 × 会话属主」推定属主，补种登记行。

用法：

    python scripts/backfill_attachment_owner.py            # 默认 dry-run，只打印计划
    python scripts/backfill_attachment_owner.py --apply    # 真正写库

**写入语义（只 INSERT，绝不覆盖）**：只在该键于 `chat_attachment_uploads` 中**无行**时
插入；表中已有行的一律不动——无论它是否已消费。表里已有的行携带的是**上传时的真实事实**
（真实 `user_id`、真实 `created_at`），而回填写的是**推定值**，覆盖等于用猜测压倒事实：

- 已有行未消费（`consumed_at` 为空）＝这把键仍在途，归上传者所有，扫描到的归属纯属巧合；
- 已有行已消费＝本就不在清扫候选集里，回填既无必要也无权改写。

并发下靠 `object_key`（主键）的唯一约束兜底：撞主键 = 已有行 = 跳过。因此重复跑幂等。

**归属推定的残余面（上线前请人工核对）**：推定本身不是事实。同一把键若被两个不同属主的
会话引用（历史脏数据、或跨用户回带的键），`build_plan` 按 `messages.id` 升序取**最早**那条
引用所在会话的属主，于是另一个属主从此对这把键**无权回收**——这是 deny-by-default 的必然
代价（比修复前的「谁都能删」严格更好，但确实存在误归属面）。这类键在 dry-run 输出的
`attachments` 里看得见（同一 `object_key` 只会出现一次），**执行 `--apply` 之前请人工过一遍
该段输出**：确认每把键的推定属主与你的认知一致，尤其留意那些在多个用户之间流转过的键。

**执行时机（与 issue #233 的回滚方案是同一条铁律）**：本脚本属于上线必需项（否则历史
键永远漏删），但**必须在「新代码全量生效之后」单独执行**，不得与代码同批上线。原因：
回填写出的 `consumed_at IS NOT NULL` 的行一旦落进**仍在跑的旧代码**的视野，旧代码的
`list_pending_attachment_uploads` / `claim_attachment_upload` 没有 `consumed_at IS NULL`
过滤，会把这些已消费的历史键当成孤儿候选——历史 `created_at` 必然超期，清扫会当场删掉
**活消息正在引用的对象**。回填自己就成了那次事故的触发者。回滚期间同样禁止跑本脚本。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from model.models import ChatAttachmentUpload, Conversation, Message  # noqa: E402
from service.json_utils import load_json_value  # noqa: E402


def build_plan(db) -> dict:
    """计算回填计划，不修改任何数据。

    逐条列出「消息里有这把键、登记表里没有这一行」的键及其推定属主（消息所属会话的属主）。
    同一把键被多条消息引用时只算一条，取**最早出现**的那条消息的会话——`messages` 按 id
    升序扫，第一条命中的就是最早的引用。
    """
    existing = {key for (key,) in db.query(ChatAttachmentUpload.object_key).all()}
    owners = dict(db.query(Conversation.id, Conversation.user_id).all())

    planned: dict[str, dict] = {}
    skipped_owned: int = 0
    skipped_orphan: list[dict] = []
    rows = (
        db.query(Message.id, Message.conversation_id, Message.attachments)
        .filter(Message.attachments.isnot(None), Message.attachments != "")
        .order_by(Message.id.asc())
        .all()
    )
    for message_id, conversation_id, raw_attachments in rows:
        for item in load_json_value(raw_attachments, []):
            # 历史脏数据与上传中断留下的记录都可能不是 dict、或缺 object_key。
            if not isinstance(item, dict):
                continue
            object_key = item.get("object_key")
            if not isinstance(object_key, str) or not object_key:
                continue
            if object_key in existing or object_key in planned:
                skipped_owned += 1
                continue
            owner_id = owners.get(conversation_id)
            if owner_id is None:
                # 会话行已经不在了（消息是历史残留）：推不出属主，不猜。
                skipped_orphan.append({"object_key": object_key, "conversation_id": conversation_id})
                continue
            planned[object_key] = {
                "object_key": object_key,
                "user_id": owner_id,
                "conversation_id": conversation_id,
                "message_id": message_id,
            }

    return {
        "attachments": list(planned.values()),
        "skipped_existing_rows": skipped_owned,
        "skipped_without_owner": skipped_orphan,
    }


def backfill(db, *, apply: bool = False) -> dict:
    """按推定属主补种缺失的登记行，返回执行结果。"""
    plan = build_plan(db)
    plan["applied"] = False

    if not apply:
        db.rollback()
        return plan

    # created_at 与 consumed_at 取**同一次**时钟读数：这把键在历史语义下已经被发送消费过
    # （它出现在一条消息的附件列里），所以 consumed_at 必须有值，否则新代码的清扫任务会把
    # 一条活消息正在引用的对象当成孤儿删掉。两者相等也让按时间做的对账保持单调
    # （consumed_at >= created_at 恒成立），而整批共用一次读数则避免了逐行读时钟带来的
    # 毫秒级乱序。
    now = datetime.now()
    inserted = 0
    for entry in plan["attachments"]:
        # 再查一次：build_plan 与这里之间可能有并发写入（撞主键会抛 IntegrityError，
        # 那正是「已有行」的情形，跳过即可，所以这里的选择是「少删一次」而不是「快一点」）。
        if db.query(ChatAttachmentUpload).filter_by(object_key=entry["object_key"]).first():
            continue
        db.add(ChatAttachmentUpload(
            object_key=entry["object_key"],
            user_id=entry["user_id"],
            created_at=now,
            consumed_at=now,
        ))
        inserted += 1
    db.commit()

    plan["inserted"] = inserted
    plan["applied"] = True
    return plan


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="给历史聊天附件键补种归属登记行")
    parser.add_argument("--apply", action="store_true", help="真正写库；不传则只打印计划（dry-run）")
    args = parser.parse_args(argv)

    from database.session import SessionLocal

    db = SessionLocal()
    try:
        plan = backfill(db, apply=args.apply)
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
