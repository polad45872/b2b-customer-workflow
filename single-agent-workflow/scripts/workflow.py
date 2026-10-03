#!/usr/bin/env python3
"""Stage state machine, batch leasing and idempotent commit controller.

写操作（init/seed/next/commit/advance）必须提供 --caller-token，
并通过 state_guard 进行令牌校验、flock 排他锁和只读文件保护。
读操作（status）不需要令牌。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"search-skill/scripts"))
from b2b_config import check_if_bound, binding_for
from typing import Any

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from validate import (
    ValidationError, load_json, load_permissions, validate_file, validate_gate,
    validate_search_completion, conflict_count,
)


class StateGuardError(RuntimeError):
    """主控状态写入防护失败。"""


def token_file_path(workdir: Path) -> Path:
    return workdir / "state" / ".caller_token"


def ensure_caller_token(workdir: Path) -> str:
    path = token_file_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8").strip():
        os.chmod(path, 0o600)
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_hex(32)
    path.write_text(token + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return token


def verify_caller_token(token: str, workdir: Path) -> None:
    path = token_file_path(workdir)
    if not token or not path.exists() or token != path.read_text(encoding="utf-8").strip():
        raise StateGuardError("调用方令牌校验失败；只有主控可以执行状态写操作")


@contextlib.contextmanager
def protected_write(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+") as lock:
        if sys.platform == "win32":
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if path.exists():
                os.chmod(path, 0o644)
            yield
        finally:
            try:
                if path.exists():
                    os.chmod(path, 0o444)
            finally:
                if sys.platform == "win32":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


ROOT = Path(__file__).resolve().parents[1]
STATE_FILE: Path
BATCH_LOG: Path
OUTPUT_DIR: Path


def configure_runtime(workdir: Path) -> None:
    global STATE_FILE, BATCH_LOG, OUTPUT_DIR
    resolved = workdir.resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError:
        pass
    else:
        raise ValidationError("运行目录不得位于Skill安装目录内")
    STATE_FILE = resolved / "state" / "workflow_state.json"
    BATCH_LOG = resolved / "state" / "batches.jsonl"
    OUTPUT_DIR = resolved / "outputs"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, data: Any) -> None:
    with protected_write(path):
        fd, tmp_name = tempfile.mkstemp(prefix=path.name, dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


def require_token(args: argparse.Namespace) -> None:
    """校验调用方令牌。所有写命令必须在执行前调用。"""
    verify_caller_token(
        getattr(args, "caller_token", ""),
        workdir=STATE_FILE.parent.parent,
    )


def append_event(event: dict[str, Any]) -> None:
    BATCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with BATCH_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_state() -> dict[str, Any]:
    state = load_json(STATE_FILE)
    if not isinstance(state, dict):
        raise ValidationError("workflow_state.json 必须是对象")
    return state


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = now()
    atomic_write_json(STATE_FILE, state)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_search_binding(state: dict[str, Any]) -> tuple[Path, Path]:
    binding = state.get("search_controller") or {}
    scripts = Path(str(binding.get("scripts_dir", ""))).resolve()
    city_state = Path(str(binding.get("city_state", ""))).resolve()
    required = scripts / "workflow_orchestrator.py"
    if not required.is_file():
        raise ValidationError(f"search编排器不存在: {required}")
    if not city_state.is_file():
        raise ValidationError(f"search城市状态不存在: {city_state}")
    return scripts, city_state


def run_json_command(command: list[str]) -> dict[str, Any]:
    child_env = os.environ.copy()
    child_env['PYTHONIOENCODING'] = 'utf-8'
    if "--caller-token" in command:
        token_index = command.index("--caller-token") + 1
        if token_index < len(command):
            child_env["B2B_WORKFLOW_CALLER_TOKEN"] = command[token_index]
    completed = subprocess.run(command, text=True, encoding='utf-8', capture_output=True, check=False, env=child_env)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ValidationError(f"阶段控制脚本执行失败: {detail}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError("阶段控制脚本未返回合法JSON") from exc
    if not isinstance(payload, dict):
        raise ValidationError("阶段控制脚本返回值必须是JSON对象")
    return payload


def finalize_search(state: dict[str, Any], scripts: Path, city_state: Path,
                    caller_token: str) -> dict[str, Any]:
    audit = run_json_command([sys.executable, str(scripts / "city_coverage_control.py"),
                              "status", "--state", str(city_state)])
    if not (audit.get("stop_candidate") is True
            and audit.get("coverage_complete") is True
            and audit.get("base_search_saturated") is True
            and audit.get("expansion_complete") is True
            and audit.get("pending_work") is False
            and audit.get("all_batches_closed") is True
):
        raise ValidationError("search尚未通过发现收敛、覆盖、扩展及待办状态的正式停止计算")
    search_dir = STATE_FILE.parent.parent / "search-control"
    search_dir.mkdir(parents=True, exist_ok=True)
    manifest = search_dir / "freeze-manifest.json"
    snapshot = search_dir / "formal-coverage-snapshot.json"
    control = scripts / "city_coverage_control.py"
    run_json_command([
        sys.executable, str(control), "freeze", "--state", str(city_state),
        "--caller-token", caller_token, "--manifest", str(manifest),
    ])
    formal = run_json_command([
        sys.executable, str(control), "recalculate", "--manifest", str(manifest),
        "--caller-token", caller_token, "--output", str(snapshot),
    ])
    validate_search_completion(state, snapshot, manifest)
    state.setdefault("artifacts", {})["SEARCH_FREEZE_MANIFEST"] = {
        "path": str(manifest), "sha256": sha256_file(manifest)
    }
    state["artifacts"]["SEARCH_DONE"] = {
        "path": str(snapshot), "sha256": sha256_file(snapshot),
        "snapshot_id": formal["snapshot_id"],
        "manifest_sha256": formal["manifest_sha256"],
    }
    from final_delivery_control import verify_freeze_chain, build_enterprises
    gate = verify_freeze_chain(city_state, manifest, snapshot)
    companies = build_enterprises(gate.city_state, city_state, gate)
    state['company_registry'] = {c['company_id']:{'企业名称':c['company_name'],'官网地址':c['official_website'] or None} for c in companies}
    state['company_metadata'] = {}
    state["stage"] = "SEARCH_DONE"
    state["status"] = "RUNNING"
    save_state(state)
    append_event({"event": "SEARCH_FORMALLY_COMPLETED", "snapshot_id": formal["snapshot_id"], "at": now()})
    return formal


def cmd_status(_: argparse.Namespace) -> None:
    print(json.dumps(load_state(), ensure_ascii=False, indent=2))


def cmd_init(args: argparse.Namespace) -> None:
    if STATE_FILE.exists():
        raise ValidationError(f"运行目录已存在状态文件，禁止覆盖: {STATE_FILE}")
    # 初始化时创建调用方令牌文件（权限600），后续所有写命令必须校验
    workdir = STATE_FILE.parent.parent
    token = ensure_caller_token(workdir)
    city = str(args.city).strip()
    if not city:
        raise ValidationError("目标城市不能为空")
    search_scripts = args.search_root.resolve() / "scripts"
    city_state = args.city_state.resolve()
    if not (search_scripts / "workflow_orchestrator.py").is_file():
        raise ValidationError(f"search根目录无有效workflow_orchestrator.py: {args.search_root}")
    if not city_state.is_file():
        raise ValidationError(f"search城市状态文件不存在: {city_state}")
    search_payload = load_json(city_state)
    binding = binding_for(search_payload)
    if str(search_payload.get("city", "")).strip() != city:
        raise ValidationError("主控城市与search城市状态中的city不一致")
    if args.batch_size < 1: raise ValidationError("批次大小必须为正整数")
    state = {
        "workflow_id": f"b2b-customer-workflow:{city}",
        "version": 3,
        "b2b_config": binding,
        "city": city,
        "stage": "SEARCH_RUNNING",
        "status": "RUNNING",
        "batch_size": args.batch_size,
        "batch_sequence": 0,
        "active_batch": None,
        "queues": {
            "SEARCH_RUNNING": [],
                        "INFERENCE_RUNNING": [],
            "COMPETITOR_SEARCH_RUNNING": [],
        },
        "counters": {
            "search_running_pending": 0,
                        "inference_running_pending": 0,
            "estimation_running_pending": 0,
            "competitor_search_running_pending": 0,
            "conflicts": 0,
        },
        "artifacts": {},
        "search_controller": {
            "scripts_dir": str(search_scripts),
            "city_state": str(city_state),
            "entrypoint": "workflow_orchestrator.py",
        },
        "created_at": now(),
    }
    save_state(state)
    append_event({"event": "WORKFLOW_INITIALIZED", "city": city, "at": now()})
    print(json.dumps({"status": "INITIALIZED", "city": city, "workdir": str(STATE_FILE.parents[1])}, ensure_ascii=False))


def cmd_seed(args: argparse.Namespace) -> None:
    require_token(args)
    state = load_state()
    if args.stage not in load_permissions(state=state)["stages"] or args.stage != state["stage"]:
        raise ValidationError(f"{args.stage} 的阶段内队列由对应阶段控制脚本管理，主控不得装载")
    if state.get("active_batch"):
        raise ValidationError("存在活动批次，不能重新装载队列")
    ids = [line.strip() for line in args.ids.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(ids) != len(set(ids)):
        raise ValidationError("输入企业ID存在重复")
    if set(ids) != set(state.get('company_registry', {})):
        raise ValidationError('阶段队列必须等于冻结检索的全部企业ID')
    if state.get('stage_seeded', {}).get(args.stage):
        raise ValidationError('本阶段已经装载，禁止重复装载覆盖进度')
    state.setdefault('stage_seeded', {})[args.stage] = True
    state.setdefault('stage_completed_ids', {})[args.stage] = []
    state['queues'][args.stage] = ids
    state["counters"][f"{args.stage.lower()}_pending"] = len(ids)
    save_state(state)
    print(json.dumps({"stage": args.stage, "queued": len(ids)}, ensure_ascii=False))


def cmd_next(args: argparse.Namespace) -> None:
    require_token(args)
    state = load_state()
    if state.get("status") in {"BLOCKED_BY_MANUAL_INPUT", "DONE"}:
        print(json.dumps({"stage": state["stage"], "status": state["status"], "action": "STOP"}, ensure_ascii=False))
        return
    stage = state["stage"]
    if stage == "SEARCH_RUNNING":
        if state.get("active_batch"):
            raise ValidationError("SEARCH_RUNNING存在旧版主控活动批次；禁止与search控制器双重管理")
        scripts, city_state = load_search_binding(state)
        plan_dir = STATE_FILE.parent.parent / "search-control" / "plans"
        command = [
            sys.executable, str(scripts / "workflow_orchestrator.py"), "prepare-next",
            "--city-state", str(city_state), "--output-dir", str(plan_dir),
            "--batch-size", str(args.size or state.get("batch_size", 20)),
            "--caller-token", args.caller_token,
        ]
        if args.proposal:
            if not args.proposal.is_file():
                raise ValidationError(f"查询提案文件不存在: {args.proposal}")
            command.extend(["--proposal", str(args.proposal.resolve())])
        payload = run_json_command(command)
        if payload.get("action") == "FREEZE_SEARCH":
            formal = finalize_search(state, scripts, city_state, args.caller_token)
            print(json.dumps({
                "stage": "SEARCH_DONE", "status": "FORMALLY_COMPLETED",
                "snapshot": formal,
            }, ensure_ascii=False, indent=2))
            return
        print(json.dumps({"stage": stage, "controller": "search-0923-discovery-first", **payload}, ensure_ascii=False, indent=2))
        return
    if state.get("active_batch"):
        print(json.dumps(state["active_batch"], ensure_ascii=False, indent=2))
        return
    queue = state.get("queues", {}).get(stage, [])
    if not queue:
        print(json.dumps({"stage": stage, "status": "NO_PENDING_ITEMS", "action": "ADVANCE_OR_STOP"}, ensure_ascii=False))
        return
    size = args.size or state.get("batch_size", 20)
    if size < 1: raise ValidationError("批次大小必须为正整数")
    ids = queue[:size]
    sequence = state.get("batch_sequence", 0) + 1
    batch_id = f"{stage.replace('_RUNNING', '')}-{sequence:04d}"
    permissions = load_permissions(state=state)["stages"][stage]
    batch = {
        "batch_id": batch_id,
        "stage": stage,
        "company_ids": ids,
        "allowed_fields": permissions.get("writable", []),
        "status": "LEASED",
        "leased_at": now(),
    }
    state["batch_sequence"] = sequence
    state["active_batch"] = batch
    save_state(state)
    append_event(batch)
    print(json.dumps(batch, ensure_ascii=False, indent=2))


def cmd_commit(args: argparse.Namespace) -> None:
    require_token(args)
    state = load_state()
    if state.get("stage") == "SEARCH_RUNNING":
        raise ValidationError("搜索结果必须提交给检索控制器")
    batch = state.get("active_batch")
    if not batch or batch.get("batch_id") != args.batch:
        raise ValidationError("提交批次不是当前活动批次")
    result_path = args.result.resolve()
    try:
        records = validate_file(batch["stage"], result_path, batch["company_ids"], state)
    except (ValidationError, OSError) as exc:
        append_event({**batch, "status": "FAILED", "error": str(exc), "failed_at": now()})
        raise
    output_name = f"{batch['batch_id'].lower()}-{sha256_file(result_path)[:12]}.json"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUTPUT_DIR / output_name
    target.write_bytes(result_path.read_bytes())
    workdir = STATE_FILE.parent.parent
    output_reference = str(target.relative_to(workdir))

    queue = state["queues"][batch["stage"]]
    completed_ids = set(batch["company_ids"])
    state["queues"][batch["stage"]] = [item for item in queue if item not in completed_ids]
    counter_key = f"{batch['stage'].lower()}_pending"
    state["counters"][counter_key] = len(state["queues"][batch["stage"]])
    for row in records:
        cid = str(row.get('企业ID') or row.get('序号')).strip()
        state.setdefault('company_registry', {}).setdefault(cid, {}).update({k:v for k,v in row.items() if k not in {'企业ID','序号','metadata'}})
        if 'metadata' in row:
            state.setdefault('company_metadata', {}).setdefault(cid, {}).update(row['metadata'])
    state.setdefault('stage_completed_ids', {}).setdefault(batch['stage'], []).extend(batch['company_ids'])
    state['counters']['conflicts'] = conflict_count(state)
    state['active_batch'] = None
    # 所有可能失败的路径计算和完成日志写入必须先于状态推进落盘；
    # 日志失败时保留活动批次，调用方可安全重试。
    append_event({**batch, "status": "COMPLETED", "output_file": output_reference, "completed_at": now()})
    save_state(state)
    print(json.dumps({"batch_id": args.batch, "status": "COMPLETED", "output_file": str(target)}, ensure_ascii=False))


def cmd_advance(args: argparse.Namespace) -> None:
    require_token(args)
    state = load_state()
    if state.get("active_batch"):
        raise ValidationError("存在未提交活动批次，不能推进阶段")
    current_queue = state.get("queues", {}).get(state["stage"], [])
    if current_queue:
        raise ValidationError("当前阶段仍有待处理企业，不能推进阶段")
    evidence_path = args.evidence.resolve() if args.evidence else None
    validate_gate(state, args.to, evidence_path)
    if args.to == 'SEARCH_DONE':
        from final_delivery_control import verify_freeze_chain, build_enterprises
        city_path = Path(state['search_controller']['city_state'])
        manifest = Path(state['artifacts']['SEARCH_FREEZE_MANIFEST']['path'])
        gate = verify_freeze_chain(city_path, manifest, evidence_path)
        state['company_registry'] = {c['company_id']:{'企业名称':c['company_name'],'官网地址':c['official_website'] or None}
                                     for c in build_enterprises(gate.city_state, city_path, gate)}
    state['stage'] = args.to
    state["status"] = "DONE" if args.to == "DONE" else "RUNNING"
    if evidence_path:
        artifact = {"path": str(evidence_path), "sha256": sha256_file(evidence_path)}
        if args.to == "SEARCH_DONE":
            snapshot = load_json(evidence_path)
            artifact.update({
                "snapshot_id": snapshot["snapshot_id"],
                "manifest_sha256": snapshot["manifest_sha256"],
            })
        state.setdefault("artifacts", {})[args.to] = artifact
    save_state(state)
    append_event({"event": "STAGE_CHANGED", "from": args.from_stage, "to": args.to, "at": now()})
    print(json.dumps({"stage": args.to, "status": state["status"]}, ensure_ascii=False))


def cmd_resolve_conflict(args):
    require_token(args)
    state = load_state()
    if state.get('stage') == 'DONE' or state.get('active_batch'):
        raise ValidationError('交付完成或存在活动批次时不能处理冲突')
    decision = load_json(args.result)
    cid = str(decision.get('企业ID','')).strip()
    if cid not in state.get('company_registry', {}) or not str(decision.get('处理依据','')).strip():
        raise ValidationError('冲突处理必须提供已登记企业ID及处理依据')
    meta = state.setdefault('company_metadata', {}).setdefault(cid, {})
    if str(meta.get('冲突状态','')).upper() not in {'OPEN','CONFLICT','未解决'}:
        raise ValidationError('企业没有待处理冲突')
    if decision.get('主体核验状态') != 'VERIFIED' or decision.get('冲突状态') != 'RESOLVED':
        raise ValidationError('冲突处理必须确认主体并明确RESOLVED')
    path = OUTPUT_DIR / f"conflict-{sha256_file(args.result)[:16]}.json"
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(args.result.read_bytes())
    meta.update({'主体核验状态':'VERIFIED','冲突状态':'RESOLVED'})
    state.setdefault('conflict_resolutions', []).append({'企业ID':cid,'path':str(path),'sha256':sha256_file(path)})
    state['counters']['conflicts'] = conflict_count(state)
    save_state(state)
    append_event({'event':'CONFLICT_RESOLVED','企业ID':cid,'at':now()})
    print(json.dumps({'status':'RESOLVED','企业ID':cid},ensure_ascii=False))


def cmd_init_token(_: argparse.Namespace) -> None:
    """为已有工作流创建调用方令牌文件（用于升级前已初始化的工作流）。"""
    if not STATE_FILE.exists():
        raise ValidationError("工作流尚未初始化，请先执行 init")
    workdir = STATE_FILE.parent.parent
    token = ensure_caller_token(workdir)
    print(json.dumps({"status": "TOKEN_CREATED", "token_file": str(workdir / "state" / ".caller_token")}, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="单Agent分阶段工作流控制器")
    parser.add_argument("--workdir", required=True, type=Path, help="本次城市任务的独立工作目录，不能位于Skill安装目录")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="在独立任务目录创建全新工作流状态")
    init.add_argument("--city", required=True)
    init.add_argument("--batch-size", type=int, default=20)
    init.add_argument("--search-root", required=True, type=Path, help="search-0923-discovery-first的技能根目录")
    init.add_argument("--city-state", required=True, type=Path, help="已完成城市画像与搜索空间初始化的城市状态JSON")
    init.set_defaults(func=cmd_init)
    sub.add_parser("status").set_defaults(func=cmd_status)
    init_token = sub.add_parser("init-token", help="为已有工作流创建调用方令牌文件（升级迁移用）")
    init_token.set_defaults(func=cmd_init_token)
    seed = sub.add_parser("seed", help="从每行一个企业ID的文本文件装载阶段队列")
    seed.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    seed.add_argument("--stage", required=True)
    seed.add_argument("--ids", required=True, type=Path)
    seed.set_defaults(func=cmd_seed)
    nxt = sub.add_parser("next", help="领取或恢复一个批次")
    nxt.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    nxt.add_argument("--size", type=int)
    nxt.add_argument("--proposal", type=Path, help="发现优先轮次由主控依据线索构造的查询提案JSON")
    nxt.set_defaults(func=cmd_next)
    commit = sub.add_parser("commit", help="校验并提交当前批次")
    commit.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    commit.add_argument("--batch", required=True)
    commit.add_argument("--result", required=True, type=Path)
    commit.set_defaults(func=cmd_commit)
    advance = sub.add_parser("advance", help="按白名单迁移到下一阶段")
    advance.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    advance.add_argument("--to", required=True)
    advance.add_argument("--evidence", type=Path, help="SEARCH_DONE的正式快照，或DONE的三项交付物清单JSON")
    advance.set_defaults(func=cmd_advance)
    resolve = sub.add_parser('resolve-conflict',help='登记主体冲突处理依据并重新计算门禁')
    resolve.add_argument('--caller-token',required=True)
    resolve.add_argument('--result',required=True,type=Path)
    resolve.set_defaults(func=cmd_resolve_conflict)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        configure_runtime(args.workdir)
    except ValidationError as exc:
        print(json.dumps({"status": "REJECTED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.command == "advance":
        args.from_stage = load_state().get("stage")
    try:
        args.func(args)
        return 0
    except (ValidationError, StateGuardError) as exc:
        print(json.dumps({"status": "REJECTED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
