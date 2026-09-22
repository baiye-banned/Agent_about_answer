from typing import Optional

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


class KnowledgeBaseRequest(BaseModel):
    name: str


class RenameRequest(BaseModel):
    title: str
