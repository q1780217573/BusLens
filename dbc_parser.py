#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DBC 报文解析工具 v3.1 — Win11 风格 GUI
========================================
功能：DBC 解析 + 日志导入(TRC/ASC/BLF) + 格式转换
"""

import sys
import os
import re
import json
import time
import ctypes
from ctypes import wintypes
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from collections import OrderedDict, Counter
from pathlib import Path
from datetime import datetime

import customtkinter as ctk
from PIL import Image

# 拖拽：tkdnd 只在根窗口注入方法，子控件要手动补上，否则
# dbc_entry / import_tree 调 drop_target_register 会 AttributeError
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except Exception:
    TkinterDnD = None
    DND_FILES = None
    HAS_DND = False

if HAS_DND:
    for _meth in ("drop_target_register", "drop_target_unregister",
                  "dnd_bind", "drag_source_register"):
        if not hasattr(tk.BaseWidget, _meth):
            setattr(tk.BaseWidget, _meth,
                    getattr(TkinterDnD.DnDWrapper, _meth))

__version__ = "3.2"

# 尝试导入 python-can
try:
    import can
    HAS_CAN = True
except ImportError:
    HAS_CAN = False

# 实时监控模块（可选依赖，缺失时自动隐藏标签页）
try:
    from live_monitor import add_live_tab
    HAS_LIVE = True
except Exception:
    HAS_LIVE = False

ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")

# ======================= DBC 解析核心 =======================

def decode_dbc_text(path):
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    with open(path, "rb") as f:
        return f.read().decode("latin-1", errors="replace")


def parse_dbc(path):
    text = decode_dbc_text(path)
    messages = OrderedDict()
    val_tables = {}
    comments = []
    cur_msg = None
    re_bo = re.compile(r'^BO_\s+(\d+)\s+(\w+)\s*:\s*(\d+)\s+(\w+)')
    re_sg = re.compile(r'^SG_\s+(\w+)(?:\s+([Mm]\d*))?\s*:\s*(\d+)\|(\d+)@([01])([+-])\s*\(([^,]+),([^)]+)\)\s*\[\s*([^\]]*)\]\s*"([^"]*)"\s*(\S+)?')
    re_val = re.compile(r'^VAL_\s+(\d+)\s+(\w+)\s+(.+?);\s*$')
    re_cm = re.compile(r'^CM_\s+SG_\s+(\d+)\s+(\w+)\s+"(.*)";\s*$', re.DOTALL)
    # 报文周期与信号初值：属性区在文件尾部，先收集再统一回填，不依赖行序
    re_ba_cycle = re.compile(r'^BA_\s+"GenMsgCycleTime"\s+BO_\s+(\d+)\s+(-?\d+)')
    re_ba_start = re.compile(
        r'^BA_\s+"GenSigStartValue"\s+SG_\s+(\d+)\s+(\w+)\s+(-?[0-9.eE+]+)')
    cycle_ms = {}
    start_vals = {}

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        m = re_bo.match(s)
        if m:
            raw_id = int(m.group(1))
            dlc = int(m.group(3))
            if dlc <= 0:          # VECTOR__INDEPENDENT_SIG_MSG 等伪条目，非真实报文
                cur_msg = None
                continue
            ext = bool(raw_id & 0x80000000)   # 最高位为 1 表示 29 位扩展帧
            mid = raw_id & 0x7FFFFFFF
            cur_msg = mid
            messages[mid] = {"id": mid, "name": m.group(2), "dlc": dlc,
                             "ext": ext, "transmitter": m.group(4),
                             "cycle_ms": None, "signals": []}
            continue
        m = re_sg.match(s)
        if m and cur_msg is not None:
            mm = m.group(9).split("|")
            try:
                smin = float(mm[0]) if len(mm)>0 and mm[0] else None
                smax = float(mm[1]) if len(mm)>1 and mm[1] else None
            except ValueError:
                smin = smax = None
            messages[cur_msg]["signals"].append({
                "name": m.group(1), "mux": m.group(2),
                "start_bit": int(m.group(3)), "length": int(m.group(4)),
                "byte_order": int(m.group(5)), "is_signed": m.group(6)=="-",
                "factor": float(m.group(7)), "offset": float(m.group(8)),
                "unit": m.group(10), "start_value": None,
                "receivers": m.group(11).split(",") if m.group(11) else []})
            continue
        m = re_val.match(s)
        if m:
            pairs = re.findall(r'(\d+)\s+"([^"]*)"', m.group(3))
            if pairs:
                val_tables[(int(m.group(1)), m.group(2))] = {int(k): v for k, v in pairs}
            continue
        m = re_cm.match(s)
        if m:
            comments.append({"mid": int(m.group(1)), "sig": m.group(2), "text": m.group(3).replace('\\"', '"')})
            continue
        m = re_ba_cycle.match(s)
        if m:
            cycle_ms[int(m.group(1)) & 0x7FFFFFFF] = int(m.group(2))
            continue
        m = re_ba_start.match(s)
        if m:
            start_vals[(int(m.group(1)) & 0x7FFFFFFF, m.group(2))] = float(m.group(3))
            continue

    # 属性统一回填
    for mid, ms in cycle_ms.items():
        if mid in messages:
            messages[mid]["cycle_ms"] = ms
    for (mid, sname), v in start_vals.items():
        msg = messages.get(mid)
        if not msg:
            continue
        for sig in msg["signals"]:
            if sig["name"] == sname:
                sig["start_value"] = v
                break

    return messages, val_tables, comments


def extract_raw(data, sig):
    length, start = sig["length"], sig["start_bit"]
    if sig["byte_order"] == 1:          # Intel: start_bit 是 LSB 位置，逐位递增
        bits = [start + i for i in range(length)]
    else:                                # Motorola: start_bit 是 MSB 位置，位编号为锯齿序列
        bits, bp = [], start             # 7→6→…→0 之后跳到下一字节的 15，而非 -1
        for _ in range(length):
            bits.append(bp)
            bp = bp + 15 if bp % 8 == 0 else bp - 1

    raw = 0
    for i, bp in enumerate(bits):
        bi, bo = bp // 8, bp % 8
        if bi >= len(data): return None
        if sig["byte_order"] == 1:
            raw |= ((data[bi] >> bo) & 1) << i
        else:
            raw = (raw << 1) | ((data[bi] >> bo) & 1)
    if sig["is_signed"] and raw >= (1 << (length - 1)):
        raw -= (1 << length)
    return raw


def insert_raw(data, sig, raw):
    """
    把原始值按 DBC 位序写进 bytearray，与 extract_raw 严格对称。
    Intel  : start_bit 是 LSB，位号递增
    Motorola: start_bit 是 MSB，位号按 7→6→…→0→15 锯齿
    返回 False 表示帧长不足。
    """
    length, start = sig["length"], sig["start_bit"]
    raw &= (1 << length) - 1          # 负数按 length 位补码截断
    if sig["byte_order"] == 1:
        bits = [start + i for i in range(length)]
    else:
        bits, bp = [], start
        for _ in range(length):
            bits.append(bp)
            bp = bp + 15 if bp % 8 == 0 else bp - 1

    for i, bp in enumerate(bits):
        bi, bo = bp // 8, bp % 8
        if bi >= len(data):
            return False
        bit = (raw >> i) & 1 if sig["byte_order"] == 1 else (raw >> (length - 1 - i)) & 1
        if bit:
            data[bi] |= (1 << bo)
        else:
            data[bi] &= ~(1 << bo) & 0xFF
    return True


def pack_message(msg, values, fill=0x00):
    """
    按信号物理值打包一帧。
    values: {信号名: 物理值}，未给出的信号保持填充值。
    返回 (data: bytes, 未写入项列表)。超范围/无 factor/帧长不足都会进第二项，
    不做静默截断——静默截断会让"发了但值不对"变成最难查的问题。
    """
    dlc = max(int(msg.get("dlc") or 8), 1)
    data = bytearray([fill & 0xFF] * dlc)
    missed = []
    for sig in msg.get("signals", []):
        name = sig["name"]
        if name not in values:
            continue
        phys = values[name]
        factor = sig.get("factor") or 0
        if not factor:
            missed.append(f"{name}（factor 为 0）")
            continue
        raw = int(round((phys - sig.get("offset", 0)) / factor))
        length = sig["length"]
        if sig.get("is_signed"):
            lo, hi = -(1 << (length - 1)), (1 << (length - 1)) - 1
        else:
            lo, hi = 0, (1 << length) - 1
        if not (lo <= raw <= hi):
            missed.append(f"{name}（{phys} 超出 {lo}~{hi}）")
            continue
        if not insert_raw(data, sig, raw):
            missed.append(f"{name}（帧长不足）")
    return bytes(data), missed


def decode_message(data, msg, extract=None):
    """
    按 DBC 定义解码一帧，处理多路复用信号。
    返回 [(signal, raw, phys), ...]，仅含当前 mux 值下有效的信号。
    """
    ex = extract or extract_raw

    # 先取多路复用开关值（mux == "M"）
    mux_val = None
    for sig in msg.get("signals", []):
        if sig.get("mux") == "M":
            r = ex(data, sig)
            if r is not None:
                mux_val = r
            break

    out = []
    for sig in msg.get("signals", []):
        mk = sig.get("mux")
        if mk and mk != "M" and mk.startswith("m"):
            try:
                want = int(mk[1:])
            except ValueError:
                want = None
            if want is not None and (mux_val is None or want != mux_val):
                continue      # 当前 mux 值下该信号无效
        raw = ex(data, sig)
        if raw is None:
            continue
        out.append((sig, raw, raw * sig["factor"] + sig["offset"]))
    return out


def parse_bytes(arg):
    arg = arg.strip().replace(",", " ").replace(";", " ").strip()
    if not arg: return None
    arg = re.sub(r"\s+", " ", arg)
    if " " not in arg and len(arg) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in arg):
        return bytes.fromhex(arg)
    try:
        return bytes(int(x, 16) for x in arg.split())
    except ValueError:
        return None


# ======================= TRC 解析 =======================

TRC_LINE_RE = re.compile(
    r"^\s*(\d+)\s+([\d.]+)\s+([A-Za-z]{2})\s+([0-9A-Fa-f]+)\s+(Rx|Tx)\s+(\d+)\s*(.*?)\s*$"
)
TRC_TYPE_INFO = {
    "DT": ("data", False), "RT": ("remote", False), "ET": ("error", False),
    "FB": ("data", True), "RB": ("remote", True), "EB": ("error", True),
    "FD": ("data_fd", None),
}


def parse_trc(path):
    records = []
    skipped = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            s = line.strip()
            if not s or s.startswith(";"):
                continue
            m = TRC_LINE_RE.match(s)
            if not m:
                skipped.append((lineno, line.rstrip()))
                continue
            _num, t_ms, mtype, id_hex, direction, length, data_str = m.groups()
            if mtype not in TRC_TYPE_INFO:
                skipped.append((lineno, line.rstrip()))
                continue
            kind, ext = TRC_TYPE_INFO[mtype]
            if kind == "error":
                skipped.append((lineno, line.rstrip()))
                continue
            try:
                ts = float(t_ms)
                arb_id = int(id_hex, 16)
                nbytes = int(length)
                data = bytes(int(b, 16) for b in data_str.split())
            except ValueError:
                skipped.append((lineno, line.rstrip()))
                continue
            if ext is None:
                ext = arb_id > 0x7FF
            if len(data) < nbytes:
                data = data + bytes(nbytes - len(data))
            data = data[:nbytes]
            records.append((ts, arb_id, ext, data, direction == "Rx"))
    return records, skipped


# ======================= ASC 解析 =======================

def parse_asc(path):
    records = []
    skipped = []
    # 两种 ASC 行格式都要认：
    #   A) 时间 通道 ID#DATA            —— 早期 CANoe / 精简导出
    #   B) 时间 通道 ID  Rx  d 8 DATA   —— PCAN-View、CANoe 常规导出（含 CAN FD 的 d 12/64）
    re_b = re.compile(
        r'^\s*([\d.]+)\s+\d+\s+([0-9A-Fa-f]+)(x?)\s+(Rx|Tx)\s+d\s+(\d+)\s*([0-9A-Fa-f ]*)$',
        re.I)
    re_a = re.compile(
        r'^\s*([\d.]+)\s+\d+\s+([0-9A-Fa-f]+)(x?)\s*#\s*([0-9A-Fa-f]*)\s*(?:(Rx|Tx))?\s*$',
        re.I)
    skip_prefix = ("date", "base", "version", ";", "//", "begin", "end",
                   "internal", "no internal", "triggerblock", "events")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            s = line.strip()
            if not s or s.lower().startswith(skip_prefix):
                continue
            m = re_b.match(s)
            if m:
                ts = float(m.group(1))
                arb_id = int(m.group(2), 16)
                ext = bool(m.group(3)) or arb_id > 0x7FF
                dlc = int(m.group(5))
                raw = (m.group(6) or "").replace(" ", "")
                try:
                    data = bytes.fromhex(raw)[:dlc] if raw else b""
                except ValueError:
                    skipped.append((lineno, s[:80]))
                    continue
                records.append((ts, arb_id, ext, data,
                                m.group(4).lower() == "rx"))
                continue
            m = re_a.match(s)
            if m:
                ts = float(m.group(1))
                arb_id = int(m.group(2), 16)
                ext = bool(m.group(3)) or arb_id > 0x7FF
                raw = m.group(4) or ""
                try:
                    data = bytes.fromhex(raw) if raw else b""
                except ValueError:
                    skipped.append((lineno, s[:80]))
                    continue
                direction = (m.group(5) or "Rx").lower()
                records.append((ts, arb_id, ext, data, direction == "rx"))
                continue
            skipped.append((lineno, s[:80]))
    return records, skipped


# ======================= BLF 解析 =======================

def parse_blf(path):
    records = []
    if not HAS_CAN:
        return records, ["python-can 未安装，无法解析 BLF"]
    try:
        with can.io.BLFReader(path) as reader:
            for msg in reader:
                records.append((msg.timestamp, msg.arbitration_id, msg.is_extended_id, msg.data, msg.is_rx))
    except Exception as e:
        return records, [str(e)]
    return records, []


# ======================= 日志导入统一入口 =======================

def parse_log(path):
    ext = Path(path).suffix.lower()
    if ext == ".trc":
        return parse_trc(path)
    elif ext == ".asc":
        return parse_asc(path)
    elif ext == ".blf":
        return parse_blf(path)
    elif ext in (".txt", ".log", ".hex", ".dat"):
        # 尝试按 TRC 解析，失败则按纯 hex 行解析
        recs, skip = parse_trc(path)
        if recs:
            return recs, skip
        # 纯 hex 行: 每行是 ID+DATA 或 ID DATA
        recs = []
        with open(path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                s = line.strip()
                if not s or s.startswith("#") or s.startswith("//") or s.startswith(";"):
                    continue
                parts = s.split()
                try:
                    if len(parts) >= 2:
                        arb_id = int(parts[0], 16) if parts[0].lower().startswith("0x") else int(parts[0], 16)
                        data = bytes(int(x, 16) for x in parts[1:])
                        ext = arb_id > 0x7FF
                        recs.append((lineno, arb_id, ext, data, True))
                except ValueError:
                    continue
        return recs, skip
    return [], [f"不支持的文件格式: {ext}"]


# ======================= 格式转换核心 =======================

def convert_log(input_path, output_path, fmt_out):
    """转换日志格式。fmt_out: 'blf', 'asc', 'trc'"""
    recs, skipped = parse_log(input_path)
    if not recs:
        return False, f"未解析到任何报文，跳过 {len(skipped)} 行"

    ext_out = fmt_out.lower()
    if ext_out == "blf":
        if not HAS_CAN:
            return False, "python-can 未安装，无法输出 BLF"
        has_fd = any(len(r[3]) > 8 for r in recs)
        with can.io.BLFWriter(output_path) as writer:
            for ts, arb_id, ext, data, is_rx in recs:
                msg = can.Message(timestamp=ts, arbitration_id=arb_id, is_extended_id=ext,
                                  is_rx=is_rx, is_fd=has_fd, data=data)
                writer.on_message_received(msg)
        return True, f"已转换 {len(recs)} 帧 → {output_path}"

    elif ext_out == "asc":
        if not HAS_CAN:
            return False, "python-can 未安装，无法输出 ASC"
        with can.io.ASCWriter(output_path) as writer:
            for ts, arb_id, ext, data, is_rx in recs:
                msg = can.Message(timestamp=ts, arbitration_id=arb_id, is_extended_id=ext,
                                  is_rx=is_rx, data=data)
                writer.on_message_received(msg)
        return True, f"已转换 {len(recs)} 帧 → {output_path}"

    elif ext_out == "trc":
        lines = [
            ";$FILEVERSION=2.0",
            ";$STARTTIME=0",
            "; PCAN-View TRC 转换输出",
            "; 序号\t时间[ms]\t类型\tID\t方向\t长度\t数据",
        ]
        for i, (ts, arb_id, ext, data, is_rx) in enumerate(recs, 1):
            mtype = "FB" if ext else "DT"
            direction = "Rx" if is_rx else "Tx"
            data_str = " ".join(f"{b:02X}" for b in data)
            lines.append(f"{i}\t{ts*1000:.3f}\t{mtype}\t{arb_id:03X}\t{direction}\t{len(data)}\t{data_str}")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return True, f"已转换 {len(recs)} 帧 → {output_path}"

    return False, f"不支持的输出格式: {fmt_out}"


# ======================= GUI =======================

def _is_admin():
    """是否以管理员身份运行：管理员进程收不到普通资源管理器的拖放（UIPI 隔离）"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _res_path(name):
    """资源路径：打包后在 _MEIPASS，源码运行时在脚本目录"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def _icon_path():
    """窗口 / 任务栏图标"""
    p = _res_path("app.ico")
    return p if os.path.isfile(p) else None


class NavHost(ctk.CTkFrame):
    """
    左侧大图标导航 + 右侧内容区，替代 CTkTabview。

    对外接口与 Tabview 保持一致：add(label) 返回页面容器，set(label) 切页。
    label 允许写成 "🔍 解析"（图标 + 空格 + 名字），也允许只给名字。
    """

    NAV_W = 116
    ICON_FILES = {
        "parse": "nav_parse",
        "import": "nav_import",
        "convert": "nav_convert",
        "monitor": "nav_monitor",
    }
    ICON_SIZE = 28

    def __init__(self, master, on_change=None, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.nav = ctk.CTkFrame(self, width=self.NAV_W, corner_radius=0,
                                fg_color=("#E9EEF4", "#191D22"))
        self.nav.grid(row=0, column=0, sticky="ns")
        self.nav.grid_propagate(False)

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.body.grid_rowconfigure(0, weight=1)
        self.body.grid_columnconfigure(0, weight=1)

        self._pages = {}
        self._items = {}
        self._cur = None
        self._on_change = on_change
        self._icon_cache = {}
        self._icon_key = {}     # 页面名 → 图标键（"解析" → "parse"）

        brand = ctk.CTkFrame(self.nav, fg_color="transparent")
        brand.pack(fill="x", pady=(14, 12))
        ctk.CTkLabel(brand, text="BusLens",
                     font=("Microsoft YaHei UI", 16, "bold"),
                     text_color=("#1F6FB2", "#5AA9E6")).pack()
        ctk.CTkLabel(brand, text="CANova · 观澜",
                     font=("Microsoft YaHei UI", 10),
                     text_color=("#6B7785", "#8A939E")).pack(pady=(1, 0))

    # ---- 内建 ----
    def _icon(self, name, on):
        """取导航图标；CTkImage 必须缓存持有，否则被回收后显示空白"""
        key = (name, on)
        if key in self._icon_cache:
            return self._icon_cache[key]
        stem = self.ICON_FILES.get(name)
        if not stem:
            return None
        path = _res_path(stem + ("_on" if on else "") + ".png")
        if not os.path.isfile(path):
            return None
        try:
            im = Image.open(path)
            im.load()
            ci = ctk.CTkImage(light_image=im, dark_image=im,
                              size=(self.ICON_SIZE, self.ICON_SIZE))
        except Exception:
            return None
        self._icon_cache[key] = ci
        return ci

    def _make_item(self, name, icon):
        item = ctk.CTkFrame(self.nav, height=86, corner_radius=9,
                            fg_color="transparent")
        item.pack(fill="x", padx=9, pady=3)
        item.pack_propagate(False)
        ci = self._icon(self._icon_key.get(name) or name, False)
        if ci is not None:
            ico = ctk.CTkLabel(item, image=ci, text="", height=34)
        else:
            ico = ctk.CTkLabel(item, text="•", height=34,
                               font=("Segoe UI Emoji", 22),
                               text_color=("#3A4753", "#C9D1D9"))
        ico.pack(pady=(12, 4))
        lbl = ctk.CTkLabel(item, text=name, font=("Microsoft YaHei UI", 11),
                           text_color=("#3A4753", "#C9D1D9"))
        lbl.pack()
        for w in (item, ico, lbl):
            w.bind("<Button-1>", lambda _e, n=name: self.select(n))
            w.bind("<Enter>", lambda _e, n=name: self._hover(n, True))
            w.bind("<Leave>", lambda _e, n=name: self._hover(n, False))
        return item, ico, lbl

    def _hover(self, name, on):
        if name == self._cur:
            return
        item, ico, lbl = self._items[name]
        item.configure(fg_color=("#DCE5EF", "#232930") if on else "transparent")

    # ---- 对外 ----
    def add(self, label, icon=None):
        if icon is None and " " in label.strip():
            parts = label.strip().split(None, 1)
            icon, name = parts[0], parts[1]
        else:
            name = label.strip() if icon is None else label
        page = ctk.CTkFrame(self.body, fg_color="transparent")
        page.grid(row=0, column=0, sticky="nsew")
        self._pages[name] = page
        self._icon_key[name] = icon
        self._items[name] = self._make_item(name, icon)
        if self._cur is None:
            self.select(name)
        else:
            # 只让当前页留在网格里，其余隐藏——比 tkraise 稳，
            # 不会出现「高亮在 A、内容却是 B」的错位
            page.grid_remove()
        return page

    def names(self):
        return list(self._pages)

    def _resolve(self, label):
        if label in self._pages:
            return label
        for n in self._pages:
            if n in label or label in n:
                return n
        parts = str(label).split(None, 1)
        if len(parts) == 2 and parts[1] in self._pages:
            return parts[1]
        return None

    def select(self, label):
        name = self._resolve(label)
        if name is None:
            return
        prev = self._cur
        self._cur = name
        for n, page in self._pages.items():
            if n == name:
                page.grid(row=0, column=0, sticky="nsew")
            else:
                page.grid_remove()
        if prev and prev in self._items:
            it, ic, lb = self._items[prev]
            it.configure(fg_color="transparent")
            ci = self._icon(self._icon_key.get(prev) or prev, False)
            if ci is not None:
                ic.configure(image=ci)
            else:
                ic.configure(text_color=("#3A4753", "#C9D1D9"))
            lb.configure(text_color=("#3A4753", "#C9D1D9"),
                         font=("Microsoft YaHei UI", 11))
        it, ic, lb = self._items[name]
        it.configure(fg_color=("#1F6FB2", "#2B6CB0"))
        ci = self._icon(self._icon_key.get(name) or name, True)
        if ci is not None:
            ic.configure(image=ci)
        else:
            ic.configure(text_color="#FFFFFF")
        lb.configure(text_color="#FFFFFF", font=("Microsoft YaHei UI", 11, "bold"))
        if self._on_change:
            try:
                self._on_change(name)
            except Exception:
                pass

    def get(self):
        return self._cur

    # 兼容 CTkTabview 的调用习惯：tabs.set("解析")
    def set(self, label):
        self.select(label)


if HAS_DND:
    class _AppBase(ctk.CTk, TkinterDnD.DnDWrapper):
        """把 tkdnd 的拖拽能力挂到主窗口"""
else:
    class _AppBase(ctk.CTk):
        pass


class DbcParserApp(_AppBase):
    def __init__(self):
        super().__init__()
        self.dnd_ready = False
        if HAS_DND:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
                self.dnd_ready = True
            except Exception:
                self.dnd_ready = False
        self.title(f"BusLens · 观澜  v{__version__}")
        self.geometry("1360x860")
        self.minsize(1120, 700)
        try:
            ip = _icon_path()
            if ip:
                self.iconbitmap(ip)
        except Exception:
            pass

        self.messages = {}
        self.val_tables = {}
        self.comments = []
        self.dbc_loaded = False
        self.dbc_path = None
        self.config_path = self._config_path()
        self.imported_records = []  # [(ts, id, ext, data, is_rx), ...]

        self._build_ui()
        self._load_last_dbc()

    def _config_path(self):
        """
        配置放 %LOCALAPPDATA%\\BusLens\\config.json：
        exe 放在 Program Files 这类受保护目录时，普通权限也能正常保存。
        """
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "BusLens")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            d = os.path.dirname(os.path.abspath(sys.argv[0]))
        return os.path.join(d, "config.json")

    @staticmethod
    def _legacy_cfg_paths():
        """旧版本把配置写在 exe 同级目录，读取时做一次迁移"""
        base = os.path.dirname(os.path.abspath(sys.argv[0]))
        return [os.path.join(base, ".dbc_parser_config.json"),
                os.path.join(base, ".buslens_config.json")]

    # =============== UI ===============

    def _build_ui(self):
        self._build_body()
        self._build_statusbar()
        self._setup_dnd()

    # =============== 拖拽导入 ===============

    DBC_EXT = (".dbc",)
    LOG_EXT = (".trc", ".asc", ".blf", ".txt", ".log", ".hex", ".dat", ".csv")

    def _setup_dnd(self):
        """
        两套并行，保证「拖到哪儿都收」：
          1) 原生 WM_DROPFILES —— 挂在主窗口句柄上，与控件无关，最可靠
          2) tkdnd —— 递归注册所有控件做兜底（原生不可用时顶上）
        """
        self._last_drop = None
        self._native_drop_ok = self._install_native_drop()

        if getattr(self, "dnd_ready", False):
            self._register_dnd_tree(self)

        ok = bool(self._native_drop_ok or getattr(self, "dnd_ready", False))
        self.is_admin = _is_admin()
        try:
            if self.is_admin:
                # 管理员进程与普通资源管理器不在同一完整性级别，拖放会被系统拦掉
                self.dnd_lbl.configure(
                    text="⚠ 管理员权限运行 → 拖入会被系统阻止，请普通权限启动",
                    text_color="#C08A2A")
            elif ok:
                self.dnd_lbl.configure(
                    text="拖拽就绪 · 任意位置拖入 DBC / 日志",
                    text_color="#3C9A5F")
            else:
                self.dnd_lbl.configure(text="拖拽不可用", text_color="#C08A2A")
            # 点状态栏这一角可看拖放诊断，便于排查
            self.dnd_lbl.bind("<Button-1>", lambda _e: self._show_dnd_diag())
        except Exception:
            pass

    def _show_dnd_diag(self):
        try:
            u = ctypes.windll.user32
            u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            hwnd = getattr(self, "_drop_hwnd", 0)
            cls = ctypes.create_unicode_buffer(128)
            if hwnd:
                u.GetClassNameW(ctypes.c_void_p(hwnd), cls, 128)
            ex = u.GetWindowLongW(ctypes.c_void_p(hwnd), -20) if hwnd else 0
            accepts = bool(ex & 0x10)
            messagebox.showinfo(
                "拖放诊断",
                f"窗口句柄：0x{hwnd:X}\n"
                f"窗口类：{cls.value or '—'}\n"
                f"扩展样式：0x{ex:X}（ACCEPTFILES={'是' if accepts else '否'}）\n"
                f"原生钩子：{'已挂载' if getattr(self, '_native_drop_ok', False) else '未挂载'}\n"
                f"tkdnd：{'已加载' if getattr(self, 'dnd_ready', False) else '未加载'}\n"
                f"管理员运行：{'是' if getattr(self, 'is_admin', False) else '否'}\n\n"
                + ("拖放被系统阻止的原因：程序以管理员权限运行。\n"
                   "请关掉本窗口，从资源管理器双击 exe（不要右键“以管理员身份运行”）。"
                   if getattr(self, "is_admin", False) else
                   "窗口已接受文件拖放；若仍无反应，可能是安全软件拦截。"))
        except Exception as e:
            messagebox.showerror("拖放诊断", str(e))

    # ---------------- 原生 WM_DROPFILES ----------------
    WM_DROPFILES = 0x0233
    GWLP_WNDPROC = -4

    def _install_native_drop(self):
        if sys.platform != "win32":
            return False
        try:
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32

            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            user32.SetWindowLongPtrW.argtypes = [
                wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            user32.CallWindowProcW.restype = ctypes.c_ssize_t
            user32.CallWindowProcW.argtypes = [
                ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                wintypes.WPARAM, wintypes.LPARAM]
            user32.GetParent.restype = wintypes.HWND

            hwnd = self.winfo_id()
            parent = user32.GetParent(hwnd)
            if parent:
                hwnd = parent           # 带标题栏的真正顶层窗口
            shell32.DragAcceptFiles(ctypes.c_void_p(hwnd), True)

            WNDPROC = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                wintypes.WPARAM, wintypes.LPARAM)

            def _proc(h, msg, wp, lp):
                if msg == self.WM_DROPFILES:
                    try:
                        paths = self._query_drop_files(wp)
                        if paths:
                            # 回到 Tk 线程再动界面
                            self.after(0, lambda p=list(paths): self._handle_paths(p))
                    except Exception:
                        pass
                    finally:
                        try:
                            shell32.DragFinish(ctypes.c_void_p(wp))
                        except Exception:
                            pass
                    return 0
                return user32.CallWindowProcW(
                    self._old_wndproc, h, msg, wp, lp)

            self._wndproc_ref = WNDPROC(_proc)   # 必须持有，否则被回收
            old = user32.SetWindowLongPtrW(
                ctypes.c_void_p(hwnd), self.GWLP_WNDPROC,
                ctypes.cast(self._wndproc_ref, ctypes.c_void_p))
            self._old_wndproc = old
            self._drop_hwnd = hwnd
            return bool(old)
        except Exception:
            return False

    @staticmethod
    def _query_drop_files(hdrop):
        shell32 = ctypes.windll.shell32
        h = ctypes.c_void_p(hdrop)
        count = shell32.DragQueryFileW(h, 0xFFFFFFFF, None, 0)
        out = []
        for i in range(count):
            ln = shell32.DragQueryFileW(h, i, None, 0)
            if ln <= 0:
                continue
            buf = ctypes.create_unicode_buffer(ln + 1)
            shell32.DragQueryFileW(h, i, buf, ln + 1)
            if buf.value:
                out.append(buf.value)
        return out

    # ---------------- tkdnd 兜底 ----------------
    def _register_dnd_tree(self, widget):
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop_any)
        except Exception:
            pass
        try:
            kids = widget.winfo_children()
        except Exception:
            kids = []
        for k in kids:
            self._register_dnd_tree(k)

    def _on_drop_any(self, event):
        """不看到达控件，一律按扩展名识别"""
        self._handle_paths(self._dnd_paths(getattr(event, "data", "")))
        return getattr(event, "action", "copy")

    # ---------------- 统一入口 ----------------
    DBC_EXT = (".dbc",)
    LOG_EXT = (".trc", ".asc", ".blf", ".txt", ".log", ".hex", ".dat", ".csv")

    def _handle_paths(self, paths):
        """文件类型决定行为，与拖到哪个控件无关"""
        paths = [p for p in (paths or []) if p and os.path.isfile(p)]
        if not paths:
            return
        now = time.time()
        # 原生钩子与 tkdnd 可能同时触发，800ms 内同样的文件只处理一次
        if (self._last_drop and now - self._last_drop[0] < 0.8
                and paths == self._last_drop[1]):
            return
        self._last_drop = (now, list(paths))

        dbcs = [p for p in paths if p.lower().endswith(self.DBC_EXT)]
        logs = [p for p in paths if p.lower().endswith(self.LOG_EXT)]

        if dbcs:
            self._load_dbc(dbcs[0])
        if logs:
            self._do_import(logs[0])
        if dbcs or logs:
            self.tabs.set("解析")
            done = []
            if dbcs:
                done.append(f"DBC {os.path.basename(dbcs[0])}")
            if logs:
                done.append(f"日志 {os.path.basename(logs[0])}")
            self._status("已拖入 · " + " / ".join(done))
        else:
            self._status("!! 不支持的文件类型：只认 .dbc 与 "
                         ".trc/.asc/.blf/.txt/.hex")

    @staticmethod
    def _dnd_paths(data):
        """tkdnd 的 data 形如 {C:\\a b\\x.dbc} C:/y.asc，花括号包住含空格的路径"""
        out = []
        for m in re.finditer(r"\{([^}]*)\}|(\S+)", data or ""):
            p = (m.group(1) or m.group(2) or "").strip().strip('"')
            if p:
                out.append(p)
        return out

    def _build_body(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=6, pady=6)

        self.tabs = NavHost(body)
        self.tabs.pack(fill="both", expand=True)

        self._build_tab_parse()
        self._build_tab_convert()
        if HAS_LIVE:
            self.live_tab = add_live_tab(self, decode_message, parse_dbc, pack_message)

    def _build_dbc_bar(self, parent):
        """DBC 选择条。用 grid 分配，避免按钮被输入框挤出可视区。
        页面内已有导入区，顶部不再放重复的「导入数据」按钮。"""
        bar = ctk.CTkFrame(parent, height=46)
        bar.pack(fill="x", padx=6, pady=(6, 2))
        bar.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(bar, text="DBC",
                     font=("Microsoft YaHei UI", 12, "bold")).grid(
            row=0, column=0, sticky="w", padx=(8, 6), pady=8)
        self.dbc_var = ctk.StringVar()
        self.dbc_entry = ctk.CTkEntry(
            bar, textvariable=self.dbc_var,
            placeholder_text="选择或拖拽 DBC 文件")
        self.dbc_entry.grid(row=0, column=1, sticky="ew", padx=2, pady=8)
        self.dbc_entry.bind("<Return>", lambda e: self._load_dbc())
        ctk.CTkButton(bar, text="📂 浏览", width=76,
                      command=self._browse_dbc).grid(
            row=0, column=2, padx=2, pady=8)
        ctk.CTkButton(bar, text="📥 示例", width=76,
                      command=self._load_example_dbc).grid(
            row=0, column=3, padx=(2, 8), pady=8)

    def _build_tab_parse(self):
        tab = self.tabs.add("解析", "parse")
        self._build_dbc_bar(tab)
        inner = ctk.CTkFrame(tab, fg_color="transparent")
        inner.pack(fill="both", expand=True)
        inner.grid_columnconfigure(1, weight=1)
        inner.grid_rowconfigure(0, weight=1)

        # 左侧：报文列表
        left = ctk.CTkFrame(inner, width=272)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 4))
        left.grid_propagate(False)
        ctk.CTkLabel(left, text="📋 报文列表", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w", padx=6, pady=(6, 2))
        self.search_entry = ctk.CTkEntry(left, placeholder_text="搜索 ID 或名称…")
        self.search_entry.pack(fill="x", padx=6, pady=2)
        self.search_entry.bind("<KeyRelease>", lambda e: self._filter_msgs())

        lc = ctk.CTkFrame(left)
        lc.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.msg_tree = ttk.Treeview(lc, columns=("id", "name", "sc"), show="headings", height=20, selectmode="browse")
        self.msg_tree.heading("id", text="ID", anchor="w")
        self.msg_tree.heading("name", text="报文名称", anchor="w")
        self.msg_tree.heading("sc", text="信号", anchor="center")
        self.msg_tree.column("id", width=70, minwidth=55)
        self.msg_tree.column("name", width=160, minwidth=100)
        self.msg_tree.column("sc", width=45, minwidth=35, anchor="center")
        self.msg_tree.bind("<<TreeviewSelect>>", self._on_msg_select)
        vsb = ttk.Scrollbar(lc, orient="vertical", command=self.msg_tree.yview)
        self.msg_tree.configure(yscrollcommand=vsb.set)
        self.msg_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # 右侧：上=单帧解析，下=日志导入。选 DBC → 导入 → 解析 全在这一页
        right = ctk.CTkFrame(inner, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=5)
        right.grid_rowconfigure(1, weight=5)

        # ---- 上：单帧解析 ----
        top = ctk.CTkFrame(right)
        top.grid(row=0, column=0, sticky="nsew", pady=(0, 3))

        inp = ctk.CTkFrame(top)
        inp.pack(fill="x")
        ctk.CTkLabel(inp, text="ID", font=("Microsoft YaHei UI", 12)).grid(row=0, column=0, padx=(6, 2), pady=4, sticky="w")
        self.id_var = ctk.StringVar()
        self.id_entry = ctk.CTkEntry(inp, textvariable=self.id_var, width=92, placeholder_text="0x214 或 532")
        self.id_entry.grid(row=0, column=1, padx=2, pady=4, sticky="w")
        self.id_entry.bind("<KeyRelease>", self._on_id_key)

        ctk.CTkLabel(inp, text="数据", font=("Microsoft YaHei UI", 12)).grid(row=0, column=2, padx=(10, 2), pady=4, sticky="w")
        self.data_var = ctk.StringVar()
        self.data_entry = ctk.CTkEntry(inp, textvariable=self.data_var, width=150, placeholder_text="E8 03 DC 05 或 112233")
        self.data_entry.grid(row=0, column=3, padx=2, pady=4, sticky="ew")
        self.data_entry.bind("<Return>", lambda e: self._parse())

        ctk.CTkButton(inp, text="解析", width=62, command=self._parse).grid(row=0, column=4, padx=3, pady=4)
        ctk.CTkButton(inp, text="清空", width=52, fg_color="#888", command=self._clear_input).grid(row=0, column=5, padx=3, pady=4)
        ctk.CTkButton(inp, text="粘贴", width=52, command=self._paste_from_clipboard).grid(row=0, column=6, padx=3, pady=4)
        inp.grid_columnconfigure(3, weight=1)

        self.complete_lbl = ctk.CTkLabel(inp, text="", font=("Microsoft YaHei UI", 11), text_color="#888")
        self.complete_lbl.grid(row=1, column=0, columnspan=7, padx=6, pady=(0, 2), sticky="w")

        # 结果
        res = ctk.CTkFrame(top)
        res.pack(fill="both", expand=True, pady=(6, 0))
        self.result_hdr = ctk.CTkLabel(res, text="解析结果", font=("Microsoft YaHei UI", 13, "bold"), anchor="w")
        self.result_hdr.pack(fill="x", padx=6, pady=(4, 2))
        rc = ctk.CTkFrame(res)
        rc.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.result_tree = ttk.Treeview(rc, columns=("name", "value", "unit", "cmt"), show="headings", height=9)
        self.result_tree.heading("name", text="信号名", anchor="w")
        self.result_tree.heading("value", text="值", anchor="e")
        self.result_tree.heading("unit", text="单位", anchor="w")
        self.result_tree.heading("cmt", text="注释", anchor="w")
        self.result_tree.column("name", width=170, minwidth=90)
        self.result_tree.column("value", width=85, minwidth=50, anchor="e")
        self.result_tree.column("unit", width=48, minwidth=30)
        self.result_tree.column("cmt", width=150, minwidth=60)
        rv = ttk.Scrollbar(rc, orient="vertical", command=self.result_tree.yview)
        self.result_tree.configure(yscrollcommand=rv.set)
        self.result_tree.pack(side="left", fill="both", expand=True)
        rv.pack(side="right", fill="y")

        # ---- 下：日志导入 ----
        bot = ctk.CTkFrame(right)
        bot.grid(row=1, column=0, sticky="nsew", pady=(3, 0))

        hd = ctk.CTkFrame(bot)
        hd.pack(fill="x", padx=8, pady=(6, 0))
        ctk.CTkLabel(hd, text="日志导入",
                     font=("Microsoft YaHei UI", 13, "bold")).pack(side="left")
        self.import_count_lbl = ctk.CTkLabel(
            hd, text="", font=("Microsoft YaHei UI", 11), text_color="#888")
        self.import_count_lbl.pack(side="left", padx=(10, 0))

        # 按钮单独一行：挤在标题行会把整块撑宽，右侧被裁
        br = ctk.CTkFrame(bot)
        br.pack(fill="x", padx=8, pady=(2, 4))
        ctk.CTkButton(br, text="📂 从文件导入", width=110,
                      command=self._import_file).pack(side="left", padx=(0, 4))
        ctk.CTkButton(br, text="📋 从剪贴板", width=100,
                      command=self._import_clipboard).pack(side="left", padx=(0, 4))
        ctk.CTkButton(br, text="清空列表", width=70, fg_color="#888",
                      command=self._clear_import).pack(side="right")

        ic = ctk.CTkFrame(bot)
        ic.pack(fill="both", expand=True, padx=8, pady=(0, 2))
        self.import_tree = ttk.Treeview(ic, columns=("idx", "ts", "id", "ext", "len", "data"), show="headings", height=7)
        self.import_tree.heading("idx", text="#", anchor="center")
        self.import_tree.heading("ts", text="时间(s)", anchor="e")
        self.import_tree.heading("id", text="ID", anchor="w")
        self.import_tree.heading("ext", text="扩展", anchor="center")
        self.import_tree.heading("len", text="DLC", anchor="center")
        self.import_tree.heading("data", text="数据", anchor="w")
        self.import_tree.column("idx", width=40, anchor="center")
        self.import_tree.column("ts", width=90, anchor="e")
        self.import_tree.column("id", width=70)
        self.import_tree.column("ext", width=40, anchor="center")
        self.import_tree.column("len", width=40, anchor="center")
        self.import_tree.column("data", width=200, minwidth=120)
        self.import_tree.bind("<<TreeviewSelect>>", self._on_import_select)
        self.import_tree.bind("<Double-1>", lambda e: self._on_import_double())
        iv = ttk.Scrollbar(ic, orient="vertical", command=self.import_tree.yview)
        self.import_tree.configure(yscrollcommand=iv.set)
        self.import_tree.pack(side="left", fill="both", expand=True)
        iv.pack(side="right", fill="y")

        ctk.CTkLabel(
            bot, text="支持 TRC / ASC / BLF / TXT / HEX　·　双击记录行 → 弹出解析窗口，可同时开多个对照",
            font=("Microsoft YaHei UI", 11), text_color="#888", anchor="w"
        ).pack(fill="x", padx=8, pady=(0, 6))

    def _build_tab_convert(self):
        tab = self.tabs.add("转换", "convert")
        ctk.CTkLabel(tab, text="CAN 日志格式转换", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w", padx=8, pady=(8, 4))

        # 输入
        f1 = ctk.CTkFrame(tab)
        f1.pack(fill="x", padx=8, pady=4)
        ctk.CTkLabel(f1, text="输入文件：", font=("Microsoft YaHei UI", 12)).pack(side="left")
        self.conv_in_var = ctk.StringVar()
        ctk.CTkEntry(f1, textvariable=self.conv_in_var, width=350).pack(side="left", padx=4, fill="x", expand=True)
        ctk.CTkButton(f1, text="浏览", width=60, command=self._conv_browse_in).pack(side="left", padx=2)

        # 输出
        f2 = ctk.CTkFrame(tab)
        f2.pack(fill="x", padx=8, pady=4)
        ctk.CTkLabel(f2, text="输出格式：", font=("Microsoft YaHei UI", 12)).pack(side="left")
        self.conv_fmt_var = ctk.StringVar(value="BLF")
        fmt_menu = ctk.CTkOptionMenu(f2, values=["BLF", "ASC", "TRC"], variable=self.conv_fmt_var, width=80)
        fmt_menu.pack(side="left", padx=4)
        self.conv_out_var = ctk.StringVar()
        ctk.CTkEntry(f2, textvariable=self.conv_out_var, width=350, placeholder_text="自动生成或手动指定").pack(side="left", padx=4, fill="x", expand=True)
        ctk.CTkButton(f2, text="转换", width=60, command=self._conv_do).pack(side="left", padx=2)

        # 日志
        self.conv_log = ctk.CTkTextbox(tab, height=200, font=("Consolas", 11))
        self.conv_log.pack(fill="both", expand=True, padx=8, pady=8)
        self.conv_log.insert("0.0", "选择输入文件后点击「转换」\n")

    # =============== 状态栏 ===============

    def _build_statusbar(self):
        bar = ctk.CTkFrame(self, height=30, corner_radius=0)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.status_lbl = ctk.CTkLabel(bar, text="就绪", font=("Microsoft YaHei UI", 11), anchor="w")
        self.status_lbl.pack(side="left", padx=10)
        # 拖拽能力指示：打包后 tkdnd 若没加载成功，这里会显示「拖拽不可用」
        self.dnd_lbl = ctk.CTkLabel(bar, text="", anchor="e",
                                    font=("Microsoft YaHei UI", 10),
                                    text_color="#888")
        self.dnd_lbl.pack(side="right", padx=10)

    def _status(self, text):
        self.status_lbl.configure(text=text)

    # =============== DBC 加载 ===============

    def _load_dbc(self, path=None):
        if path is None:
            path = self.dbc_var.get().strip().strip('"').strip("'")
        if not path or not os.path.isfile(path):
            self._status(f"!! 找不到文件: {path}")
            return
        try:
            msgs, vtab, cmts = parse_dbc(path)
            if not msgs:
                self._status("!! 未解析到任何报文")
                return
            self.messages, self.val_tables, self.comments = msgs, vtab, cmts
            self.dbc_loaded, self.dbc_path = True, path
            self.dbc_var.set(path)
            self._save_cfg(path)
            self._refresh_msgs()
            self._clear_result()
            self._status(f"已加载 {len(msgs)} 条报文 — {os.path.basename(path)}")
            self.id_entry.focus()
        except Exception as e:
            self._status(f"!! 解析失败: {e}")

    def _load_example_dbc(self):
        last = self._load_cfg()
        if last and os.path.isfile(last):
            self._load_dbc(last)
            return
        cand = "D:\\1、项目资料\\25年项目\\9、WL-25-D283-CDCU新增岚图产品\\6-项目验收\\11、上位机程序\\OP050高温EOL测试\\热态EOL测试\\CDCU_Mini_FCT测试_v1.9_20260304.dbc"
        if os.path.isfile(cand):
            self._load_dbc(cand)
            return
        self._status("!! 未找到示例 DBC，请手动选择")

    def _browse_dbc(self):
        p = filedialog.askopenfilename(title="选择 DBC 文件", filetypes=[("DBC 文件", "*.dbc"), ("所有文件", "*.*")])
        if p:
            self._load_dbc(p)

    def _save_cfg(self, path):
        try:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump({"last_dbc": os.path.abspath(path)}, f)
        except Exception:
            pass

    def _load_cfg(self):
        try:
            if os.path.isfile(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as f:
                    p = json.load(f).get("last_dbc", "")
                    if p and os.path.isfile(p):
                        return p
        except Exception:
            pass
        # 旧位置有配置就迁到新位置，之后只认新的
        for old in self._legacy_cfg_paths():
            try:
                if os.path.isfile(old):
                    with open(old, "r", encoding="utf-8") as f:
                        p = json.load(f).get("last_dbc", "")
                    if p and os.path.isfile(p):
                        self._save_cfg(p)
                        return p
            except Exception:
                continue
        return None

    def _load_last_dbc(self):
        p = self._load_cfg()
        if p:
            self._load_dbc(p)


    def _refresh_msgs(self, flt=""):
        self.msg_tree.delete(*self.msg_tree.get_children())
        flt = flt.lower()
        for mid, msg in sorted(self.messages.items()):
            h = f"0x{mid:X}"
            if flt and flt not in str(mid) and flt not in h.lower() and flt not in msg["name"].lower():
                continue
            self.msg_tree.insert("", "end", iid=str(mid), values=(h, msg["name"], len(msg["signals"])))

    def _filter_msgs(self):
        self._refresh_msgs(self.search_entry.get())

    def _on_msg_select(self, event):
        sel = self.msg_tree.selection()
        if not sel:
            return
        self.id_var.set(f"0x{int(sel[0]):X}")
        self.complete_lbl.configure(text="")
        self.data_entry.focus()

    # =============== ID 自动补全 ===============

    def _on_id_key(self, event):
        if event.keysym in ("Return", "Tab", "Up", "Down"):
            return
        t = self.id_entry.get().strip()
        if not t:
            self.complete_lbl.configure(text="")
            return
        tl = t.lower()
        ms = []
        for mid, msg in self.messages.items():
            h = f"0x{mid:X}"
            if tl in h.lower() or tl in str(mid) or tl in msg["name"].lower():
                ms.append((h, msg["name"]))
        if not ms:
            self.complete_lbl.configure(text="")
        elif len(ms) == 1:
            h, n = ms[0]
            self.complete_lbl.configure(text=f"  → {h}  {n}" if h.lower() != tl else f"  {h}  {n}")
        else:
            self.complete_lbl.configure(text=f"  → 共 {len(ms)} 条匹配: " + " | ".join([f"{h}" for h, _ in ms[:5]]))

    # =============== 解析 ===============

    def _parse(self, id_str=None, data_str=None):
        if not self.dbc_loaded:
            self._status("!! 请先加载 DBC 文件")
            return
        if id_str is None:
            id_str = self.id_var.get().strip()
        if data_str is None:
            data_str = self.data_var.get().strip()
        if not id_str or not data_str:
            self._status("!! 请输入报文 ID 和原始字节")
            return
        try:
            mid = int(id_str, 16) if id_str.lower().startswith("0x") else int(id_str, 10)
        except ValueError:
            self._status("!! 无效的报文 ID")
            return
        data = parse_bytes(data_str)
        if data is None:
            self._status("!! 无效的字节数据")
            return
        if mid not in self.messages:
            self._status(f"!! 报文 ID {mid} (0x{mid:X}) 不在该 DBC 中")
            return
        msg = self.messages[mid]
        self._show_result(msg, mid, data)
        self._status(f"解析完成 — {msg['name']} ({len(msg['signals'])} 个信号)")

    def _show_result(self, msg, mid, data):
        self.result_tree.delete(*self.result_tree.get_children())
        hex_str = data.hex().upper()
        self.result_hdr.configure(text=f"解析结果 — {msg['name']}  (ID=0x{mid:X}, {len(data)}B: {hex_str})")
        if not msg["signals"]:
            self.result_tree.insert("", "end", values=("(无信号定义—诊断/CCP 透传报文)", "", "", ""))
            return
        cm_map = {c["sig"]: c["text"] for c in self.comments if c["mid"] == mid}
        shown = 0
        for sig, raw, phys in decode_message(data, msg):
            disp = int(round(phys)) if abs(phys - round(phys)) < 1e-9 else round(phys, 3)
            vt = self.val_tables.get((mid, sig["name"]))
            en = f" [{vt[raw]}]" if vt and raw in vt else ""
            self.result_tree.insert("", "end", values=(sig["name"], str(disp) + en, sig["unit"], cm_map.get(sig["name"], "")))
            shown += 1
        if shown == 0:
            self.result_tree.insert("", "end", values=("(当前数据下无有效信号)", "", "", ""))

    def _clear_input(self):
        self.id_var.set(""); self.data_var.set(""); self.complete_lbl.configure(text="")
        self._clear_result(); self.id_entry.focus()

    def _clear_result(self):
        self.result_tree.delete(*self.result_tree.get_children())
        self.result_hdr.configure(text="解析结果")

    def _paste_from_clipboard(self):
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return
        if not text:
            return
        # 尝试解析剪贴板内容：可能是 ID+DATA 或纯 hex
        parts = text.split()
        if len(parts) >= 2:
            # 第一段可能是 ID
            id_part = parts[0]
            data_part = " ".join(parts[1:])
            try:
                mid = int(id_part, 16) if id_part.lower().startswith("0x") else int(id_part, 16)
                self.id_var.set(id_part)
                self.data_var.set(data_part)
                self._parse(id_part, data_part)
                return
            except ValueError:
                pass
        # 尝试作为纯 hex 数据
        data = parse_bytes(text)
        if data:
            self.data_var.set(text)
            self._status("已粘贴数据，请输入报文 ID")
            self.id_entry.focus()

    # =============== 导入数据 ===============

    def _import_data_dialog(self):
        """顶栏导入按钮"""
        self.tabs.set("解析")
        self._import_file()

    def _import_file(self):
        path = filedialog.askopenfilename(
            title="导入 CAN 日志",
            filetypes=[("CAN 日志", "*.trc;*.asc;*.blf;*.txt;*.log;*.hex;*.dat"), ("所有文件", "*.*")])
        if not path:
            return
        self._do_import(path)

    def _import_clipboard(self):
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            self._status("!! 剪贴板为空")
            return
        if not text:
            return
        # 把剪贴板内容保存到临时文件再导入
        tmp = os.path.join(os.environ.get("TEMP", os.path.expanduser("~")), "_dbc_import_clipboard.txt")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        self._do_import(tmp, delete_after=True)

    def _do_import(self, path, delete_after=False):
        self._status(f"正在解析: {os.path.basename(path)} ...")
        self.update_idletasks()
        try:
            recs, skipped = parse_log(path)
            if not recs:
                self._status(f"!! 未解析到报文，跳过 {len(skipped)} 行")
                return
            self.imported_records = recs
            self._refresh_import()
            self._status(f"已导入 {len(recs)} 帧 ({os.path.basename(path)})")
            self.tabs.set("解析")
        except Exception as e:
            self._status(f"!! 导入失败: {e}")
        finally:
            if delete_after:
                try:
                    os.remove(path)
                except Exception:
                    pass

    def _refresh_import(self):
        self.import_tree.delete(*self.import_tree.get_children())
        for i, (ts, aid, ext, data, _) in enumerate(self.imported_records):
            self.import_tree.insert("", "end", iid=str(i), values=(
                i+1, f"{ts:.4f}", f"0x{aid:X}", "29" if ext else "11", len(data), data.hex().upper()))
        self.import_count_lbl.configure(text=f"共 {len(self.imported_records)} 条记录")

    def _on_import_select(self, event):
        """单击导入记录 → 弹出解析结果窗口"""
        sel = self.import_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx >= len(self.imported_records):
            return
        ts, aid, ext, data, _ = self.imported_records[idx]
        if aid not in self.messages:
            self._status(f"!! 报文 ID 0x{aid:X} 不在 DBC 中，请先加载对应 DBC")
            return
        self._show_parse_popup(aid, data)

    def _on_import_double(self):
        """双击导入记录 → 弹出解析结果窗口"""
        self._on_import_select(None)

    def _show_parse_popup(self, mid, data):
        """在新窗口中显示解析结果，可复用窗口"""
        if mid not in self.messages:
            return
        msg = self.messages[mid]
        hex_str = data.hex().upper()

        # 复用已有弹窗
        if hasattr(self, '_popup') and self._popup and self._popup.winfo_exists():
            popup = self._popup
            popup.title(f"解析: 0x{mid:X}  {msg['name']}")
            # 清空旧内容
            for w in popup.winfo_children():
                w.destroy()
        else:
            popup = ctk.CTkToplevel(self)
            popup.title(f"解析: 0x{mid:X}  {msg['name']}")
            popup.geometry("650x450")
            popup.minsize(400, 250)
            popup.attributes("-topmost", True)
            self._popup = popup
            # 关闭时清理引用
            popup.protocol("WM_DELETE_WINDOW", lambda: self._popup.destroy() if hasattr(self, '_popup') else None)

        # 标题
        title = ctk.CTkLabel(popup, text=f"{msg['name']}  (ID=0x{mid:X}, {len(data)}B: {hex_str})",
                             font=("Microsoft YaHei UI", 14, "bold"), anchor="w")
        title.pack(fill="x", padx=12, pady=(10, 2))

        # 分隔线
        ctk.CTkLabel(popup, text="", height=2).pack(fill="x", padx=12, pady=2)

        # 信号表格
        container = ctk.CTkFrame(popup)
        container.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        tree = ttk.Treeview(container, columns=("name", "value", "unit", "cmt"), show="headings", height=15)
        tree.heading("name", text="信号名", anchor="w")
        tree.heading("value", text="值", anchor="e")
        tree.heading("unit", text="单位", anchor="w")
        tree.heading("cmt", text="注释", anchor="w")
        tree.column("name", width=200, minwidth=120)
        tree.column("value", width=100, minwidth=60, anchor="e")
        tree.column("unit", width=50, minwidth=30)
        tree.column("cmt", width=200, minwidth=80)

        vsb = ttk.Scrollbar(container, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        if not msg["signals"]:
            tree.insert("", "end", values=("(无信号定义—诊断/CCP 透传报文)", "", "", ""))
        else:
            cm_map = {c["sig"]: c["text"] for c in self.comments if c["mid"] == mid}
            shown = 0
            for sig, raw, phys in decode_message(data, msg):
                disp = int(round(phys)) if abs(phys - round(phys)) < 1e-9 else round(phys, 3)
                vt = self.val_tables.get((mid, sig["name"]))
                en = f" [{vt[raw]}]" if vt and raw in vt else ""
                tree.insert("", "end", values=(sig["name"], str(disp) + en, sig["unit"], cm_map.get(sig["name"], "")))
                shown += 1
            if shown == 0:
                tree.insert("", "end", values=("(当前数据下无有效信号)", "", "", ""))

        self._status(f"弹窗解析 — {msg['name']} ({len(msg['signals'])} 个信号)")

    def _clear_import(self):
        self.imported_records = []
        self.import_tree.delete(*self.import_tree.get_children())
        self.import_count_lbl.configure(text="")

    # =============== 格式转换 ===============

    def _conv_browse_in(self):
        path = filedialog.askopenfilename(
            title="选择输入文件",
            filetypes=[("CAN 日志", "*.trc;*.asc;*.blf;*.txt;*.log"), ("所有文件", "*.*")])
        if not path:
            return
        self.conv_in_var.set(path)
        # 自动生成输出路径
        p = Path(path)
        fmt = self.conv_fmt_var.get().lower()
        self.conv_out_var.set(str(p.with_suffix(f".{fmt}")))

    def _conv_do(self):
        inp = self.conv_in_var.get().strip()
        if not inp or not os.path.isfile(inp):
            self._conv_log("!! 请选择输入文件")
            return
        fmt = self.conv_fmt_var.get().lower()
        out = self.conv_out_var.get().strip()
        if not out:
            out = str(Path(inp).with_suffix(f".{fmt}"))
            self.conv_out_var.set(out)
        self._conv_log(f"正在转换: {inp} → {out} ...")
        self.update_idletasks()
        ok, msg = convert_log(inp, out, fmt)
        self._conv_log(msg)
        self._status(msg)

    def _conv_log(self, text):
        self.conv_log.insert("end", text + "\n")
        self.conv_log.see("end")


# ======================= 入口 =======================

def main():
    app = DbcParserApp()
    app.mainloop()


if __name__ == "__main__":
    if "--cli" in sys.argv:
        print("CLI 模式已弃用，请使用 GUI 版本。")
        sys.exit(0)
    main()