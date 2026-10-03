#!/usr/bin/env python3
"""状态文件写入防护模块。

提供三层技术防护，防止研究子 Agent 越权修改正式状态：

1. 调用方令牌（--caller-token）：所有写命令必须传入与令牌文件一致的令牌，
   令牌文件权限 600，仅主控知道路径和内容。
2. flock 排他锁：写入期间对锁文件加排他锁，防止并发写入。
3. 只读文件保护：写入完成后将状态文件 chmod 444，直接写文件会被拒绝；
   写入前在锁内临时 chmod 644。

本模块只依赖 Python 标准库。
"""

from __future__ import annotations

import contextlib
import argparse
import os
import secrets
import sys
from pathlib import Path

# 跨平台文件锁：Unix 使用 fcntl.flock，Windows 使用 msvcrt.locking
if sys.platform == "win32":
    import msvcrt

    LOCK_EX = 1  # 排他锁（占位常量，实际由 _win_flock 处理）
    LOCK_UN = 2  # 解锁（占位常量）

    def _win_flock(fd, operation):
        """Windows 下模拟 fcntl.flock 的排他锁/解锁。

        固定锁定首字节（空文件也适用），锁操作失败时向调用方抛错。
        """
        current_pos = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if operation == LOCK_EX:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            elif operation == LOCK_UN:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                raise ValueError("不支持的锁操作")
        finally:
            os.lseek(fd, current_pos, os.SEEK_SET)
else:
    import fcntl

    LOCK_EX = fcntl.LOCK_EX
    LOCK_UN = fcntl.LOCK_UN

    def _win_flock(fd, operation):
        """Unix 下直接调用 fcntl.flock。"""
        fcntl.flock(fd, operation)


class StateGuardError(RuntimeError):
    """防护校验失败。"""


# ---------------------------------------------------------------------------
# 调用方令牌
# ---------------------------------------------------------------------------

def token_file_path(workdir: Path | str) -> Path:
    """令牌文件固定位于 <workdir>/state/.caller_token。"""
    return Path(workdir) / "state" / ".caller_token"


def ensure_caller_token(workdir: Path | str) -> str:
    """确保令牌文件存在并返回令牌。文件不存在时生成 64 位十六进制随机令牌。

    令牌文件权限强制 600（仅属主可读写）。
    """
    path = token_file_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            # 确保权限正确
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return token
    token = secrets.token_hex(32)
    path.write_text(token + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return token


def derive_workdir_from_state(state_path: Path | str) -> Path:
    """从状态文件路径反推 workdir。

    支持两种布局：
    - <workdir>/batches/S01.json          → workdir = ../../
    - <workdir>/state/city-coverage.json  → workdir = ../
    - <workdir>/state/workflow_state.json → workdir = ../
    """
    p = Path(state_path).resolve()
    parent = p.parent
    if parent.name == "batches":
        return parent.parent
    if parent.name == "state":
        return parent.parent
    # 兜底：向上找包含 state/ 目录的最近一级
    candidate = p.parent
    for _ in range(4):
        if (candidate / "state").is_dir():
            return candidate
        candidate = candidate.parent
    return p.parent


def verify_caller_token(token: str, state_path: Path | str | None = None,
                        workdir: Path | str | None = None) -> None:
    """校验调用方令牌。令牌为空、令牌文件不存在或不匹配时抛出 StateGuardError。

    优先级：显式 workdir > 从 state_path 反推 > 环境变量 B2B_WORKFLOW_CALLER_TOKEN。
    """
    if not token:
        raise StateGuardError(
            "缺少 --caller-token；只有主控 Agent 可以执行状态写操作。"
            "研究子 Agent 禁止调用本脚本的写命令。"
        )

    # 优先用显式 workdir
    if workdir is not None:
        expected_path = token_file_path(workdir)
        if not expected_path.exists():
            raise StateGuardError(
                f"调用方令牌文件不存在：{expected_path}；请先执行 workflow init 生成令牌。"
            )
        expected = expected_path.read_text(encoding="utf-8").strip()
        if token != expected:
            raise StateGuardError("调用方令牌校验失败：非主控进程禁止执行写操作。")
        return

    # 其次从 state_path 反推
    if state_path is not None:
        wd = derive_workdir_from_state(state_path)
        expected_path = token_file_path(wd)
        if expected_path.exists():
            expected = expected_path.read_text(encoding="utf-8").strip()
            if token == expected:
                return
        # 反推路径下没有令牌文件时，回退到环境变量
        env_token = os.environ.get("B2B_WORKFLOW_CALLER_TOKEN", "")
        if env_token and token == env_token:
            return
        raise StateGuardError(
            f"调用方令牌校验失败：在 {expected_path} 未找到匹配令牌，且环境变量未设置。"
        )

    # 最后回退环境变量
    env_token = os.environ.get("B2B_WORKFLOW_CALLER_TOKEN", "")
    if env_token and token == env_token:
        return
    raise StateGuardError("调用方令牌校验失败：未指定 workdir/state 且环境变量 B2B_WORKFLOW_CALLER_TOKEN 未设置。")


# ---------------------------------------------------------------------------
# flock 排他锁 + 只读保护
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def protected_write(path: Path | str):
    """写入防护上下文管理器。

    进入时：
      1. 打开（或创建）<path>.lock 锁文件；
      2. 对锁文件加 flock 排他锁；
      3. 若目标文件已存在且为只读，临时 chmod 644。

    退出时（无论成功或异常）：
      1. 若目标文件存在，chmod 444 设为只读；
      2. 释放 flock 并关闭锁文件。

    用法：
        with protected_write(state_path):
            # 执行 tempfile + os.replace 原子写入
            ...
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")

    # 创建/打开锁文件
    lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    acquired = False
    try:
        _win_flock(lock_fd, LOCK_EX)
        acquired = True

        # 临时切换目标文件为可写（如果存在且只读）
        if path.exists():
            try:
                os.chmod(path, 0o644)
            except OSError:
                pass

        try:
            yield
        finally:
            # 即使写入抛错，也恢复只读保护。
            if path.exists():
                os.chmod(path, 0o444)
    finally:
        try:
            if acquired:
                _win_flock(lock_fd, LOCK_UN)
        finally:
            os.close(lock_fd)


def read_only(path: Path | str) -> None:
    """将文件设为只读（444）。用于初始化后立即保护新文件。"""
    p = Path(path)
    if p.exists():
        try:
            os.chmod(p, 0o444)
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化工作流调用方令牌")
    parser.add_argument("command", choices=["init-token"])
    parser.add_argument("--workdir", required=True)
    args = parser.parse_args()
    print(ensure_caller_token(args.workdir))


if __name__ == "__main__":
    main()
