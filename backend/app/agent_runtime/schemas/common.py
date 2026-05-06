"""
通用数据结构定义
定义 Agent 服务中使用的通用数据结构
"""
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Union, TYPE_CHECKING
from enum import Enum

if TYPE_CHECKING:
    from typing import ForwardRef


class ChangeType(str, Enum):
    """文件变更类型"""
    INSERT = "insert"
    REPLACE = "replace"
    DELETE = "delete"


@dataclass
class Position:
    """
    位置信息（行号 + 列号）
    用于定位 LaTeX 文档中的具体位置
    
    设计文档要求：
    - line: 行号（从 1 开始）
    - character: 列号（从 0 开始）
    """
    line: int  # 行号（从 1 开始）
    character: int  # 列号（从 0 开始）
    
    def __post_init__(self):
        """验证位置数据"""
        if self.line < 1:
            raise ValueError(f"line must be >= 1, got {self.line}")
        if self.character < 0:
            raise ValueError(f"character must be >= 0, got {self.character}")
    
    def __lt__(self, other: "Position") -> bool:
        """比较位置（用于排序）"""
        if not isinstance(other, Position):
            return NotImplemented
        if self.line != other.line:
            return self.line < other.line
        return self.character < other.character
    
    def __le__(self, other: "Position") -> bool:
        """小于等于"""
        return self < other or self == other
    
    def __gt__(self, other: "Position") -> bool:
        """大于"""
        if not isinstance(other, Position):
            return NotImplemented
        return not (self <= other)
    
    def __ge__(self, other: "Position") -> bool:
        """大于等于"""
        return self > other or self == other
    
    def to_dict(self) -> Dict[str, int]:
        """转换为字典"""
        return {"line": self.line, "character": self.character}
    
    @classmethod
    def from_dict(cls, data: Dict[str, int]) -> "Position":
        """从字典创建"""
        return cls(line=data["line"], character=data["character"])


@dataclass
class TextRange:
    """
    文本范围（起始位置 + 结束位置）
    用于表示选中的文本范围
    """
    start: Position
    end: Position
    
    def __post_init__(self):
        """验证文本范围"""
        if self.start > self.end:
            raise ValueError(
                f"start position ({self.start.line}:{self.start.character}) "
                f"must be <= end position ({self.end.line}:{self.end.character})"
            )
    
    def is_valid(self) -> bool:
        """检查范围是否有效"""
        return self.start <= self.end
    
    def to_dict(self) -> Dict[str, Dict[str, int]]:
        """转换为字典"""
        return {
            "start": self.start.to_dict(),
            "end": self.end.to_dict()
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Dict[str, int]]) -> "TextRange":
        """从字典创建"""
        return cls(
            start=Position.from_dict(data["start"]),
            end=Position.from_dict(data["end"])
        )


@dataclass
class FileChange:
    """
    文件变更信息
    表示对文件的一次编辑操作
    
    设计文档要求（API 响应格式）：
    {
      "file": "sections/related_work.tex",
      "position": { "line": 15, "character": 50 },
      "type": "insert",
      "content": "\\cite{kipf2017semi}"
    }
    """
    file: str  # 文件路径
    position: Position  # 变更位置
    type: ChangeType  # 变更类型
    content: str  # 变更内容（对于 delete 类型，content 为空）
    old_content: Optional[str] = None  # 原始内容（对于 replace 类型）
    
    def __post_init__(self):
        """验证变更数据"""
        # DELETE 类型：content 应该为空，但不强制（允许空字符串）
        if self.type == ChangeType.DELETE and self.content and self.content.strip():
            raise ValueError("DELETE type change should have empty content")
        # REPLACE 类型：old_content 应该存在，但允许为空字符串（表示插入后替换）
        # 注意：某些场景下可能不需要 old_content（如批量替换），所以放宽验证
        # if self.type == ChangeType.REPLACE and self.old_content is None:
        #     raise ValueError("REPLACE type change should have old_content")
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        result = {
            "file": self.file,
            "position": self.position.to_dict(),
            "type": self.type.value,
            "content": self.content
        }
        if self.old_content:
            result["old_content"] = self.old_content
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FileChange":
        """从字典创建"""
        return cls(
            file=data["file"],
            position=Position.from_dict(data["position"]),
            type=ChangeType(data["type"]),
            content=data["content"],
            old_content=data.get("old_content")
        )


@dataclass
class SemanticPosition:
    """
    语义定位
    用于通过语义信息定位（如"在 Related Work 章节中"）
    
    设计文档要求：
    - type: 'section' | 'paragraph' | 'sentence' | 'citation' | 'command'
    - identifier: section 名称、citation key 等
    - index: 如果是第几个 paragraph/sentence
    - child: 嵌套语义定位（如 section -> paragraph）
    - match: 文本匹配（用于 child 中，如 semantic.child.match）
    """
    type: str  # 'section' | 'paragraph' | 'sentence' | 'citation' | 'command'
    identifier: Optional[str] = None  # section 名称、citation key 等
    index: Optional[int] = None  # 如果是第几个 paragraph/sentence
    child: Optional["SemanticPosition"] = None  # 嵌套语义定位（如 section -> paragraph）
    match: Optional["TextMatchPosition"] = None  # 文本匹配（用于 child 中，支持 semantic.child.match 结构）
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        result = {"type": self.type}
        if self.identifier:
            result["identifier"] = self.identifier
        if self.index is not None:
            result["index"] = self.index
        if self.child:
            result["child"] = self.child.to_dict()
        if self.match:
            result["match"] = self.match.to_dict()
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SemanticPosition":
        """从字典创建"""
        child = None
        if "child" in data:
            child = cls.from_dict(data["child"])
        
        match = None
        if "match" in data and isinstance(data["match"], dict):
            # TextMatchPosition 在 SemanticPosition 之后定义，运行时已存在
            # 直接使用类名（字符串类型注解已处理循环引用）
            match = TextMatchPosition.from_dict(data["match"])
        
        return cls(
            type=data["type"],
            identifier=data.get("identifier"),
            index=data.get("index"),
            child=child,
            match=match
        )


@dataclass
class TextMatchPosition:
    """
    文本匹配定位
    用于通过文本内容匹配定位（如"找到包含'GNN'的段落"）
    
    设计文档要求：
    - text: 匹配的文本内容
    - context: 上下文（前后各 N 个字符）
    - file: 文件路径（多文件项目）
    """
    text: str  # 匹配的文本内容
    context: Optional[str] = None  # 上下文（前后各 N 个字符）
    file: Optional[str] = None  # 文件路径（多文件项目）
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        result = {"text": self.text}
        if self.context:
            result["context"] = self.context
        if self.file:
            result["file"] = self.file
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TextMatchPosition":
        """从字典创建"""
        return cls(
            text=data["text"],
            context=data.get("context"),
            file=data.get("file")
        )


@dataclass
class TargetLocation:
    """
    目标位置信息
    用于指定编辑操作的目标位置
    
    支持三种定位方式：
    1. position (TextRange) - 行号+列号定位
    2. semantic (SemanticPosition) - 语义定位
    3. text_match (TextMatchPosition) - 文本匹配定位
    
    设计文档要求（API 请求格式）：
    {
      "file": "sections/related_work.tex",
      "position": {
        "start": { "line": 15, "character": 10 },
        "end": { "line": 15, "character": 50 }
      },
      "text": "Graph Neural Networks have shown success..."
    }
    """
    file: str  # 文件路径
    position: Optional[TextRange] = None  # 位置范围（可选，行号+列号定位）
    semantic: Optional[SemanticPosition] = None  # 语义定位（可选）
    text_match: Optional[TextMatchPosition] = None  # 文本匹配定位（可选）
    text: Optional[str] = None  # 目标文本（可选，用于兼容 API 格式）
    context: Optional[str] = None  # 上下文信息（可选）
    
    def __post_init__(self):
        """验证定位数据"""
        # 至少需要一种定位方式（position、semantic、text_match 或 text）
        # text 字段可以单独使用（用于兼容 API 格式，Agent 会通过文本匹配定位）
        if not (self.position or self.semantic or self.text_match or self.text):
            raise ValueError(
                "At least one of position, semantic, text_match, or text must be provided."
            )
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        result = {"file": self.file}
        if self.position:
            result["position"] = self.position.to_dict()
        if self.semantic:
            result["semantic"] = self.semantic.to_dict()
        if self.text_match:
            result["text_match"] = self.text_match.to_dict()
        if self.text:
            result["text"] = self.text
        if self.context:
            result["context"] = self.context
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TargetLocation":
        """从字典创建"""
        position = None
        if "position" in data:
            position = TextRange.from_dict(data["position"])
        
        semantic = None
        if "semantic" in data:
            semantic = SemanticPosition.from_dict(data["semantic"])
        
        text_match = None
        if "text_match" in data:
            text_match = TextMatchPosition.from_dict(data["text_match"])
        
        return cls(
            file=data["file"],
            position=position,
            semantic=semantic,
            text_match=text_match,
            text=data.get("text"),
            context=data.get("context")
        )


@dataclass
class BibliographyUpdate:
    """
    参考文献更新信息
    表示对参考文献的更新操作
    
    设计文档要求（API 响应格式）：
    {
      "new_entries": ["@article{kipf2017semi, ...}"]
    }
    """
    new_entries: List[str] = field(default_factory=list)  # 新的 BibTeX 条目列表
    updated_entries: List[str] = field(default_factory=list)  # 更新的 BibTeX 条目列表
    removed_keys: List[str] = field(default_factory=list)  # 删除的引用键列表
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        # 统一序列化所有字段，包括空列表（保持一致性）
        return {
            "new_entries": self.new_entries,
            "updated_entries": self.updated_entries,
            "removed_keys": self.removed_keys
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BibliographyUpdate":
        """从字典创建"""
        return cls(
            new_entries=data.get("new_entries", []),
            updated_entries=data.get("updated_entries", []),
            removed_keys=data.get("removed_keys", [])
        )


@dataclass
class LaTeXNode:
    """
    LaTeX AST 节点
    用于表示 LaTeX 文档的抽象语法树节点
    
    设计文档要求：
    - type: 'document' | 'section' | 'paragraph' | 'sentence' | 
            'citation' | 'command' | 'environment' | 'text'
    - content: 节点内容
    - position: 位置范围（start, end）
    - metadata: 元数据（sectionName, citationKey, commandName 等）
    - children: 子节点列表
    """
    type: str  # 'document' | 'section' | 'paragraph' | 'sentence' | 
               # 'citation' | 'command' | 'environment' | 'text'
    content: str  # 节点内容
    position: TextRange  # 位置范围
    metadata: Optional[Dict[str, Any]] = None  # 元数据
    children: Optional[List["LaTeXNode"]] = None  # 子节点列表（None 表示无子节点，空列表也表示无子节点）
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        result = {
            "type": self.type,
            "content": self.content,
            "position": self.position.to_dict()
        }
        if self.metadata:
            result["metadata"] = self.metadata
        # 只有当 children 不为 None 且不为空列表时才序列化
        if self.children:
            result["children"] = [child.to_dict() for child in self.children]
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LaTeXNode":
        """从字典创建"""
        children = None
        if "children" in data and data["children"]:
            children = [cls.from_dict(child) for child in data["children"]]
        return cls(
            type=data["type"],
            content=data["content"],
            position=TextRange.from_dict(data["position"]),
            metadata=data.get("metadata"),
            children=children
        )

