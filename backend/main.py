import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError

from config import (
    REBUILD_KNOWLEDGE_INDEX_ON_STARTUP,
    ensure_mysql_password_configured,
    ensure_secret_key_configured,
)
from database.session import init_db
from paths import UPLOAD_DIR
from router.auth import router as auth_router
from router.chat import router as chat_router
from router.checkpointer import router as checkpointer_router
from router.knowledge import router as knowledge_router
from router.user import router as user_router
from service.chat_service import schedule_orphan_attachment_sweep
from service.knowledge_service import rebuild_existing_knowledge_index, run_ingest_step
from service.user_service import seed_default_users


logger = logging.getLogger(__name__)

INTEGRITY_CONFLICT_MESSAGE = "数据冲突：本次操作与当前数据状态不一致，请重试。"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_secret_key_configured()
    ensure_mysql_password_configured()
    init_db()
    seed_default_users()
    if REBUILD_KNOWLEDGE_INDEX_ON_STARTUP:
        # 重建对每个历史文件同步向量化并写库，跑在**入库专用**线程池上；启动顺序不变
        # （重建完成才接流量），但重建期间事件循环仍可调度其它协程。不设总时限：
        # 重建按文件数逐个跑，没有「一份文档」的预算语义，逐文件的失败隔离已由
        # rebuild_existing_knowledge_index 自己保证。
        await run_ingest_step(rebuild_existing_knowledge_index)
    # 回收「上传了但从未被发送」的聊天附件（issue #142）：上传即登记一条待确认行，发送成功
    # 时被消费，超过保留窗口还没被消费的由清扫删掉对应对象。挂后台守护线程而不是 await：
    # DELETE 是串行外呼，积压一批能把就绪时间拖成分钟级；清扫只碰超过保留窗口的登记行，
    # 与启动时正在上传的对象没有交集，晚一点跑没有正确性代价。
    #
    # 启动只是第一轮：线程里是个循环，之后按间隔继续扫。只扫启动那一轮的话，跑几个月不重启
    # 的进程永远等不到第二轮，孤儿会一直攒着——issue #142 的形态会以「服务没重启」为由复活。
    schedule_orphan_attachment_sweep()
    yield


app = FastAPI(title="RAG API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError):
    """写路径未单独处理的唯一/外键冲突兜底成 409，不再冒成 500（issue #60 验收标准第 4 条）。

    用户可见的知识库重名竞态已在 service 层翻译成带具体文案的 400，这里只兜住漏网者
    （例如并发删除后仍被引用的写入），保证响应是可读的 4xx 而不是 Starlette 默认的 500。
    """
    logger.warning("Unhandled integrity error: %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(status_code=409, content={"detail": INTEGRITY_CONFLICT_MESSAGE})


@app.get("/")
def root():
    return {"status": "ok", "service": "RAG API"}


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(auth_router)
app.include_router(user_router)
app.include_router(chat_router)
app.include_router(knowledge_router)
app.include_router(checkpointer_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8002,
        reload=True,
        reload_excludes=[
            "uploads/*",
            "uploads/**",
            "backend/uploads/*",
            "backend/uploads/**",
            "*.db",
            "backend/*.db",
            "*.sqlite",
            "*.sqlite3",
            "backend/*.sqlite",
            "backend/*.sqlite3",
        ],
    )
