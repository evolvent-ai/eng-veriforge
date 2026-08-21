"""correctness validator 骨架。

接口契约：`check_correctness(payload: dict, reference: dict) -> tuple[float, str]`。
返回 0.0–1.0 的得分比例，scorer 会乘以该维度权重。
与 check_schema 不同，这里做部分给分，让分数能区分"全错"和"错一半"。
"""


def check_correctness(payload: dict, reference: dict) -> tuple[float, str]:
    expected = {item["record_id"]: item["rationale_code"] for item in reference["reviews"]}
    actual = {
        item["record_id"]: item["rationale_code"]
        for item in payload.get("reviews", [])
        if isinstance(item, dict) and "record_id" in item
    }

    if not expected:
        return 0.0, "标答为空，任务包配置有误"

    hits = sum(1 for key, value in expected.items() if actual.get(key) == value)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    ratio = hits / len(expected)

    detail = f"{hits}/{len(expected)} 条判定与标答一致"
    if missing:
        detail += f"；遗漏 {len(missing)} 条（如 {missing[0]}）"
    if extra:
        detail += f"；多出 {len(extra)} 条（如 {extra[0]}）"
    return ratio, detail
