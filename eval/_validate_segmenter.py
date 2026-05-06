"""验证 script_segmenter 在真实剧本上的切分质量。

跑完 5 docx + 3 pdf，输出：
1. 每个文件：总场景数、识别到的集数、字符总量、警告
2. 每个文件抽样 3 个 scene 看字段是否正确
3. 警示性指标：metadata 块占比、平均 scene 字符数
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 让 segmenter 可被 import
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "backend" / "app"))

from docx import Document as DocxDocument
import fitz

from service.core.ingestion.script_segmenter import segment_script  # noqa: E402

SCRIPT_DIR = Path(__file__).parent / "短剧剧本" / "爆款短剧剧本（完整本）"
OUT = Path(__file__).parent / "_validate_segmenter_out.txt"


def load_docx(path: Path) -> list[str]:
    doc = DocxDocument(str(path))
    return [p.text.strip() for p in doc.paragraphs if p.text.strip()]


def load_pdf(path: Path) -> list[str]:
    doc = fitz.open(str(path))
    out: list[str] = []
    for page in doc:
        text = page.get_text("text")
        for line in text.splitlines():
            line = line.strip()
            if line:
                out.append(line)
    doc.close()
    return out


def report(path: Path, paragraphs: list[str], lines: list[str]) -> None:
    lines.append(f"\n========== {path.name} ==========")
    lines.append(f"file size: {path.stat().st_size} bytes, paragraphs: {len(paragraphs)}")
    res = segment_script(paragraphs)
    lines.append(f"metadata_block_chars: {len(res.metadata_block)}")
    lines.append(f"total_scenes: {res.total_scenes}")
    lines.append(f"total_episodes: {res.total_episodes}")
    lines.append(f"total_chars: {res.total_chars}")
    lines.append(f"fallback: {res.fallback_strategy}")
    lines.append(f"warnings: {res.parsing_warnings}")
    if res.scenes:
        avg = res.total_chars / len(res.scenes)
        lines.append(f"avg_scene_chars: {avg:.0f}")
        lines.append(f"min_scene_chars: {min(len(s.text) for s in res.scenes)}")
        lines.append(f"max_scene_chars: {max(len(s.text) for s in res.scenes)}")

    # 抽样 3 个：第 0、中、倒数第 1
    if res.scenes:
        sample_idxs = [0, len(res.scenes) // 2, len(res.scenes) - 1]
        for k, idx in enumerate(sample_idxs):
            s = res.scenes[idx]
            lines.append(f"--- sample {k} (scene #{idx}) ---")
            lines.append(f"  episode_no={s.episode_no}  scene_no={s.scene_no}")
            lines.append(f"  scene_label={s.scene_label[:80]!r}")
            lines.append(f"  characters={s.characters[:8]}")
            lines.append(f"  text_chars={len(s.text)}  start={s.start_idx} end={s.end_idx}")
            text_preview = s.text[:200].replace("\n", " | ")
            lines.append(f"  text_preview: {text_preview}")


def main() -> None:
    out: list[str] = []
    if not SCRIPT_DIR.exists():
        out.append(f"ERROR: {SCRIPT_DIR} not found")
        OUT.write_text("\n".join(out), encoding="utf-8")
        return

    docx_files = sorted(
        [p for p in SCRIPT_DIR.iterdir() if p.suffix.lower() == ".docx"],
        key=lambda p: p.stat().st_size,
    )[:5]
    pdf_files = sorted(
        [p for p in SCRIPT_DIR.iterdir() if p.suffix.lower() == ".pdf"],
        key=lambda p: p.stat().st_size,
    )[:3]

    for p in docx_files:
        try:
            paras = load_docx(p)
        except Exception as e:
            out.append(f"\n=== {p.name} ===\nload failed: {type(e).__name__}: {e}")
            continue
        report(p, paras, out)

    for p in pdf_files:
        try:
            paras = load_pdf(p)
        except Exception as e:
            out.append(f"\n=== {p.name} ===\nload failed: {type(e).__name__}: {e}")
            continue
        report(p, paras, out)

    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
