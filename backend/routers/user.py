from fastapi import APIRouter

from services.business.user_service import get_profile, update_password, upload_avatar


router = APIRouter()
router.add_api_route("/api/user/profile", get_profile, methods=["GET"])
router.add_api_route("/api/user/password", update_password, methods=["PUT"])
router.add_api_route("/api/user/avatar", upload_avatar, methods=["POST"])
