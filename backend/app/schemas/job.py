from typing import Optional, Any
from datetime import datetime
from pydantic import BaseModel, Field, field_serializer, computed_field


class JobBase(BaseModel):
    type: str = Field(..., description="Job 类型")
    status: str = Field(..., description="Job 状态")
    progress: int = 0
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    error: Optional[str] = None
    payload: Optional[Any] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class JobCreate(BaseModel):
    knowledge_base_id: int
    type: str
    payload: Optional[Any] = None


class JobInDB(JobBase):
    id: int
    user_id: int
    knowledge_base_id: int

    class Config:
        from_attributes = True
        
    @computed_field
    @property
    def details(self) -> Optional[list]:
        """从 payload.resultDetails 或 payload.documents 提取 details（兼容旧数据）"""
        if self.payload and isinstance(self.payload, dict):
            # 优先使用 resultDetails（新格式）
            result = self.payload.get("resultDetails")
            if result is not None:
                return result
            # 回退到 documents（旧格式或初始 payload）
            return self.payload.get("documents")
        return None


