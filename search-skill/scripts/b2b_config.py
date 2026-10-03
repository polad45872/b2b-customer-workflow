"""Bind a B2B offering, customer profile and assets to one city task."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


class ConfigError(ValueError):
    pass


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"无法读取配置：{path}：{exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError("配置必须是JSON对象")
    return value


def fingerprint(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_profile(profile):
    for field in ("schema_version", "profile_id", "version"):
        if not isinstance(profile.get(field), str) or not profile[field].strip():
            raise ConfigError(f"行业配置缺少{field}")
    offering = profile.get("offering", {})
    if not all(isinstance(offering.get(k), str) and offering[k].strip()
               for k in ("name", "description", "problem_solved")):
        raise ConfigError("必须明确我方产品或服务、说明及解决的问题")
    target = profile.get("target", {})
    for field in ("roles", "industries", "source_types"):
        values = target.get(field)
        if not isinstance(values, list) or not values or any(not isinstance(x, str) or not x.strip() for x in values):
            raise ConfigError(f"target.{field}必须为非空字符串数组")
        if len(values) != len(set(values)):
            raise ConfigError(f"target.{field}存在重复值")
    rules = profile.get("fit_rules")
    if not isinstance(rules, list) or not rules:
        raise ConfigError("必须配置具体的客户业务匹配规则，禁止默认纳入全部企业")
    ids = set()
    for rule in rules:
        rid = rule.get("rule_id")
        if not isinstance(rid, str) or not rid.strip() or rid in ids:
            raise ConfigError("fit_rules.rule_id缺失或重复")
        ids.add(rid)
        if not rule.get("description") or not rule.get("relevance_to_offering"):
            raise ConfigError("客户匹配规则必须说明业务条件及与我方产品的关联")
        roles = rule.get("allowed_roles", [])
        if not roles or not set(roles) <= set(target["roles"]):
            raise ConfigError("匹配规则的allowed_roles必须属于目标角色")
        terms = rule.get("evidence_any", [])
        if not isinstance(terms, list) or not terms or any(not isinstance(x, str) or not x.strip() for x in terms):
            raise ConfigError("匹配规则必须提供可在来源摘录中核对的evidence_any线索")
    for rule in profile.get("exclude_rules", []):
        if not rule.get("rule_id") or not rule.get("description") or not rule.get("evidence_any"):
            raise ConfigError("排除规则必须有标识、说明及证据线索")
        if not isinstance(rule["evidence_any"], list) or any(not isinstance(x, str) or not x.strip() for x in rule["evidence_any"]):
            raise ConfigError("排除线索必须为非空字符串数组")
    columns = profile.get("output_columns")
    if not isinstance(columns, list) or columns[:3] != ["序号", "企业名称", "官网地址"]:
        raise ConfigError("输出模板前三列必须为序号、企业名称、官网地址")
    if len(columns) != len(set(columns)) or any(not isinstance(x, str) or not x.strip() for x in columns):
        raise ConfigError("输出表头必须为非空且唯一的字符串")
    return profile


def bind_profile(path):
    path = Path(path).resolve()
    profile = validate_profile(read_json(path))
    files = [{"kind": "profile", "path": str(path), "sha256": sha(path)}]
    package_root = Path(__file__).resolve().parents[2]
    defaults = {"policy": package_root/'search-skill/assets/policies.json',
                "permissions": package_root/'single-agent-workflow/config/field_permissions.yml'}
    documents = {}
    for kind in ("keywords", "expansion", "permissions", "policy"):
        relative = profile.get("assets", {}).get(kind) or defaults.get(kind)
        if isinstance(relative, Path):
            relative = str(relative)
        if not isinstance(relative, str) or not relative.strip():
            raise ConfigError(f"缺少assets.{kind}")
        asset = (path.parent / relative).resolve()
        document = read_json(asset)
        documents[kind] = document
        if kind == "keywords" and (not document.get("base_keywords") or document.get("total_keywords") != len(document["base_keywords"])):
            raise ConfigError("词库为空或声明计数不一致")
        if kind == "expansion" and not document.get("feature_chains"):
            raise ConfigError("扩展特征链不能为空")
        files.append({"kind": kind, "path": str(asset), "sha256": sha(asset)})
    from config_lint import validate_bundle
    try:
        validate_bundle(profile, documents['keywords'], documents['expansion'], documents['permissions'], documents['policy'])
        # Keep the complete existing query-template validator as the authority for variant syntax.
        from base_keyword_traversal_control import load_templates
        load_templates(next(Path(f['path']) for f in files if f['kind']=='keywords'))
    except (ValueError, RuntimeError, KeyError, TypeError, AttributeError) as exc:
        raise ConfigError(f'配置整包校验失败：{exc}') from exc
    engine_paths = sorted([*Path(__file__).parent.glob('*.py'), *(package_root/'single-agent-workflow/scripts').glob('*.py')])
    engine_files = [{'path':str(p.resolve()),'sha256':sha(p)} for p in engine_paths]
    core = {"profile": profile, "files": files, "engine_files":engine_files,
            "execution_version":fingerprint(engine_files), "policy_name":profile.get('policy','standard'),
            "binding_version":2}
    return {**core, "fingerprint": fingerprint(core)}


def binding_for(state):
    binding = state.get("b2b_config")
    if not isinstance(binding, dict):
        raise ConfigError("任务未绑定B2B配置；请使用--profile重新初始化，旧行业状态不能直接续跑")
    core = {k: binding.get(k) for k in ("profile", "files", "engine_files", "execution_version", "policy_name", "binding_version")}
    if fingerprint(core) != binding.get("fingerprint"):
        raise ConfigError("任务配置绑定内容已变化")
    validate_profile(binding["profile"])
    kinds = {x.get("kind") for x in binding.get("files", [])}
    if kinds != {"profile", "keywords", "expansion", "permissions", "policy"} or len(binding["files"]) != 5 or binding.get('binding_version') != 2:
        raise ConfigError("配置绑定必须包含配置、词库、扩展链、权限和策略；旧状态请新建任务")
    engine_files=binding.get('engine_files',[])
    if not engine_files or fingerprint(engine_files)!=binding.get('execution_version'):
        raise ConfigError('执行引擎版本缺失或已变化')
    package_root = Path(__file__).resolve().parents[2]
    current = {*Path(__file__).parent.glob('*.py'), *(package_root/'single-agent-workflow/scripts').glob('*.py')}
    if len(engine_files) != len(current) or {Path(f['path']).resolve() for f in engine_files} != {p.resolve() for p in current}:
        raise ConfigError('绑定的执行引擎文件集合不完整或不属于当前安装目录')
    for record in [*binding["files"], *engine_files]:
        path = Path(record["path"])
        if not path.is_file() or sha(path) != record["sha256"]:
            raise ConfigError(f"本次任务绑定的{record.get('kind','engine')}文件已变化或丢失，请恢复原版本或另建任务")
    return binding


def profile_for(state):
    return binding_for(state)["profile"]


def asset_path(state, kind):
    return Path(next(x["path"] for x in binding_for(state)["files"] if x["kind"] == kind))


def check_if_bound(value):
    if isinstance(value, dict) and "b2b_config" in value:
        binding_for(value)
    return value


def same_binding(left, right):
    if binding_for(left)["fingerprint"] != binding_for(right)["fingerprint"]:
        raise ConfigError("批次与城市任务使用不同的B2B配置，禁止混合汇入")


def policy_for(state):
    binding=binding_for(state)
    document=read_json(next(f['path'] for f in binding['files'] if f['kind']=='policy'))
    return document['profiles'][binding['policy_name']]


def permissions_for(state):
    return read_json(asset_path(state,'permissions'))
