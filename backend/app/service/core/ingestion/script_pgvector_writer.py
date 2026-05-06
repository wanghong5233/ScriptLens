"""ScriptLens 专属 pgvector 写入器。

针对 schema `scriptlens` 下的 6 张表（见 alembic/versions/01_init_scriptlens.py）。
与 ScholarMind 的 `pgvector_writer.PgVectorChunkWriter`（写 `rag_chunks` 公共表）
完全隔离。

事务原则：单次 ingest 用一个事务包裹 scripts+scenes+script_chunks 三表的写入；
任何步骤失败即回滚，避免半成品落库。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Iterable, List, Optional

from sqlalchemy import text

from utils.database import engine
from service.core.ingestion.script_segmenter import ParsedScene

logger = logging.getLogger(__name__)


@dataclass
class WrittenScene:
    scene_id: str
    scene_no: str
    episode_no: Optional[int]
    chunk_id: Optional[str]


class ScriptPgVectorWriter:
    """事务化写入 scripts + scenes + script_chunks。

    embedding 维度严格校验为 1024（DashScope text-embedding-v3 默认）。
    """

    EMBEDDING_DIM = 1024

    def insert_script_with_scenes(
        self,
        *,
        user_id: int,
        title: str,
        source_format: str,
        raw_storage_path: str,
        total_episodes: int,
        scenes: List[ParsedScene],
        scene_embeddings: List[Optional[List[float]]],
    ) -> tuple[str, List[WrittenScene]]:
        """一次性写入一份剧本及其所有场景与向量。

        Args:
            user_id: 上传者 ID（外键到 public.users）
            title: 剧本展示名（来自原始文件名或用户编辑）
            source_format: docx / pdf / txt / md
            raw_storage_path: 原始文件落盘路径（用于后续重新解析）
            total_episodes: 集数
            scenes: 切分后的场景列表（顺序对应 scene_embeddings）
            scene_embeddings: 每个 scene 的 embedding；None 表示该 scene 跳过向量写入

        Returns:
            (script_id, [WrittenScene...])

        Raises:
            ValueError: 当 embedding 维度不匹配
        """
        if len(scenes) != len(scene_embeddings):
            raise ValueError(
                f"scene/embedding count mismatch: {len(scenes)} vs {len(scene_embeddings)}"
            )

        script_id = str(uuid.uuid4())
        total_chars = sum(len(s.text) for s in scenes)
        started_at = time.perf_counter()

        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO scriptlens.scripts (
                        id, user_id, title, source_format, raw_storage_path,
                        total_episodes, total_scenes, total_chars, status
                    ) VALUES (
                        :id, :user_id, :title, :source_format, :raw_storage_path,
                        :total_episodes, :total_scenes, :total_chars, 'ready'
                    )
                    """
                ),
                {
                    "id": script_id,
                    "user_id": user_id,
                    "title": title,
                    "source_format": source_format,
                    "raw_storage_path": raw_storage_path,
                    "total_episodes": total_episodes,
                    "total_scenes": len(scenes),
                    "total_chars": total_chars,
                },
            )

            written: List[WrittenScene] = []
            scene_rows: List[dict] = []
            chunk_rows: List[dict] = []

            for scene, emb in zip(scenes, scene_embeddings):
                scene_id = str(uuid.uuid4())
                scene_rows.append({
                    "id": scene_id,
                    "script_id": script_id,
                    "episode_no": scene.episode_no,
                    "scene_no": scene.scene_no,
                    "scene_label": scene.scene_label or "",
                    "characters": scene.characters or [],
                    "start_line": scene.start_idx,
                    "end_line": scene.end_idx,
                    "text": scene.text,
                })

                chunk_id: Optional[str] = None
                if emb is not None:
                    if len(emb) != self.EMBEDDING_DIM:
                        raise ValueError(
                            f"embedding dim mismatch: expected {self.EMBEDDING_DIM}, "
                            f"got {len(emb)} for scene {scene.scene_no}"
                        )
                    chunk_id = str(uuid.uuid4())
                    chunk_rows.append({
                        "id": chunk_id,
                        "scene_id": scene_id,
                        "script_id": script_id,
                        "text": scene.text,
                        "embedding": _vector_literal(emb),
                        "metadata": json.dumps({
                            "episode_no": scene.episode_no,
                            "scene_no": scene.scene_no,
                            "scene_label": scene.scene_label,
                            "characters": scene.characters,
                        }, ensure_ascii=False),
                    })

                written.append(WrittenScene(
                    scene_id=scene_id,
                    scene_no=scene.scene_no,
                    episode_no=scene.episode_no,
                    chunk_id=chunk_id,
                ))

            if scene_rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO scriptlens.scenes (
                            id, script_id, episode_no, scene_no, scene_label,
                            characters, start_line, end_line, text
                        ) VALUES (
                            :id, :script_id, :episode_no, :scene_no, :scene_label,
                            :characters, :start_line, :end_line, :text
                        )
                        """
                    ),
                    scene_rows,
                )

            if chunk_rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO scriptlens.script_chunks (
                            id, scene_id, script_id, text, embedding, metadata
                        ) VALUES (
                            :id, :scene_id, :script_id, :text,
                            CAST(:embedding AS vector), CAST(:metadata AS jsonb)
                        )
                        """
                    ),
                    chunk_rows,
                )

        logger.info(
            "ScriptPgVectorWriter.write script_id=%s scenes=%s chunks=%s elapsed_ms=%s",
            script_id,
            len(scene_rows),
            len(chunk_rows),
            int((time.perf_counter() - started_at) * 1000),
        )
        return script_id, written

    def mark_failed(self, script_id: str, reason: str) -> None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE scriptlens.scripts
                    SET status='failed', failure_reason=:reason, updated_at=now()
                    WHERE id=:id
                    """
                ),
                {"id": script_id, "reason": reason[:500]},
            )

    # ============================================================
    # 两阶段写入（HTTP 入口异步 ingestion 用）：
    #   1. create_pending_script —— 上传立刻 INSERT pending，前端拿到 ID
    #   2. complete_script_with_scenes —— BackgroundTask 跑完 segment+embed
    #      后写 scenes+chunks 并 UPDATE status='ready'
    # 单阶段 insert_script_with_scenes 仍保留，dryrun 等离线场景用。
    # ============================================================

    def create_pending_script(
        self,
        *,
        user_id: int,
        title: str,
        source_format: str,
        raw_storage_path: str,
    ) -> str:
        """阶段一：先 INSERT scripts(status='pending')，立即返回 script_id。"""
        script_id = str(uuid.uuid4())
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO scriptlens.scripts (
                        id, user_id, title, source_format, raw_storage_path, status
                    ) VALUES (
                        :id, :user_id, :title, :source_format, :raw_storage_path, 'pending'
                    )
                    """
                ),
                {
                    "id": script_id,
                    "user_id": user_id,
                    "title": title,
                    "source_format": source_format,
                    "raw_storage_path": raw_storage_path,
                },
            )
        return script_id

    def update_status(self, script_id: str, status: str) -> None:
        """中间态切换（pending → parsing → indexing）。终态用 mark_failed/complete。"""
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE scriptlens.scripts
                    SET status=:status, updated_at=now()
                    WHERE id=:id
                    """
                ),
                {"id": script_id, "status": status},
            )

    def complete_script_with_scenes(
        self,
        *,
        script_id: str,
        total_episodes: int,
        scenes: List[ParsedScene],
        scene_embeddings: List[Optional[List[float]]],
    ) -> List[WrittenScene]:
        """阶段二：写 scenes + script_chunks，UPDATE scripts(status='ready') + 填统计字段。

        单事务包裹三表写入；任何异常 → 回滚 → 由调用方 mark_failed。
        """
        if len(scenes) != len(scene_embeddings):
            raise ValueError(
                f"scene/embedding count mismatch: {len(scenes)} vs {len(scene_embeddings)}"
            )

        total_chars = sum(len(s.text) for s in scenes)
        started_at = time.perf_counter()
        written: List[WrittenScene] = []
        scene_rows: List[dict] = []
        chunk_rows: List[dict] = []

        for scene, emb in zip(scenes, scene_embeddings):
            scene_id = str(uuid.uuid4())
            scene_rows.append({
                "id": scene_id,
                "script_id": script_id,
                "episode_no": scene.episode_no,
                "scene_no": scene.scene_no,
                "scene_label": scene.scene_label or "",
                "characters": scene.characters or [],
                "start_line": scene.start_idx,
                "end_line": scene.end_idx,
                "text": scene.text,
            })

            chunk_id: Optional[str] = None
            if emb is not None:
                if len(emb) != self.EMBEDDING_DIM:
                    raise ValueError(
                        f"embedding dim mismatch: expected {self.EMBEDDING_DIM}, "
                        f"got {len(emb)} for scene {scene.scene_no}"
                    )
                chunk_id = str(uuid.uuid4())
                chunk_rows.append({
                    "id": chunk_id,
                    "scene_id": scene_id,
                    "script_id": script_id,
                    "text": scene.text,
                    "embedding": _vector_literal(emb),
                    "metadata": json.dumps({
                        "episode_no": scene.episode_no,
                        "scene_no": scene.scene_no,
                        "scene_label": scene.scene_label,
                        "characters": scene.characters,
                    }, ensure_ascii=False),
                })

            written.append(WrittenScene(
                scene_id=scene_id,
                scene_no=scene.scene_no,
                episode_no=scene.episode_no,
                chunk_id=chunk_id,
            ))

        with engine.begin() as conn:
            if scene_rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO scriptlens.scenes (
                            id, script_id, episode_no, scene_no, scene_label,
                            characters, start_line, end_line, text
                        ) VALUES (
                            :id, :script_id, :episode_no, :scene_no, :scene_label,
                            :characters, :start_line, :end_line, :text
                        )
                        """
                    ),
                    scene_rows,
                )
            if chunk_rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO scriptlens.script_chunks (
                            id, scene_id, script_id, text, embedding, metadata
                        ) VALUES (
                            :id, :scene_id, :script_id, :text,
                            CAST(:embedding AS vector), CAST(:metadata AS jsonb)
                        )
                        """
                    ),
                    chunk_rows,
                )
            conn.execute(
                text(
                    """
                    UPDATE scriptlens.scripts
                    SET status='ready',
                        total_episodes=:total_episodes,
                        total_scenes=:total_scenes,
                        total_chars=:total_chars,
                        updated_at=now()
                    WHERE id=:id
                    """
                ),
                {
                    "id": script_id,
                    "total_episodes": total_episodes,
                    "total_scenes": len(scenes),
                    "total_chars": total_chars,
                },
            )

        logger.info(
            "ScriptPgVectorWriter.complete script_id=%s scenes=%s chunks=%s elapsed_ms=%s",
            script_id,
            len(scene_rows),
            len(chunk_rows),
            int((time.perf_counter() - started_at) * 1000),
        )
        return written


def _vector_literal(vec: Iterable[float]) -> str:
    return "[" + ",".join(f"{float(v):.6f}" for v in vec) + "]"
