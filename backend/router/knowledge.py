from fastapi import APIRouter

from service.knowledge_service import (
    create_knowledge_base,
    delete_knowledge,
    delete_knowledge_base,
    get_knowledge_content,
    get_knowledge_detail,
    list_knowledge,
    list_knowledge_bases,
    rename_knowledge_base,
    upload_knowledge,
)


router = APIRouter()
router.add_api_route("/api/knowledge-bases", list_knowledge_bases, methods=["GET"])
router.add_api_route("/api/knowledge-bases", create_knowledge_base, methods=["POST"])
router.add_api_route("/api/knowledge-bases/{kid}", rename_knowledge_base, methods=["PUT"])
router.add_api_route("/api/knowledge-bases/{kid}", delete_knowledge_base, methods=["DELETE"])
router.add_api_route("/api/knowledge", list_knowledge, methods=["GET"])
router.add_api_route("/api/knowledge/upload", upload_knowledge, methods=["POST"])
router.add_api_route("/api/knowledge/{fid}", delete_knowledge, methods=["DELETE"])
router.add_api_route("/api/knowledge/{fid}", get_knowledge_detail, methods=["GET"])
router.add_api_route("/api/knowledge/{fid}/content", get_knowledge_content, methods=["GET"])
