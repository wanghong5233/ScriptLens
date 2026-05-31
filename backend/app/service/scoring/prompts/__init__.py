"""scoring v4 LLM judge prompts。

每个 prompt 文件必须包含：
- build_prompt(...): 拼接 prompt 字符串
- *PayloadSchema: Pydantic schema（含 field_validator(mode='before') coerce 容错）
- 顶部 docstring 注明业内出处
"""
