"""Validate the complete industry bundle before a task is created."""
from __future__ import annotations
import argparse
import string

class BundleError(ValueError):
    pass

SUPPORTED_TRANSITIONS = {
    'SEARCH_RUNNING':['SEARCH_DONE'], 'SEARCH_DONE':['DELIVERY_READY','INFERENCE_RUNNING'],
    'INFERENCE_RUNNING':['INFERENCE_DONE'], 'INFERENCE_DONE':['BASE_FIELDS_LOCKED'],
    'BASE_FIELDS_LOCKED':['COMPETITOR_SEARCH_RUNNING'], 'COMPETITOR_SEARCH_RUNNING':['COMPETITOR_SEARCH_DONE'],
    'COMPETITOR_SEARCH_DONE':['DELIVERY_READY'], 'DELIVERY_READY':['DONE']}

def fields(pattern):
    try:
        return {field for _, field, _, _ in string.Formatter().parse(pattern) if field}
    except (ValueError, TypeError) as exc:
        raise BundleError('查询模板格式错误') from exc

def validate_bundle(profile, keywords, expansion, permissions, policies):
    columns = set(profile['output_columns'])
    if len(profile['target']['source_types']) < 2:
        raise BundleError('产业覆盖至少需要两类独立来源，source_types不能少于两项')
    if not set(expansion.get('allowed_source_routes', [])) <= set(profile['target']['source_types']):
        raise BundleError('扩展来源必须属于target.source_types')
    chains = expansion.get('feature_chains', [])
    ids = [c.get('feature_chain_id') for c in chains]
    if not chains or len(set(ids)) != len(ids) or any(not i for i in ids):
        raise BundleError('扩展特征链标识缺失或重复')
    allowed_dimensions = set(expansion.get('allowed_similarity_dimensions', []))
    for chain in chains:
        dims = chain.get('dimensions', [])
        if not dims or not set(dims) <= allowed_dimensions:
            raise BundleError('扩展特征链维度未授权')
        if fields(chain.get('query_template', '')) != {'city', *dims}:
            raise BundleError('扩展模板必须使用city及该链全部维度')
    rows = keywords.get('base_keywords', [])
    kids = [r.get('keyword_id') for r in rows]
    if not rows or keywords.get('total_keywords') != len(rows) or len(set(kids)) != len(kids) or any(not x for x in kids):
        raise BundleError('词库数量或关键词ID不一致')
    families = {r.get('keyword_family') for r in rows}
    if any(not r.get('keyword') or not r.get('keyword_family') for r in rows):
        raise BundleError('词库缺少关键词或族标识')
    templates = keywords.get('query_templates', {})
    family_templates = templates.get('family_templates', {})
    aliases = templates.get('coverage_family_aliases', {})
    deepening = keywords.get('deepening_policy', {}).get('families', {})
    if set(family_templates) != families or set(deepening) != families:
        raise BundleError('词库族、族模板与深挖策略必须完整对应')
    if set(aliases) != families or any(not values or not set(values) <= families for values in aliases.values()):
        raise BundleError('覆盖族映射缺少族或引用未知族')
    intents = templates.get('purpose_variants', {}).get('intents', {})
    for family, rule in deepening.items():
        required = rule.get('required_intents', [])
        if not required or len(set(required)) != len(required) or not set(required) <= set(intents):
            raise BundleError(f'深挖用途无效：{family}')
        if any(family not in intents[i].get('applicable_families', []) for i in required):
            raise BundleError(f'深挖用途不适用于关键词族：{family}')
    for entry in [*family_templates.values(), templates.get('combination_template', {})]:
        pattern_fields = fields(entry.get('pattern', ''))
        if not {'city', 'keyword'} <= pattern_fields or not pattern_fields <= {'city', 'keyword', 'combination_term'}:
            raise BundleError('基础查询模板占位符非法')
    for intent in intents.values():
        for entry in intent.get('variants', []):
            f = fields(entry.get('pattern', ''))
            if not {'city', 'keyword', 'combination_term'} <= f or not f <= {'city','keyword','combination_term','context'}:
                raise BundleError('用途查询模板占位符非法')
    stages = permissions.get('stages', {})
    if set(stages) != {'INFERENCE_RUNNING','COMPETITOR_SEARCH_RUNNING'}:
        raise BundleError('字段权限阶段必须对应两个受控业务分析阶段')
    transitions = permissions.get('transitions', {})
    if not transitions or any(k not in SUPPORTED_TRANSITIONS or not isinstance(v,list)
                              or not set(v) <= set(SUPPORTED_TRANSITIONS[k]) for k,v in transitions.items()):
        raise BundleError('阶段迁移超出检索与可选业务分析范围')
    for name, rule in stages.items():
        writable, required = set(rule.get('writable', [])), set(rule.get('required_on_commit', []))
        if not writable <= columns | {'企业ID'} or not required <= writable:
            raise BundleError(f'阶段业务字段与输出列不一致：{name}')
    if set(policies.get('profiles', {})) != {'standard', 'strict', 'light'}:
        raise BundleError('策略文件仅支持standard、strict、light三个已校验档位')
    for name in ('standard','strict','light'):
        validate_policy(policies.get('profiles', {}).get(name))
    selected = profile.get('policy', 'standard')
    if selected not in policies.get('profiles', {}):
        raise BundleError('未知策略档')
    return True

def validate_policy(policy):
    if not isinstance(policy, dict):
        raise BundleError('策略档缺失')
    for section, key, floor in [('system_search','min_queries',8),('system_search','zero_candidate_rounds',3),
                                ('system_search','budget',8),('expansion','duplicate_streak',3),('expansion','query_budget',6)]:
        value = policy.get(section, {}).get(key)
        if not isinstance(value,int) or isinstance(value,bool) or value < floor:
            raise BundleError(f'策略阈值低于受控下限：{section}.{key}')
    if policy['system_search']['budget'] < policy['system_search']['min_queries']:
        raise BundleError('查询预算小于有效查询下限')
    if policy.get('coverage_threshold') != 1.0:
        raise BundleError('正式交付要求覆盖矩阵完成率为100%')
    if not isinstance(policy.get('base_keyword_batch_max'),int) or isinstance(policy['base_keyword_batch_max'],bool) or policy['base_keyword_batch_max'] < 1:
        raise BundleError('基础关键词批次上限非法')
    for mode in ('search','expansion'):
        limits = policy.get('limits',{}).get(mode,{})
        if any(not isinstance(limits.get(k),int) or isinstance(limits.get(k),bool) or limits[k] < 1
               for k in ('candidate_max','candidate_min','workset_max','delivery_size')):
            raise BundleError('批次限制非法')
        if limits['candidate_min'] > limits['candidate_max'] or limits['workset_max'] > limits['candidate_max']:
            raise BundleError('批次限制相互矛盾')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',required=True)
    args=parser.parse_args()
    from b2b_config import bind_profile
    binding=bind_profile(args.profile)
    print('配置整包校验通过：'+binding['fingerprint'])

if __name__=='__main__':
    main()
