from typing import Optional

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str


class PasswordUpdate(BaseModel):
    old_password: str
    new_password: str


class ChatRequest(BaseModel):
    conversation_id: Optional[str] = None
    knowledge_base_id: Optional[int] = None
    question: str
    attachments: list[dict] = Field(default_factory=list)


class KnowledgeBaseRequest(BaseModel):
    name: str


class RenameRequest(BaseModel):
    title: str
