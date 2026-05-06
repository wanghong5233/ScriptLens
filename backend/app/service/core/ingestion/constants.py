from __future__ import annotations

# 需要特殊处理的多模态块类型（图像、表格、公式等）
# 注意：caption 只作为元数据存在，不作为独立类型
MULTIMODAL_LOGICAL_TYPES = {
    "figure",
    "figure_summary",
    "image",
    "table",
    "table_json",
    "chart",
    "diagram",
    "equation",
    "equation_latex",
}


def is_multimodal_metadata(metadata: dict | None) -> bool:
    """判断元数据是否属于多模态块（图/表/公式等）。
    
    只根据 logical_type 和 element_type 判断，不检查其他字段。
    """
    if not metadata:
        return False
    logical_type = str(metadata.get("logical_type") or "").lower()
    element_type = str(metadata.get("element_type") or "").lower()
    
    # 只检查类型字段，不检查内容字段
    if logical_type in MULTIMODAL_LOGICAL_TYPES:
        return True
    if element_type in MULTIMODAL_LOGICAL_TYPES:
        return True
    
    # 完全移除 fallback_keys 检查，避免误判
    # 如果一个 section 包含了 equation_latex 字段，它仍然是 section，不是多模态块
    return False

