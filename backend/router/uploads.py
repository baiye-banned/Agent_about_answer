"""上传目录的读取面（issue #186）。

修复前这里是 `app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR))`：整个目录匿名可读，
而头像文件名是 `user_{id}_{ts}`——id 是小整数、时间戳是秒级，两个分量都可预测，于是别人的
头像可以按「id × 时间窗」枚举取走。`UPLOAD_DIR` 下只有 `avatars/`（见 `paths.py`），
知识文件正文存在 `messages.content` 列里、不落盘，所以这一条路由就是 `/uploads` 的全部内容。

授权用「身份 + 归属」：先要有效 token（`get_current_user`），再要求请求的路径**逐字等于**
请求方 `users.avatar` 列里记着的那一条。判据绑定在库里的值上而不是文件名上，因此：

- 换用随机文件名之后判据不用改；
- 历史遗留的旧命名文件继续对**其本人**可读（issue 验收第 5 条选的是「保留兼容读取」，
  不搬迁、不改库，旧文件不会因为改名而变成一堆谁都读不到的死数据）；
- 顺带挡掉目录穿越：列里若被手工订正成 `../` 之类，`_avatar_file_from_path` 不会放行。

非本人的请求一律 404 而不是 403：403 等于确认「这个文件存在，只是不给你」，对一个隐私面
的修复来说那是白送的一个存在性探测口。路由层不区分「不是你的」与「不存在」。
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from model.models import User
from service.auth_service import get_current_user
from service.user_service import avatar_file_for_owner


router = APIRouter()

AVATAR_NOT_FOUND = "头像不存在"


@router.get("/uploads/avatars/{filename}")
def read_avatar(filename: str, user: User = Depends(get_current_user)):
    """把请求方自己的头像文件发回去；不是他的、或盘上已经没有了，都回 404。

    `{filename}` 是单段路径参数，含 `/` 的路径根本匹配不到这条路由（`/uploads/avatars/../x`
    在路由层就 404），能进来的只有单个文件名，再由 `avatar_file_for_owner` 与库里的值比对。
    """
    target = avatar_file_for_owner(user, filename)
    if target is None:
        raise HTTPException(404, AVATAR_NOT_FOUND)
    return FileResponse(target)
