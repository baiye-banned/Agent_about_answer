from fastapi import APIRouter

from service.auth_service import login, logout


router = APIRouter()
router.add_api_route("/api/auth/login", login, methods=["POST"])
router.add_api_route("/api/auth/logout", logout, methods=["POST"])
