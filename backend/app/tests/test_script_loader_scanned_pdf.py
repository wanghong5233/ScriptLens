"""script_loader 健壮性回归：扫描件 PDF / 空文档应给出 actionable 中文错误。

回归点：
    线上用户上传 25 页全图的扫描件 PDF（《今夜星辰闪耀》1-30集短剧.pdf），
    pymupdf 提取 0 文本，旧实现抛 `ValueError: 剧本解析后段落为空：<uuid>.pdf`，
    用户看到的是"未知报错 + 内部 uuid"，无法判断该做什么。

修复：
    1. 0 文本 + 高图片占比 → ScannedPdfError，文案告诉用户去 OCR 或换 .docx
    2. 0 文本 + 低图片占比 → EmptyScriptError（空文档 / 加密 / 损坏）
    3. 错误文案绝不能包含 uuid 文件名（内部细节）

测试策略：
    用 monkeypatch 替换 fitz.open 返回的对象，把 page 的 `get_text` / `get_images`
    替换成 stub —— 这样不用构造真实 PDF（PyMuPDF 用真实 PNG fixture 在 CI 上经常
    踩中文字体 / 图片解码兼容性问题，不稳）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest

from service.core.ingestion import script_loader
from service.core.ingestion.script_loader import (
    EmptyScriptError,
    ScannedPdfError,
    _load_pdf,
)


class _StubPage:
    """模拟 fitz.Page —— 只暴露 _load_pdf 用到的两个方法。"""

    def __init__(self, *, text: str, has_image: bool) -> None:
        self._text = text
        self._has_image = has_image

    def get_text(self, _kind: str) -> str:
        return self._text

    def get_images(self, full: bool = False) -> List[tuple]:  # noqa: ARG002
        # fitz 返回 xref tuple 列表；这里只需要长度 > 0 / == 0 的语义
        return [(1, 0, 0, 0, 0, 0, 0)] if self._has_image else []


class _StubDoc:
    """模拟 fitz.Document iterable + context manager 风格。"""

    def __init__(self, pages: List[_StubPage]) -> None:
        self._pages = pages

    def __iter__(self):
        return iter(self._pages)

    def close(self) -> None:
        pass


def _patch_fitz(monkeypatch: pytest.MonkeyPatch, pages: List[_StubPage]) -> None:
    """让 `_load_pdf` 看到指定的 page stub 序列。"""
    import fitz

    def _fake_open(_path: str) -> _StubDoc:
        return _StubDoc(pages)

    monkeypatch.setattr(fitz, "open", _fake_open)


@pytest.fixture
def _dummy_path(tmp_path: Path) -> Path:
    """_load_pdf 只把 path 传给 fitz.open（已被 stub），文件不需要真的存在。"""
    return tmp_path / "any.pdf"


def test_scanned_pdf_raises_scanned_pdf_error_with_user_friendly_text(
    monkeypatch: pytest.MonkeyPatch, _dummy_path: Path
) -> None:
    """复刻线上现场：25 页 / 每页 0 文本 + 1 张图 → ScannedPdfError。"""
    pages = [_StubPage(text="", has_image=True) for _ in range(25)]
    _patch_fitz(monkeypatch, pages)

    with pytest.raises(ScannedPdfError) as exc:
        _load_pdf(_dummy_path)
    msg = str(exc.value)
    assert "扫描件" in msg
    # 必须提供可操作建议（OCR 或换格式）
    assert ("OCR" in msg) or ("docx" in msg) or ("txt" in msg)
    # 严禁泄露任何 storage uuid 文件名（内部细节）
    assert _dummy_path.name not in msg
    assert ".pdf" not in msg.lower()


def test_empty_pdf_raises_empty_script_error_not_scanned(
    monkeypatch: pytest.MonkeyPatch, _dummy_path: Path
) -> None:
    """0 文本 + 0 图（空白文档 / 加密占位）→ EmptyScriptError（不应误判为扫描件）。"""
    pages = [_StubPage(text="", has_image=False) for _ in range(3)]
    _patch_fitz(monkeypatch, pages)

    with pytest.raises(EmptyScriptError) as exc:
        _load_pdf(_dummy_path)
    msg = str(exc.value)
    assert "没有任何文字" in msg
    assert _dummy_path.name not in msg


def test_text_pdf_loads_successfully_without_being_misdetected(
    monkeypatch: pytest.MonkeyPatch, _dummy_path: Path
) -> None:
    """文本型 PDF：每页有正常文字 + 偶尔配图，不能被新检测逻辑误伤。"""
    pages = [
        _StubPage(text="第1集\n1-1 客厅 日内", has_image=False),
        _StubPage(text="张三：你好。\n李四：你也好。", has_image=True),
        _StubPage(text="第2集\n2-1 街头 日外", has_image=False),
    ]
    _patch_fitz(monkeypatch, pages)

    paragraphs = _load_pdf(_dummy_path)
    assert paragraphs
    assert any("第1集" in p for p in paragraphs)
    assert any("张三：你好。" in p for p in paragraphs)


def test_mixed_pdf_with_low_image_ratio_still_loads(
    monkeypatch: pytest.MonkeyPatch, _dummy_path: Path
) -> None:
    """混合 PDF：大部分页有文字（即使只有 1 行），不能被判扫描件。"""
    pages = [_StubPage(text="第N集 短文本", has_image=True) for _ in range(10)]
    _patch_fitz(monkeypatch, pages)

    paragraphs = _load_pdf(_dummy_path)
    assert len(paragraphs) == 10
