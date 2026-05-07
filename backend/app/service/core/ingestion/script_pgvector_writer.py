"""ScriptLens 专属数据库写入器。

针对 schema `scriptlens` 下的 5 张表（见 alembic/versions/01_init_scriptlens.py
+ 03_drop_script_chunks.py）。与 ScholarMind 的 `pgvector_writer.PgVectorChunkWriter`
（写 `rag_chunks` 公共表）完全隔离。

事务原则：单次 ingest 用一个事务包裹 scripts + scenes 两表的写入；
任何步骤失败即回滚，避免半成品落库。

embedding 拆除历史：v0 曾每场写一份 1024 维向量到 `script_chunks` 表用于
RAG hybrid 召回，v1 起彻底移除——理由见 `docs/04-script-pipeline.md` §4.4
（评分/证据/任务派发链路均不查向量；唯一调用方 locate_scenes_tool 用
BM25 + LLM metadata 二级兜底已足够；长剧场景下 embedding 反而成为
ingestion 的瓶颈）。

类名 `ScriptPgVectorWriter` 沿用历史名称避免外部 import 大改；
内部已不再涉及 pgvector。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import text

from utils.database import engine
from service.core.ingestion.script_segmenter import ParsedScene

logger = logging.getLogger(__name__)


@dataclass
class WrittenScene:
    scene_id: str
    scene_no: str
    episode_no: Optional[int]


class ScriptPgVectorWriter:
    """事务化写入 scripts + scenes。"""

    def insert_script_with_scenes(
        self,
        *,
        user_id: int,
        title: str,
        source_format: str,
        raw_storage_path: str,
        total_episodes: int,
        scenes: List[ParsedScene],
    ) -> tuple[str, List[WrittenScene]]:
        """一次性写入一份剧本及其所有场景。

        Args:
            user_id: 上传者 ID（外键到 public.users）
            title: 剧本展示名（来自原始文件名或用户编辑）
            source_format: docx / pdf / txt / md
            raw_storage_path: 原始文件落盘路径（用于后续重新解析）
            total_episodes: 集数
            scenes: 切分后的场景列表

        Returns:
            (script_id, [WrittenScene...])
        """
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

            written, scene_rows = self._build_scene_rows(script_id, scenes)
            self._insert_scene_rows(conn, scene_rows)

        logger.info(
            "ScriptPgVectorWriter.write script_id=%s scenes=%s elapsed_ms=%s",
            script_id,
            len(scene_rows),
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
    #   2. complete_script_with_scenes —— BackgroundTask 跑完 segment
    #      后写 scenes 并 UPDATE status='ready'
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
    ) -> List[WrittenScene]:
        """阶段二：写 scenes，UPDATE scripts(status='ready') + 填统计字段。

        单事务包裹两表写入；任何异常 → 回滚 → 由调用方 mark_failed。
        """
        total_chars = sum(len(s.text) for s in scenes)
        started_at = time.perf_counter()

        written, scene_rows = self._build_scene_rows(script_id, scenes)

        with engine.begin() as conn:
            self._insert_scene_rows(conn, scene_rows)
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
            "ScriptPgVectorWriter.complete script_id=%s scenes=%s elapsed_ms=%s",
            script_id,
            len(scene_rows),
            int((time.perf_counter() - started_at) * 1000),
        )
        return written

    # ============================================================
    # 内部：scene rows 构造 + INSERT
    # ============================================================

    @staticmethod
    def _build_scene_rows(
        script_id: str,
        scenes: List[ParsedScene],
    ) -> tuple[List[WrittenScene], List[dict]]:
        written: List[WrittenScene] = []
        scene_rows: List[dict] = []
        for scene in scenes:
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
            written.append(WrittenScene(
                scene_id=scene_id,
                scene_no=scene.scene_no,
                episode_no=scene.episode_no,
            ))
        return written, scene_rows

    @staticmethod
    def _insert_scene_rows(conn, scene_rows: List[dict]) -> None:
        if not scene_rows:
            return
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
