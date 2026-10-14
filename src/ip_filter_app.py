# -*- coding: utf-8 -*-
"""IP 地址筛选工具

功能：
1. 批量输入 IP 地址（IPv4 / IPv6）。
2. 与持久化的历史 IP 记录比较。
3. 历史中不存在的 IP 会进入“本次新增 IP”，并写入历史记录。
4. 历史中已存在的 IP 不进入“本次新增 IP”。
"""

from __future__ import annotations

import csv
import ipaddress
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, VERTICAL, Menu, StringVar, TclError, Tk, filedialog, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText


APP_NAME = "IP筛选工具"
APP_VERSION = "1.1.1"
AUTHOR = "Uncledavid"
SPLIT_PATTERN = re.compile(r"[\s,，;；]+")


def get_data_dir() -> Path:
    """返回历史记录存储目录。"""
    override = os.environ.get("IP_FILTER_DATA_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / f".{APP_NAME}"


def get_history_path() -> Path:
    return get_data_dir() / "history.json"


def get_resource_path(filename: str) -> Path:
    """返回源码运行或 PyInstaller 打包后的资源路径。"""
    bundle_dir = getattr(sys, "_MEIPASS", None)
    base_dir = Path(bundle_dir) if bundle_dir else Path(__file__).resolve().parent
    return base_dir / filename


@dataclass(frozen=True)
class ParseResult:
    valid: list[str]
    invalid: list[str]
    duplicate_count: int


def parse_ip_text(text: str) -> ParseResult:
    """解析批量输入，按输入顺序返回规范化且去重后的有效 IP。"""
    tokens = [token.strip() for token in SPLIT_PATTERN.split(text) if token.strip()]
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    duplicate_count = 0

    for token in tokens:
        try:
            ip = str(ipaddress.ip_address(token))
        except ValueError:
            invalid.append(token)
            continue
        if ip in seen:
            duplicate_count += 1
            continue
        seen.add(ip)
        valid.append(ip)

    return ParseResult(valid=valid, invalid=invalid, duplicate_count=duplicate_count)


def load_history(path: Path | None = None) -> dict[str, dict[str, object]]:
    """读取历史记录；兼容旧的纯列表格式。"""
    path = path or get_history_path()
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        try:
            if path.exists():
                backup = path.with_suffix(f".broken-{datetime.now():%Y%m%d-%H%M%S}.json")
                shutil.copy2(path, backup)
        except OSError:
            pass
        return {}

    history: dict[str, dict[str, object]] = {}
    now = format_time(datetime.now())
    if isinstance(raw, list):
        for item in raw:
            ip = str(item).strip()
            if ip:
                try:
                    ip = str(ipaddress.ip_address(ip))
                except ValueError:
                    continue
                history[ip] = {"first_seen": now, "last_seen": now, "seen_count": 1}
        return history

    if not isinstance(raw, dict):
        return {}

    records = raw.get("records", raw)
    if not isinstance(records, dict):
        return {}

    for raw_ip, value in records.items():
        try:
            ip = str(ipaddress.ip_address(str(raw_ip).strip()))
        except ValueError:
            continue
        if isinstance(value, dict):
            first_seen = str(value.get("first_seen") or now)
            last_seen = str(value.get("last_seen") or first_seen)
            try:
                seen_count = max(1, int(value.get("seen_count", 1)))
            except (TypeError, ValueError):
                seen_count = 1
        else:
            first_seen = now
            last_seen = now
            seen_count = 1
        history[ip] = {
            "first_seen": first_seen,
            "last_seen": last_seen,
            "seen_count": seen_count,
        }
    return history


def save_history(history: dict[str, dict[str, object]], path: Path | None = None) -> None:
    """原子化保存历史记录，降低异常退出导致文件损坏的风险。"""
    path = path or get_history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": APP_VERSION,
        "saved_at": format_time(datetime.now()),
        "records": history,
    }

    fd, temp_name = tempfile.mkstemp(prefix="history-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def filter_ips(text: str, history: dict[str, dict[str, object]]) -> tuple[ParseResult, list[str], int]:
    """筛选输入 IP。返回解析结果、本次新增列表、已存在数量。"""
    parsed = parse_ip_text(text)
    new_ips: list[str] = []
    existing_count = 0
    now = format_time(datetime.now())

    for ip in parsed.valid:
        record = history.get(ip)
        if record is None:
            history[ip] = {
                "first_seen": now,
                "last_seen": now,
                "seen_count": 1,
            }
            new_ips.append(ip)
        else:
            existing_count += 1
            record["last_seen"] = now
            try:
                record["seen_count"] = int(record.get("seen_count", 1)) + 1
            except (TypeError, ValueError):
                record["seen_count"] = 2

    return parsed, new_ips, existing_count


def format_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


class IPFilterApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.history = load_history()
        self.current_new_ips: list[str] = []
        self.status_var = StringVar()
        self.history_query_var = StringVar()
        self.checked_history_ips: set[str] = set()
        self.checked_new_ips: set[str] = set()
        self.new_ip_added_at: dict[str, str] = {}
        self._configure_window()
        self._configure_styles()
        self._build_ui()
        self._refresh_history_tree()
        self._refresh_new_tree()
        self._set_status("就绪。请粘贴或输入 IP 地址后点击“开始筛选”。")

    def _configure_window(self) -> None:
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("1100x760")
        self.root.minsize(860, 600)
        icon_path = get_resource_path("ip_filter.ico")
        if icon_path.exists():
            try:
                self.root.iconbitmap(default=str(icon_path))
            except TclError:
                pass

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except Exception:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Hint.TLabel", foreground="#555555")
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(16, 8))
        style.configure("Treeview", rowheight=27, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=14)
        main.pack(fill=BOTH, expand=True)

        header = ttk.Frame(main)
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="IP 地址筛选工具", style="Title.TLabel").pack(side=LEFT)
        ttk.Label(
            header,
            text="历史中不存在 → 放入新增并写入历史；历史中已存在 → 跳过",
            style="Hint.TLabel",
        ).pack(side=LEFT, padx=(16, 0), pady=(5, 0))

        input_frame = ttk.LabelFrame(main, text="批量输入待筛选 IP（每行一个，也支持空格、逗号分隔）", padding=10)
        input_frame.pack(fill="x", pady=(0, 10))

        self.input_text = ScrolledText(
            input_frame,
            height=7,
            font=("Consolas", 10),
            wrap="word",
            undo=True,
        )
        self.input_text.pack(fill="x")

        input_actions = ttk.Frame(input_frame)
        input_actions.pack(fill="x", pady=(8, 0))
        ttk.Button(
            input_actions,
            text="开始筛选",
            style="Primary.TButton",
            command=self.on_filter,
        ).pack(side=LEFT)
        ttk.Button(input_actions, text="清空输入", command=self.clear_input).pack(side=LEFT, padx=(8, 0))
        ttk.Button(input_actions, text="粘贴示例", command=self.insert_example).pack(side=LEFT, padx=(8, 0))

        actions = ttk.Frame(main)
        actions.pack(fill="x", pady=(0, 8))
        ttk.Button(actions, text="导出历史记录", command=self.export_history).pack(side=LEFT)
        ttk.Button(actions, text="打开数据目录", command=self.open_data_dir).pack(side=LEFT, padx=(8, 0))
        ttk.Button(actions, text="删除勾选历史 IP", command=self.delete_checked_history_ips).pack(side=LEFT, padx=(8, 0))
        ttk.Button(actions, text="复制勾选历史 IP", command=self.copy_checked_history_ips).pack(side=LEFT, padx=(8, 0))
        ttk.Button(actions, text="清空历史记录", command=self.clear_history).pack(side=LEFT, padx=(8, 0))
        ttk.Button(actions, text="删除勾选新增 IP", command=self.delete_checked_new_ips).pack(side=LEFT, padx=(8, 0))
        ttk.Button(actions, text="复制勾选新增 IP", command=self.copy_checked_new_ips).pack(side=LEFT, padx=(8, 0))

        panes = ttk.Panedwindow(main, orient="horizontal")
        panes.pack(fill=BOTH, expand=True)

        self.history_frame = ttk.LabelFrame(panes, text="历史 IP 记录（0）", padding=8)
        self.new_frame = ttk.LabelFrame(panes, text="本次新增 IP（0）", padding=8)
        panes.add(self.history_frame, weight=1)
        panes.add(self.new_frame, weight=1)

        history_columns = ("checked", "index", "ip", "first_seen", "last_seen", "seen_count")
        self.history_tree = ttk.Treeview(self.history_frame, columns=history_columns, show="headings", selectmode="extended")
        self.history_tree.heading("checked", text="勾选")
        self.history_tree.heading("index", text="#")
        self.history_tree.heading("ip", text="IP 地址")
        self.history_tree.heading("first_seen", text="首次添加")
        self.history_tree.heading("last_seen", text="最近出现")
        self.history_tree.heading("seen_count", text="出现次数")
        self.history_tree.column("checked", width=58, anchor="center", stretch=False)
        self.history_tree.column("index", width=52, anchor="center", stretch=False)
        self.history_tree.column("ip", width=180, anchor="w")
        self.history_tree.column("first_seen", width=150, anchor="center")
        self.history_tree.column("last_seen", width=150, anchor="center")
        self.history_tree.column("seen_count", width=80, anchor="center", stretch=False)
        self.history_tree.bind("<ButtonRelease-1>", self.on_history_tree_click)
        self.history_tree.bind("<space>", lambda _event: self.toggle_selected_history_checks())
        self.history_tree.bind("<Delete>", lambda _event: self.delete_checked_history_ips())

        self.history_menu = Menu(self.root, tearoff=False)
        self.history_menu.add_command(label="复制勾选历史 IP", command=self.copy_checked_history_ips)
        self.history_menu.add_command(label="删除勾选历史 IP", command=self.delete_checked_history_ips)
        self.history_menu.add_separator()
        self.history_menu.add_command(label="勾选/取消当前行", command=self.toggle_selected_history_checks)
        self.history_tree.bind("<Button-3>", self.show_history_menu)

        history_search = ttk.Frame(self.history_frame)
        history_search.pack(fill="x", pady=(0, 6))

        self.check_all_button = ttk.Button(
            history_search,
            text="全选当前",
            command=self.toggle_all_filtered_history,
        )
        self.check_all_button.pack(side=LEFT)

        ttk.Label(history_search, text="查询历史 IP：").pack(side=LEFT, padx=(8, 0))
        self.history_query_entry = ttk.Entry(history_search, textvariable=self.history_query_var)
        self.history_query_entry.pack(side=LEFT, fill="x", expand=True, padx=(0, 6))
        self.history_query_entry.bind("<Return>", lambda _event: self._refresh_history_tree())
        self.history_query_entry.bind("<Button-3>", self.show_history_query_menu)

        self.history_query_menu = Menu(self.root, tearoff=False)
        self.history_query_menu.add_command(label="粘贴 IP", command=self.paste_history_query)
        self.history_query_menu.add_command(label="复制", command=lambda: self.history_query_entry.event_generate("<<Copy>>"))
        self.history_query_menu.add_command(label="全选", command=lambda: self.history_query_entry.selection_range(0, END))
        self.history_query_menu.add_separator()
        self.history_query_menu.add_command(label="清空", command=self.clear_history_query)

        ttk.Button(history_search, text="查询", command=self._refresh_history_tree).pack(side=LEFT)
        ttk.Button(history_search, text="重置", command=self.reset_history_query).pack(side=LEFT, padx=(6, 0))

        history_scroll_y = ttk.Scrollbar(self.history_frame, orient=VERTICAL, command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=history_scroll_y.set)
        self.history_tree.pack(side=LEFT, fill=BOTH, expand=True)
        history_scroll_y.pack(side=RIGHT, fill="y")

        new_controls = ttk.Frame(self.new_frame)
        new_controls.pack(fill="x", pady=(0, 6))
        self.check_all_new_button = ttk.Button(
            new_controls,
            text="全选当前",
            command=self.toggle_all_new,
            state="disabled",
        )
        self.check_all_new_button.pack(side=LEFT)

        new_columns = ("checked", "index", "ip", "added_at")
        self.new_tree = ttk.Treeview(self.new_frame, columns=new_columns, show="headings", selectmode="extended")
        self.new_tree.heading("checked", text="勾选")
        self.new_tree.heading("index", text="#")
        self.new_tree.heading("ip", text="本次新增 IP")
        self.new_tree.heading("added_at", text="添加时间")
        self.new_tree.column("checked", width=58, anchor="center", stretch=False)
        self.new_tree.column("index", width=52, anchor="center", stretch=False)
        self.new_tree.column("ip", width=250, anchor="w")
        self.new_tree.column("added_at", width=160, anchor="center")
        self.new_tree.bind("<ButtonRelease-1>", self.on_new_tree_click)
        self.new_tree.bind("<space>", lambda _event: self.toggle_selected_new_checks())
        self.new_tree.bind("<Delete>", lambda _event: self.delete_checked_new_ips())

        self.new_menu = Menu(self.root, tearoff=False)
        self.new_menu.add_command(label="复制勾选新增 IP", command=self.copy_checked_new_ips)
        self.new_menu.add_command(label="删除勾选新增 IP", command=self.delete_checked_new_ips)
        self.new_menu.add_separator()
        self.new_menu.add_command(label="勾选/取消当前行", command=self.toggle_selected_new_checks)
        self.new_tree.bind("<Button-3>", self.show_new_menu)

        new_scroll_y = ttk.Scrollbar(self.new_frame, orient=VERTICAL, command=self.new_tree.yview)
        self.new_tree.configure(yscrollcommand=new_scroll_y.set)
        self.new_tree.pack(side=LEFT, fill=BOTH, expand=True)
        new_scroll_y.pack(side=RIGHT, fill="y")

        status_row = ttk.Frame(main)
        status_row.pack(fill="x", pady=(10, 0))

        status = ttk.Label(status_row, textvariable=self.status_var, style="Hint.TLabel", anchor="w")
        status.pack(side=LEFT, fill="x", expand=True)

        author = ttk.Label(
            status_row,
            text=f"作者：{AUTHOR}",
            style="Hint.TLabel",
            anchor="e",
        )
        author.pack(side=RIGHT, padx=(12, 0))

    def _refresh_history_tree(self) -> None:
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)

        query = self.history_query_var.get().strip().lower()

        def sort_key(item: tuple[str, dict[str, object]]) -> tuple[str, str]:
            ip, record = item
            return (str(record.get("first_seen", "")), ip)

        filtered_items = [
            item for item in self.history.items()
            if not query or query in item[0].lower()
        ]

        for index, (ip, record) in enumerate(sorted(filtered_items, key=sort_key), start=1):
            self.history_tree.insert(
                "",
                END,
                values=(
                    "☑" if ip in self.checked_history_ips else "☐",
                    index,
                    ip,
                    record.get("first_seen", ""),
                    record.get("last_seen", ""),
                    record.get("seen_count", 1),
                ),
            )

        checked_text = f"，已勾选 {len(self.checked_history_ips)}" if self.checked_history_ips else ""
        if query:
            self.history_frame.configure(
                text=f"历史 IP 记录（显示 {len(filtered_items)} / 共 {len(self.history)}{checked_text}）"
            )
        else:
            self.history_frame.configure(text=f"历史 IP 记录（{len(self.history)}{checked_text}）")

        visible_ips = [ip for ip, _record in filtered_items]
        all_visible_checked = bool(visible_ips) and all(ip in self.checked_history_ips for ip in visible_ips)
        self.check_all_button.configure(
            text="取消全选" if all_visible_checked else "全选当前",
            state="normal" if visible_ips else "disabled",
        )

    def reset_history_query(self) -> None:
        self.history_query_var.set("")
        self._refresh_history_tree()

    def clear_history_query(self) -> None:
        self.history_query_var.set("")
        self._refresh_history_tree()

    def paste_history_query(self) -> None:
        try:
            clipboard_text = self.root.clipboard_get()
        except TclError:
            return

        parts = [part for part in SPLIT_PATTERN.split(clipboard_text.strip()) if part]
        if not parts:
            return

        self.history_query_var.set(parts[0])
        self._refresh_history_tree()
        self.history_query_entry.focus_set()
        self.history_query_entry.icursor(END)

    def show_history_query_menu(self, event) -> None:
        self.history_query_entry.focus_set()
        try:
            self.history_query_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.history_query_menu.grab_release()

    def show_history_menu(self, event) -> None:
        item_id = self.history_tree.identify_row(event.y)
        clicked_ip = None
        if item_id:
            values = self.history_tree.item(item_id, "values")
            if len(values) >= 3:
                clicked_ip = str(values[2])
            if item_id not in self.history_tree.selection():
                self.history_tree.selection_set(item_id)
            self.history_tree.focus(item_id)

        checked_count = len(self.checked_history_ips)
        if clicked_ip and clicked_ip not in self.checked_history_ips:
            copy_label = "复制此 IP"
            delete_label = "删除此 IP"
            copy_state = "normal"
            delete_state = "normal"
            copy_action = lambda ip=clicked_ip: self.copy_specific_history_ips([ip])
            delete_action = lambda ip=clicked_ip: self.delete_specific_history_ips([ip])
        elif checked_count:
            copy_label = f"复制勾选历史 IP（{checked_count}）"
            delete_label = f"删除勾选历史 IP（{checked_count}）"
            copy_state = "normal"
            delete_state = "normal"
            copy_action = self.copy_checked_history_ips
            delete_action = self.delete_checked_history_ips
        else:
            copy_label = "复制此 IP"
            delete_label = "删除此 IP"
            copy_state = "disabled"
            delete_state = "disabled"
            copy_action = self.copy_checked_history_ips
            delete_action = self.delete_checked_history_ips

        self.history_menu.entryconfigure(
            0,
            label=copy_label,
            state=copy_state,
            command=copy_action,
        )
        self.history_menu.entryconfigure(
            1,
            label=delete_label,
            state=delete_state,
            command=delete_action,
        )
        self.history_menu.entryconfigure(3, state="normal" if item_id else "disabled")

        try:
            self.history_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.history_menu.grab_release()

    def on_history_tree_click(self, event) -> None:
        if self.history_tree.identify_region(event.x, event.y) != "cell":
            return
        if self.history_tree.identify_column(event.x) != "#1":
            return

        item_id = self.history_tree.identify_row(event.y)
        if not item_id:
            return

        values = self.history_tree.item(item_id, "values")
        if len(values) < 3:
            return

        ip = str(values[2])
        if ip in self.checked_history_ips:
            self.checked_history_ips.remove(ip)
        else:
            self.checked_history_ips.add(ip)
        self._refresh_history_tree()

    def toggle_selected_history_checks(self) -> None:
        selected_ips = self._history_ips_from_tree_items(self.history_tree.selection())
        if not selected_ips:
            return

        if all(ip in self.checked_history_ips for ip in selected_ips):
            for ip in selected_ips:
                self.checked_history_ips.discard(ip)
        else:
            self.checked_history_ips.update(selected_ips)
        self._refresh_history_tree()

    def toggle_all_filtered_history(self) -> None:
        visible_ips = self._history_ips_from_tree_items(self.history_tree.get_children())
        if not visible_ips:
            return

        if all(ip in self.checked_history_ips for ip in visible_ips):
            self.checked_history_ips.clear()
        else:
            self.checked_history_ips.update(visible_ips)
        self._refresh_history_tree()

    def _history_ips_from_tree_items(self, item_ids) -> list[str]:
        ips: list[str] = []
        for item_id in item_ids:
            values = self.history_tree.item(item_id, "values")
            if len(values) >= 3:
                ips.append(str(values[2]))
        return list(dict.fromkeys(ips))

    def on_new_tree_click(self, event) -> None:
        if self.new_tree.identify_region(event.x, event.y) != "cell":
            return
        if self.new_tree.identify_column(event.x) != "#1":
            return

        item_id = self.new_tree.identify_row(event.y)
        if not item_id:
            return

        values = self.new_tree.item(item_id, "values")
        if len(values) < 3:
            return

        ip = str(values[2])
        if ip in self.checked_new_ips:
            self.checked_new_ips.remove(ip)
        else:
            self.checked_new_ips.add(ip)
        self._refresh_new_tree()

    def toggle_selected_new_checks(self) -> None:
        selected_ips = self._new_ips_from_tree_items(self.new_tree.selection())
        if not selected_ips:
            return

        if all(ip in self.checked_new_ips for ip in selected_ips):
            for ip in selected_ips:
                self.checked_new_ips.discard(ip)
        else:
            self.checked_new_ips.update(selected_ips)
        self._refresh_new_tree()

    def toggle_all_new(self) -> None:
        visible_ips = list(self.current_new_ips)
        if not visible_ips:
            return

        if all(ip in self.checked_new_ips for ip in visible_ips):
            self.checked_new_ips.clear()
        else:
            self.checked_new_ips.update(visible_ips)
        self._refresh_new_tree()

    def _new_ips_from_tree_items(self, item_ids) -> list[str]:
        ips: list[str] = []
        for item_id in item_ids:
            values = self.new_tree.item(item_id, "values")
            if len(values) >= 3:
                ips.append(str(values[2]))
        return list(dict.fromkeys(ips))

    def show_new_menu(self, event) -> None:
        item_id = self.new_tree.identify_row(event.y)
        clicked_ip = None
        if item_id:
            values = self.new_tree.item(item_id, "values")
            if len(values) >= 3:
                clicked_ip = str(values[2])
            if item_id not in self.new_tree.selection():
                self.new_tree.selection_set(item_id)
            self.new_tree.focus(item_id)

        checked_count = len(self.checked_new_ips)
        if clicked_ip and clicked_ip not in self.checked_new_ips:
            copy_label = "复制此 IP"
            delete_label = "删除此 IP"
            copy_state = "normal"
            delete_state = "normal"
            copy_action = lambda ip=clicked_ip: self.copy_specific_new_ips([ip])
            delete_action = lambda ip=clicked_ip: self.delete_specific_new_ips([ip])
        elif checked_count:
            copy_label = f"复制勾选新增 IP（{checked_count}）"
            delete_label = f"删除勾选新增 IP（{checked_count}）"
            copy_state = "normal"
            delete_state = "normal"
            copy_action = self.copy_checked_new_ips
            delete_action = self.delete_checked_new_ips
        else:
            copy_label = "复制此 IP"
            delete_label = "删除此 IP"
            copy_state = "disabled"
            delete_state = "disabled"
            copy_action = self.copy_checked_new_ips
            delete_action = self.delete_checked_new_ips

        self.new_menu.entryconfigure(0, label=copy_label, state=copy_state, command=copy_action)
        self.new_menu.entryconfigure(1, label=delete_label, state=delete_state, command=delete_action)
        self.new_menu.entryconfigure(3, state="normal" if item_id else "disabled")

        try:
            self.new_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.new_menu.grab_release()

    def copy_checked_new_ips(self) -> None:
        checked_ips = [ip for ip in self.current_new_ips if ip in self.checked_new_ips]
        if not checked_ips:
            messagebox.showinfo(APP_NAME, "请先勾选要复制的新增 IP。", parent=self.root)
            return

        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(checked_ips))
        self.root.update()
        self._set_status(f"已复制 {len(checked_ips)} 个已勾选的新增 IP 到剪贴板。")

    def copy_specific_new_ips(self, ips: list[str]) -> None:
        unique_ips = list(dict.fromkeys(ip for ip in ips if ip in self.current_new_ips))
        if not unique_ips:
            messagebox.showinfo(APP_NAME, "当前没有可复制的新增 IP。", parent=self.root)
            return

        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(unique_ips))
        self.root.update()
        self._set_status(f"已复制 {len(unique_ips)} 个右击选择的新增 IP 到剪贴板。")

    def delete_checked_new_ips(self) -> None:
        target_ips = [ip for ip in self.current_new_ips if ip in self.checked_new_ips]
        self._delete_new_ips(target_ips, "已勾选的新增 IP")

    def delete_specific_new_ips(self, ips: list[str]) -> None:
        target_ips = [ip for ip in self.current_new_ips if ip in set(ips)]
        self._delete_new_ips(target_ips, "右击选择的新增 IP")

    def _delete_new_ips(self, target_ips: list[str], source_label: str) -> None:
        if not target_ips:
            messagebox.showinfo(APP_NAME, "当前没有可删除的新增 IP。", parent=self.root)
            return

        if len(target_ips) == 1:
            detail = target_ips[0]
        else:
            preview = "、".join(target_ips[:5])
            if len(target_ips) > 5:
                preview += "…"
            detail = f"{len(target_ips)} 个 IP（{preview}）"

        if not messagebox.askyesno(
            APP_NAME,
            f"确定删除{source_label}吗？\n{detail}\n这些 IP 也会从历史记录中删除，此操作不可撤销。",
            parent=self.root,
        ):
            return

        target_set = set(target_ips)
        new_history = {
            ip: record for ip, record in self.history.items()
            if ip not in target_set
        }
        try:
            save_history(new_history)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"删除失败：\n{exc}", parent=self.root)
            return

        deleted_count = len(self.history) - len(new_history)
        self.history = new_history
        self.current_new_ips = [ip for ip in self.current_new_ips if ip not in target_set]
        self.new_ip_added_at = {
            ip: added_at for ip, added_at in self.new_ip_added_at.items()
            if ip not in target_set
        }
        self.checked_history_ips.difference_update(target_set)
        self.checked_new_ips.difference_update(target_set)
        self._refresh_history_tree()
        self._refresh_new_tree()
        self._set_status(f"已删除 {len(target_ips)} 个新增 IP，并同步移除 {deleted_count} 条历史记录。")

    def _refresh_new_tree(self) -> None:
        for item in self.new_tree.get_children():
            self.new_tree.delete(item)
        for index, ip in enumerate(self.current_new_ips, start=1):
            self.new_tree.insert(
                "",
                END,
                values=(
                    "☑" if ip in self.checked_new_ips else "☐",
                    index,
                    ip,
                    self.new_ip_added_at.get(ip, ""),
                ),
            )

        checked_text = f"，已勾选 {len(self.checked_new_ips)}" if self.checked_new_ips else ""
        self.new_frame.configure(text=f"本次新增 IP（{len(self.current_new_ips)}{checked_text}）")

        visible_ips = list(self.current_new_ips)
        all_visible_checked = bool(visible_ips) and all(ip in self.checked_new_ips for ip in visible_ips)
        self.check_all_new_button.configure(
            text="取消全选" if all_visible_checked else "全选当前",
            state="normal" if visible_ips else "disabled",
        )

    def on_filter(self) -> None:
        text = self.input_text.get("1.0", END)
        if not text.strip():
            messagebox.showinfo(APP_NAME, "请先输入需要筛选的 IP 地址。", parent=self.root)
            return

        try:
            parsed, new_ips, existing_count = filter_ips(text, self.history)
            save_history(self.history)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"保存历史记录失败：\n{exc}", parent=self.root)
            return

        self.current_new_ips = new_ips
        self.new_ip_added_at = {ip: format_time(datetime.now()) for ip in new_ips}
        self.checked_new_ips.clear()
        self._refresh_history_tree()
        self._refresh_new_tree()

        invalid_count = len(parsed.invalid)
        duplicate_count = parsed.duplicate_count
        message = (
            f"本次输入 {len(parsed.valid) + invalid_count + duplicate_count} 个；"
            f"有效且去重后 {len(parsed.valid)} 个；新增 {len(new_ips)} 个；"
            f"历史已存在 {existing_count} 个；重复输入 {duplicate_count} 个；无效 {invalid_count} 个。"
        )
        if parsed.invalid:
            preview = "、".join(parsed.invalid[:8])
            if len(parsed.invalid) > 8:
                preview += "…"
            message += f" 无效项：{preview}"
        self._set_status(message)

    def clear_input(self) -> None:
        self.input_text.delete("1.0", END)

    def insert_example(self) -> None:
        sample = "192.168.1.10\n192.168.1.11\n10.0.0.1\n2001:db8::1"
        if self.input_text.get("1.0", END).strip():
            self.input_text.insert(END, "\n" + sample)
        else:
            self.input_text.insert("1.0", sample)

    def export_history(self) -> None:
        if not self.history:
            messagebox.showinfo(APP_NAME, "当前没有历史记录可导出。", parent=self.root)
            return
        default_name = f"IP历史记录_{datetime.now():%Y%m%d_%H%M%S}.csv"
        file_path = filedialog.asksaveasfilename(
            parent=self.root,
            title="导出历史记录",
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not file_path:
            return
        try:
            with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["IP地址", "首次添加", "最近出现", "出现次数"])
                for ip, record in sorted(self.history.items()):
                    writer.writerow([
                        ip,
                        record.get("first_seen", ""),
                        record.get("last_seen", ""),
                        record.get("seen_count", 1),
                    ])
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"导出失败：\n{exc}", parent=self.root)
            return
        self._set_status(f"历史记录已导出：{file_path}")

    def delete_checked_history_ips(self) -> None:
        checked_ips = sorted(self.checked_history_ips)
        if not checked_ips:
            messagebox.showinfo(APP_NAME, "请先勾选要删除的历史 IP。", parent=self.root)
            return

        if len(checked_ips) == 1:
            detail = checked_ips[0]
        else:
            preview = "、".join(checked_ips[:5])
            if len(checked_ips) > 5:
                preview += "…"
            detail = f"{len(checked_ips)} 个 IP（{preview}）"

        if not messagebox.askyesno(
            APP_NAME,
            f"确定删除已勾选的历史 IP 吗？\n{detail}\n此操作不可撤销。",
            parent=self.root,
        ):
            return

        checked_set = set(checked_ips)
        new_history = {
            ip: record for ip, record in self.history.items()
            if ip not in checked_set
        }
        try:
            save_history(new_history)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"删除失败：\n{exc}", parent=self.root)
            return

        deleted_count = len(self.history) - len(new_history)
        self.history = new_history
        self.checked_history_ips.difference_update(checked_set)
        self._refresh_history_tree()
        self._set_status(f"已删除 {deleted_count} 个已勾选的历史 IP。")

    def copy_specific_history_ips(self, ips: list[str]) -> None:
        unique_ips = list(dict.fromkeys(ip for ip in ips if ip in self.history))
        if not unique_ips:
            messagebox.showinfo(APP_NAME, "当前没有可复制的历史 IP。", parent=self.root)
            return

        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(unique_ips))
        self.root.update()
        self._set_status(f"已复制 {len(unique_ips)} 个右击选择的历史 IP 到剪贴板。")

    def delete_specific_history_ips(self, ips: list[str]) -> None:
        target_ips = sorted({ip for ip in ips if ip in self.history})
        if not target_ips:
            messagebox.showinfo(APP_NAME, "当前没有可删除的历史 IP。", parent=self.root)
            return

        if len(target_ips) == 1:
            detail = target_ips[0]
        else:
            preview = "、".join(target_ips[:5])
            if len(target_ips) > 5:
                preview += "…"
            detail = f"{len(target_ips)} 个 IP（{preview}）"

        if not messagebox.askyesno(
            APP_NAME,
            f"确定删除右击选择的历史 IP 吗？\n{detail}\n此操作不可撤销。",
            parent=self.root,
        ):
            return

        target_set = set(target_ips)
        new_history = {
            ip: record for ip, record in self.history.items()
            if ip not in target_set
        }
        try:
            save_history(new_history)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"删除失败：\n{exc}", parent=self.root)
            return

        deleted_count = len(self.history) - len(new_history)
        self.history = new_history
        self.checked_history_ips.difference_update(target_set)
        self._refresh_history_tree()
        self._set_status(f"已删除 {deleted_count} 个右击选择的历史 IP。")

    def copy_checked_history_ips(self) -> None:
        checked_ips = sorted(self.checked_history_ips)
        if not checked_ips:
            messagebox.showinfo(APP_NAME, "请先勾选要复制的历史 IP。", parent=self.root)
            return

        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(checked_ips))
        self.root.update()
        self._set_status(f"已复制 {len(checked_ips)} 个已勾选的历史 IP 到剪贴板。")

    def clear_history(self) -> None:
        if not self.history:
            messagebox.showinfo(APP_NAME, "当前历史记录已经为空。", parent=self.root)
            return
        if not messagebox.askyesno(
            APP_NAME,
            f"确定清空全部 {len(self.history)} 条历史 IP 记录吗？\n此操作不可撤销。",
            parent=self.root,
        ):
            return
        try:
            save_history({})
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"清空失败：\n{exc}", parent=self.root)
            return
        self.history = {}
        self.checked_history_ips.clear()
        self._refresh_history_tree()
        self._set_status("历史 IP 记录已清空。")

    def open_data_dir(self) -> None:
        path = get_data_dir()
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"无法打开数据目录：\n{exc}", parent=self.root)

    def _set_status(self, text: str) -> None:
        self.status_var.set(f"{text}  数据文件：{get_history_path()}")


def run_smoke_test() -> int:
    """无界面自检，供打包后命令行验证使用。"""
    with tempfile.TemporaryDirectory(prefix="ip-filter-test-") as temp_dir:
        path = Path(temp_dir) / "history.json"
        history = load_history(path)
        parsed, new_ips, existing = filter_ips("192.168.1.1\n10.0.0.1, 192.168.1.1\ninvalid", history)
        assert parsed.valid == ["192.168.1.1", "10.0.0.1"], parsed
        assert new_ips == ["192.168.1.1", "10.0.0.1"], new_ips
        assert existing == 0, existing
        assert parsed.duplicate_count == 1, parsed.duplicate_count
        assert parsed.invalid == ["invalid"], parsed.invalid
        save_history(history, path)
        loaded = load_history(path)
        assert set(loaded) == {"192.168.1.1", "10.0.0.1"}, loaded
        _, second_new, second_existing = filter_ips("192.168.1.1\n2001:db8::1", loaded)
        assert second_new == ["2001:db8::1"], second_new
        assert second_existing == 1, second_existing
    print("SMOKE_TEST_OK")
    return 0


def main() -> int:
    if "--smoke-test" in sys.argv:
        return run_smoke_test()
    root = Tk()
    IPFilterApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
