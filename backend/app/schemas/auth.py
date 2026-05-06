"""Authentication request schemas."""

from pydantic import BaseModel


class LoginRequest(BaseModel):
    """Login request payload."""

    username: str
    password: str


class RegisterRequest(BaseModel):
    """Registration request payload."""

    username: str
    password: str


class STSTokenRequest(BaseModel):
    """STS token request payload."""

    appid: str
    accessKey: str
