from fastapi import APIRouter

from services.base.trace_service import list_checkpointer_threads


router = APIRouter()
router.add_api_route("/api/checkpointer/threads", list_checkpointer_threads, methods=["GET"])
