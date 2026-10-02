from typing import Literal, Optional

from pydantic import BaseModel, Field


# bcrypt 只读入口令的前 72 字节，比这更长的部分被静默丢弃：两个前 72 字节相同的长口令
# 会互相登录。把上限放在口令进入系统的字段上，超长在进路由之前就被 422 拒绝（issue #183）。
# 取值对齐 bcrypt 的输入长度上限；登录、改口令的旧口令、新口令三处都走同一条约束。
PASSWORD_MAX_LENGTH = 72


class LoginRequest(BaseModel):
    username: str
    password: str = Field(max_length=PASSWORD_MAX_LENGTH)


class LoginResponse(BaseModel):
    token: str
    username: str


class PasswordUpdate(BaseModel):
    # old_password 也要收口：它同样喂给 bcrypt，漏掉它等于把没约束的入口留在原地。
    old_password: str = Field(max_length=PASSWORD_MAX_LENGTH)
    new_password: str = Field(max_length=PASSWORD_MAX_LENGTH)


class ChatRequest(BaseModel):
    conversation_id: Optional[str] = None
    knowledge_base_id: Optional[int] = None
    question: str
    attachments: list[dict] = Field(default_factory=list)


# 知识库名称的入口上限。列宽 String(100)（model/models.py:58）与前端输入框
# maxlength="100"（src/views/Knowledge.vue:200）本来就是同一个值，这里把第三层——入口
# 契约——也写实，三层归一到同一个数（issue #234）。
# 单位是**字符**（Python len() 与 MySQL utf8mb4 的 CHAR_LENGTH 同口径），**不是字节**；
# 文件头的 PASSWORD_MAX_LENGTH 是字节口径（bcrypt 只读前 72 字节），两者不可类推。
KNOWLEDGE_BASE_NAME_MAX_LENGTH = 100


class KnowledgeBaseRequest(BaseModel):
    # 新建与重命名共用本模型（knowledge_service.py 的 create/rename 两个入口），
    # 改这一处即覆盖 POST 与 PUT /api/knowledge-bases 两条端点。
    name: str = Field(max_length=KNOWLEDGE_BASE_NAME_MAX_LENGTH)


# 会话标题的入口上限。取值对齐 UI 现值与系统自生成标题的真实口径，而不是列宽 String(200)：
# 前端改名框 maxlength="40"（src/views/Chat.vue:12）、系统自生成标题 ≤33
# （chat_service.py:711 `title_source[:30] + ("..." if len(title_source) > 30 else "")`）。
# 于是四层单调且可解释：列宽 200（存储余量）⊇ 接口 40 = UI 40 ⊇ 自生成 33。
# 取 40 以上的值会放行 UI 既显示不全、又改不全的标题（侧栏/头部一律 truncate，见 issue #234）。
# 单位同样是**字符**（CHAR_LENGTH 口径），**不是字节**——40 个汉字是 40 字符/120 字节，
# 按字节收会把合法标题误杀。
CONVERSATION_TITLE_MAX_LENGTH = 40


class RenameRequest(BaseModel):
    title: str = Field(max_length=CONVERSATION_TITLE_MAX_LENGTH)


class MessageFeedbackRequest(BaseModel):
    # 1=赞 / -1=踩 / 0=取消（issue #250）。闭集用 Literal：非法值由框架统一产出 422，
    # 不依赖 service 层手抛，避免漏判。
    feedback: Literal[-1, 0, 1]
