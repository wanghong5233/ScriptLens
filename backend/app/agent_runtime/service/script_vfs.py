"""ScriptVFS: 把 scriptlens.scenes 映射成虚拟文件系统。

核心契约：
- 对外只暴露 file_path（`scenes/E03-S005.txt`）与文本内容
- scene_id 仅作为后端内部主键使用
- 兼容历史输入：调用方传 scene_id 时也能自动归一化到 file_path
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Dict, List, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Engine

from utils.database import engine as default_engine


class ScriptVFSError(RuntimeError):
    """ScriptVFS 基类异常。"""


class ScriptVFSPathError(ScriptVFSError):
    """虚拟路径格式错误。"""


class ScriptVFSNotFoundError(ScriptVFSError):
    """path 或 scene_id 在当前 script 下不存在。"""


@dataclass(frozen=True)
class ScriptVFSFile:
    file_path: str
    scene_id: str
    episode_no: int
    scene_no: int
    scene_label: str | None
    text: str = ""


class ScriptVFS:
    """script_id 作用域内的虚拟文件系统。"""

    _PATH_RE = re.compile(r"^scenes/E(?P<episode>\d{2,})-S(?P<scene>\d{3})\.txt$")
    _UUID_RE = re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
    )
    _SCENE_NO_SUFFIX_RE = re.compile(r"(\d+)\s*$")

    def __init__(self, *, script_id: str, db_engine: Engine = default_engine) -> None:
        sid = str(script_id or "").strip()
        if not sid:
            raise ScriptVFSPathError("script_id is required")
        self.script_id = sid
        self._engine = db_engine
        self._path_to_scene: Dict[str, str] = {}
        self._scene_to_path: Dict[str, str] = {}
        self._index_loaded = False

    @classmethod
    def normalize_path(cls, file_path: str) -> str:
        return str(file_path or "").strip().replace("\\", "/")

    @classmethod
    def is_vfs_path(cls, file_path: str) -> bool:
        normalized = cls.normalize_path(file_path)
        return bool(cls._PATH_RE.match(normalized))

    @classmethod
    def build_file_path(cls, episode_no: int, scene_no: int) -> str:
        ep = int(episode_no)
        sn = int(scene_no)
        if ep < 0:
            raise ScriptVFSPathError(f"episode_no must be >= 0: {ep}")
        if sn < 0 or sn > 999:
            raise ScriptVFSPathError(f"scene_no out of range [0,999]: {sn}")
        return f"scenes/E{ep:02d}-S{sn:03d}.txt"

    @classmethod
    def parse_file_path(cls, file_path: str) -> tuple[int, int]:
        normalized = cls.normalize_path(file_path)
        match = cls._PATH_RE.match(normalized)
        if not match:
            raise ScriptVFSPathError(
                f"invalid ScriptVFS path: {normalized!r}, expected scenes/E03-S005.txt"
            )
        return int(match.group("episode")), int(match.group("scene"))

    def _fetch_scene_rows(self, *, include_text: bool) -> List[Mapping[str, Any]]:
        text_col = "s.text" if include_text else "NULL::text AS text"
        sql = text(
            f"""
            SELECT s.id::text AS scene_id,
                   COALESCE(s.episode_no, 0)::int AS episode_no,
                   COALESCE(s.scene_no::text, '') AS scene_no_raw,
                   COALESCE(s.start_line, 0)::int AS start_line,
                   s.scene_label,
                   {text_col}
            FROM scriptlens.scenes s
            WHERE s.script_id::text = :sid
            ORDER BY s.episode_no NULLS LAST, s.scene_no NULLS LAST, s.start_line NULLS LAST, s.id
            """
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sql, {"sid": self.script_id}).mappings().all()
        return rows

    @classmethod
    def _parse_scene_index(cls, scene_no_raw: str) -> int | None:
        raw = str(scene_no_raw or "").strip()
        if not raw:
            return None
        if raw.isdigit():
            value = int(raw)
            return value if value > 0 else None
        match = cls._SCENE_NO_SUFFIX_RE.search(raw)
        if not match:
            return None
        value = int(match.group(1))
        return value if value > 0 else None

    def _build_index_from_rows(self, rows: Sequence[Mapping[str, Any]]) -> None:
        path_to_scene: Dict[str, str] = {}
        scene_to_path: Dict[str, str] = {}
        used_scene_no_by_episode: Dict[int, set[int]] = {}
        next_scene_no_by_episode: Dict[int, int] = {}
        for row in rows:
            scene_id = str(row["scene_id"] or "").strip()
            if not scene_id:
                raise ScriptVFSError("encountered empty scene_id while building ScriptVFS index")
            episode_no = int(row["episode_no"] or 0)
            used_scene_no = used_scene_no_by_episode.setdefault(episode_no, set())
            parsed_scene_no = self._parse_scene_index(str(row.get("scene_no_raw") or ""))
            scene_no = parsed_scene_no if parsed_scene_no is not None else 0
            if scene_no <= 0 or scene_no in used_scene_no:
                scene_no = max(1, next_scene_no_by_episode.get(episode_no, 1))
                while scene_no in used_scene_no and scene_no <= 999:
                    scene_no += 1
            if scene_no > 999:
                raise ScriptVFSError(
                    f"too many scenes in episode {episode_no} for script {self.script_id}"
                )
            used_scene_no.add(scene_no)
            next_scene_no_by_episode[episode_no] = max(
                next_scene_no_by_episode.get(episode_no, 1),
                scene_no + 1,
            )
            file_path = self.build_file_path(episode_no=episode_no, scene_no=scene_no)
            conflict_scene = path_to_scene.get(file_path)
            if conflict_scene and conflict_scene != scene_id:
                raise ScriptVFSError(
                    f"duplicate ScriptVFS path {file_path} in script {self.script_id}: "
                    f"{conflict_scene} vs {scene_id}"
                )
            path_to_scene[file_path] = scene_id
            scene_to_path[scene_id] = file_path
        self._path_to_scene = path_to_scene
        self._scene_to_path = scene_to_path
        self._index_loaded = True

    def _ensure_index(self) -> None:
        if self._index_loaded:
            return
        rows = self._fetch_scene_rows(include_text=False)
        self._build_index_from_rows(rows)

    def list_files(self) -> List[ScriptVFSFile]:
        rows = self._fetch_scene_rows(include_text=False)
        self._build_index_from_rows(rows)
        files: List[ScriptVFSFile] = []
        for row in rows:
            scene_id = str(row["scene_id"])
            file_path = self._scene_to_path[scene_id]
            _, scene_no = self.parse_file_path(file_path)
            files.append(
                ScriptVFSFile(
                    file_path=file_path,
                    scene_id=scene_id,
                    episode_no=int(row["episode_no"] or 0),
                    scene_no=scene_no,
                    scene_label=(str(row["scene_label"]).strip() if row["scene_label"] else None),
                    text="",
                )
            )
        return files

    def resolve_scene_id(self, file_path_or_scene_id: str) -> str:
        raw = self.normalize_path(file_path_or_scene_id)
        if not raw:
            raise ScriptVFSPathError("path or scene_id is empty")
        self._ensure_index()
        if raw in self._path_to_scene:
            return self._path_to_scene[raw]
        if self._UUID_RE.match(raw):
            if raw in self._scene_to_path:
                return raw
            # 索引可能过期（新增场景），重建一次再判断
            rows = self._fetch_scene_rows(include_text=False)
            self._build_index_from_rows(rows)
            if raw in self._scene_to_path:
                return raw
            raise ScriptVFSNotFoundError(
                f"scene_id {raw} not found in script {self.script_id}"
            )
        if self.is_vfs_path(raw):
            rows = self._fetch_scene_rows(include_text=False)
            self._build_index_from_rows(rows)
            if raw in self._path_to_scene:
                return self._path_to_scene[raw]
            raise ScriptVFSNotFoundError(
                f"file_path {raw} not found in script {self.script_id}"
            )
        raise ScriptVFSPathError(
            f"unsupported path format {raw!r}, expected scene UUID or scenes/E03-S005.txt"
        )

    def resolve_file_path(self, scene_id: str) -> str:
        sid = str(scene_id or "").strip()
        if not sid:
            raise ScriptVFSPathError("scene_id is empty")
        self._ensure_index()
        path = self._scene_to_path.get(sid)
        if path:
            return path
        rows = self._fetch_scene_rows(include_text=False)
        self._build_index_from_rows(rows)
        path = self._scene_to_path.get(sid)
        if path:
            return path
        raise ScriptVFSNotFoundError(
            f"scene_id {sid} not found in script {self.script_id}"
        )

    def coerce_file_path(self, file_path_or_scene_id: str) -> str:
        scene_id = self.resolve_scene_id(file_path_or_scene_id)
        return self.resolve_file_path(scene_id)

    def snapshot_all(self) -> Dict[str, str]:
        rows = self._fetch_scene_rows(include_text=True)
        self._build_index_from_rows(rows)
        snapshot: Dict[str, str] = {}
        for row in rows:
            scene_id = str(row["scene_id"])
            file_path = self._scene_to_path[scene_id]
            snapshot[file_path] = str(row["text"] or "")
        return snapshot

    def read(self, file_path_or_scene_id: str) -> str:
        scene_id = self.resolve_scene_id(file_path_or_scene_id)
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT s.text
                    FROM scriptlens.scenes s
                    WHERE s.script_id::text = :script_id
                      AND s.id::text = :scene_id
                    """
                ),
                {"script_id": self.script_id, "scene_id": scene_id},
            ).first()
        if not row:
            raise ScriptVFSNotFoundError(
                f"scene_id {scene_id} not found in script {self.script_id}"
            )
        return str(row[0] or "")

    def write(self, file_path_or_scene_id: str, content: str) -> str:
        scene_id = self.resolve_scene_id(file_path_or_scene_id)
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE scriptlens.scenes
                       SET text = :content
                     WHERE script_id::text = :script_id
                       AND id::text = :scene_id
                    """
                ),
                {"content": content, "script_id": self.script_id, "scene_id": scene_id},
            )
        if result.rowcount == 0:
            raise ScriptVFSNotFoundError(
                f"scene_id {scene_id} not found in script {self.script_id}"
            )
        return scene_id
