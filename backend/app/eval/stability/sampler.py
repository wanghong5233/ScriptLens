from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


SplitName = Literal["dev", "test"]


@dataclass
class StabilitySample:
    split: SplitName
    script_id: str
    script_path: str
    drama_genre_slice: dict[str, str]
    episodes_sampled: list[int]


def _guess_genre_slice_from_name(name: str) -> dict[str, str]:
    lower = name.lower()
    world_setting = "modern_urban"
    protagonist = "unclear"
    gender = "unclear"

    if any(k in name for k in ("战神", "赘婿", "神医")):
        protagonist = "war_god_return"
        gender = "male_lead"
    if "重生" in name:
        protagonist = "reborn_revenge"
    if any(k in name for k in ("甜", "宠", "总裁")):
        protagonist = "sweet_pet"
    if any(k in name for k in ("宫", "王", "太子", "朝")):
        world_setting = "ancient_palace"
    if any(k in name for k in ("仙", "玄", "修真")):
        world_setting = "xianxia"
    if any(k in name for k in ("校园", "校花")):
        world_setting = "school"
    if any(k in lower for k in ("女", "千金", "皇后", "王妃")) and gender == "unclear":
        gender = "female_lead"
    if gender == "unclear" and protagonist in {"war_god_return", "son_in_law_counter"}:
        gender = "male_lead"

    return {
        "gender_axis": gender,
        "world_setting": world_setting,
        "protagonist_archetype": protagonist,
    }


def _list_script_files(source_dir: str) -> list[Path]:
    root = Path(source_dir)
    if not root.exists():
        return []
    exts = {".docx", ".txt", ".md", ".pdf"}
    files = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in exts]
    files.sort(key=lambda p: p.name)
    return files


def sample_split(
    split: SplitName,
    n_scripts: int = 10,
    n_episodes_per_script: int = 5,
    source_dir: str = "ScriptLens/eval/ai漫剧剧本数据集/完整本",
    seed: int = 42,
    script_ids: list[str] | None = None,
) -> list[StabilitySample]:
    files = _list_script_files(source_dir)
    if script_ids is not None:
        file_by_stem = {path.stem: path for path in files}
        chosen_ids = script_ids[:n_scripts] if n_scripts > 0 else list(script_ids)
        out: list[StabilitySample] = []
        for script_id in chosen_ids:
            file_path = file_by_stem.get(script_id)
            genre_slice = _guess_genre_slice_from_name(script_id)
            out.append(
                StabilitySample(
                    split=split,
                    script_id=script_id,
                    script_path=str(file_path) if file_path is not None else "",
                    drama_genre_slice=genre_slice,
                    episodes_sampled=list(range(1, n_episodes_per_script + 1)),
                )
            )
        return out

    if not files:
        return []

    rng = random.Random(seed)
    shuffled = files[:]
    rng.shuffle(shuffled)

    # dev/test deterministic partition with no overlap
    dev_pool = shuffled[: max(n_scripts * 2, n_scripts)]
    test_pool = shuffled[max(n_scripts * 2, n_scripts) :]
    chosen = dev_pool[:n_scripts] if split == "dev" else test_pool[:n_scripts]
    if len(chosen) < n_scripts:
        # Fallback when dataset size is smaller than expected.
        chosen = shuffled[:n_scripts]

    samples: list[StabilitySample] = []
    for file in chosen:
        script_id = file.stem
        genre_slice = _guess_genre_slice_from_name(file.stem)
        episodes = list(range(1, n_episodes_per_script + 1))
        samples.append(
            StabilitySample(
                split=split,
                script_id=script_id,
                script_path=str(file),
                drama_genre_slice=genre_slice,
                episodes_sampled=episodes,
            )
        )
    return samples
