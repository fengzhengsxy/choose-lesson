#!/usr/bin/env python3
# encoding=utf8
"""USTC 选课监控助手图形界面。"""

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, simpledialog, ttk


HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
SCRIPT_PATH = os.path.join(HERE, "grabbing.py")
MULTI_SCRIPT_PATH = os.path.join(HERE, "multi_grabbing.py")
AUTH_PATH = os.path.join(HERE, "auth.json")
EVENT_PREFIX = "__COURSE_EVENT__"
STATUS_LABELS = {"pending": "等待启动", "running": "监控中", "full": "已满",
                 "available": "发现空位", "submit_failed": "提交失败",
                 "error": "查询异常", "fused": "已熔断",
                 "login_expired": "登录过期", "completed": "已完成",
                 "stopped": "已停止"}

DEFAULT_CONFIG = {
    "student_id": "",
    "turn_id": "",
    "target_lesson_id": "",
    "target_course_name": "",
    "limit_count": 0,
    "interval_seconds": 60,
    "jitter_seconds": 15,
    "heart_beat": 300,
    "active_hours": "6:30-1:00",
    "max_errors": 20,
    "max_grab_failures": 3,
    "mode": "spam",
    "notify_webhook_url": "",
    "headless": False,
    "disable_browser_sandbox": False,
    "targets": [],
    "completion_policy": "all",
}

INTEGER_RULES = {
    "limit_count": (0, "课程容量"),
    "interval_seconds": (10, "查询间隔"),
    "jitter_seconds": (0, "随机抖动"),
    "heart_beat": (30, "非活跃心跳"),
    "max_errors": (3, "异常熔断次数"),
    "max_grab_failures": (1, "提交失败熔断次数"),
}


def load_config_file(path=CONFIG_PATH):
    cfg = DEFAULT_CONFIG.copy()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError("配置文件根节点必须是对象")
        cfg.update(loaded)
    return cfg


def save_config_file(cfg, path=CONFIG_PATH):
    """原子写入配置，避免程序中断留下半个 JSON 文件。"""
    folder = os.path.dirname(os.path.abspath(path))
    fd, temp_path = tempfile.mkstemp(prefix="config-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def _valid_active_hours(spec):
    if not spec:
        return True
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", spec)
    if not match:
        return False
    h1, m1, h2, m2 = map(int, match.groups())
    return 0 <= h1 <= 23 and 0 <= h2 <= 23 and 0 <= m1 <= 59 and 0 <= m2 <= 59


def validate_config(cfg, for_run=False):
    errors = []
    normalized = dict(cfg)
    for key, (minimum, label) in INTEGER_RULES.items():
        try:
            value = int(str(cfg.get(key, "")).strip())
        except (TypeError, ValueError):
            errors.append(f"{label}必须是整数")
            continue
        if value < minimum:
            errors.append(f"{label}不能小于 {minimum}")
        normalized[key] = value

    active = str(cfg.get("active_hours", "")).strip()
    if not _valid_active_hours(active):
        errors.append("活跃时段格式应为 HH:MM-HH:MM，例如 6:30-1:00")
    normalized["active_hours"] = active

    mode = str(cfg.get("mode", "spam"))
    if mode not in ("spam", "monitor", "grab"):
        errors.append("运行模式无效")

    webhook = str(cfg.get("notify_webhook_url", "")).strip()
    if webhook and not webhook.lower().startswith(("http://", "https://")):
        errors.append("通知地址必须以 http:// 或 https:// 开头")

    if for_run:
        enabled_targets = [item for item in (cfg.get("targets") or [])
                           if isinstance(item, dict) and item.get("enabled", True)]
        pending_targets = [item for item in enabled_targets
                           if item.get("status") != "completed"]
        if enabled_targets:
            if not pending_targets:
                errors.append("并行课程均已完成；请重新设为待监控或添加新课程")
            for index, target in enumerate(pending_targets, 1):
                if not str(target.get("lesson_id", "")).strip():
                    errors.append(f"并行课程 {index} 缺少 lessonId")
                try:
                    target_limit = int(target.get("limit_count") or 0)
                except (TypeError, ValueError):
                    target_limit = 0
                if target_limit <= 0:
                    errors.append(f"并行课程 {index} 的容量必须大于 0")
        else:
            if not (str(cfg.get("target_lesson_id", "")).strip()
                    or str(cfg.get("target_course_name", "")).strip()):
                errors.append("请填写单门课程 lessonId/名称，或先把课程加入并行监控列表")
            if normalized.get("limit_count", 0) <= 0:
                errors.append("开始监控前必须填写大于 0 的课程容量")

    return normalized, errors


def resolve_python():
    candidates = [
        os.path.join(HERE, ".venv", "Scripts", "python.exe"),
        os.path.join(HERE, ".venv", "bin", "python"),
        sys.executable,
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return sys.executable


def _first_value(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return ""


def course_summary(item):
    """提取课程结果表需要的常用字段。"""
    course = item.get("course") if isinstance(item.get("course"), dict) else {}
    teachers = item.get("teachers") or item.get("teacherAssignmentList") or []
    teacher_names = []
    if isinstance(teachers, list):
        for teacher in teachers:
            if isinstance(teacher, dict):
                person = teacher.get("person") if isinstance(teacher.get("person"), dict) else teacher
                name = _first_value(person.get("nameZh"), person.get("nameEn"), person.get("name"))
                if name:
                    teacher_names.append(str(name))
            elif teacher:
                teacher_names.append(str(teacher))
    return {
        "lesson_id": str(_first_value(item.get("id"), item.get("lessonId"))),
        "lesson_code": str(item.get("code") or ""),
        "course_code": str(course.get("code") or item.get("courseCode") or ""),
        "name_zh": str(_first_value(course.get("nameZh"), item.get("courseNameZh"))),
        "name_en": str(_first_value(course.get("nameEn"), item.get("courseNameEn"))),
        "audience": str(_first_value(item.get("nameZh"), item.get("nameEn"))),
        "teachers": "、".join(teacher_names),
        "capacity": str(_first_value(item.get("limitCount"), item.get("capacity"))),
        "credits": str(_first_value(item.get("credits"), course.get("credits"))),
    }


FIELD_LABELS = {
    "id": "ID",
    "lessonId": "课程 lessonId",
    "limitCount": "课程容量",
    "capacity": "课程容量",
    "credits": "学分",
    "teachers": "教师",
    "course": "课程信息",
}


def _field_label(key, prefix):
    """根据字段所在层级给出准确中文含义。"""
    in_course = prefix.startswith("课程信息")
    in_teacher = prefix.startswith("教师")
    if key == "id":
        if in_course:
            return "课程 ID"
        if in_teacher:
            return "教师 ID"
        return "课程 lessonId"
    if key == "code":
        return "课程代码" if in_course else "教学班代码"
    if key == "nameZh":
        if in_course:
            return "中文课程名"
        if in_teacher:
            return "教师中文名"
        return "授课对象（中文）"
    if key == "nameEn":
        if in_course:
            return "英文课程名"
        if in_teacher:
            return "教师英文名"
        return "授课对象（英文）"
    return FIELD_LABELS.get(key, key)


def flatten_course_fields(value, prefix=""):
    """把任意嵌套课程数据转换为便于表格展示的“字段—值”。"""
    rows = []
    if isinstance(value, dict):
        if not value and prefix:
            rows.append((prefix, "{}"))
        for key, child in value.items():
            label = _field_label(key, prefix)
            path = f"{prefix} / {label}" if prefix else label
            rows.extend(flatten_course_fields(child, path))
    elif isinstance(value, list):
        if not value:
            rows.append((prefix, "[]"))
        elif all(not isinstance(child, (dict, list)) for child in value):
            rows.append((prefix, "、".join("空" if child is None else str(child) for child in value)))
        else:
            for index, child in enumerate(value, 1):
                rows.extend(flatten_course_fields(child, f"{prefix} [{index}]"))
    else:
        rows.append((prefix, "空" if value is None else str(value)))
    return rows


def normalize_gui_targets(cfg):
    """返回界面可管理的有效目标，按 lessonId 去重。"""
    result, seen = [], set()
    for raw in cfg.get("targets") or []:
        if not isinstance(raw, dict):
            continue
        lesson_id = str(raw.get("lesson_id") or "").strip()
        name = str(raw.get("course_name") or "").strip()
        try:
            limit = int(raw.get("limit_count") or 0)
        except (TypeError, ValueError):
            limit = 0
        if not lesson_id or lesson_id in seen:
            continue
        seen.add(lesson_id)
        result.append({"lesson_id": lesson_id, "course_name": name,
                       "limit_count": limit,
                       "enabled": bool(raw.get("enabled", True)),
                       "status": raw.get("status") or "pending",
                       "completed_at": str(raw.get("completed_at") or "")})
    return result


def upsert_target(targets, target):
    """按 lessonId 添加或更新目标，不产生重复课程。"""
    normalized = [dict(item) for item in targets]
    lesson_id = str(target["lesson_id"])
    for index, item in enumerate(normalized):
        if str(item.get("lesson_id")) == lesson_id:
            normalized[index] = dict(target)
            return normalized
    normalized.append(dict(target))
    return normalized


class CourseBotGUI:
    FIELD_SPECS = (
        ("student_id", "学生关联 ID", "可留空自动识别"),
        ("turn_id", "选课轮次 ID", "每学期会变化"),
        ("target_lesson_id", "课程 lessonId", "与课程名称至少填一个"),
        ("target_course_name", "课程名称", "找不到 lessonId 时可模糊匹配"),
        ("limit_count", "课程容量", "必须填写准确容量"),
        ("interval_seconds", "查询间隔（秒）", "建议不少于 60 秒"),
        ("jitter_seconds", "随机抖动（秒）", "避免固定查询节拍"),
        ("heart_beat", "非活跃心跳（秒）", "非活跃时段的查询间隔"),
        ("active_hours", "活跃时段", "例如 6:30-1:00；留空为全天"),
        ("max_errors", "异常熔断次数", "连续异常后停止"),
        ("max_grab_failures", "提交失败熔断次数", "建议保持 3"),
        ("notify_webhook_url", "通知地址", "可选，HTTP/HTTPS 地址"),
    )

    def __init__(self, root):
        self.root = root
        self.root.title("中科大选课监控助手")
        self.root.geometry("1040x820")
        self.root.minsize(860, 680)
        self.process = None
        self.pending_result_path = None
        self.pending_result_query = ""
        self.events = queue.Queue()
        self.live_course_status = {}
        self.vars = {}
        self.python = resolve_python()
        self._build_ui()
        self._load_into_form()
        self._refresh_status()
        self.root.after(100, self._poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        title_row = ttk.Frame(outer)
        title_row.pack(fill="x", pady=(0, 8))
        ttk.Label(title_row, text="中科大选课监控助手",
                  font=("Microsoft YaHei UI", 17, "bold")).pack(side="left")
        self.status_var = tk.StringVar(value="准备中…")
        ttk.Label(title_row, textvariable=self.status_var).pack(side="right")

        form = ttk.LabelFrame(outer, text="运行配置", padding=10)
        form.pack(fill="x")
        for column in (1, 4):
            form.columnconfigure(column, weight=1)

        for index, (key, label, hint) in enumerate(self.FIELD_SPECS):
            block = 0 if index < 6 else 3
            row = index if index < 6 else index - 6
            ttk.Label(form, text=label).grid(row=row, column=block, sticky="w", padx=(0, 6), pady=4)
            var = tk.StringVar()
            self.vars[key] = var
            ttk.Entry(form, textvariable=var, width=25).grid(
                row=row, column=block + 1, sticky="ew", padx=(0, 6), pady=4)
            ttk.Label(form, text=hint, foreground="#666666").grid(
                row=row, column=block + 2, sticky="w", padx=(0, 16), pady=4)

        options = ttk.Frame(form)
        options.grid(row=6, column=0, columnspan=6, sticky="ew", pady=(8, 2))
        ttk.Label(options, text="运行模式").pack(side="left")
        self.vars["mode"] = tk.StringVar(value="spam")
        ttk.Combobox(options, textvariable=self.vars["mode"], width=12,
                     state="readonly", values=("spam", "monitor", "grab")).pack(side="left", padx=(6, 20))
        self.vars["headless"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text="隐藏浏览器窗口", variable=self.vars["headless"]).pack(side="left", padx=(0, 18))
        self.vars["disable_browser_sandbox"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text="禁用浏览器沙箱（不推荐）",
                        variable=self.vars["disable_browser_sandbox"]).pack(side="left")

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=10)
        ttk.Button(actions, text="保存配置", command=self.save_form).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="登录教务系统", command=lambda: self._start_action("login")).pack(side="left", padx=6)
        ttk.Button(actions, text="查看可选课程", command=lambda: self._start_action("list")).pack(side="left", padx=6)
        self.start_button = ttk.Button(actions, text="开始监控", command=lambda: self._start_action("run"))
        self.start_button.pack(side="left", padx=6)
        self.stop_button = ttk.Button(actions, text="停止", command=self.stop_process, state="disabled")
        self.stop_button.pack(side="left", padx=6)
        ttk.Button(actions, text="清空日志", command=lambda: self.log.delete("1.0", "end")).pack(side="right")

        filter_row = ttk.Frame(outer)
        filter_row.pack(fill="x", pady=(0, 10))
        ttk.Label(filter_row, text="课程筛选").pack(side="left")
        self.course_filter_var = tk.StringVar()
        filter_entry = ttk.Entry(filter_row, textvariable=self.course_filter_var, width=36)
        filter_entry.pack(side="left", padx=(8, 8))
        filter_entry.bind("<Return>", lambda _event: self._start_action("filter"))
        ttk.Button(filter_row, text="模糊筛选名称或 ID",
                   command=lambda: self._start_action("filter")).pack(side="left")
        ttk.Label(filter_row, text="支持名称片段、简称和少量错字；结果显示全部课程字段",
                  foreground="#666666").pack(side="left", padx=(10, 0))

        target_row = ttk.Frame(outer)
        target_row.pack(fill="x", pady=(0, 10))
        self.target_count_var = tk.StringVar(value="并行课程：0 门")
        ttk.Label(target_row, textvariable=self.target_count_var,
                  font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        ttk.Button(target_row, text="将当前课程加入列表",
                   command=self._add_current_target).pack(side="left", padx=(12, 6))
        ttk.Button(target_row, text="管理并行课程",
                   command=self._show_target_manager).pack(side="left", padx=6)
        ttk.Label(target_row, text="完成策略").pack(side="left", padx=(22, 6))
        self.completion_policy_var = tk.StringVar(value="所有课程分别抢到")
        ttk.Combobox(target_row, textvariable=self.completion_policy_var, state="readonly",
                     width=24, values=("所有课程分别抢到", "任意一门成功就停止全部")).pack(side="left")

        dashboard = ttk.LabelFrame(outer, text="课程实时状态", padding=6)
        dashboard.pack(fill="x", pady=(0, 10))
        dashboard_columns = ("name", "lesson_id", "status", "seats", "updated", "message")
        self.dashboard_tree = ttk.Treeview(dashboard, columns=dashboard_columns,
                                           show="headings", height=6)
        dashboard_headings = {"name": "课程", "lesson_id": "lessonId", "status": "状态",
                              "seats": "人数", "updated": "更新时间", "message": "最新消息"}
        dashboard_widths = {"name": 190, "lesson_id": 85, "status": 75,
                            "seats": 65, "updated": 75, "message": 430}
        for column in dashboard_columns:
            self.dashboard_tree.heading(column, text=dashboard_headings[column])
            self.dashboard_tree.column(column, width=dashboard_widths[column], anchor="w")
        dashboard_y = ttk.Scrollbar(dashboard, orient="vertical",
                                    command=self.dashboard_tree.yview)
        self.dashboard_tree.configure(yscrollcommand=dashboard_y.set)
        self.dashboard_tree.pack(side="left", fill="x", expand=True)
        dashboard_y.pack(side="right", fill="y")
        self.dashboard_tree.tag_configure("completed", foreground="#16833b")
        self.dashboard_tree.tag_configure("available", foreground="#b35a00")
        self.dashboard_tree.tag_configure("error", foreground="#b00020")
        self.dashboard_tree.tag_configure("fused", foreground="#b00020")

        log_frame = ttk.LabelFrame(outer, text="运行日志", padding=8)
        log_frame.pack(fill="both", expand=True)
        self.log = scrolledtext.ScrolledText(
            log_frame, wrap="word", height=18, font=("Consolas", 10), state="normal")
        self.log.pack(fill="both", expand=True)
        self._append_log("界面已启动。请先保存配置，再登录或开始监控。\n")

    def _load_into_form(self):
        try:
            cfg = load_config_file()
        except Exception as exc:
            messagebox.showerror("配置读取失败", str(exc))
            cfg = DEFAULT_CONFIG.copy()
        for key, var in self.vars.items():
            value = cfg.get(key, DEFAULT_CONFIG.get(key, ""))
            var.set(value)
        self.completion_policy_var.set(
            "任意一门成功就停止全部" if cfg.get("completion_policy") == "any"
            else "所有课程分别抢到")
        self._refresh_target_count()

    def _collect_form(self):
        try:
            existing = load_config_file()
        except Exception:
            existing = DEFAULT_CONFIG.copy()
        for key, var in self.vars.items():
            existing[key] = var.get()
        for key in ("student_id", "turn_id", "target_lesson_id",
                    "target_course_name", "notify_webhook_url"):
            existing[key] = str(existing.get(key, "")).strip()
        existing["completion_policy"] = (
            "any" if self.completion_policy_var.get().startswith("任意") else "all")
        return existing

    def save_form(self, for_run=False, quiet=False):
        cfg, errors = validate_config(self._collect_form(), for_run=for_run)
        if errors:
            messagebox.showerror("配置需要修改", "\n".join(f"• {item}" for item in errors))
            return False
        try:
            save_config_file(cfg)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return False
        if not quiet:
            self._append_log("配置已保存。\n")
        return True

    def _target_list(self):
        try:
            return normalize_gui_targets(load_config_file())
        except Exception:
            return []

    def _refresh_target_count(self):
        self._refresh_dashboard_from_config()
        self._update_target_count_label()

    def _update_target_count_label(self):
        targets = [item for item in self._target_list() if item.get("enabled", True)]
        completed = sum(1 for item in targets
                        if self.live_course_status.get(item["lesson_id"], item.get("status"))
                        == "completed")
        self.target_count_var.set(
            f"并行课程：待抢 {len(targets) - completed} 门 · 已完成 {completed} 门")

    def _refresh_dashboard_from_config(self):
        if not hasattr(self, "dashboard_tree"):
            return
        self.dashboard_tree.delete(*self.dashboard_tree.get_children())
        self.live_course_status = {}
        for target in self._target_list():
            status = target.get("status") or "pending"
            self.live_course_status[target["lesson_id"]] = status
            message = (f"完成于 {target['completed_at']}" if target.get("completed_at") else "")
            self.dashboard_tree.insert("", "end", iid=target["lesson_id"],
                                       values=(target["course_name"], target["lesson_id"],
                                               STATUS_LABELS.get(status, status), "", "", message),
                                       tags=(status,))

    def _apply_course_event(self, event):
        lesson_id = str(event.get("lesson_id") or "")
        if not lesson_id:
            return
        status = event.get("status") or "running"
        self.live_course_status[lesson_id] = status
        count, limit = event.get("count"), event.get("limit")
        seats = f"{count}/{limit}" if count is not None and limit is not None else ""
        values = (event.get("course_name") or lesson_id, lesson_id,
                  STATUS_LABELS.get(status, status), seats,
                  event.get("updated_at") or "", event.get("message") or "")
        if self.dashboard_tree.exists(lesson_id):
            self.dashboard_tree.item(lesson_id, values=values, tags=(status,))
        else:
            self.dashboard_tree.insert("", "end", iid=lesson_id, values=values, tags=(status,))
        self._update_target_count_label()

    def _handle_process_output(self, line):
        if line.startswith(EVENT_PREFIX):
            try:
                self._apply_course_event(json.loads(line[len(EVENT_PREFIX):]))
            except Exception as exc:
                self._append_log(f"状态事件解析失败：{exc}\n")
            return
        self._append_log(line)

    def _save_target_list(self, targets):
        cfg = self._collect_form()
        cfg["targets"] = targets
        normalized, errors = validate_config(cfg, for_run=False)
        if errors:
            messagebox.showerror("配置需要修改", "\n".join(f"• {item}" for item in errors))
            return False
        save_config_file(normalized)
        self._refresh_target_count()
        return True

    def _add_target(self, lesson_id, name, capacity, parent=None):
        lesson_id = str(lesson_id or "").strip()
        name = str(name or "").strip()
        try:
            capacity = int(str(capacity or "0").strip())
        except ValueError:
            capacity = 0
        if not lesson_id:
            messagebox.showerror("无法添加", "课程缺少 lessonId。", parent=parent)
            return False
        if capacity <= 0:
            capacity = simpledialog.askinteger(
                "填写课程容量", f"请输入 {name or lesson_id} 的准确容量：",
                minvalue=1, parent=parent or self.root)
            if not capacity:
                return False
        target = {"lesson_id": lesson_id, "course_name": name or f"lessonId={lesson_id}",
                  "limit_count": capacity, "enabled": True,
                  "status": "pending", "completed_at": ""}
        targets = upsert_target(self._target_list(), target)
        if not self._save_target_list(targets):
            return False
        self._append_log(f"已加入并行监控：{target['course_name']} "
                         f"(lessonId={lesson_id}, 容量={capacity})\n")
        return True

    def _add_current_target(self):
        self._add_target(self.vars["target_lesson_id"].get(),
                         self.vars["target_course_name"].get(),
                         self.vars["limit_count"].get())

    def _show_target_manager(self):
        window = tk.Toplevel(self.root)
        window.title("并行监控课程")
        window.geometry("760x430")
        window.transient(self.root)
        outer = ttk.Frame(window, padding=10)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="所有课程会在开始监控时同时启动独立任务。",
                  foreground="#555555").pack(anchor="w", pady=(0, 8))
        tree = ttk.Treeview(outer, columns=("id", "name", "capacity", "status", "completed"),
                            show="headings", selectmode="extended")
        tree.heading("id", text="lessonId")
        tree.heading("name", text="课程名称")
        tree.heading("capacity", text="容量")
        tree.heading("status", text="状态")
        tree.heading("completed", text="完成时间")
        tree.column("id", width=95)
        tree.column("name", width=300)
        tree.column("capacity", width=60)
        tree.column("status", width=75)
        tree.column("completed", width=145)
        tree.pack(fill="both", expand=True)

        def reload_tree():
            tree.delete(*tree.get_children())
            for index, target in enumerate(self._target_list()):
                status = target.get("status") or "pending"
                tree.insert("", "end", iid=str(index), values=(
                    target["lesson_id"], target["course_name"], target["limit_count"],
                    STATUS_LABELS.get(status, status), target.get("completed_at", "")))

        def remove_selected():
            selected = {int(iid) for iid in tree.selection()}
            if not selected:
                return
            targets = [item for index, item in enumerate(self._target_list())
                       if index not in selected]
            self._save_target_list(targets)
            reload_tree()

        def reset_selected():
            selected = {int(iid) for iid in tree.selection()}
            if not selected:
                return
            targets = self._target_list()
            for index in selected:
                targets[index]["status"] = "pending"
                targets[index]["completed_at"] = ""
            self._save_target_list(targets)
            reload_tree()

        reload_tree()
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="删除选中课程", command=remove_selected).pack(side="left")
        ttk.Button(buttons, text="重新设为待监控", command=reset_selected).pack(side="left", padx=8)
        ttk.Button(buttons, text="关闭", command=window.destroy).pack(side="right")

    def _runtime_ready(self):
        check = subprocess.run(
            [self.python, "-c", "import playwright, requests"],
            cwd=HERE, capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if check.returncode == 0:
            return True
        messagebox.showerror(
            "运行环境未安装",
            "当前 Python 环境缺少 Playwright 或 Requests。\n\n"
            "请先按照 README 的“安装环境”步骤安装依赖和 Chromium。")
        return False

    def _start_action(self, action):
        if self.process and self.process.poll() is None:
            messagebox.showinfo("正在运行", "请先停止当前任务。")
            return
        if not self.save_form(for_run=(action == "run"), quiet=True):
            return
        if not self._runtime_ready():
            return

        filter_text = ""
        if action == "filter":
            filter_text = self.course_filter_var.get().strip()
            if not filter_text:
                messagebox.showinfo("请输入筛选条件", "请输入课程名称、名称片段、简称或 lessonId。")
                return

        enabled_targets = [item for item in self._target_list()
                           if item.get("enabled", True) and item.get("status") != "completed"]
        command = ([self.python, MULTI_SCRIPT_PATH] if action == "run" and enabled_targets
                   else [self.python, SCRIPT_PATH])
        labels = {"login": "登录", "list": "课程列表", "filter": "课程筛选", "run": "监控"}
        if action == "login":
            command.append("--login")
        elif action == "list":
            command.append("--list")
        elif action == "filter":
            command.extend(["--list", "--filter", filter_text])

        if action in ("list", "filter"):
            fd, result_path = tempfile.mkstemp(prefix="ustc-course-results-", suffix=".json")
            os.close(fd)
            self.pending_result_path = result_path
            self.pending_result_query = filter_text
            command.extend(["--json-output", result_path])

        self._append_log(f"\n===== 开始{labels[action]} =====\n")
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["USTC_GUI_EVENTS"] = "1"
        try:
            self.process = subprocess.Popen(
                command, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))
            self._discard_pending_results()
            self.process = None
            return

        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set(f"正在{labels[action]}")
        threading.Thread(target=self._read_process, args=(self.process,), daemon=True).start()

    def _read_process(self, process):
        try:
            for line in process.stdout:
                self.events.put(("log", line))
            code = process.wait()
        except Exception as exc:
            self.events.put(("log", f"读取运行日志失败：{exc}\n"))
            code = -1
        self.events.put(("finished", code))

    def _poll_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self._handle_process_output(value)
                elif kind == "finished":
                    self._process_finished(value)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _process_finished(self, code):
        self._append_log(f"===== 任务结束（退出码 {code}）=====\n")
        if self.pending_result_path:
            if code == 0:
                self._open_pending_results()
            else:
                self._discard_pending_results()
        self.process = None
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self._refresh_status()
        self._refresh_target_count()

    def _open_pending_results(self):
        path = self.pending_result_path
        query = self.pending_result_query
        try:
            with open(path, encoding="utf-8") as f:
                items = json.load(f)
            if not isinstance(items, list):
                raise ValueError("课程结果格式不正确")
            self._show_course_results(items, query)
        except Exception as exc:
            messagebox.showerror("课程结果读取失败", str(exc))
        finally:
            self._discard_pending_results()

    def _discard_pending_results(self):
        if self.pending_result_path:
            try:
                os.unlink(self.pending_result_path)
            except OSError:
                pass
        self.pending_result_path = None
        self.pending_result_query = ""

    def _show_course_results(self, items, query=""):
        window = tk.Toplevel(self.root)
        window.title("课程筛选结果" if query else "可选课程")
        window.geometry("1260x720")
        window.minsize(900, 560)
        window.transient(self.root)

        outer = ttk.Frame(window, padding=10)
        outer.pack(fill="both", expand=True)
        title = f"找到 {len(items)} 门课程"
        if query:
            title += f" · 筛选条件：{query}"
        ttk.Label(outer, text=title, font=("Microsoft YaHei UI", 13, "bold")).pack(
            anchor="w", pady=(0, 8))

        panes = ttk.Panedwindow(outer, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.LabelFrame(panes, text="课程列表", padding=6)
        right = ttk.LabelFrame(panes, text="选中课程的全部信息", padding=6)
        panes.add(left, weight=3)
        panes.add(right, weight=2)

        columns = ("lesson_id", "lesson_code", "course_code", "name_zh", "name_en",
                   "audience", "teachers", "capacity", "credits")
        headings = {
            "lesson_id": "lessonId", "lesson_code": "教学班代码", "course_code": "课程代码",
            "name_zh": "课程名称", "name_en": "英文课程名", "audience": "授课对象",
            "teachers": "教师", "capacity": "容量", "credits": "学分",
        }
        widths = {"lesson_id": 80, "lesson_code": 90, "course_code": 80,
                  "name_zh": 180, "name_en": 220, "audience": 120,
                  "teachers": 100, "capacity": 50, "credits": 45}
        course_tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        for column in columns:
            course_tree.heading(column, text=headings[column])
            course_tree.column(column, width=widths[column], minwidth=45, anchor="w")
        course_y = ttk.Scrollbar(left, orient="vertical", command=course_tree.yview)
        course_x = ttk.Scrollbar(left, orient="horizontal", command=course_tree.xview)
        course_tree.configure(yscrollcommand=course_y.set, xscrollcommand=course_x.set)
        course_tree.grid(row=0, column=0, sticky="nsew")
        course_y.grid(row=0, column=1, sticky="ns")
        course_x.grid(row=1, column=0, sticky="ew")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        detail_tree = ttk.Treeview(right, columns=("field", "value"), show="headings")
        detail_tree.heading("field", text="字段")
        detail_tree.heading("value", text="值")
        detail_tree.column("field", width=210, minwidth=130, anchor="w")
        detail_tree.column("value", width=330, minwidth=150, anchor="w")
        detail_y = ttk.Scrollbar(right, orient="vertical", command=detail_tree.yview)
        detail_x = ttk.Scrollbar(right, orient="horizontal", command=detail_tree.xview)
        detail_tree.configure(yscrollcommand=detail_y.set, xscrollcommand=detail_x.set)
        detail_tree.grid(row=0, column=0, sticky="nsew")
        detail_y.grid(row=0, column=1, sticky="ns")
        detail_x.grid(row=1, column=0, sticky="ew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        summaries = []
        for index, item in enumerate(items):
            summary = course_summary(item)
            summaries.append(summary)
            course_tree.insert("", "end", iid=str(index), values=tuple(summary[key] for key in columns))

        def selected_index():
            selection = course_tree.selection()
            return int(selection[0]) if selection else None

        def show_details(_event=None):
            detail_tree.delete(*detail_tree.get_children())
            index = selected_index()
            if index is None:
                return
            for field, value in flatten_course_fields(items[index]):
                detail_tree.insert("", "end", values=(field, value))

        def copy_lesson_id():
            index = selected_index()
            if index is None:
                return
            lesson_id = summaries[index]["lesson_id"]
            window.clipboard_clear()
            window.clipboard_append(lesson_id)
            messagebox.showinfo("已复制", f"lessonId {lesson_id} 已复制。", parent=window)

        def use_selected():
            index = selected_index()
            if index is None:
                messagebox.showinfo("请选择课程", "请先在左侧选择一门课程。", parent=window)
                return
            summary = summaries[index]
            self.vars["target_lesson_id"].set(summary["lesson_id"])
            self.vars["target_course_name"].set(summary["name_zh"] or summary["name_en"])
            if summary["capacity"]:
                self.vars["limit_count"].set(summary["capacity"])
            self.save_form(quiet=True)
            self._append_log(f"已从课程列表填入：{summary['name_zh'] or summary['name_en']} "
                             f"(lessonId={summary['lesson_id']})\n")
            messagebox.showinfo("已填入", "课程信息已填入主界面并保存。", parent=window)

        def add_selected_to_parallel():
            index = selected_index()
            if index is None:
                messagebox.showinfo("请选择课程", "请先在左侧选择一门课程。", parent=window)
                return
            summary = summaries[index]
            if self._add_target(summary["lesson_id"],
                                summary["name_zh"] or summary["name_en"],
                                summary["capacity"], parent=window):
                messagebox.showinfo("已加入", "课程已加入并行监控列表。", parent=window)

        course_tree.bind("<<TreeviewSelect>>", show_details)
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="填入主界面", command=use_selected).pack(side="left")
        ttk.Button(buttons, text="加入并行监控", command=add_selected_to_parallel).pack(side="left", padx=8)
        ttk.Button(buttons, text="复制 lessonId", command=copy_lesson_id).pack(side="left", padx=8)
        ttk.Button(buttons, text="关闭", command=window.destroy).pack(side="right")

        if items:
            course_tree.selection_set("0")
            course_tree.focus("0")
            show_details()
        else:
            ttk.Label(left, text="没有符合条件的课程。", foreground="#666666").grid(
                row=2, column=0, sticky="w", pady=8)

    def stop_process(self):
        if not self.process or self.process.poll() is not None:
            return
        self._append_log("正在停止任务…\n")
        self._terminate_process_tree()

    def _terminate_process_tree(self):
        """停止本程序启动的任务及其浏览器子进程，避免残留后台进程。"""
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                self.process.terminate()
        except Exception as exc:
            self._append_log(f"停止失败：{exc}\n")

    def _refresh_status(self):
        auth = "已登录过" if os.path.exists(AUTH_PATH) else "尚未生成登录态"
        self.status_var.set(f"{auth} · Python {os.path.basename(self.python)}")

    def _append_log(self, text):
        self.log.insert("end", text)
        self.log.see("end")

    def _on_close(self):
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno("退出", "任务仍在运行，是否停止任务并退出？"):
                return
            self._terminate_process_tree()
        self._discard_pending_results()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if os.name == "nt" else "clam")
    except tk.TclError:
        pass
    CourseBotGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
