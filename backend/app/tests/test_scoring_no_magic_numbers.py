"""scoring v4 零魔法数字守门员。

扫描 service/scoring/dimensions/*.py 和 service/scoring/aggregator.py，
确认它们不包含：
1. 裸阈值字面量（float >= 0.05 且不是 0.0 / 1.0 这种归一化常量）
2. 中文业务关键词字符串（如 "重生" / "战神"——这些必须只在 YAML 里）

设计依据：docs/2026-05-31-投资决策评分框架-v4.md §六 零魔法数字策略
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1] / "service" / "scoring"

# 允许的"无业务含义"裸数字：0/1（索引/clamp）、0.5（中位点权重）、
# 1.0（归一化上限）、-1（rfind 等），以及 2/3（rfind 偏移、splitlines 索引）等
_ALLOWED_INT_LITERALS: frozenset[int] = frozenset({-1, 0, 1, 2, 3, 5, 10, 100, 160, 200})
_ALLOWED_FLOAT_LITERALS: frozenset[float] = frozenset({0.0, 0.5, 1.0})

# 允许出现裸字符串的语境：docstring / log message / error message
# 我们只拦截"显式中文业务关键词列表"，因此具体策略是：
# 扫描中文连续字符的 string literal，若文件名属于敏感模块，则 fail。
_CN_BUSINESS_HINTS: frozenset[str] = frozenset({
    "重生", "穿越", "战神", "霸总", "甜宠", "复仇", "弃妇", "豪门", "宫斗",
    "翻身", "扫地出门",
})


def _iter_target_files() -> list[Path]:
    files = []
    for sub in ("dimensions", ""):
        if sub:
            files.extend((_ROOT / sub).glob("*.py"))
        else:
            for name in ("aggregator.py", "confidence.py"):
                p = _ROOT / name
                if p.exists():
                    files.append(p)
    return [f for f in files if f.name != "__init__.py"]


def _check_file_for_magic_floats(path: Path) -> list[str]:
    """返回该文件中触发"裸 float 阈值"违规的描述列表。"""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    violations: list[str] = []

    for node in ast.walk(tree):
        # 拦截 BinOp / Compare 中右边或左边出现的非允许 float
        if isinstance(node, ast.Constant):
            if isinstance(node.value, float):
                if node.value not in _ALLOWED_FLOAT_LITERALS:
                    violations.append(
                        f"{path.name}:{node.lineno} 出现裸 float {node.value} —— "
                        f"业务阈值必须放 rubric YAML"
                    )
            elif isinstance(node.value, int):
                if node.value not in _ALLOWED_INT_LITERALS:
                    violations.append(
                        f"{path.name}:{node.lineno} 出现裸 int {node.value} —— "
                        f"业务阈值必须放 rubric YAML 或 _ALLOWED_INT_LITERALS 白名单"
                    )
    return violations


def _check_file_for_business_keywords(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            for kw in _CN_BUSINESS_HINTS:
                if kw in v:
                    # 排除：docstring（模块/函数首句）会出现"重生 / 穿越"等举例 —— 这里
                    # 只拦截短字符串（< 30 字），因为业务关键词列表一般是短词
                    if len(v) <= 30:
                        violations.append(
                            f"{path.name}:{node.lineno} 出现业务关键词 {kw!r} 短字符串 "
                            f"({v!r}) —— 业务关键词必须放 signals/_keywords.yaml"
                        )
                        break
    return violations


def test_no_magic_floats_in_dimension_modules() -> None:
    """scoring/dimensions/*.py 和 aggregator.py / confidence.py 内禁止裸 float / int 阈值。"""
    all_violations: list[str] = []
    for f in _iter_target_files():
        all_violations.extend(_check_file_for_magic_floats(f))
    assert not all_violations, "发现裸阈值字面量：\n" + "\n".join(all_violations)


def test_no_business_keywords_in_dimension_modules() -> None:
    """scoring/dimensions/*.py 内禁止业务关键词字面量。

    业务关键词（如"重生""穿越"）必须只出现在 signals/_keywords.yaml。
    """
    all_violations: list[str] = []
    for f in (_ROOT / "dimensions").glob("*.py"):
        if f.name == "__init__.py":
            continue
        all_violations.extend(_check_file_for_business_keywords(f))
    assert not all_violations, "发现业务关键词裸字符串：\n" + "\n".join(all_violations)


if __name__ == "__main__":
    # 调试运行：python tests/test_scoring_no_magic_numbers.py
    pytest.main([__file__, "-v"])
