from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from utils.database import get_db

HTTP_KB_NOT_FOUND = 461
HTTP_INTERNAL_SERVER_ERROR = status.HTTP_500_INTERNAL_SERVER_ERROR


def insert_knowledgebase(user_id: str, file_name: str) -> None:
    db = next(get_db())
    try:
        db.execute(
            text(
                """
                INSERT INTO knowledgebases (user_id, file_name)
                VALUES (:user_id, :file_name)
                """
            ),
            {
                "user_id": user_id,
                "file_name": file_name,
            },
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise RuntimeError(f"Failed to insert into knowledgebases: {exc}") from exc
    finally:
        db.close()


def verify_user_knowledgebase(user_id: str) -> None:
    db = next(get_db())
    try:
        query_result = db.execute(
            text("SELECT id FROM knowledgebases WHERE user_id = :user_id LIMIT 1"),
            {"user_id": user_id},
        ).fetchone()

        if not query_result:
            raise HTTPException(
                status_code=HTTP_KB_NOT_FOUND,
                detail="You do not have your own knowledge base yet.",
            )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=HTTP_INTERNAL_SERVER_ERROR,
            detail=f"Database operation failed: {exc}",
        ) from exc
    finally:
        db.close()
