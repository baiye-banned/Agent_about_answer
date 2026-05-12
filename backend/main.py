import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import REBUILD_KNOWLEDGE_INDEX_ON_STARTUP
from database.session import init_db
from paths import UPLOAD_DIR, AVATAR_DIR
from router.auth import router as auth_router
from router.chat import router as chat_router
from router.checkpointer import router as checkpointer_router
from router.knowledge import router as knowledge_router
from router.user import router as user_router
from service.knowledge_service import rebuild_existing_knowledge_index
from service.user_service import seed_default_users


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    seed_default_users()
    if REBUILD_KNOWLEDGE_INDEX_ON_STARTUP:
        rebuild_existing_knowledge_index()
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
            "chroma_data/*",
            "chroma_data/**",
            "backend/chroma_data/*",
            "backend/chroma_data/**",
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
