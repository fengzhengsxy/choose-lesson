#!/usr/bin/env python3
# encoding=utf8
"""每门课程启动一个独立进程，实现真正并行监控。"""

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time

import grabbing

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "grabbing.py")
SUCCESS_TEXT = "选课成功，退出。"
EVENT_PREFIX = "__COURSE_EVENT__"


def normalize_targets(cfg):
    result, seen = [], set()
    for raw in cfg.get("targets") or []:
        if (not isinstance(raw, dict) or not raw.get("enabled", True)
                or raw.get("status") == "completed"):
            continue
        lesson_id = str(raw.get("lesson_id") or "").strip()
        name = str(raw.get("course_name") or "").strip()
        try:
            limit = int(raw.get("limit_count") or 0)
        except (TypeError, ValueError):
            limit = 0
        if not lesson_id or limit <= 0 or lesson_id in seen:
            continue
        seen.add(lesson_id)
        result.append({"lesson_id": lesson_id,
                       "course_name": name or f"lessonId={lesson_id}",
                       "limit_count": limit, "enabled": True})
    return result


def worker_command(python, target, cfg):
    command = [python, WORKER,
               "--lesson", target["lesson_id"],
               "--name", target["course_name"],
               "--limit", str(target["limit_count"]),
               "--mode", str(cfg.get("mode", "spam"))]
    if cfg.get("headless", False):
        command.append("--headless")
    return command


def emit_event(target, status, message="", count=None, limit=None):
    if os.environ.get("USTC_GUI_EVENTS") != "1":
        return
    payload = {"lesson_id": target["lesson_id"],
               "course_name": target["course_name"],
               "status": status, "message": message,
               "count": count, "limit": limit,
               "updated_at": time.strftime("%H:%M:%S")}
    print(EVENT_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def event_from_line(target, line):
    match = re.search(r"已满\s+(\d+)/(\d+)", line)
    if match:
        return "full", int(match.group(1)), int(match.group(2))
    match = re.search(r"有空位！\s*(\d+)/(\d+)", line)
    if match:
        return "available", int(match.group(1)), int(match.group(2))
    if "选课成功" in line:
        return "completed", None, None
    if "登录态已过期" in line:
        return "login_expired", None, None
    if "熔断" in line:
        return "fused", None, None
    if "人数解析失败" in line or " 异常 " in line:
        return "error", None, None
    if "选课结果: ok=False" in line:
        return "submit_failed", None, None
    return None


def mark_target_completed(lesson_id, completed_at=None):
    """原子写回成功状态；下次启动会自动跳过该课程。"""
    cfg = grabbing.load_config()
    changed = False
    for target in cfg.get("targets") or []:
        if isinstance(target, dict) and str(target.get("lesson_id")) == str(lesson_id):
            target["status"] = "completed"
            target["completed_at"] = completed_at or time.strftime("%Y-%m-%d %H:%M:%S")
            changed = True
            break
    if not changed:
        return False
    folder = os.path.dirname(grabbing.CONFIG_PATH)
    fd, temp_path = tempfile.mkstemp(prefix="config-status-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temp_path, grabbing.CONFIG_PATH)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return True


def _reader(process, target, events):
    success = False
    try:
        for line in process.stdout:
            if SUCCESS_TEXT in line:
                success = True
            events.put(("line", target, line))
        code = process.wait()
    except Exception as exc:
        events.put(("line", target, f"读取任务输出失败：{exc}\n"))
        code = -1
    events.put(("finished", target, (code, success)))


def _stop_process_tree(process):
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            process.terminate()
    except Exception:
        pass


def run_parallel(targets, cfg, completion_policy="all"):
    events, processes = queue.Queue(), {}
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    policy_text = "任意一门成功即停止全部" if completion_policy == "any" else "所有课程分别完成"
    print(f"并行监控启动：{len(targets)} 门课程 | 完成策略={policy_text}")
    try:
        for target in targets:
            process = subprocess.Popen(
                worker_command(sys.executable, target, cfg), cwd=HERE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=env, creationflags=flags)
            processes[target["lesson_id"]] = process
            threading.Thread(target=_reader, args=(process, target, events), daemon=True).start()
            print(f"[{target['course_name']}] 独立监控任务已启动（PID={process.pid}）")
            emit_event(target, "running", "独立监控任务已启动")

        successful = set()
        while processes:
            kind, target, payload = events.get()
            label = target["course_name"]
            if kind == "line":
                parsed = event_from_line(target, payload)
                if parsed:
                    status, count, limit = parsed
                    emit_event(target, status, payload.strip(), count, limit)
                print(f"[{label}] {payload}", end="")
                continue
            code, success = payload
            processes.pop(target["lesson_id"], None)
            if success:
                successful.add(target["lesson_id"])
                try:
                    mark_target_completed(target["lesson_id"])
                except Exception as exc:
                    print(f"[{label}] 保存完成状态失败：{exc}")
                emit_event(target, "completed", "选课成功，已保存完成状态")
                print(f"[{label}] 任务完成：选课成功。")
                if completion_policy == "any":
                    print("已有一门课程成功，正在按策略停止其他课程。")
                    for process in list(processes.values()):
                        _stop_process_tree(process)
                    return 0
            else:
                emit_event(target, "stopped", f"任务结束，退出码 {code}")
                print(f"[{label}] 任务结束，未检测到选课成功（退出码 {code}）。")

        if len(successful) == len(targets):
            print("全部目标课程均已选课成功。")
            return 0
        print(f"并行监控已结束：成功 {len(successful)}/{len(targets)} 门。")
        return 1
    except KeyboardInterrupt:
        print("收到停止指令，正在结束全部课程任务。")
        return 130
    finally:
        for process in list(processes.values()):
            _stop_process_tree(process)


def main():
    parser = argparse.ArgumentParser(description="USTC 多课程并行监控调度器")
    parser.add_argument("--policy", choices=("all", "any"), help="完成策略")
    args = parser.parse_args()
    cfg = grabbing.load_config()
    targets = normalize_targets(cfg)
    if not targets:
        print("没有可运行的多课程目标。请在图形界面添加课程并填写容量。")
        return 2
    policy = args.policy or cfg.get("completion_policy", "all")
    if policy not in ("all", "any"):
        policy = "all"
    return run_parallel(targets, cfg, policy)


if __name__ == "__main__":
    raise SystemExit(main())
