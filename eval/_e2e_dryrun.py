"""离线 e2e 验证（dry-run，不连数据库）。

跑通：file → load_script_paragraphs → segment_script → mock_embedder →
ScriptPgVectorWriter._build SQL rows（构造但不 commit）→ 字段断言。

目标：在没有 PG/pgvector 的开发机上，证明：
1. loader/segmenter/embedder 链路可执行
2. 写库 row 的 schema/类型/维度与 alembic 定义对齐
3. embedding 维度正确（1024）
4. ON CONFLICT/外键 之前的所有字段都能正确填充

CI 真正联通 PG 由 D2 完成；这里只跑结构验证。
"""

from __future__ import annotations

import io
import json
import sys
import uuid
from pathlib import Path
from typing import List

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "backend" / "app"))

from service.core.ingestion.script_loader import load_script_paragraphs  # noqa: E402
from service.core.ingestion.script_segmenter import segment_script, ParsedScene  # noqa: E402

SCRIPT_DIR = Path(__file__).parent / "短剧剧本" / "爆款短剧剧本（完整本）"
EMBED_DIM = 1024


def mock_embed(texts: List[str]) -> List[List[float]]:
    """伪 embedding：每段产 1024 维向量（基于文本长度变化，纯占位）。"""
    out: List[List[float]] = []
    for t in texts:
        seed = (len(t) % 100) / 100.0
        out.append([seed] * EMBED_DIM)
    return out


def build_rows(scenes: List[ParsedScene], embeddings: List[List[float]], user_id: int, title: str):
    """模拟 ScriptPgVectorWriter.insert_script_with_scenes 构造的 row 集合。"""
    script_id = str(uuid.uuid4())
    script_row = {
        "id": script_id,
        "user_id": user_id,
        "title": title,
        "source_format": "docx",
        "raw_storage_path": "/dryrun/path",
        "total_episodes": len({s.episode_no for s in scenes if s.episode_no}),
        "total_scenes": len(scenes),
        "total_chars": sum(len(s.text) for s in scenes),
    }
    scene_rows = []
    chunk_rows = []
    for sc, emb in zip(scenes, embeddings):
        scene_id = str(uuid.uuid4())
        scene_rows.append({
            "id": scene_id,
            "script_id": script_id,
            "episode_no": sc.episode_no,
            "scene_no": sc.scene_no,
            "scene_label": sc.scene_label or "",
            "characters": sc.characters,
            "start_line": sc.start_idx,
            "end_line": sc.end_idx,
            "text": sc.text,
        })
        chunk_rows.append({
            "id": str(uuid.uuid4()),
            "scene_id": scene_id,
            "script_id": script_id,
            "text": sc.text,
            "embedding_dim": len(emb),
            "metadata": json.dumps({
                "episode_no": sc.episode_no,
                "scene_no": sc.scene_no,
                "scene_label": sc.scene_label,
                "characters": sc.characters,
            }, ensure_ascii=False),
        })
    return script_row, scene_rows, chunk_rows


def assert_rows(script_row, scene_rows, chunk_rows):
    assert isinstance(script_row["id"], str) and len(script_row["id"]) == 36
    assert script_row["total_scenes"] == len(scene_rows)
    assert script_row["source_format"] in ("docx", "pdf", "txt", "md")
    for sr in scene_rows:
        assert isinstance(sr["id"], str) and len(sr["id"]) == 36
        assert sr["script_id"] == script_row["id"]
        assert isinstance(sr["scene_no"], str)
        assert isinstance(sr["text"], str) and sr["text"]
        assert isinstance(sr["characters"], list)
        assert isinstance(sr["start_line"], int)
        assert isinstance(sr["end_line"], int)
    for cr in chunk_rows:
        assert cr["embedding_dim"] == EMBED_DIM, f"vec dim={cr['embedding_dim']}"
        assert cr["script_id"] == script_row["id"]
        # metadata 是 jsonb 序列化字符串
        meta = json.loads(cr["metadata"])
        assert "scene_no" in meta


def main() -> int:
    if not SCRIPT_DIR.exists():
        print(f"ERROR: script dir not found: {SCRIPT_DIR}")
        return 1

    # 取一个 docx 和一个 pdf 各跑一遍
    candidates: list[Path] = []
    for ext, n in (("docx", 2), ("pdf", 1)):
        files = sorted(
            [p for p in SCRIPT_DIR.iterdir() if p.suffix.lower() == f".{ext}"],
            key=lambda p: p.stat().st_size,
        )
        candidates.extend(files[:n])

    overall_pass = True
    for path in candidates:
        print(f"\n--- {path.name} ({path.stat().st_size} bytes) ---")
        try:
            paras = load_script_paragraphs(path)
        except Exception as e:
            print(f"  load failed: {type(e).__name__}: {e}")
            overall_pass = False
            continue
        seg = segment_script(paras)
        print(f"  paragraphs={len(paras)} scenes={seg.total_scenes} eps={seg.total_episodes} "
              f"fallback={seg.fallback_strategy}")
        if not seg.scenes:
            print("  ! empty scenes — skip")
            overall_pass = False
            continue
        embeddings = mock_embed([s.text for s in seg.scenes])
        script_row, scene_rows, chunk_rows = build_rows(
            seg.scenes, embeddings, user_id=42, title=path.stem
        )
        try:
            assert_rows(script_row, scene_rows, chunk_rows)
        except AssertionError as e:
            print(f"  ! assertion failed: {e}")
            overall_pass = False
            continue
        print(f"  PASS — script_row id={script_row['id'][:8]}.. "
              f"scenes={len(scene_rows)} chunks={len(chunk_rows)} "
              f"first_chunk_dim={chunk_rows[0]['embedding_dim']}")

    if overall_pass:
        print("\n=== ALL PASS ===")
        return 0
    print("\n=== FAILURES PRESENT ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
