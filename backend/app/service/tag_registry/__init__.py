"""Tag registry loader/validator for v0-v2 tag sets."""

from service.tag_registry.loader import (
    BundleConfig,
    DimConfig,
    TagSetConfig,
    get_prompt_ver,
    list_bundles,
    load_bundle,
    load_prompt,
    load_prompt_by_bundle,
    load_tag_set,
)
from service.tag_registry.compat_check import CompatIssue, CompatResult, check_tagset_compatibility, compare_tag_sets
from service.tag_registry.validator import TagValueError, validate, validate_tagset

__all__ = [
    "BundleConfig",
    "DimConfig",
    "TagSetConfig",
    "load_tag_set",
    "load_bundle",
    "list_bundles",
    "load_prompt",
    "load_prompt_by_bundle",
    "get_prompt_ver",
    "CompatIssue",
    "CompatResult",
    "compare_tag_sets",
    "check_tagset_compatibility",
    "TagValueError",
    "validate",
    "validate_tagset",
]
