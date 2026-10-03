#!/usr/bin/env python3
"""同类扩展计划控制器：生成并校验种子—特征链—查询的确定性追溯记录。"""
from __future__ import annotations
from b2b_qualification import seed_eligible

from b2b_config import asset_path, check_if_bound, profile_for
import argparse, hashlib, json, string, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

class ExpansionError(RuntimeError): pass
def text(v:Any)->str:return str(v or "").strip()
def load(p:Path)->dict:
    try:v=json.loads(p.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError) as e:raise ExpansionError(f"无法读取JSON {p}：{e}") from e
    if not isinstance(v,dict):raise ExpansionError(f"JSON顶层必须为对象：{p}")
    return v
def sha(p:Path)->str:return hashlib.sha256(p.read_bytes()).hexdigest()
def config(state)->dict:
    c=load(asset_path(state, "expansion")); chains=c.get("feature_chains",[])
    if not isinstance(chains,list) or not chains:raise ExpansionError("扩展特征链资产为空")
    ids=[text(x.get("feature_chain_id")) for x in chains]
    if any(not x for x in ids) or len(ids)!=len(set(ids)):raise ExpansionError("feature_chain_id缺失或重复")
    return c
def chain(c:dict,cid:str)->dict:
    x=next((x for x in c["feature_chains"] if x["feature_chain_id"]==cid),None)
    if not x:raise ExpansionError(f"未知feature_chain_id：{cid}")
    return x
def seed(state:dict,sid:str)->dict:
    x=next((x for x in state.get("candidates",[]) if text(x.get("candidate_id"))==sid),None)
    if not x or not seed_eligible(x):raise ExpansionError("扩展种子须是城市状态中可追溯的入池候选")
    return x
def render(pattern:str,values:dict)->str:
    fields={n for _,n,_,_ in string.Formatter().parse(pattern) if n}
    missing=[n for n in fields if not text(values.get(n))]
    if missing:raise ExpansionError(f"特征链缺少模板字段：{missing}")
    return " ".join(pattern.format(**values).split())
def validate(state_path:Path,plan:dict)->dict:
    state=check_if_bound(load(state_path)); c=config(state); sd=seed(state,text(plan.get("seed_id"))); ch=chain(c,text(plan.get("feature_chain_id")))
    if plan.get("discovery_method")!="YIELD_EXPANSION":raise ExpansionError("discovery_method必须为YIELD_EXPANSION")
    if text(plan.get("input_version"))!=sha(state_path):raise ExpansionError("input_version已过期")
    vals=plan.get("feature_values")
    if not isinstance(vals,dict):raise ExpansionError("feature_values必须为对象")
    expected=render(ch["query_template"],{"city":text(state.get("city")) or text(state.get("search_space",{}).get("city")),**vals})
    if text(plan.get("query_text"))!=expected:raise ExpansionError("query_text未按受控特征链模板生成")
    if text(plan.get("seed_name"))!=text(sd.get("normalized_name") or sd.get("company_name")):raise ExpansionError("seed_name与种子主体不一致")
    allowed_roles=state.get("search_space",{}).get("roles",[])
    if text(plan.get("seed_role")) not in allowed_roles:raise ExpansionError("seed_role必须是目标角色列表中的一个明确角色")
    if text(plan.get("source_route")) not in c["allowed_source_routes"]:raise ExpansionError("source_route非法")
    dims=plan.get("similarity_dimensions",[])
    if dims!=ch["dimensions"]:raise ExpansionError("similarity_dimensions与特征链不一致")
    required=("expansion_task_id","round_id","query_id","coverage_gap")
    if any(not text(plan.get(k)) for k in required):raise ExpansionError(f"扩展计划缺少字段：{required}")
    return {"valid":True,"expansion_task_id":plan["expansion_task_id"],"query_id":plan["query_id"]}
def cmd_status(a):
    s=load(Path(a.state)); finals=[text(x.get("candidate_id")) for x in s.get("candidates",[]) if seed_eligible(x)]
    print(json.dumps({"final_seed_ids":finals,"feature_chain_ids":[x["feature_chain_id"] for x in config(s)["feature_chains"]]},ensure_ascii=False,indent=2))
def cmd_plan(a):
    p=Path(a.state).resolve(); s=check_if_bound(load(p)); c=config(s); sd=seed(s,a.seed_id); ch=chain(c,a.feature_chain_id)
    vals=json.loads(a.feature_values); city=text(s.get("city")) or text(s.get("search_space",{}).get("city"))
    q=render(ch["query_template"],{"city":city,**vals})
    plan={"schema_version":1,"plan_type":"YIELD_EXPANSION","discovery_method":"YIELD_EXPANSION","input_version":sha(p),"round_id":a.round_id,"expansion_task_id":a.expansion_task_id,"seed_id":a.seed_id,"seed_name":text(sd.get("normalized_name") or sd.get("company_name")),"seed_role":text(a.seed_role),"feature_chain_id":a.feature_chain_id,"feature_chain":ch["dimensions"],"feature_values":vals,"similarity_dimensions":ch["dimensions"],"coverage_gap":a.coverage_gap,"source_route":a.source_route,"search_task_id":a.search_task_id,"query_id":a.query_id,"query_text":q,"query_purpose":"EXPANSION_DISCOVERY","keyword_family":"同类扩展","source_type":a.source_route}
    validate(p,plan); Path(a.output).write_text(json.dumps({"search_tasks":[plan]},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
def cmd_validate(a):
    wrapper=load(Path(a.plan)); tasks=wrapper.get("search_tasks",[])
    if not isinstance(tasks,list) or not tasks:raise ExpansionError("计划缺少search_tasks")
    print(json.dumps({"valid":True,"tasks":[validate(Path(a.state).resolve(),x) for x in tasks]},ensure_ascii=False,indent=2))
def parser():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="cmd",required=True)
    x=sub.add_parser("status");x.add_argument("--state",required=True);x.set_defaults(func=cmd_status)
    x=sub.add_parser("plan")
    for n in ("state","seed-id","seed-role","feature-chain-id","feature-values","round-id","expansion-task-id","coverage-gap","source-route","search-task-id","query-id","output"):x.add_argument("--"+n,required=True)
    x.set_defaults(func=cmd_plan)
    x=sub.add_parser("validate-plan");x.add_argument("--state",required=True);x.add_argument("--plan",required=True);x.set_defaults(func=cmd_validate)
    return p
def main():
    try:a=parser().parse_args();a.func(a);return 0
    except (ExpansionError,OSError,ValueError,KeyError,TypeError) as e:print(f"扩展计划控制失败：{e}",file=sys.stderr);return 2
if __name__=="__main__":raise SystemExit(main())
