from typing import Any, Dict, Optional

class APIException(Exception):
    """
    所有自定义API异常的基类。
    提供了标准化的结构，包含状态码、错误码和错误信息。
    """
    def __init__(
        self,
        status_code: int = 500,
        code: int = 50000,
        message: str = "Internal Server Error",
        data: Any = None,
        headers: Optional[Dict[str, Any]] = None,
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.data = data
        self.headers = headers
        super().__init__(message)

    def to_dict(self) -> Dict[str, Any]:
        """将异常信息转换为字典格式，用于JSON响应。"""
        return {
            "code": self.code,
            "message": self.message,
            "data": self.data
        }

class ResourceNotFoundException(APIException):
    def __init__(self, message: str = "Resource Not Found", data: Any = None):
        super().__init__(status_code=404, code=40400, message=message, data=data)

class PermissionDeniedException(APIException):
    def __init__(self, message: str = "Permission Denied", data: Any = None):
        super().__init__(status_code=403, code=40300, message=message, data=data)

class ModelNotFoundError(APIException):
    """
    当无法在配置的模型路径中找到指定的模型文件时引发。
    """
    def __init__(self, model_name: str, message: Optional[str] = None):
        super().__init__(
            status_code=404,
            code=40401,
            message=message or f"Model '{model_name}' not found.",
        )

class VectorStoreError(APIException):
    """
    当向量数据库操作失败时抛出。
    """
    def __init__(self, operation: str, message: Optional[str] = None):
        super().__init__(
            status_code=500,
            code=50002,
            message=message or f"Vector store operation '{operation}' failed.",
        )

class InvalidAPIKeyError(APIException):
    """
    当提供的API密钥无效或权限不足时抛出。
    """
    def __init__(self, message: str = "Invalid or missing API key."):
        super().__init__(
            status_code=401,
            code=40101,
            message=message,
            headers={"WWW-Authenticate": "Bearer"},
        )
