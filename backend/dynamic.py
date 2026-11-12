"""Dynamic preset rules: per-image arithmetic and conditional parameter overrides.

A step may carry ordered ``rules``. Every rule has a boolean ``when``
expression and a set of ``params``. Parameter values are literals or
``{"$expr": "..."}`` expressions. Expressions can only use the variables and
functions exposed by :mod:`backend.expressions`; they cannot read files,
import modules or call arbitrary functions.

Resolution happens per image, immediately before the step runs. The current
``width``/``height``/``orientation`` describe the image entering that step
(i.e. the result of the previous step), while ``source.*`` describes the
original asset snapshot captured when the image was submitted.
"""
from .errors import StudioError
from .expressions import ExpressionError, compile_expression, evaluate
from .validation import (
    object_value,
    reject_unknown,
    text_value,
    number_value,
    choice_value,
    list_value,
    identifier,
    validate_parameter,
    validate_parameters,
)

EXPRESSION_KEY = "$expr"
RULE_LIMIT = 32
RULE_PARAM_LIMIT = 24
SNAPSHOT_FIELDS = {"asset_id", "name", "kind", "tags", "width", "height"}


def orientation_of(width, height):
    if width > height:
        return "landscape"
    if width < height:
        return "portrait"
    return "square"


def snapshot_from_record(record):
    """Capture only rule-relevant, non-binary asset metadata."""
    return {
        "asset_id": record["id"],
        "name": record["name"],
        "kind": record["kind"],
        "tags": list(record.get("tags", [])),
        "width": int(record["width"]),
        "height": int(record["height"]),
    }


def normalize_snapshot(value, label="素材快照"):
    object_value(value, label)
    reject_unknown(value, SNAPSHOT_FIELDS, label)
    snapshot = {
        "asset_id": identifier(value.get("asset_id"), f"{label}素材 ID"),
        "name": text_value(value.get("name"), f"{label}名称", 180, 1),
        "kind": choice_value(value.get("kind", "original"), ["original", "result", "sample"], f"{label}类型"),
        "tags": list(dict.fromkeys(
            text_value(tag, f"{label}标签", 30, 1)
            for tag in list_value(value.get("tags", []), f"{label}标签", minimum=0, maximum=12)
        )),
        "width": number_value(value.get("width"), f"{label}宽度", 1, 8192, integer=True),
        "height": number_value(value.get("height"), f"{label}高度", 1, 8192, integer=True),
    }
    return snapshot


def source_context(snapshot):
    if not snapshot:
        return {}
    width = int(snapshot["width"])
    height = int(snapshot["height"])
    name = snapshot["name"]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return {
        "source.width": width,
        "source.height": height,
        "source.orientation": orientation_of(width, height),
        "source.megapixels": round(width * height / 1_000_000, 2),
        "source.name": name,
        "source.stem": stem,
        "source.tags": list(snapshot.get("tags", [])),
        "source.kind": snapshot.get("kind", "original"),
    }


def step_context(width, height, snapshot=None):
    context = {
        "width": int(width),
        "height": int(height),
        "orientation": orientation_of(width, height),
    }
    context.update(source_context(snapshot))
    return context


def is_expression(value):
    return isinstance(value, dict) and set(value) == {EXPRESSION_KEY}


def _field_map(spec):
    return {field["name"]: field for field in spec["parameters"]}


def _rule_error(step_index, spec, rule, parameter, expression, reason, phase="params"):
    label = rule["label"]
    position = f"第 {step_index + 1} 步「{spec['title']}」" if step_index is not None else f"「{spec['title']}」"
    if phase == "when":
        message = f"{position}规则「{label}」的适用条件无法计算：{reason}"
    elif expression is not None:
        message = f"{position}规则「{label}」为参数「{parameter}」计算的值未通过检查：{reason}"
    else:
        message = f"{position}规则「{label}」设置的参数「{parameter}」未通过检查：{reason}"
    return StudioError(message, code="rule_error", details={
        "step": None if step_index is None else step_index + 1,
        "operation": spec["id"],
        "rule": label,
        "rule_index": rule.get("index"),
        "parameter": parameter,
        "expression": expression,
        "phase": phase,
    })


def _compile_rule(rule, spec, index, step_index):
    """Shape-check one rule; expressions are compiled but not evaluated."""
    if not isinstance(rule, dict):
        position = f"第 {step_index + 1} 步" if step_index is not None else "规则"
        raise StudioError(f"{position}的规则必须是 JSON 对象", details={"rule_index": index})
    reject_unknown(rule, {"label", "when", "params"}, "规则")
    label = text_value(rule.get("label", f"规则 {index + 1}"), "规则名称", 40, 1)
    when = rule.get("when")
    if not isinstance(when, str) or not when.strip():
        raise StudioError(f"规则「{label}」缺少适用条件 when", details={"rule_index": index})
    try:
        compile_expression(when)
    except ExpressionError as error:
        raise StudioError(
            f"规则「{label}」的适用条件无效：{error}",
            code="rule_error",
            details={"rule_index": index, "phase": "when", "expression": when},
        ) from error
    params = rule.get("params")
    if not isinstance(params, dict) or not params:
        raise StudioError(f"规则「{label}」必须至少设置一个参数", details={"rule_index": index})
    fields = _field_map(spec)
    normalized = {}
    for name, value in params.items():
        if name not in fields:
            raise StudioError(
                f"规则「{label}」包含操作不支持的参数「{name}」",
                details={"rule_index": index, "field": name},
            )
        if is_expression(value):
            expression = value[EXPRESSION_KEY]
            try:
                compile_expression(expression)
            except ExpressionError as error:
                raise StudioError(
                    f"规则「{label}」参数「{fields[name]['label']}」的表达式无效：{error}",
                    code="rule_error",
                    details={"rule_index": index, "parameter": name, "expression": expression},
                ) from error
            normalized[name] = {EXPRESSION_KEY: expression.strip()}
        elif isinstance(value, dict):
            raise StudioError(
                f"规则「{label}」参数「{fields[name]['label']}」必须是普通值或 {{\"$expr\": \"表达式\"}}",
                details={"rule_index": index, "parameter": name},
            )
        else:
            try:
                normalized[name] = validate_parameter(fields[name], value)
            except StudioError as error:
                raise StudioError(
                    f"规则「{label}」参数「{fields[name]['label']}」无效：{error.message}",
                    code="rule_error",
                    details={"rule_index": index, "parameter": name},
                ) from error
    return {"label": label, "when": when.strip(), "params": normalized}


def normalize_step(step, spec, step_index=None):
    """Validate and normalize a step, including optional dynamic rules."""
    fields = _field_map(spec)
    position = f"第 {step_index + 1} 步" if step_index is not None else "处理步骤"
    params = step.get("params", {})
    if not isinstance(params, dict):
        raise StudioError(f"{position}的参数必须是 JSON 对象")
    for name in params:
        if name not in fields:
            raise StudioError(
                f"{position}包含操作不支持的参数「{name}」",
                details={"step": None if step_index is None else step_index + 1, "field": name},
            )
    validated = validate_parameters(spec, params)
    rules = step.get("rules")
    normalized_rules = []
    if rules is not None:
        if not isinstance(rules, list) or not rules:
            raise StudioError(f"{position}的规则必须是非空数组")
        if len(rules) > RULE_LIMIT:
            raise StudioError(f"{position}最多支持 {RULE_LIMIT} 条规则")
        for index, rule in enumerate(rules):
            compiled = _compile_rule(rule, spec, index, step_index)
            if len(compiled["params"]) > RULE_PARAM_LIMIT:
                raise StudioError(f"{position}单条规则最多设置 {RULE_PARAM_LIMIT} 个参数")
            normalized_rules.append(compiled)
    result = {"operation": spec["id"], "params": validated}
    if normalized_rules:
        result["rules"] = normalized_rules
    return result


def resolve_step(step, spec, context, step_index=None):
    """Apply matching rules, then run the usual strict parameter checks.

    Returns ``(resolved_step, report)``. Any failure identifies the step, rule,
    parameter and expression so one image's problem is never confused with
    another's.
    """
    params = dict(step["params"])
    provenance = {}
    matched = []
    for rule_index, rule in enumerate(step.get("rules", [])):
        rule = dict(rule, index=rule_index)
        try:
            condition = evaluate(compile_expression(rule["when"]), context)
        except ExpressionError as error:
            raise _rule_error(step_index, spec, rule, "when", rule["when"], str(error), phase="when") from error
        if type(condition) is not bool:
            raise _rule_error(
                step_index, spec, rule, "when", rule["when"],
                "条件必须得到 true 或 false", phase="when",
            )
        if not condition:
            continue
        for name, value in rule["params"].items():
            if is_expression(value):
                expression = value[EXPRESSION_KEY]
                try:
                    computed = evaluate(compile_expression(expression), context)
                except ExpressionError as error:
                    raise _rule_error(step_index, spec, rule, name, expression, str(error)) from error
                params[name] = computed
                provenance[name] = (rule, expression)
            else:
                params[name] = value
                provenance[name] = (rule, None)
        matched.append({"index": rule["index"], "label": rule["label"]})

    validated = {}
    fields = _field_map(spec)
    for field in spec["parameters"]:
        name = field["name"]
        raw = params.get(name, field["default"])
        try:
            validated[name] = validate_parameter(field, raw)
        except StudioError as error:
            if name in provenance:
                rule, expression = provenance[name]
                raise _rule_error(step_index, spec, rule, field["label"], expression, error.message) from error
            position = f"第 {step_index + 1} 步「{spec['title']}」" if step_index is not None else f"「{spec['title']}」"
            raise StudioError(f"{position}参数「{field['label']}」未通过检查：{error.message}") from error
    return {"operation": spec["id"], "params": validated}, {
        "index": None if step_index is None else step_index + 1,
        "operation": spec["id"],
        "title": spec["title"],
        "matched_rules": matched,
        "params": validated,
    }


def verify_snapshot(snapshot, record):
    """A supplied snapshot may name the asset, but pixel size must be real."""
    if snapshot["asset_id"] != record["id"]:
        raise StudioError("素材快照与素材 ID 不一致", details={"asset_id": record["id"]})
    if snapshot["width"] != record["width"] or snapshot["height"] != record["height"]:
        raise StudioError(
            "素材快照的宽高与素材实际尺寸不一致",
            details={
                "snapshot": {"width": snapshot["width"], "height": snapshot["height"]},
                "actual": {"width": record["width"], "height": record["height"]},
            },
        )
    return snapshot
