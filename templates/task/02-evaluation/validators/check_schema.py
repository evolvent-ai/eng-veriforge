"""schema validator 骨架。

接口契约：`check_schema(payload: dict) -> tuple[bool, str]`。
scorer 通过 sys.path 导入本模块，不作为独立进程执行。
只做结构判断，不做业务正确性判断。返回的 str 是给人看的诊断，会进 dimensions[].detail。
"""

# 必须与 task.yaml 的 required_output.schema.required_fields 完全一致。
REQUIRED_FIELDS = ("summary", "reviews")
# 每个 review 条目的必需 key。字段名一律用 task.yaml 的原词，不许用近义词。
REQUIRED_ITEM_FIELDS = ("record_id", "rationale_code")
# 精确匹配的封闭编码表，必须同时出现在 task.yaml 和 adapter prompt 中。
RATIONALE_CODES = frozenset({"approve", "reject", "needs_info"})


def check_schema(payload: dict) -> tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "顶层必须是 JSON 对象"

    missing = [field for field in REQUIRED_FIELDS if field not in payload]
    if missing:
        return False, f"缺少必需字段：{', '.join(missing)}"

    reviews = payload["reviews"]
    if not isinstance(reviews, list) or not reviews:
        return False, "reviews 必须是非空数组"

    for index, item in enumerate(reviews):
        if not isinstance(item, dict):
            return False, f"reviews[{index}] 必须是对象"
        item_missing = [field for field in REQUIRED_ITEM_FIELDS if field not in item]
        if item_missing:
            return False, f"reviews[{index}] 缺少字段：{', '.join(item_missing)}"
        code = item["rationale_code"]
        if code not in RATIONALE_CODES:
            return False, f"reviews[{index}].rationale_code 不在编码表中：{code!r}"

    return True, f"schema 校验通过，共 {len(reviews)} 条记录"
