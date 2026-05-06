"""User service for authentication and utilities."""

from __future__ import annotations

import httpx
from fastapi import HTTPException, status

from exceptions.auth import AuthError
from service.auth import authenticate, register_user
from schemas.auth import LoginRequest, RegisterRequest, STSTokenRequest


class UserService:
    """Handle user authentication actions."""

    def login(self, request: LoginRequest) -> dict:
        """Authenticate and return token payload."""
        try:
            token = authenticate(request.username, request.password)
            return {"access_token": token, "token_type": "bearer"}
        except AuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(exc),
            ) from exc

    def register(self, request: RegisterRequest) -> dict:
        """Register a new user."""
        try:
            register_user(request.username, request.password)
            return {"message": "User registered successfully"}
        except AuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(exc),
            ) from exc

    async def get_sts_token(self, request: STSTokenRequest) -> dict:
        """Fetch STS token from ByteDance."""
        try:
            headers = {
                "Authorization": f"Bearer; {request.accessKey}",
                "Content-Type": "application/json",
            }
            body = {"appid": request.appid, "duration": 300}
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://openspeech.bytedance.com/api/v1/sts/token",
                    headers=headers,
                    json=body,
                    timeout=30.0,
                )
                return response.json()
        except httpx.TimeoutException as exc:
            raise HTTPException(
                status_code=status.HTTP_408_REQUEST_TIMEOUT,
                detail="Request timeout when calling STS token API",
            ) from exc
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Error calling STS token API: {str(exc)}",
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Internal server error: {str(exc)}",
            ) from exc
