# -*- coding: utf-8 -*-
"""
实时监控模块 — 供 dbc_parser.py 调用
=====================================
数据源: PCAN 通道，经 python-can 的 pcan backend 调用 PCAN-Basic，可 listen-only 打开

设计要点:
  - 接收线程只负责取帧入队，绝不触碰 tkinter 控件
  - 聚合状态只由 UI 线程访问，因此无需加锁
  - 界面按报文 ID 汇总，不做滚动日志（高负载下 Treeview 会卡死）
  - 发送面板是独立窗口，不占标签页，避免挤压报文阅读区域
"""

import csv
import json
import os
import queue
import threading
import time
import ctypes
from ctypes import wintypes

import customtkinter as ctk
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

try:
    import can
    HAS_CAN = True
except ImportError:
    HAS_CAN = False

try:
    import uds
    HAS_UDS = True
except ImportError:
    HAS_UDS = False

try:
    import pcan_config as pc
    HAS_PCAN_CFG = True
except Exception:
    pc = None
    HAS_PCAN_CFG = False


# ============================ 数据源 ============================

class FrameSource:
    label = "未定义"

    def open(self):
        raise NotImplementedError

    def recv(self, timeout=0.1):
        raise NotImplementedError

    def close(self):
        pass


# ============================ 列表导入导出 ============================

_LIST_OUTPUT_DIR = r'D:\3、工作输出\数据'


def _list_dir():
    return _LIST_OUTPUT_DIR if os.path.isdir(_LIST_OUTPUT_DIR) else None


def _ask_save(parent, default_name):
    return filedialog.asksaveasfilename(
        parent=parent, title="导出列表", defaultextension=".csv",
        initialfile=default_name, initialdir=_list_dir(),
        filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])


def _ask_open(parent):
    return filedialog.askopenfilename(
        parent=parent, title="导入列表", initialdir=_list_dir(),
        filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])


def _write_csv(path, header, rows):
    """UTF-8 with BOM：Excel 双击打开不乱码"""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _read_csv(path):
    """读成 dict 列表；表头去空格，值也去首尾空白"""
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        out = []
        for row in reader:
            if not row:
                continue
            item = {}
            for k, v in row.items():
                if k is None:
                    continue
                item[str(k).strip()] = (v or "").strip() if isinstance(v, str) else v
            out.append(item)
        return out


def _truthy(v, default=True):
    if v is None or v == "":
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off", "否")


def _ensure_pcan_dll():
    """若程序目录存在 PCANBasic.dll，加入 DLL 搜索路径，便于免安装驱动时直接使用"""
    import os
    import sys
    try:
        base = os.path.dirname(os.path.abspath(sys.argv[0]))
    except Exception:
        return None
    for sub in ("", "x64"):
        d = os.path.join(base, sub) if sub else base
        if os.path.isfile(os.path.join(d, "PCANBasic.dll")):
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(d)
                except Exception:
                    pass
            return d
    return None


def _friendly_pcan_error(exc, channel):
    """把 PCAN-Basic 的原始错误翻译成可操作的提示"""
    msg = str(exc)
    low = msg.lower()
    if "f_clock" in low or "timing" in low:
        return (f"位定时参数未被接受：{msg}\n"
                "经典 CAN 的 PCAN 硬件时钟固定 8 MHz，"
                "请调整采样点或改用标准波特率后重试。")
    if "not found" in low and "pcanbasic" in low:
        return ("未找到 PCANBasic.dll。请安装 PEAK-System 驱动包，"
                "或把 PCANBasic.dll 放到本程序所在目录")
    if "handle" in low or "invalid" in low:
        return (f"通道 {channel} 不可用：设备未连接、通道名有误，"
                f"或被 PCAN-View / 其他程序占用。\n原始错误：{msg}")
    if "occupied" in low or "initialize" in low:
        return (f"通道 {channel} 初始化失败：该通道已被别的程序占用。\n\n"
                "占用者是 PCAN-View → 本程序会用波特率自适应尝试接入；\n"
                "占用者是另一个 PCAN-Basic 程序（如 Testflow）→ "
                "PCAN-Basic 不支持两个客户端共用，只能关闭它或换一个物理通道。\n\n"
                f"原始错误：{msg}")
    return msg


class PcanSource(FrameSource):
    """
    PCAN-Basic 通道。
    listen_only=True → 以 BusState.PASSIVE 打开，不发 ACK、不发帧，对总线零影响。
    位定时由 CanConfig 显式给出（brp/tseg/sjw），不再依赖 from_sample_point 的隐式推导。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.bus = None
        self.share_note = None

    @property
    def label(self):
        return f"PCAN {self.cfg.summary()}"

    def open(self):
        _ensure_pcan_dll()
        cfg = self.cfg
        self.share_note = None
        # 通道被别的程序占着时，PCAN-Basic 默认拒绝第二个客户端接入。
        # 只读场景下先在初始化之前置 PCAN_BITRATE_ADAPTING，让驱动沿用
        # 通道上已有的那套波特率（PCAN-View 占用时这条路可行）。
        if cfg.listen_only:
            try:
                cond, _ = pc.query_condition(cfg.channel)
                if cond in (pc.COND_OCCUPIED, pc.COND_PCANVIEW):
                    ok, note = pc.set_bitrate_adapting(cfg.channel, True)
                    self.share_note = note if ok else None
            except Exception:
                pass
        else:
            # 正常收发必须由本程序决定波特率。若上一次试共享时置过自适应，
            # 这里要清掉，否则驱动会沿用通道上残留的旧波特率。
            try:
                pc.set_bitrate_adapting(cfg.channel, False)
            except Exception:
                pass
        kwargs = {
            "interface": "pcan",
            "channel": cfg.channel,
            "state": can.BusState.PASSIVE if cfg.listen_only else can.BusState.ACTIVE,
        }

        if cfg.is_fd:
            kwargs["fd"] = True
            t1, t2 = cfg.nom_timing, cfg.data_timing
            if t1 and t2:
                try:
                    kwargs["timing"] = can.BitTimingFd(
                        f_clock=cfg.f_clock,
                        nom_brp=t1["brp"], nom_tseg1=t1["tseg1"],
                        nom_tseg2=t1["tseg2"], nom_sjw=t1["sjw"],
                        data_brp=t2["brp"], data_tseg1=t2["tseg1"],
                        data_tseg2=t2["tseg2"], data_sjw=t2["sjw"])
                except Exception:
                    kwargs["bitrate"] = cfg.nom_bitrate
            else:
                kwargs["bitrate"] = cfg.nom_bitrate
        else:
            kwargs["bitrate"] = cfg.nom_bitrate
            # PCAN 经典 CAN 的硬件时钟固定 8 MHz：python-can 在 pcan.py 里用
            # VALID_PCAN_CAN_CLOCKS = {8_000_000} 校验，把 80/40 MHz 算出的
            # brp 直接传进去会抛 CanInitializationError
            # （"doesn't match any of the allowed device f_clock values: 8000000"）。
            if (getattr(cfg, "nom_manual", False) and cfg.nom_brp
                    and cfg.nom_seg1 and cfg.nom_seg2):
                # 手动模式：用户填的 brp/seg 就是最终值，直接用
                try:
                    kwargs["timing"] = can.BitTiming(
                        f_clock=8_000_000, brp=cfg.nom_brp,
                        tseg1=cfg.nom_seg1, tseg2=cfg.nom_seg2,
                        sjw=cfg.nom_sjw)
                except Exception:
                    kwargs.pop("timing", None)
            else:
                t8 = pc.calc_bit_timing(8_000_000, cfg.nom_bitrate,
                                        cfg.nom_sample_point, cfg.nom_sjw)
                if t8 and abs(t8.get("deviation", 99)) <= 2.0:
                    try:
                        kwargs["timing"] = can.BitTiming(
                            f_clock=8_000_000, brp=t8["brp"],
                            tseg1=t8["tseg1"], tseg2=t8["tseg2"], sjw=t8["sjw"])
                    except Exception:
                        kwargs.pop("timing", None)
            # 8 MHz 下找不到接近目标的采样点时，退回只给 bitrate，
            # 由 PCAN 使用标准 BTR（500k 即 87.5%），不强行套自定义采样点。

        try:
            self.bus = can.Bus(**kwargs)
        except Exception as e:
            raise RuntimeError(_friendly_pcan_error(e, cfg.channel)) from e

    def recv(self, timeout=0.1):
        return self.bus.recv(timeout=timeout)

    def close(self):
        if self.bus is not None:
            try:
                self.bus.shutdown()
            except Exception:
                pass
            self.bus = None


# ============================ 聚合状态 ============================

class MonitorState:
    """只由 UI 线程访问，因此不加锁"""

    def __init__(self):
        self.msgs = {}
        self.total = 0
        self.dropped = 0
        self.t0 = time.time()

    def reset(self):
        self.msgs.clear()
        self.total = 0
        self.dropped = 0
        self.t0 = time.time()

    def feed(self, msg):
        mid = msg.arbitration_id
        now = getattr(msg, "timestamp", None) or time.time()
        rec = self.msgs.get(mid)
        if rec is None:
            rec = self.msgs[mid] = {
                "count": 0, "period": None, "last_ts": None,
                "last_data": None, "changed": 0,
                "is_fd": bool(getattr(msg, "is_fd", False)),
                "ext": bool(getattr(msg, "is_extended_id", False)),
            }
        rec["count"] += 1
        if rec["last_ts"] is not None:
            dt = (now - rec["last_ts"]) * 1000.0
            if 0 < dt < 10000:
                rec["period"] = dt if rec["period"] is None else rec["period"] * 0.8 + dt * 0.2
        rec["last_ts"] = now
        new_data = bytes(msg.data)
        if rec["last_data"] is not None and new_data != rec["last_data"]:
            rec["changed"] += 1
        rec["last_data"] = new_data
        self.total += 1

    @property
    def fps(self):
        elapsed = time.time() - self.t0
        return self.total / elapsed if elapsed > 0.5 else 0.0


# ============================ 界面 ============================

def _palette():
    dark = ctk.get_appearance_mode() == "Dark"
    return {
        "card": "#2A2A2A" if dark else "#FFFFFF",
        "card_alt": "#323232" if dark else "#F6F7F9",
        "text": "#DCDCDC" if dark else "#1B1B1B",
        "muted": "#8C8C8C" if dark else "#6B7280",
        "border": "#3A3A3A" if dark else "#E3E5E8",
        "head": "#262626" if dark else "#F0F2F5",
        "row_alt": "#2F2F2F" if dark else "#FAFBFC",
        "accent": "#3B8ED0" if dark else "#2A7CC7",
        "danger": "#B2453C",
        "neutral": "#5A5A5A" if dark else "#9AA0A6",
    }


class CanConfigDialog(ctk.CTkToplevel):
    """CAN 通道配置对话框：通道 / 帧格式 / 时钟 / 仲裁段 / 数据段 / 收发模式"""

    def __init__(self, master, cfg, owner=None):
        super().__init__(master)
        self.C = _palette()
        self.cfg = cfg.copy()
        self.owner = owner          # 用来判断"通道是不是本程序自己占着"
        self.channels = []
        self.result = None

        self.title("CAN 通道配置")
        self.geometry("600x620")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self._build()
        self._sync_from_cfg()
        self._refresh_channels()
        self._recalc()
        # 按内容撑高度：写死 620 会在底部留一大块空白
        try:
            self.update_idletasks()
            h = self.winfo_reqheight()
            self.geometry(f"600x{max(560, min(h + 8, 980))}")
        except Exception:
            pass
        present_window(self, master)
        # CTk 建窗后 10ms 会 withdraw 再 deiconify，withdraw 会顺带撤掉模态；
        # 等提层落定再补一次 grab，免得对话框变成"浮在主窗口下面又不拦点击"
        self.after(420, self._regrab)

    def _regrab(self):
        try:
            if self.winfo_exists() and self.winfo_viewable():
                self.grab_set()
                self.focus_force()
        except Exception:
            pass

    # ---------------- 构件 ----------------

    def _lab(self, master, text, width=0):
        return ctk.CTkLabel(master, text=text, anchor="w", width=width,
                            font=("Microsoft YaHei UI", 11),
                            text_color=self.C["muted"])

    def _build(self):
        c = self.C
        root = ctk.CTkFrame(self, fg_color="transparent")
        root.pack(fill="both", expand=True, padx=14, pady=12)
        root.grid_columnconfigure(1, weight=1)

        r = 0
        self._lab(root, "通道", 64).grid(row=r, column=0, sticky="w", pady=4)
        self.ch_var = ctk.StringVar(value=self.cfg.channel)
        self.ch_combo = ctk.CTkComboBox(root, variable=self.ch_var, values=[self.cfg.channel],
                                        height=30, font=("Consolas", 11),
                                        command=lambda _v: self._on_channel())
        self.ch_combo.grid(row=r, column=1, sticky="ew", padx=8, pady=4)
        ctk.CTkButton(root, text="枚举", width=56, height=30,
                      fg_color=c["neutral"], hover_color="#6B6B6B",
                      command=self._refresh_channels).grid(row=r, column=2, padx=(0, 6), pady=4)
        self.ch_state = ctk.CTkLabel(root, text="—", width=104, anchor="w",
                                     font=("Microsoft YaHei UI", 11),
                                     text_color=c["muted"])
        self.ch_state.grid(row=r, column=3, sticky="w", pady=4)

        r += 1
        self._lab(root, "帧格式", 64).grid(row=r, column=0, sticky="w", pady=4)
        fm = ctk.CTkFrame(root, fg_color="transparent")
        fm.grid(row=r, column=1, columnspan=3, sticky="w", padx=8, pady=4)
        self.fmode_var = ctk.StringVar(value=self.cfg.frame_mode)
        ctk.CTkRadioButton(fm, text="经典 CAN", variable=self.fmode_var, value="classic",
                           font=("Microsoft YaHei UI", 11),
                           command=self._on_fmode).pack(side="left", padx=(0, 20))
        ctk.CTkRadioButton(fm, text="CAN FD", variable=self.fmode_var, value="fd",
                           font=("Microsoft YaHei UI", 11),
                           command=self._on_fmode).pack(side="left")

        r += 1
        self._lab(root, "时钟", 64).grid(row=r, column=0, sticky="w", pady=4)
        self.fclk_var = ctk.StringVar(value="80 MHz")
        ctk.CTkOptionMenu(root, values=[t for _, t in pc.F_CLOCK_OPTIONS],
                          variable=self.fclk_var, width=150, height=30,
                          font=("Consolas", 11),
                          command=lambda _v: self._recalc()
                          ).grid(row=r, column=1, sticky="w", padx=8, pady=4)

        self.nom_br_var = ctk.StringVar()
        self.nom_sp_var = ctk.StringVar()
        self.nom_sjw_var = ctk.StringVar()
        self.data_br_var = ctk.StringVar()
        self.data_sp_var = ctk.StringVar()
        self.data_sjw_var = ctk.StringVar()
        # 手动位定时
        self.nom_manual_var = ctk.BooleanVar(value=False)
        self.nom_brp_var = ctk.StringVar()
        self.nom_seg1_var = ctk.StringVar()
        self.nom_seg2_var = ctk.StringVar()
        self.data_manual_var = ctk.BooleanVar(value=False)
        self.data_brp_var = ctk.StringVar()
        self.data_seg1_var = ctk.StringVar()
        self.data_seg2_var = ctk.StringVar()
        self._in_recalc = False

        r = self._section(root, r + 1, "仲裁段（经典 CAN 与 FD 共用）",
                          self.nom_br_var, self.nom_sp_var, self.nom_sjw_var,
                          pc.BITRATE_PRESETS,
                          manual_var=self.nom_manual_var,
                          brp_var=self.nom_brp_var,
                          seg1_var=self.nom_seg1_var,
                          seg2_var=self.nom_seg2_var)
        self.nom_timing_lbl = self._lab(root, "", 0)
        self.nom_timing_lbl.grid(row=r, column=0, columnspan=4, sticky="w", padx=(72, 0), pady=(0, 4))

        r += 1
        self.data_hdr_row = r
        self.data_widgets = []
        r = self._section(root, r, "数据段（仅 CAN FD）",
                          self.data_br_var, self.data_sp_var, self.data_sjw_var,
                          pc.DATA_BITRATE_PRESETS, store=self.data_widgets,
                          manual_var=self.data_manual_var,
                          brp_var=self.data_brp_var,
                          seg1_var=self.data_seg1_var,
                          seg2_var=self.data_seg2_var)
        self.data_timing_lbl = self._lab(root, "", 0)
        self.data_timing_lbl.grid(row=r, column=0, columnspan=4, sticky="w", padx=(72, 0), pady=(0, 4))

        r += 1
        self._lab(root, "DBC", 64).grid(row=r, column=0, sticky="w", pady=4)
        dbf = ctk.CTkFrame(root, fg_color="transparent")
        dbf.grid(row=r, column=1, columnspan=3, sticky="we", padx=8, pady=4)
        dbf.grid_columnconfigure(0, weight=1)
        self.dbc_var = ctk.StringVar(value=getattr(self.cfg, "dbc_path", "") or "")
        ctk.CTkEntry(dbf, textvariable=self.dbc_var, height=30,
                     font=("Microsoft YaHei UI", 10),
                     placeholder_text="留空＝沿用主界面的 DBC"
                     ).grid(row=0, column=0, sticky="we")
        ctk.CTkButton(dbf, text="选择…", width=64, height=30,
                      fg_color=c["neutral"], hover_color="#6B6B6B",
                      command=self._pick_dbc).grid(row=0, column=1, padx=(6, 0))
        ctk.CTkButton(dbf, text="清空", width=52, height=30,
                      fg_color=c["neutral"], hover_color="#6B6B6B",
                      command=lambda: self.dbc_var.set("")
                      ).grid(row=0, column=2, padx=(6, 0))

        r += 1
        self._lab(root, "收发模式", 64).grid(row=r, column=0, sticky="w", pady=4)
        md = ctk.CTkFrame(root, fg_color="transparent")
        md.grid(row=r, column=1, columnspan=3, sticky="w", padx=8, pady=4)
        self.mode_var = ctk.StringVar(value=self.cfg.bus_mode)
        self.rb_active = ctk.CTkRadioButton(md, text="正常收发", variable=self.mode_var,
                                            value="active", font=("Microsoft YaHei UI", 11))
        self.rb_active.pack(side="left", padx=(0, 20))
        self.rb_listen = ctk.CTkRadioButton(md, text="只监听", variable=self.mode_var,
                                            value="listen_only", font=("Microsoft YaHei UI", 11))
        self.rb_listen.pack(side="left")

        r += 1
        self.hint = ctk.CTkLabel(root, text="", anchor="w", justify="left",
                                 wraplength=540, font=("Microsoft YaHei UI", 11),
                                 text_color="#C08A2A")
        self.hint.grid(row=r, column=0, columnspan=4, sticky="w", pady=(8, 0))

        # 底部
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=14, pady=(0, 12))
        ctk.CTkButton(foot, text="确定", width=88, height=32,
                      fg_color=c["accent"], hover_color="#2A6FA8",
                      command=self._ok).pack(side="right")
        ctk.CTkButton(foot, text="取消", width=88, height=32,
                      fg_color=c["neutral"], hover_color="#6B6B6B",
                      command=self._cancel).pack(side="right", padx=(0, 8))

        self._apply_fd_state()

    def _section(self, root, r, title, br_var, sp_var, sjw_var, presets,
                 store=None, manual_var=None, brp_var=None,
                 seg1_var=None, seg2_var=None):
        c = self.C
        ctk.CTkLabel(root, text=title, anchor="w",
                     font=("Microsoft YaHei UI", 11, "bold"),
                     text_color=c["text"]).grid(row=r, column=0, columnspan=4,
                                                sticky="w", pady=(12, 2))
        r += 1
        box = ctk.CTkFrame(root, fg_color="transparent")
        box.grid(row=r, column=0, columnspan=4, sticky="w", padx=(8, 0), pady=2)

        self._lab(box, "波特率", 48).pack(side="left")
        combo = ctk.CTkComboBox(box, variable=br_var, values=[t for _, t in presets],
                                width=132, height=30, font=("Consolas", 11),
                                command=lambda _v: self._recalc())
        combo.pack(side="left", padx=(0, 14))

        self._lab(box, "采样点%", 52).pack(side="left")
        sp_entry = ctk.CTkEntry(box, textvariable=sp_var, width=72, height=30,
                                font=("Consolas", 11))
        sp_entry.pack(side="left", padx=(0, 14))
        sp_var.trace_add("write", lambda *_: self._recalc())

        self._lab(box, "SJW", 34).pack(side="left")
        sjw_entry = ctk.CTkEntry(box, textvariable=sjw_var, width=54, height=30,
                                 font=("Consolas", 11))
        sjw_entry.pack(side="left")
        sjw_var.trace_add("write", lambda *_: self._recalc())

        widgets = [combo, sp_entry, sjw_entry]

        # 手动位定时行
        manual_cb = brp_e = seg1_e = seg2_e = None
        if manual_var is not None:
            r += 1
            mbox = ctk.CTkFrame(root, fg_color="transparent")
            mbox.grid(row=r, column=0, columnspan=4, sticky="w",
                      padx=(8, 0), pady=2)

            manual_cb = ctk.CTkCheckBox(
                mbox, text="手动 BRP/SEG", variable=manual_var,
                font=("Microsoft YaHei UI", 11),
                checkbox_width=18, checkbox_height=18,
                command=self._recalc)
            manual_cb.pack(side="left", padx=(0, 14))

            self._lab(mbox, "BRP", 34).pack(side="left")
            brp_e = ctk.CTkEntry(mbox, textvariable=brp_var, width=62, height=30,
                                 font=("Consolas", 11))
            brp_e.pack(side="left", padx=(0, 12))

            self._lab(mbox, "SEG1", 42).pack(side="left")
            seg1_e = ctk.CTkEntry(mbox, textvariable=seg1_var, width=62, height=30,
                                  font=("Consolas", 11))
            seg1_e.pack(side="left", padx=(0, 12))

            self._lab(mbox, "SEG2", 42).pack(side="left")
            seg2_e = ctk.CTkEntry(mbox, textvariable=seg2_var, width=62, height=30,
                                  font=("Consolas", 11))
            seg2_e.pack(side="left", padx=(0, 12))

            for v in (brp_var, seg1_var, seg2_var):
                v.trace_add("write", lambda *_: self._recalc())
            widgets.extend([manual_cb, brp_e, seg1_e, seg2_e])

        if store is not None:
            store.extend(widgets)
        return r + 1

    # ---------------- 数据同步 ----------------

    def _sync_from_cfg(self):
        cfg = self.cfg
        self.ch_var.set(pc.channel_label(cfg.channel) if HAS_PCAN_CFG and cfg.channel else cfg.channel)
        self.fmode_var.set(cfg.frame_mode)
        self.fclk_var.set(f"{cfg.f_clock // 1_000_000} MHz")
        self.nom_br_var.set(self._br_text(cfg.nom_bitrate))
        self.nom_sp_var.set(f"{cfg.nom_sample_point:g}")
        self.nom_sjw_var.set(str(cfg.nom_sjw))
        self.data_br_var.set(self._br_text(cfg.data_bitrate))
        self.data_sp_var.set(f"{cfg.data_sample_point:g}")
        self.data_sjw_var.set(str(cfg.data_sjw))
        self.mode_var.set(cfg.bus_mode)

        # 手动位定时回填
        self.nom_manual_var.set(bool(getattr(cfg, "nom_manual", False)))
        self.data_manual_var.set(bool(getattr(cfg, "data_manual", False)))
        t = cfg.nom_timing
        if t:
            self.nom_brp_var.set(str(t["brp"]))
            self.nom_seg1_var.set(str(t["tseg1"]))
            self.nom_seg2_var.set(str(t["tseg2"]))
        dt = cfg.data_timing
        if dt:
            self.data_brp_var.set(str(dt["brp"]))
            self.data_seg1_var.set(str(dt["tseg1"]))
            self.data_seg2_var.set(str(dt["tseg2"]))

    @staticmethod
    def _br_text(br):
        for v, t in pc.ALL_BITRATE_PRESETS:
            if v == br:
                return t
        return str(br)

    @staticmethod
    def _parse_br(text):
        t = (text or "").strip().lower().replace(" ", "")
        if t.endswith("kbit/s"):
            t = t[:-6]
        elif t.endswith("mbit/s"):
            t = t[:-6]
            try:
                return int(float(t) * 1_000_000)
            except ValueError:
                return None
        elif t.endswith("k"):
            t = t[:-1]
        elif t.endswith("m"):
            t = t[:-1]
            try:
                return int(float(t) * 1_000_000)
            except ValueError:
                return None
        try:
            v = float(t)
        except ValueError:
            return None
        if v < 100_000:
            v *= 1000
        return int(v)

    # ---------------- 交互 ----------------

    def _on_fmode(self):
        fd = self.fmode_var.get() == "fd"
        self.cfg.frame_mode = "fd" if fd else "classic"
        if fd:
            self.cfg.set_fd_defaults()
            # 只回填被 set_fd_defaults 改动的那几项。
            # 不能走 _sync_from_cfg：它会把 fmode_var 写回 cfg.frame_mode，
            # 而 cfg.frame_mode 要到点「确定」才更新，等于把刚点下的单选弹回去，
            # 数据段控件也随之被判成「经典 CAN」而禁用。
            self.nom_sp_var.set(f"{self.cfg.nom_sample_point:g}")
            self.data_br_var.set(self._br_text(self.cfg.data_bitrate))
            self.data_sp_var.set(f"{self.cfg.data_sample_point:g}")
            self.data_sjw_var.set(str(self.cfg.data_sjw))
        self._apply_fd_state()
        self._recalc()

    def _apply_fd_state(self):
        fd = self.fmode_var.get() == "fd"
        st = "normal" if fd else "disabled"
        for w in getattr(self, "data_widgets", []):
            try:
                w.configure(state=st)
            except Exception:
                pass
        if fd:
            self.data_timing_lbl.configure(text="", text_color=self.C["muted"])
        else:
            self.data_timing_lbl.configure(text="（经典 CAN 不使用数据段）",
                                           text_color=self.C["muted"])

    def _pick_dbc(self):
        p = filedialog.askopenfilename(
            parent=self, title="选择实时解析用的 DBC",
            filetypes=[("DBC 文件", "*.dbc"), ("所有文件", "*.*")])
        if p:
            self.dbc_var.set(p)

    @staticmethod
    def _channel_name(text):
        """从 'PCAN_PCIBUS1（通道 1 · PCIe）' 取出 'PCAN_PCIBUS1'"""
        return (text or "").split("（")[0].split("(")[0].strip()

    def _is_owner_channel(self, name):
        """该通道是不是本程序自己占着的"""
        owner = getattr(self, "owner", None)
        fn = getattr(owner, "_self_channel", None)
        if fn is None:
            return False
        try:
            return fn() == name
        except Exception:
            return False

    def _on_channel(self):
        name = self._channel_name(self.ch_var.get())
        if not name:
            return
        cond, text = pc.query_condition(name)
        if cond is None:
            self.ch_state.configure(text=text[:18], text_color=self.C["muted"])
            self.hint.configure(text=f"无法查询通道状态：{text}")
            return
        color = {pc.COND_AVAILABLE: "#3C9A5F",
                 pc.COND_OCCUPIED: "#C08A2A",
                 pc.COND_PCANVIEW: "#C08A2A"}.get(cond, self.C["muted"])
        self.ch_state.configure(text=f"● {text}", text_color=color)

        if cond in (pc.COND_OCCUPIED, pc.COND_PCANVIEW):
            if self._is_owner_channel(name):
                # 是本程序自己开着这条通道：不是冲突，改完配置会自动重连
                self.ch_state.configure(text="● 正在使用（本程序）",
                                        text_color="#2A7CC7")
                self.mode_var.set(self.cfg.bus_mode)
                self.rb_active.configure(state="normal")
                self.hint.configure(
                    text="该通道正由本程序占用。改完配置点「确定」会自动重连，"
                         "不需要先断开。")
                return
            # 不强行把「收发模式」改成只监听：默认就是正常收发，占用只是提示，
            # 用户可能正要去关掉占用程序。真要连不上，_precheck_channel 还会再问一次。
            self.rb_active.configure(state="normal")
            tip = ("该通道已被 PCAN-View 占用，请先关闭 PCAN-View 再连接。"
                   if cond == pc.COND_PCANVIEW
                   else "该通道已被其他程序占用（如 Testflow）。"
                        "直接连会失败，需要先释放该通道。")
            self.hint.configure(text=tip)
        elif cond == pc.COND_UNAVAILABLE:
            self.hint.configure(text="该通道不存在或设备未连接。")
        else:
            self.rb_active.configure(state="normal")
            self.hint.configure(text="")

    def _refresh_channels(self):
        if not HAS_PCAN_CFG:
            return
        res = pc.list_channels()
        if not res["ok"]:
            self.hint.configure(text=res["error"])
            self.channels = []
            return
        self.channels = res["channels"]
        values = [ch["label"] for ch in self.channels] or [self.ch_var.get()]
        self.ch_combo.configure(values=values)
        if self.channels:
            # 保持当前通道被选中
            cur = self.ch_var.get()
            match = [ch for ch in self.channels if ch["name"] == cur]
            self.ch_combo.set(match[0]["label"] if match else values[0])
            self._on_channel()
        else:
            self.hint.configure(text="未检测到可用通道（驱动未安装或设备未连接）")

    @staticmethod
    def _int_or_none(t):
        try:
            return int(float(str(t).strip()))
        except (TypeError, ValueError):
            return None

    def _manual_timing(self, f_clock, brp_var, seg1_var, seg2_var, sjw):
        b = self._int_or_none(brp_var.get())
        s1 = self._int_or_none(seg1_var.get())
        s2 = self._int_or_none(seg2_var.get())
        if b is None or s1 is None or s2 is None:
            return None
        return pc.timing_from_parts(f_clock, b, s1, s2, sjw)

    def _recalc(self):
        # 自动模式会把算出的 brp/seg 写回输入框，写回又会触发 trace，
        # 这里用重入保护掐断，否则就是无限递归。
        if getattr(self, "_in_recalc", False):
            return
        self._in_recalc = True
        try:
            self._recalc_inner()
        finally:
            self._in_recalc = False

    def _recalc_inner(self):
        fd = self.fmode_var.get() == "fd"
        fclk = 80_000_000 if self.fclk_var.get().startswith("80") else 40_000_000
        # PCAN 经典 CAN 的硬件时钟固定 8 MHz（python-can 只接受 8_000_000），
        # 界面上选的 80/40 MHz 对经典 CAN 不生效。按 8 MHz 显示真实采样点，
        # 否则界面报 80.00%、实际连上的是 81.25%。
        calc_clock = fclk if fd else 8_000_000
        nom_sjw = self._int_or_none(self.nom_sjw_var.get()) or 1

        if getattr(self, "nom_manual_var", None) is not None and self.nom_manual_var.get():
            t = self._manual_timing(calc_clock, self.nom_brp_var,
                                    self.nom_seg1_var, self.nom_seg2_var, nom_sjw)
            if t:
                text = ("手动 · brp={} seg1={} seg2={} → {} 实际 {:.2f}%".format(
                    t["brp"], t["tseg1"], t["tseg2"],
                    self._br_text(t["actual_bitrate"]), t["actual_sp"]))
                if not fd:
                    text += "　· 经典 CAN 固定 8 MHz"
                self.nom_timing_lbl.configure(text=text,
                                              text_color=self.C["muted"])
            else:
                self.nom_timing_lbl.configure(text="手动参数无效",
                                              text_color="#C08A2A")
        else:
            nom_br = self._parse_br(self.nom_br_var.get())
            try:
                nom_sp = float(self.nom_sp_var.get())
            except ValueError:
                nom_sp = None
            if nom_br and nom_sp:
                t = pc.calc_bit_timing(calc_clock, nom_br, nom_sp, nom_sjw)
                if t:
                    self.nom_brp_var.set(str(t["brp"]))
                    self.nom_seg1_var.set(str(t["tseg1"]))
                    self.nom_seg2_var.set(str(t["tseg2"]))
                text = pc.describe_timing(t)
                if not fd:
                    text += "　· 经典 CAN 固定 8 MHz 时钟"
                self.nom_timing_lbl.configure(
                    text=text,
                    text_color="#C08A2A" if (t and t["deviation"] >= 1.0) else self.C["muted"])
            else:
                self.nom_timing_lbl.configure(text="参数无效",
                                              text_color="#C08A2A")

        if not fd:
            self.data_timing_lbl.configure(text="（经典 CAN 不使用数据段）",
                                           text_color=self.C["muted"])
            return

        data_sjw = self._int_or_none(self.data_sjw_var.get()) or 1
        if getattr(self, "data_manual_var", None) is not None and self.data_manual_var.get():
            t = self._manual_timing(fclk, self.data_brp_var,
                                    self.data_seg1_var, self.data_seg2_var, data_sjw)
            if t:
                self.data_timing_lbl.configure(
                    text="手动 · brp={} seg1={} seg2={} → {} 实际 {:.2f}%".format(
                        t["brp"], t["tseg1"], t["tseg2"],
                        self._br_text(t["actual_bitrate"]), t["actual_sp"]),
                    text_color=self.C["muted"])
            else:
                self.data_timing_lbl.configure(text="手动参数无效",
                                               text_color="#C08A2A")
            return

        data_br = self._parse_br(self.data_br_var.get())
        try:
            data_sp = float(self.data_sp_var.get())
        except ValueError:
            data_sp = None
        if data_br and data_sp:
            t = pc.calc_bit_timing(fclk, data_br, data_sp, data_sjw)
            if t:
                self.data_brp_var.set(str(t["brp"]))
                self.data_seg1_var.set(str(t["tseg1"]))
                self.data_seg2_var.set(str(t["tseg2"]))
            self.data_timing_lbl.configure(
                text=pc.describe_timing(t),
                text_color="#C08A2A" if (t and t["deviation"] >= 1.0) else self.C["muted"])
        else:
            self.data_timing_lbl.configure(text="参数无效", text_color="#C08A2A")

    # ---------------- 结果 ----------------

    def _collect(self):
        cfg = self.cfg.copy()
        cfg.channel = self._channel_name(self.ch_var.get())
        cfg.frame_mode = self.fmode_var.get()
        cfg.f_clock = 80_000_000 if self.fclk_var.get().startswith("80") else 40_000_000
        cfg.nom_bitrate = self._parse_br(self.nom_br_var.get()) or cfg.nom_bitrate
        try:
            cfg.nom_sample_point = float(self.nom_sp_var.get())
        except ValueError:
            pass
        try:
            cfg.nom_sjw = int(float(self.nom_sjw_var.get()))
        except ValueError:
            pass
        cfg.data_bitrate = self._parse_br(self.data_br_var.get()) or cfg.data_bitrate
        try:
            cfg.data_sample_point = float(self.data_sp_var.get())
        except ValueError:
            pass
        try:
            cfg.data_sjw = int(float(self.data_sjw_var.get()))
        except ValueError:
            pass
        cfg.bus_mode = self.mode_var.get()
        # 手动位定时
        cfg.nom_manual = bool(self.nom_manual_var.get())
        cfg.nom_brp = self._int_or_none(self.nom_brp_var.get())
        cfg.nom_seg1 = self._int_or_none(self.nom_seg1_var.get())
        cfg.nom_seg2 = self._int_or_none(self.nom_seg2_var.get())
        cfg.data_manual = bool(self.data_manual_var.get())
        cfg.data_brp = self._int_or_none(self.data_brp_var.get())
        cfg.data_seg1 = self._int_or_none(self.data_seg1_var.get())
        cfg.data_seg2 = self._int_or_none(self.data_seg2_var.get())
        # 实时解析专用 DBC：空字符串表示沿用主界面已加载的那份
        try:
            cfg.dbc_path = self.dbc_var.get().strip()
        except AttributeError:
            cfg.dbc_path = getattr(cfg, "dbc_path", "")
        cfg.recompute()
        return cfg

    def _ok(self):
        cfg = self._collect()
        problems = cfg.problems()
        if problems:
            if not messagebox.askokcancel("参数检查", "\n".join(problems) + "\n\n仍要保存吗？"):
                return
        self.result = cfg
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class LiveTab(ctk.CTkFrame):
    def __init__(self, master, app, decode_fn, parse_fn=None):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.decode_fn = decode_fn
        self.parse_fn = parse_fn      # dbc_parser.parse_dbc，用于独立加载
        self.dbc = None               # {"messages","val_tables","path"}，None 表示沿用主界面
        self.monitor = MonitorState()
        self.src = None
        self.rx_thread = None
        self.q = queue.Queue(maxsize=20000)
        self.running = False
        self.paused = False
        self.selected = None
        self.filter_text = ""
        self.view_mode = "agg"
        self.frame_seq = 0
        self._pending_frames = []
        self.tp_listeners = []
        self.recorder = None
        self.read_only = False
        self.pcan_cfg = pc.CanConfig() if HAS_PCAN_CFG else None
        self._style_ready = False
        self.C = _palette()
        self._build()
        self._tick()

    # ---------------- 样式 ----------------

    def _setup_tree_style(self):
        if self._style_ready:
            return
        c = self.C
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("Live.Treeview",
                     background=c["card"], foreground=c["text"],
                     fieldbackground=c["card"], borderwidth=0,
                     rowheight=27, font=("Consolas", 11))
        st.configure("Live.Treeview.Heading",
                     background=c["head"], foreground=c["muted"],
                     borderwidth=0, relief="flat",
                     font=("Microsoft YaHei UI", 11, "bold"), padding=(8, 7))
        st.map("Live.Treeview.Heading", background=[("active", c["head"])])
        st.map("Live.Treeview",
               background=[("selected", c["accent"])],
               foreground=[("selected", "#FFFFFF")])
        # 发送列表专用：行更高、字更大，勾选格容易点中
        st.configure("Send.Treeview",
                     background=c["card"], foreground=c["text"],
                     fieldbackground=c["card"], borderwidth=0,
                     rowheight=34, font=("Consolas", 12))
        st.configure("Send.Treeview.Heading",
                     background=c["head"], foreground=c["muted"],
                     borderwidth=0, relief="flat",
                     font=("Microsoft YaHei UI", 11, "bold"), padding=(8, 7))
        st.map("Send.Treeview.Heading", background=[("active", c["head"])])
        st.map("Send.Treeview",
               background=[("selected", c["accent"])],
               foreground=[("selected", "#FFFFFF")])
        st.configure("Live.Vertical.TScrollbar",
                     background=c["card_alt"], troughcolor=c["card"],
                     bordercolor=c["card"], arrowcolor=c["muted"], width=10)
        self._style_ready = True

    # ---------------- 构建 ----------------

    def _label(self, master, text, muted=True, size=11, bold=False):
        font = ("Microsoft YaHei UI", size, "bold") if bold else ("Microsoft YaHei UI", size)
        return ctk.CTkLabel(master, text=text, font=font,
                            text_color=self.C["muted"] if muted else self.C["text"])

    def _build(self):
        c = self.C
        self._setup_tree_style()

        # ---------- 工具条 ----------
        bar = ctk.CTkFrame(self, fg_color=c["card"], corner_radius=6)
        bar.pack(fill="x", padx=10, pady=(10, 6))
        bar.grid_columnconfigure(1, weight=1)

        grp1 = ctk.CTkFrame(bar, fg_color="transparent", height=36)
        grp1.grid(row=0, column=0, sticky="w", padx=(14, 0), pady=8)

        self.btn_cfg = ctk.CTkButton(grp1, text="通道配置", width=88, height=30,
                                     fg_color=c["neutral"], hover_color="#6B6B6B",
                                     font=("Microsoft YaHei UI", 11),
                                     command=self._open_config)
        self.btn_cfg.pack(side="left")

        self.summary_lbl = ctk.CTkLabel(bar, text="", font=("Consolas", 11),
                                        text_color=c["muted"], anchor="w")
        self.summary_lbl.grid(row=0, column=1, sticky="w", padx=12, pady=8)

        self.btn_conn = ctk.CTkButton(bar, text="连接", width=68, height=30,
                                      fg_color=c["accent"], hover_color="#2A6FA8",
                                      font=("Microsoft YaHei UI", 11),
                                      command=self._toggle)
        self.btn_conn.grid(row=0, column=2, padx=(6, 0), pady=8)

        self.btn_rec = ctk.CTkButton(bar, text="录制", width=60, height=30,
                                     fg_color=c["neutral"], hover_color="#6B6B6B",
                                     font=("Microsoft YaHei UI", 11),
                                     command=self._toggle_record)
        self.btn_rec.grid(row=0, column=3, padx=6, pady=8)

        self.btn_send_win = ctk.CTkButton(
            bar, text="发送", width=68, height=30,
            fg_color=c["neutral"], hover_color="#6B6B6B",
            font=("Microsoft YaHei UI", 11),
            command=self._open_send)
        self.btn_send_win.grid(row=0, column=4, padx=6, pady=8)

        self.btn_pause = ctk.CTkButton(bar, text="暂停", width=56, height=30,
                                       fg_color=c["neutral"], hover_color="#6B6B6B",
                                       font=("Microsoft YaHei UI", 11),
                                       command=self._toggle_pause)
        self.btn_pause.grid(row=0, column=5, padx=6, pady=8)

        self.btn_clear = ctk.CTkButton(bar, text="清空", width=56, height=30,
                                       fg_color=c["neutral"], hover_color="#6B6B6B",
                                       font=("Microsoft YaHei UI", 11),
                                       command=self._clear)
        self.btn_clear.grid(row=0, column=6, padx=(6, 14), pady=8)

        # ---------- 视图切换 ----------
        vbar = ctk.CTkFrame(self, fg_color="transparent")
        vbar.pack(fill="x", padx=10, pady=(0, 2))
        self.view_mode = "agg"
        self.btn_agg = ctk.CTkButton(
            vbar, text="聚合视图", width=88, height=26,
            fg_color=c["accent"], hover_color="#2A6FA8",
            font=("Microsoft YaHei UI", 11),
            command=lambda: self._switch_view("agg"))
        self.btn_agg.pack(side="left")
        self.btn_frames = ctk.CTkButton(
            vbar, text="逐帧视图", width=88, height=26,
            fg_color=c["neutral"], hover_color="#6B6B6B",
            font=("Microsoft YaHei UI", 11),
            command=lambda: self._switch_view("frames"))
        self.btn_frames.pack(side="left", padx=(6, 0))
        self.view_hint = ctk.CTkLabel(
            vbar, text="", font=("Microsoft YaHei UI", 10),
            text_color=c["muted"])
        self.view_hint.pack(side="left", padx=(10, 0))

        # ---------- 主体：左列表 / 右详情 ----------
        area = ctk.CTkFrame(self, fg_color="transparent")
        area.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        area.grid_rowconfigure(0, weight=1)
        area.grid_columnconfigure(0, weight=1)

        body = ctk.CTkFrame(area, fg_color="transparent")
        body.grid(row=0, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=6)
        body.grid_columnconfigure(1, weight=5)
        self.agg_body = body
        self.frame_body = self._build_frame_panel(area)

        # 左：报文列表
        left = ctk.CTkFrame(body, fg_color=c["card"], corner_radius=6)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        lhead = ctk.CTkFrame(left, fg_color="transparent", height=38)
        lhead.pack(fill="x", padx=10, pady=(8, 4))
        lhead.pack_propagate(False)
        self._label(lhead, "报文", muted=False, size=12, bold=True).pack(side="left")
        self.count_lbl = self._label(lhead, "0 种", size=11)
        self.count_lbl.pack(side="left", padx=(8, 0))

        self.filter_var = ctk.StringVar()
        self.filter_entry = ctk.CTkEntry(lhead, textvariable=self.filter_var, width=148,
                                         height=28, placeholder_text="ID 或名称",
                                         font=("Microsoft YaHei UI", 11))
        self.filter_entry.pack(side="right")
        self._label(lhead, "过滤", size=11).pack(side="right", padx=(0, 6))
        self.filter_var.trace_add("write", lambda *_: self._rebuild_msgs())

        lc = ctk.CTkFrame(left, fg_color="transparent")
        lc.pack(fill="both", expand=True, padx=6, pady=(0, 8))

        cols = (("id", "ID", 104, "w"), ("name", "报文名称", 252, "w"),
                ("dlc", "DLC", 52, "e"), ("period", "周期 ms", 76, "e"),
                ("count", "计数", 64, "e"), ("chg", "变化", 56, "e"),
                ("_f", "", 24, "w"))
        self.msg_tree = ttk.Treeview(lc, show="headings", style="Live.Treeview",
                                     columns=[x[0] for x in cols])
        for key, text, w, anchor in cols:
            self.msg_tree.heading(key, text=text, anchor=anchor)
            self.msg_tree.column(key, width=w, minwidth=30, anchor=anchor,
                                 stretch=(key == "_f"))
        sv = ttk.Scrollbar(lc, orient="vertical", command=self.msg_tree.yview,
                           style="Live.Vertical.TScrollbar")
        self.msg_tree.configure(yscrollcommand=sv.set)
        self.msg_tree.pack(side="left", fill="both", expand=True)
        sv.pack(side="right", fill="y")
        self.msg_tree.tag_configure("alt", background=c["row_alt"])
        self.msg_tree.bind("<<TreeviewSelect>>", self._on_select)

        # 右：信号详情
        right = ctk.CTkFrame(body, fg_color=c["card"], corner_radius=6)
        right.grid(row=0, column=1, sticky="nsew")

        rhead = ctk.CTkFrame(right, fg_color="transparent", height=38)
        rhead.pack(fill="x", padx=10, pady=(8, 4))
        rhead.pack_propagate(False)
        self.sig_hdr = self._label(rhead, "信号", muted=False, size=12, bold=True)
        self.sig_hdr.pack(side="left")
        self.sig_meta = self._label(rhead, "", size=11)
        self.sig_meta.pack(side="left", padx=(8, 0))

        # 原始报文字节：核对解析是否正确时，整帧一眼看全
        # 用 Entry 而不是 Label，方便选中复制到别处比对
        self.raw_bar = ctk.CTkEntry(
            right, height=30, font=("Consolas", 12),
            placeholder_text="原始字节 —— 选中报文后显示最近一帧")
        self.raw_bar.pack(fill="x", padx=10, pady=(0, 6))

        rc = ctk.CTkFrame(right, fg_color="transparent")
        rc.pack(fill="both", expand=True, padx=6, pady=(0, 8))

        scols = (("name", "信号名", 190, "w"), ("raw", "原始值", 76, "e"),
                 ("phys", "物理值", 92, "e"), ("unit", "单位", 48, "w"),
                 ("_f", "", 14, "w"))
        self.sig_tree = ttk.Treeview(rc, show="headings", style="Live.Treeview",
                                     columns=[x[0] for x in scols])
        for key, text, w, anchor in scols:
            self.sig_tree.heading(key, text=text, anchor=anchor)
            self.sig_tree.column(key, width=w, minwidth=24, anchor=anchor,
                                 stretch=(key == "_f"))
        rv = ttk.Scrollbar(rc, orient="vertical", command=self.sig_tree.yview,
                           style="Live.Vertical.TScrollbar")
        self.sig_tree.configure(yscrollcommand=rv.set)
        self.sig_tree.pack(side="left", fill="both", expand=True)
        rv.pack(side="right", fill="y")
        self.sig_tree.tag_configure("alt", background=c["row_alt"])

        # ---------- 状态栏 ----------
        self.status = self._label(self, "未连接", size=11)
        self.status.pack(fill="x", padx=14, pady=(0, 8))

    # ---------------- 交互 ----------------

    def _toggle(self):
        if self.running:
            self._disconnect()
        else:
            self._connect()

    def _make_source(self):
        if not HAS_PCAN_CFG or self.pcan_cfg is None:
            raise RuntimeError("pcan_config 模块不可用，无法使用 PCAN 通道")
        return PcanSource(self.pcan_cfg.copy())

    # ---------------- 通道预检与状态机 ----------------

    def _self_channel(self):
        """本程序当前正在使用的通道名；未连接返回 None"""
        if not getattr(self, "running", False):
            return None
        src = getattr(self, "src", None)
        cfg = getattr(src, "cfg", None)
        return getattr(cfg, "channel", None)

    def _cfg_sig(self, cfg):
        """用于判断配置是否变化的签名（只取影响连接的字段）"""
        if cfg is None:
            return None
        return (
            cfg.channel, cfg.frame_mode, cfg.bus_mode, cfg.f_clock,
            cfg.nom_bitrate, cfg.nom_sample_point, cfg.nom_sjw,
            cfg.data_bitrate, cfg.data_sample_point, cfg.data_sjw,
            cfg.nom_manual, cfg.nom_brp, cfg.nom_seg1, cfg.nom_seg2,
            cfg.data_manual, cfg.data_brp, cfg.data_seg1, cfg.data_seg2,
            getattr(cfg, "dbc_path", ""),
        )

    def _precheck_channel(self):
        """打开前检查通道状态。返回 True 表示可以继续打开。"""
        cfg = self.pcan_cfg
        if cfg is None:
            return True

        cond, text = pc.query_condition(cfg.channel)
        label = pc.channel_label(cfg.channel)

        # 自己占着的通道：改完配置重连时必然读到 OCCUPIED，
        # 那不是"被别的程序占了"，不拦，直接放行。
        if (cond in (pc.COND_OCCUPIED, pc.COND_PCANVIEW)
                and self._self_channel() == cfg.channel):
            cfg.bus_mode_forced = False
            return True

        if cond is None:
            return messagebox.askokcancel(
                "通道状态未知",
                f"无法查询 {label} 的状态：\n{text}\n\n仍要尝试连接吗？")

        if cond == pc.COND_AVAILABLE:
            cfg.bus_mode_forced = False
            return True

        if cond == pc.COND_PCANVIEW:
            ok = messagebox.askyesno(
                "通道被 PCAN-View 占用",
                f"{label} 正被 PCAN-View 使用。\n\n"
                "是否尝试以「共享只读」方式接入？\n\n"
                "PCAN-Basic 支持在 PCAN-View 运行时读取同一通道，"
                "做法是初始化前开启波特率自适应，沿用 PCAN-View 已设好的波特率。\n"
                "前提：本程序的波特率必须与 PCAN-View 里的一致，且只能收不能发。")
            if not ok:
                return False
            cfg.bus_mode = "listen_only"
            cfg.bus_mode_forced = True
            self._update_summary()
            return True

        if cond == pc.COND_OCCUPIED:
            ok = messagebox.askyesno(
                "通道已被占用",
                f"{label} 已被其他程序占用，无法以正常模式打开。\n\n"
                "是否尝试以「只监听」方式连接？\n"
                "只监听模式下仅监控报文与解析 DBC，不能发送任何报文。\n\n"
                "注意：PCAN-Basic 不允许两个客户端共用同一通道。"
                "若占用者是另一个 PCAN-Basic 程序（例如 Testflow），"
                "只监听同样接不进去；若占用者是 PCAN-View，则可以用波特率自适应接入。")
            if not ok:
                return False
            cfg.bus_mode = "listen_only"
            cfg.bus_mode_forced = True
            self._update_summary()
            return True

        messagebox.showerror(
            "通道不可用",
            f"{label} 不存在或设备未连接。\n\n"
            "请检查 PCAN 设备是否已插入，并在「通道配置」中重新枚举。")
        return False

    def _connect(self):
        if not HAS_CAN:
            self._say("python-can 未安装")
            return

        # 已经连着就先把旧连接放掉，否则新连接一定撞上自己占用的通道
        if self.running:
            self._disconnect()

        if not self._precheck_channel():
            return

        try:
            src = self._make_source()
        except Exception as e:
            self._say(f"参数错误 · {e}")
            return

        try:
            src.open()
        except Exception as e:
            self._say(f"连接失败 · {e}")
            if "只读" not in str(e) and self.pcan_cfg is not None and self.pcan_cfg.bus_mode_forced:
                # 不能一律归咎「驱动拒绝共享」：位定时参数不被接受、
                # DLL 版本不符、通道名错误都会走到这里，归因错了反而把人带偏。
                low = str(e).lower()
                if "f_clock" in low or "timing" in low:
                    why = ("位定时参数未被接受。经典 CAN 的 PCAN 硬件时钟固定 "
                           "8 MHz，请调整采样点或改用标准波特率后重试。")
                elif ("0x04000000" in low or "occupied" in low
                      or "initialize" in low):
                    why = "该通道确实被其他程序占用，请在占用程序退出后重试。"
                else:
                    why = ("通道仍无法打开。若占用者是另一个 PCAN-Basic 程序"
                           "（例如 Testflow），PCAN-Basic 不支持共用；"
                           "请关闭它，或把本程序接到另一个空闲通道并连入同一个 CAN 网络。")
                messagebox.showerror("无法以只监听方式连接", f"{e}\n\n{why}")
            return

        ok_dbc, dbc_note = self._load_own_dbc(
            getattr(self.pcan_cfg, "dbc_path", "") if self.pcan_cfg else "")
        self.src = src
        self.running = True
        self.read_only = bool(self.pcan_cfg and self.pcan_cfg.bus_mode_forced)
        self.q = queue.Queue(maxsize=20000)
        self.rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self.rx_thread.start()
        self.btn_conn.configure(text="断开", fg_color=self.C["danger"],
                                hover_color="#8F3A33")
        tail = " · 只读" if self.read_only else ""
        if getattr(src, "share_note", None):
            tail += " · 共享接入"
        if self.dbc:
            tail += f" · DBC:{os.path.basename(self.dbc['path'])}"
        if not ok_dbc:
            self._say(f"已连接 · {src.label}{tail}　（DBC 未加载：{dbc_note}）")
        else:
            self._say(f"已连接 · {src.label}{tail}")

    def _update_summary(self):
        if self.pcan_cfg is not None:
            self.summary_lbl.configure(text="● " + self.pcan_cfg.summary())
        else:
            self.summary_lbl.configure(text="")

    def _open_send(self):
        fn = getattr(self, "open_send_fn", None)
        if fn is None:
            self._say("发送窗口不可用")
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="普通发送（原始帧）",
                         command=lambda: self._open_send_kind("raw"))
        menu.add_command(label="DBC 发送（信号）",
                         command=lambda: self._open_send_kind("dbc"))
        menu.add_separator()
        menu.add_command(label="数据回放（TRC / ASC / BLF）",
                         command=lambda: self._open_send_kind("replay"))
        menu.add_command(label="CAN UDS 诊断",
                         command=lambda: self._open_send_kind("uds"))
        try:
            x = self.btn_send_win.winfo_rootx()
            y = (self.btn_send_win.winfo_rooty()
                 + self.btn_send_win.winfo_height())
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _open_send_kind(self, kind):
        fn = getattr(self, "open_send_fn", None)
        if fn is None:
            return
        try:
            fn(kind)
        except Exception as e:
            self._say(f"打开发送窗口失败：{type(e).__name__}: {e}")

    def _open_config(self):
        if not HAS_PCAN_CFG:
            self._say("pcan_config 模块不可用")
            return
        old_sig = self._cfg_sig(self.pcan_cfg)
        dlg = CanConfigDialog(self, self.pcan_cfg, owner=self)
        self.wait_window(dlg)
        if dlg.result is None:
            return
        changed = self._cfg_sig(dlg.result) != old_sig
        self.pcan_cfg = dlg.result
        self._update_summary()
        if changed and self.running:
            # 不用手动断开再连：配置变了就直接重连
            self._say("配置已变更，正在重连…")
            self.after(100, self._reconnect)
        elif not self.running:
            # 还没连接：确认即连，省掉再点一次「连接」
            self._say("配置已确认，正在连接…")
            self.after(100, self._connect)
        else:
            self._say(f"配置已更新 · {self.pcan_cfg.summary()}")

    def _reconnect(self):
        self._disconnect()
        self.after(200, self._connect)

    # ---------------- 录制 ----------------

    def _toggle_record(self):
        if self.recorder is not None:
            try:
                self.recorder.stop()
            except Exception:
                pass
            self.recorder = None
            self.btn_rec.configure(text="录制", fg_color=self.C["neutral"])
            self._say("录制已停止")
            return

        if not HAS_CAN:
            return
        path = filedialog.asksaveasfilename(
            title="保存录制",
            defaultextension=".trc",
            filetypes=[("PCAN TRC", "*.trc"), ("Vector ASC", "*.asc"),
                       ("Binary BLF", "*.blf"), ("CSV", "*.csv")])
        if not path:
            return
        try:
            self.recorder = can.Logger(path)
        except Exception as e:
            self._say(f"无法开始录制 · {e}")
            return
        self.btn_rec.configure(text="停止录制", fg_color="#8A6A2A")
        self._say(f"录制中 · {os.path.basename(path)}")

    def _disconnect(self):
        # 通道一断，所有发送窗口必须停：不能留定时器在后台继续往总线上灌帧
        for attr in ("send_win", "raw_send_win", "replay_win", "uds_win"):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    w.on_live_disconnect()
                except Exception:
                    pass
        self.running = False
        t = self.rx_thread
        if t and t.is_alive():
            t.join(timeout=1.0)
        self.rx_thread = None
        if self.src is not None:
            try:
                self.src.close()
            except Exception:
                pass
            self.src = None
        self.btn_conn.configure(text="连接", fg_color=self.C["accent"],
                                hover_color="#2A6FA8")
        self._say("已断开")

    def _rx_loop(self):
        """只负责取帧入队，不碰任何控件"""
        while self.running:
            try:
                msg = self.src.recv(timeout=0.1)
            except Exception:
                break
            if msg is None:
                continue
            try:
                self.q.put_nowait(msg)
            except queue.Full:
                self.monitor.dropped += 1
            # 旁路分发：UDS 这类按 ID 过滤取帧的模块订阅这里。
            # 不走队列——队列是给实时页消费的，两边抢会丢帧。
            for fn in list(getattr(self, "tp_listeners", [])):
                try:
                    fn(msg)
                except Exception:
                    pass

    def _toggle_pause(self):
        self.paused = not self.paused
        self.btn_pause.configure(text="继续" if self.paused else "暂停",
                                 fg_color=self.C["accent"] if self.paused else self.C["neutral"])

    # ---------------- 模拟源 ----------------

    # ---------------- 独立 DBC ----------------

    @property
    def messages(self):
        """实时解析用的报文表：独立加载优先，否则沿用主界面"""
        if self.dbc:
            return self.dbc["messages"]
        return getattr(self.app, "messages", {}) or {}

    @property
    def val_tables(self):
        if self.dbc:
            return self.dbc["val_tables"]
        return getattr(self.app, "val_tables", {}) or {}

    def dbc_source_text(self):
        if self.dbc:
            return f"{os.path.basename(self.dbc['path'])}（独立）"
        return "主界面 DBC"

    def _load_own_dbc(self, path):
        """按通道配置里指定的 DBC 独立加载；path 为空则回到沿用主界面"""
        path = (path or "").strip()
        if not path:
            self.dbc = None
            return True, "沿用主界面 DBC"
        if self.dbc and self.dbc.get("path") == path:
            return True, f"沿用已加载的 {os.path.basename(path)}"
        if not os.path.isfile(path):
            self.dbc = None
            return False, f"DBC 不存在：{path}"
        if not self.parse_fn:
            return False, "本程序未提供 DBC 解析入口，无法独立加载"
        try:
            msgs, vtab, _cmts = self.parse_fn(path)
        except Exception as e:
            self.dbc = None
            return False, f"DBC 加载失败：{type(e).__name__}: {e}"
        self.dbc = {"messages": msgs, "val_tables": vtab, "path": path}
        return True, f"已独立加载 {os.path.basename(path)}（{len(msgs)} 条报文）"

    # ---------------- 清空 ----------------

    def _clear(self):
        self.monitor.reset()
        self.selected = None
        self.msg_tree.delete(*self.msg_tree.get_children())
        self.sig_tree.delete(*self.sig_tree.get_children())
        self.sig_hdr.configure(text="信号")
        self.sig_meta.configure(text="")
        self.count_lbl.configure(text="0 种")
        if getattr(self, "frame_tree", None) is not None:
            self.frame_tree.delete(*self.frame_tree.get_children())
        self._pending_frames = []
        self.frame_seq = 0
        if hasattr(self, "frame_count_lbl"):
            self.frame_count_lbl.configure(text="0 帧")
        self._say("已清空")

    # ---------------- 刷新 ----------------

    # ---------------- 逐帧视图 ----------------
    def _build_frame_panel(self, parent):
        c = self.C
        fb = ctk.CTkFrame(parent, fg_color="transparent")
        fb.grid(row=0, column=0, sticky="nsew")
        fb.grid_remove()
        fb.grid_rowconfigure(1, weight=1)
        fb.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(fb, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.frame_count_lbl = ctk.CTkLabel(
            head, text="0 帧", anchor="w",
            font=("Microsoft YaHei UI", 11), text_color=c["muted"])
        self.frame_count_lbl.pack(side="left")
        ctk.CTkLabel(head, text="按到达顺序，最多保留最近 800 帧；切到本视图后开始记录",
                     anchor="w", font=("Microsoft YaHei UI", 10),
                     text_color=c["muted"]).pack(side="left", padx=(10, 0))

        box = ctk.CTkFrame(fb, fg_color=c["card"], corner_radius=6)
        box.grid(row=1, column=0, sticky="nsew")
        cols = (("seq", "序号", 62, "e"), ("t", "时间(s)", 82, "e"),
                ("dir", "方向", 50, "center"), ("id", "ID(Hex)", 88, "w"),
                ("len", "长度", 50, "e"), ("data", "数据(Hex)", 400, "w"),
                ("ftype", "帧类型", 68, "w"), ("ffmt", "帧格式", 68, "w"),
                ("ctype", "CAN类型", 72, "w"), ("_f", "", 14, "w"))
        self.frame_tree = ttk.Treeview(box, show="headings",
                                       style="Live.Treeview",
                                       columns=[x[0] for x in cols])
        for key, text, w, anchor in cols:
            self.frame_tree.heading(key, text=text, anchor=anchor)
            self.frame_tree.column(key, width=w, minwidth=24, anchor=anchor,
                                   stretch=(key in ("_f", "data")))
        sv = ttk.Scrollbar(box, orient="vertical", command=self.frame_tree.yview,
                           style="Live.Vertical.TScrollbar")
        self.frame_tree.configure(yscrollcommand=sv.set)
        self.frame_tree.pack(side="left", fill="both", expand=True,
                             padx=(6, 0), pady=6)
        sv.pack(side="right", fill="y", pady=6, padx=(0, 6))
        self.frame_tree.tag_configure("alt", background=c["row_alt"])
        return fb

    def _switch_view(self, mode):
        if mode == getattr(self, "view_mode", "agg"):
            return
        c = self.C
        self.view_mode = mode
        if mode == "agg":
            self.frame_body.grid_remove()
            self.agg_body.grid()
            self.btn_agg.configure(fg_color=c["accent"], hover_color="#2A6FA8")
            self.btn_frames.configure(fg_color=c["neutral"], hover_color="#6B6B6B")
            self.view_hint.configure(text="")
        else:
            self.agg_body.grid_remove()
            self.frame_body.grid()
            self.btn_agg.configure(fg_color=c["neutral"], hover_color="#6B6B6B")
            self.btn_frames.configure(fg_color=c["accent"], hover_color="#2A6FA8")
            self.view_hint.configure(text="每帧一行，按到达顺序显示")
            self._refresh_frames(force=True)

    def _refresh_frames(self, force=False):
        tree = getattr(self, "frame_tree", None)
        if tree is None:
            return
        batch = self._pending_frames[:2000]
        # 剩下的留在 pending 里给下次 tick 继续消化，不能直接丢弃——
        # 一丢了这些帧就永远看不到了
        del self._pending_frames[:len(batch)]
        if not batch and not force:
            return
        t0 = getattr(self.monitor, "t0", 0) or 0
        for m in batch:
            self.frame_seq += 1
            ts = getattr(m, "timestamp", None)
            try:
                rel = float(ts) - t0 if ts else 0.0
            except (TypeError, ValueError):
                rel = 0.0
            data = getattr(m, "data", b"") or b""
            tree.insert(
                "", "end", iid=str(self.frame_seq),
                values=(self.frame_seq,
                        f"{rel:.3f}" if rel > 0 else "-",
                        "Tx" if getattr(m, "is_rx", True) is False else "Rx",
                        f"0x{m.arbitration_id:X}", len(data),
                        data.hex(" ").upper(),
                        "扩展帧" if getattr(m, "is_extended_id", False) else "标准帧",
                        "远程帧" if getattr(m, "is_remote_frame", False) else "数据帧",
                        "CANFD" if getattr(m, "is_fd", False) else "CAN"))
        kids = tree.get_children()
        over = len(kids) - 800
        if over > 0:
            tree.delete(*kids[:over])
            kids = tree.get_children()
        if kids:
            tree.see(kids[-1])
        try:
            self.frame_count_lbl.configure(text=f"{len(kids)} 帧")
        except Exception:
            pass

    def _drain(self):
        if self.paused:
            return
        n = 0
        while n < 5000:
            try:
                msg = self.q.get_nowait()
            except queue.Empty:
                break
            self.monitor.feed(msg)
            if self.view_mode == "frames":
                self._pending_frames.append(msg)
                if len(self._pending_frames) > 4000:
                    del self._pending_frames[:-4000]
            if self.recorder is not None:
                try:
                    self.recorder.on_message_received(msg)
                except Exception:
                    pass
            n += 1

    def _tick(self):
        self._drain()
        if self.view_mode == "frames":
            self._refresh_frames()
        else:
            self._refresh_msgs()
            if self.selected is not None:
                self._refresh_signals()
        self._update_status()
        self.after(100, self._tick)

    def _update_status(self):
        st = self.monitor
        bits = []
        if self.running and self.src is not None:
            bits.append(f"已连接 · {self.src.label}")
        else:
            bits.append("未连接")
        if self.paused:
            bits.append("已暂停")
        bits.append(f"{st.total} 帧")
        bits.append(f"{st.fps:.0f} fps")
        bits.append(f"{len(st.msgs)} 种报文")
        if st.dropped:
            bits.append(f"丢帧 {st.dropped}")
        self.status.configure(text="   ·   ".join(bits))

    def _visible(self, mid, rec):
        if not self.filter_text:
            return True
        f = self.filter_text.lower()
        name = self.messages.get(mid, {}).get("name", "").lower()
        return f in f"0x{mid:x}" or f in str(mid) or f in name

    def _row_values(self, mid, rec):
        m = self.messages.get(mid, {})
        name = m.get("name", "（DBC 中无此报文）")
        dlc = str(m.get("dlc", "-"))
        period = f"{rec['period']:.1f}" if rec["period"] is not None else "—"
        return (f"0x{mid:X}", name, dlc, period, rec["count"], rec["changed"], "")

    def _refresh_msgs(self):
        """增量更新，不重建整表，避免选中丢失和滚动跳动"""
        tree = self.msg_tree
        shown = 0
        for mid in sorted(self.monitor.msgs):
            rec = self.monitor.msgs[mid]
            iid = str(mid)
            if not self._visible(mid, rec):
                if tree.exists(iid):
                    tree.delete(iid)
                continue
            vals = self._row_values(mid, rec)
            if tree.exists(iid):
                cur = tree.item(iid, "values")
                if tuple(str(x) for x in cur) != tuple(str(x) for x in vals):
                    tree.item(iid, values=vals)
            else:
                tree.insert("", "end", iid=iid, values=vals,
                            tags=("alt",) if shown % 2 else ())
            shown += 1
        # 清掉已消失的 ID（iid 是整数字符串，monitor.msgs 的 key 是整数）
        for iid in tree.get_children():
            mid_i = int(iid)
            if mid_i not in self.monitor.msgs or not self._visible(mid_i, self.monitor.msgs[mid_i]):
                tree.delete(iid)
        self.count_lbl.configure(text=f"{shown} 种")

    def _rebuild_msgs(self):
        self.filter_text = self.filter_var.get().strip()
        self.msg_tree.delete(*self.msg_tree.get_children())
        self._refresh_msgs()

    def _on_select(self, _event=None):
        sel = self.msg_tree.selection()
        self.selected = int(sel[0]) if sel else None
        self.sig_tree.delete(*self.sig_tree.get_children())
        try:
            self.raw_bar.delete(0, "end")
        except Exception:
            pass

    def _refresh_signals(self):
        mid = self.selected
        rec = self.monitor.msgs.get(mid)
        if rec is None or rec["last_data"] is None:
            return
        msg = self.messages.get(mid)
        if msg is None:
            return
        data = rec["last_data"]
        self.sig_hdr.configure(text=msg["name"])
        self.sig_meta.configure(text=f"0x{mid:X} · {len(data)} B")
        # 整帧原始字节，完整显示不截断（CAN FD 64 字节也能看全）
        try:
            self.raw_bar.delete(0, "end")
            self.raw_bar.insert(0, " ".join(f"{b:02X}" for b in data))
        except Exception:
            pass

        tree = self.sig_tree
        vt_all = self.val_tables
        rows = self.decode_fn(data, msg)      # decode_message：已按 mux 过滤

        for idx, (sig, raw, phys) in enumerate(rows):
            iid = sig["name"]
            disp = int(round(phys)) if abs(phys - round(phys)) < 1e-9 else round(phys, 3)
            vt = vt_all.get((mid, sig["name"]))
            en = f" {vt[raw]}" if vt and raw in vt else ""
            vals = (sig["name"], raw, f"{disp}{en}", sig["unit"], "")
            if tree.exists(iid):
                cur = tree.item(iid, "values")
                if tuple(str(x) for x in cur) != tuple(str(x) for x in vals):
                    tree.item(iid, values=vals)
            else:
                tree.insert("", "end", iid=iid, values=vals,
                            tags=("alt",) if idx % 2 else ())

        # mux 值变化后，不再有效的信号行要移除
        keep = {s["name"] for s, _, _ in rows}
        for iid in tree.get_children():
            if iid not in keep:
                tree.delete(iid)

    def _say(self, text):
        self.status.configure(text=text)


# ============================ 发送页 ============================

class UdsWindow(ctk.CTkToplevel):
    """
    CAN UDS 诊断 —— 界面照图莫斯「CAN UDS 诊断」。

    左侧服务模板点一下就填进请求 PDU；右侧上方是单条发送区，中间是发送列表，
    下方是收发日志；底部给次数、循环与通过率统计。

    收发走 uds.UdsClient（ISO-TP），响应帧由实时页的接收线程旁路分发过来，
    不和实时页抢队列。通道复用实时页已打开的那一个。
    """

    def __init__(self, master, app, live_tab):
        super().__init__(master)
        self.app = app
        self.live = live_tab
        self.client = None
        self.session_mgr = None
        self.cur_service = None
        self.param_vars = {}
        self._busy = False
        self._log_file = None
        self.rows = []
        self.running = False
        self._idx = 0
        self.stats = {"total": 0, "pass": 0, "fail": 0, "unchecked": 0}
        self._edit = None
        self.C = _palette()

        self.title("CAN UDS 诊断")
        self.geometry("1120x720")
        self.minsize(940, 600)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build()
        self._subscribe()

    # ---------------- 小工具 ----------------
    def _label(self, master, text, size=11, muted=True, bold=False):
        return ctk.CTkLabel(
            master, text=text, anchor="w",
            font=("Microsoft YaHei UI", size, "bold" if bold else "normal"),
            text_color=self.C["muted"] if muted else self.C["text"])

    def _say(self, text, warn=False):
        try:
            self.status.configure(
                text=text, text_color="#C08A2A" if warn else self.C["muted"])
        except Exception:
            pass

    def _bus(self):
        src = getattr(self.live, "src", None)
        if src is None or getattr(src, "bus", None) is None:
            return None
        return src.bus

    def _addr(self, var, default=None):
        t = (var.get() or "").strip().replace("0x", "").replace("0X", "")
        if not t:
            return default
        try:
            return int(t, 16)
        except ValueError:
            return default

    def _timeout(self):
        try:
            return max(int(float(self.tmo_var.get())) / 1000.0, 0.05)
        except ValueError:
            return 1.0

    def _selected_addr(self):
        if self.addr_mode.get() == "功能地址":
            return self._addr(self.func_var, 0x7DF)
        return self._addr(self.phys_var, 0x704)

    def _rx_id(self):
        return self._addr(self.resp_var, 0x705)

    # ---------------- 构件 ----------------
    def _build(self):
        c = self.C

        top = ctk.CTkFrame(self, fg_color=c["card"], corner_radius=6)
        top.pack(fill="x", padx=10, pady=(10, 6))

        r = ctk.CTkFrame(top, fg_color="transparent")
        r.pack(fill="x", padx=12, pady=8)
        self._label(r, "物理地址", size=11).pack(side="left")
        self.phys_var = ctk.StringVar(value="704")
        ctk.CTkEntry(r, textvariable=self.phys_var, width=76, height=28,
                     font=("Consolas", 11)).pack(side="left", padx=(6, 14))

        self._label(r, "功能地址", size=11).pack(side="left")
        self.func_var = ctk.StringVar(value="7DF")
        ctk.CTkEntry(r, textvariable=self.func_var, width=76, height=28,
                     font=("Consolas", 11)).pack(side="left", padx=(6, 14))

        self._label(r, "响应地址", size=11).pack(side="left")
        self.resp_var = ctk.StringVar(value="705")
        ctk.CTkEntry(r, textvariable=self.resp_var, width=76, height=28,
                     font=("Consolas", 11)).pack(side="left", padx=(6, 14))

        self._label(r, "超时 ms", size=11).pack(side="left")
        self.tmo_var = ctk.StringVar(value="1000")
        ctk.CTkEntry(r, textvariable=self.tmo_var, width=70, height=28,
                     font=("Consolas", 11)).pack(side="left", padx=(6, 14))

        self.addr_mode = ctk.StringVar(value="物理地址")
        ctk.CTkOptionMenu(r, values=["物理地址", "功能地址"],
                          variable=self.addr_mode, width=104, height=28,
                          font=("Microsoft YaHei UI", 11)
                          ).pack(side="left", padx=(0, 12))

        ctk.CTkButton(r, text="复位布局", width=76, height=28,
                      fg_color=c["neutral"], hover_color="#6B6B6B",
                      font=("Microsoft YaHei UI", 11),
                      command=self._reset_layout).pack(side="right", padx=(0, 8))
        ctk.CTkButton(r, text="清空日志", width=80, height=28,
                      fg_color=c["neutral"], hover_color="#6B6B6B",
                      font=("Microsoft YaHei UI", 11),
                      command=self._clear_log).pack(side="right")
        ctk.CTkButton(r, text="导出日志", width=80, height=28,
                      fg_color=c["neutral"], hover_color="#6B6B6B",
                      font=("Microsoft YaHei UI", 11),
                      command=self._export_log).pack(side="right", padx=(0, 6))
        self.live_save_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(r, text="实时保存", variable=self.live_save_var,
                        font=("Microsoft YaHei UI", 11),
                        checkbox_width=18, checkbox_height=18,
                        command=self._toggle_live_save
                        ).pack(side="right", padx=(0, 10))

        # 会话 / 安全状态 + 保活
        st2 = ctk.CTkFrame(top, fg_color="transparent")
        st2.pack(fill="x", padx=12, pady=(0, 8))
        self.state_lbl = ctk.CTkLabel(
            st2, text="会话 — · 安全 未解锁", anchor="w",
            font=("Consolas", 11), text_color=c["muted"])
        self.state_lbl.pack(side="left")
        self.keep_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(st2, text="保活 3E（每 2 秒）", variable=self.keep_var,
                        font=("Microsoft YaHei UI", 11),
                        checkbox_width=18, checkbox_height=18,
                        command=self._toggle_keepalive).pack(side="right")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        # 左（服务列表）/ 右（请求与日志）可拖拽分栏
        self.h_paned = ttk.PanedWindow(body, orient="horizontal")
        self.h_paned.pack(fill="both", expand=True)

        # 左：服务模板
        left = ctk.CTkFrame(self.h_paned, fg_color=c["card"], corner_radius=6)
        self.h_paned.add(left, weight=0)
        self._label(left, "服务列表 · 点击填入请求 PDU", muted=False,
                    size=11, bold=True).pack(anchor="w", padx=10, pady=(8, 4))
        sc = ctk.CTkScrollableFrame(left, fg_color="transparent")
        sc.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        for gname, items in (uds.SERVICE_GROUPS if HAS_UDS else []):
            ctk.CTkLabel(sc, text=gname, anchor="w",
                         font=("Microsoft YaHei UI", 11, "bold"),
                         text_color=c["muted"]).pack(fill="x", pady=(6, 2))
            for name, tpl in items:
                try:
                    code = int(tpl.split()[0], 16)
                except (ValueError, IndexError):
                    continue
                ctk.CTkButton(sc, text="  " + name, height=26, anchor="w",
                              fg_color="transparent", hover_color=c["card_alt"],
                              text_color=c["text"],
                              font=("Microsoft YaHei UI", 11),
                              command=lambda c=code: self._show_service(c)
                              ).pack(fill="x", pady=1)

        # 右：发送区 + 列表 + 日志
        right = ctk.CTkFrame(self.h_paned, fg_color="transparent")
        self.h_paned.add(right, weight=1)

        # 服务参数区（选中左侧服务后动态重建）
        self.param_frame = ctk.CTkFrame(right, fg_color=c["card"],
                                        corner_radius=6)
        self.param_frame.pack(fill="x", pady=(0, 6))
        self.param_title = ctk.CTkLabel(
            self.param_frame, text="服务参数 · 点左侧服务切换", anchor="w",
            font=("Microsoft YaHei UI", 12, "bold"), text_color=c["text"])
        self.param_title.pack(anchor="w", padx=12, pady=(8, 2))
        self.param_body = ctk.CTkFrame(self.param_frame, fg_color="transparent")
        self.param_body.pack(fill="x", padx=12, pady=(0, 2))
        self.param_btns = ctk.CTkFrame(self.param_frame, fg_color="transparent")
        self.param_btns.pack(fill="x", padx=12, pady=(0, 8))

        req = ctk.CTkFrame(right, fg_color=c["card"], corner_radius=6)
        req.pack(fill="x", pady=(0, 6))
        rr = ctk.CTkFrame(req, fg_color="transparent")
        rr.pack(fill="x", padx=12, pady=(10, 4))
        self._label(rr, "请求 PDU", size=11).pack(side="left")
        self.req_var = ctk.StringVar(value="10 03")
        ctk.CTkEntry(rr, textvariable=self.req_var, height=30,
                     font=("Consolas", 12)).pack(side="left", padx=(8, 12),
                                                  fill="x", expand=True)
        self.check_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(rr, text="校验响应", variable=self.check_var,
                        font=("Microsoft YaHei UI", 11),
                        checkbox_width=18, checkbox_height=18
                        ).pack(side="left", padx=(0, 12))
        self.btn_send = ctk.CTkButton(
            rr, text="立即发送", width=96, height=30,
            fg_color="#1668DC", hover_color="#0E4FA8",
            font=("Microsoft YaHei UI", 12, "bold"),
            command=self._send_now)
        self.btn_send.pack(side="left")

        rr2 = ctk.CTkFrame(req, fg_color="transparent")
        rr2.pack(fill="x", padx=12, pady=(0, 4))
        self._label(rr2, "响应 PDU", size=11).pack(side="left")
        self.resp_lbl = ctk.CTkLabel(rr2, text="—", anchor="w",
                                     font=("Consolas", 12),
                                     text_color=c["text"])
        self.resp_lbl.pack(side="left", padx=(8, 0))

        rr3 = ctk.CTkFrame(req, fg_color="transparent")
        rr3.pack(fill="x", padx=12, pady=(0, 10))
        ctk.CTkButton(rr3, text="添加到列表", width=96, height=28,
                      fg_color=c["neutral"], hover_color="#6B6B6B",
                      font=("Microsoft YaHei UI", 11),
                      command=self._add_row).pack(side="left")
        ctk.CTkButton(rr3, text="移除", width=60, height=28,
                      fg_color=c["neutral"], hover_color="#6B6B6B",
                      font=("Microsoft YaHei UI", 11),
                      command=self._remove_row).pack(side="left", padx=(8, 0))

        lc = ctk.CTkFrame(right, fg_color=c["card"], corner_radius=6)
        lc.pack(fill="x", pady=(0, 6))
        hd = ctk.CTkFrame(lc, fg_color="transparent")
        hd.pack(fill="x", padx=10, pady=(8, 4))
        self._label(hd, "发送列表", muted=False, size=11, bold=True).pack(side="left")
        cols = (("on", "选择", 42, "center"), ("name", "名称", 150, "w"),
                ("pdu", "请求 PDU", 220, "w"), ("addr", "地址", 70, "w"),
                ("gap", "延时ms", 62, "e"), ("res", "结果", 70, "w"),
                ("_f", "", 12, "w"))
        box = ctk.CTkFrame(lc, fg_color="transparent")
        box.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        self.tree = ttk.Treeview(box, show="headings", style="Live.Treeview",
                                 columns=[x[0] for x in cols], height=6)
        for key, text, w, anchor in cols:
            self.tree.heading(key, text=text, anchor=anchor)
            self.tree.column(key, width=w, minwidth=22, anchor=anchor,
                             stretch=(key == "_f"))
        sv = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview,
                           style="Live.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=sv.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sv.pack(side="right", fill="y")
        self.tree.tag_configure("alt", background=c["row_alt"])
        self.tree.bind("<Double-1>", self._on_tree_dbl)

        lg = ctk.CTkFrame(right, fg_color=c["card"], corner_radius=6)
        lg.pack(fill="both", expand=True)
        self._label(lg, "收发日志", muted=False, size=11,
                    bold=True).pack(anchor="w", padx=10, pady=(8, 4))
        self.log_text = tk.Text(lg, height=8, font=("Consolas", 10),
                                wrap="none", relief="flat", borderwidth=0)
        self.log_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log_text.configure(state="disabled")

        # 底
        foot = ctk.CTkFrame(self, fg_color=c["card"], corner_radius=6)
        foot.pack(fill="x", padx=10, pady=(0, 10))
        fr = ctk.CTkFrame(foot, fg_color="transparent")
        fr.pack(fill="x", padx=12, pady=8)
        self._label(fr, "发送次数", size=11).pack(side="left")
        self.times_var = ctk.StringVar(value="1")
        ctk.CTkEntry(fr, textvariable=self.times_var, width=58, height=28,
                     font=("Consolas", 11)).pack(side="left", padx=(6, 12))
        self.loop_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(fr, text="循环发送", variable=self.loop_var,
                        font=("Microsoft YaHei UI", 11),
                        checkbox_width=18, checkbox_height=18
                        ).pack(side="left", padx=(0, 12))
        self.btn_list = ctk.CTkButton(
            fr, text="列表发送", width=96, height=30,
            fg_color="#1668DC", hover_color="#0E4FA8",
            font=("Microsoft YaHei UI", 12, "bold"),
            command=self._toggle_list_send)
        self.btn_list.pack(side="left", padx=(0, 16))

        self.stat_lbl = ctk.CTkLabel(fr, text="", anchor="w",
                                     font=("Consolas", 11),
                                     text_color=c["muted"])
        self.stat_lbl.pack(side="left")
        ctk.CTkButton(fr, text="重置统计", width=76, height=28,
                      fg_color=c["neutral"], hover_color="#6B6B6B",
                      font=("Microsoft YaHei UI", 11),
                      command=self._reset_stats).pack(side="right")

        st = ctk.CTkFrame(self, fg_color="transparent")
        st.pack(fill="x", padx=12, pady=(0, 8))
        self.status = ctk.CTkLabel(st, text="", anchor="w",
                                   font=("Microsoft YaHei UI", 11),
                                   text_color=c["muted"])
        self.status.pack(side="left")
        self._update_stats()

        self._paneds = [(self.h_paned, "h")]
        self._paned_defaults = [300]
        bind_paned_save(self, "uds", self._paneds)
        apply_paned_layout(self, "uds", self._paneds, self._paned_defaults)

    def _reset_layout(self):
        """恢复默认分栏与默认窗口尺寸，并清掉本窗口的存档"""
        try:
            reset_paned_layout(self, "uds", self._paneds,
                               self._paned_defaults, "1120x720")
            self._say("布局已恢复默认")
        except Exception:
            pass

    # ---------------- 订阅接收 ----------------
    def _subscribe(self):
        # 注意：不能叫 _register —— tkinter 的 Misc._register 是内部回调注册，
        # 同名会把它覆盖掉，导致 protocol()/bind() 全线报参数错误。
        lst = getattr(self.live, "tp_listeners", None)
        if lst is not None and self._on_frame not in lst:
            lst.append(self._on_frame)

    def _unsubscribe(self):
        lst = getattr(self.live, "tp_listeners", None)
        if lst is not None and self._on_frame in lst:
            try:
                lst.remove(self._on_frame)
            except ValueError:
                pass

    def _on_frame(self, msg):
        # 只交给 ISO-TP 处理接收帧；自己发出去的帧不算应答
        if getattr(msg, "is_rx", True) is False:
            return
        c = self.client
        if c is not None:
            try:
                c.on_frame(msg)
            except Exception:
                pass

    # ---------------- 请求 ----------------
    def _ensure_client(self):
        if not HAS_UDS:
            raise RuntimeError("uds 模块不可用")
        if self.client is None:
            def send_fn(aid, data):
                bus = self._bus()
                if bus is None:
                    raise RuntimeError("通道未连接")
                cfg = getattr(self.live, "pcan_cfg", None)
                fd = bool(getattr(cfg, "is_fd", False))
                bus.send(can.Message(arbitration_id=aid, data=data,
                                     is_extended_id=False, is_rx=False,
                                     is_fd=fd and len(data) > 8))
            self.client = uds.UdsClient(send_fn, 0x704, 0x705, 1.0)
            self.session_mgr = uds.UdsSession(self.client)
        self._sync_transport()
        return self.client

    def _sync_transport(self):
        """帧长跟着通道走：CAN FD 允许单帧 62 / 连续帧 63 字节"""
        cfg = getattr(self.live, "pcan_cfg", None)
        fd = bool(getattr(cfg, "is_fd", False))
        try:
            self.client.set_frame_len(64 if fd else 8)
            self.client.set_functional(self.addr_mode.get() == "功能地址")
        except Exception:
            pass
        return fd

    # ---------------- 服务参数 ----------------
    @staticmethod
    def _service_name(code):
        for n, t in (uds.SERVICE_TEMPLATES if HAS_UDS else []):
            if t.upper().startswith(f"{code:02X}"):
                return n
        return f"服务 0x{code:02X}"

    def _show_service(self, code):
        if not HAS_UDS:
            return
        self.cur_service = code
        for w in self.param_body.winfo_children():
            w.destroy()
        for w in self.param_btns.winfo_children():
            w.destroy()
        self.param_vars = {}
        self.param_title.configure(
            text=f"服务参数 · {self._service_name(code)}")

        spec = uds.SERVICE_UI.get(code) or []
        if not spec:
            self._label(self.param_body,
                        "该服务没有参数表单，直接编辑下方请求 PDU",
                        size=11).grid(row=0, column=0, sticky="w")
        # 长字段独占一行，短字段一行放两个——0x36 的数据可能上千字节，
        # 挤在同一行里会把窗口撑破
        wide_keys = ("data", "path", "alg", "did", "comm", "mask", "rec")
        self.param_body.grid_columnconfigure(1, weight=1)
        self.param_body.grid_columnconfigure(4, weight=1)
        row, col = 0, 0
        for key, label, typ, options, default in spec:
            is_wide = key in wide_keys
            if is_wide and col:
                row, col = row + 1, 0
            self._label(self.param_body, label, size=11).grid(
                row=row, column=col, sticky="w", padx=(0, 4), pady=2)
            var = ctk.StringVar(value=default)
            if typ == "select":
                w = ctk.CTkOptionMenu(self.param_body, values=options,
                                      variable=var, width=140, height=28,
                                      font=("Microsoft YaHei UI", 11))
            else:
                w = ctk.CTkEntry(
                    self.param_body, textvariable=var, height=28,
                    width=(420 if is_wide else 110),
                    font=("Consolas", 11))
            if is_wide:
                w.grid(row=row, column=1, columnspan=4, sticky="we",
                       padx=(0, 8), pady=2)
                row, col = row + 1, 0
            else:
                w.grid(row=row, column=col + 1, sticky="w",
                       padx=(0, 14), pady=2)
                col += 2
                if col >= 4:
                    row, col = row + 1, 0
            self.param_vars[key] = var

        ctk.CTkButton(self.param_btns, text="执行", width=88, height=30,
                      fg_color="#1668DC", hover_color="#0E4FA8",
                      font=("Microsoft YaHei UI", 12, "bold"),
                      command=lambda: self._execute(code)).pack(side="left")
        ctk.CTkButton(self.param_btns, text="生成 PDU", width=88, height=30,
                      fg_color=self.C["neutral"], hover_color="#6B6B6B",
                      font=("Microsoft YaHei UI", 11),
                      command=lambda: self._fill_pdu(code)
                      ).pack(side="left", padx=(8, 0))
        if code == 0x27:
            self._label(self.param_btns,
                        "算法里可用 seed / bytes，返回值须是 key 的字节",
                        size=10).pack(side="left", padx=(12, 0))
        if code == 0x36:
            ctk.CTkButton(self.param_btns, text="载入文件…", width=88, height=30,
                          fg_color=self.C["neutral"], hover_color="#6B6B6B",
                          font=("Microsoft YaHei UI", 11),
                          command=self._load_payload
                          ).pack(side="left", padx=(8, 0))
            self._label(self.param_btns,
                        "载入后「数据」留空，点执行即按 ECU 块长自动分块",
                        size=10).pack(side="left", padx=(12, 0))
        self._say(f"已选择 {self._service_name(code)}")

    def _pick_hex(self, key):
        var = self.param_vars.get(key)
        if var is None:
            return None
        t = (var.get() or "").strip()
        if not t:
            return None
        t = t.split()[0]
        try:
            return int(t, 16)
        except ValueError:
            return None

    def _params_to_pdu(self, code):
        p = self.param_vars

        def hv(key):
            v = self._pick_hex(key)
            return 0 if v is None else v

        if code == 0x10:
            return bytes([0x10, hv("mode") & 0xFF])
        if code == 0x11:
            return bytes([0x11, hv("kind") & 0xFF])
        if code == 0x14:
            g = hv("group")
            return bytes([0x14, (g >> 16) & 0xFF, (g >> 8) & 0xFF, g & 0xFF])
        if code == 0x19:
            sub = hv("sub") & 0xFF
            mask = hv("mask")
            rec = uds.parse_hex(p["rec"].get()) if "rec" in p else b""
            if sub in (0x01, 0x02):
                return bytes([0x19, sub, mask & 0xFF])
            if sub in (0x04, 0x06):
                # 19 04/06 <DTC 3字节> <记录号>
                if len(rec) < 3:
                    raise ValueError("19 04/06 需要 3 字节 DTC + 记录号")
                return bytes([0x19, sub]) + rec
            if sub in (0x03,):
                return bytes([0x19, sub, hv("rec") & 0xFF])
            if sub in (0x0B, 0x0C, 0x0D, 0x0E):
                return bytes([0x19, sub, mask & 0xFF])
            return bytes([0x19, sub])
        if code == 0x22:
            raws = (p["did"].get() if "did" in p else "") or ""
            dids = [int(x, 16) for x in raws.split() if x.strip()]
            if not dids:
                return None
            out = bytes([0x22])
            for d in dids:
                out += bytes([(d >> 8) & 0xFF, d & 0xFF])
            return out
        if code == 0x29:
            data = uds.parse_hex(p["data"].get()) if "data" in p else b""
            return bytes([0x29, hv("sub") & 0xFF]) + data
        if code == 0x28:
            comm = uds.parse_hex(p["comm"].get()) if "comm" in p else b"\x01"
            return bytes([0x28, hv("ctrl") & 0xFF]) + comm
        if code == 0x2E:
            d = hv("did")
            data = uds.parse_hex(p["data"].get()) if "data" in p else b""
            return bytes([0x2E, (d >> 8) & 0xFF, d & 0xFF]) + data
        if code == 0x31:
            r = hv("rid")
            raw = p["data"].get() if "data" in p else ""
            data = uds.parse_hex(raw) if (raw or "").strip() else b""
            return bytes([0x31, hv("kind") & 0xFF,
                          (r >> 8) & 0xFF, r & 0xFF]) + data
        if code == 0x3E:
            return bytes([0x3E, hv("sub") & 0xFF])
        if code == 0x85:
            return bytes([0x85, hv("kind") & 0xFF])
        if code == 0x23:
            addr = uds.parse_hex(p["addr"].get()) if "addr" in p else b""
            size = uds.parse_hex(p["size"].get()) if "size" in p else b""
            return bytes([0x23, hv("alfid") & 0xFF]) + addr + size
        if code == 0x24:
            d = hv("did")
            return bytes([0x24, (d >> 8) & 0xFF, d & 0xFF])
        if code == 0x2A:
            per = uds.parse_hex(p["period"].get()) if "period" in p else b""
            d = hv("did")
            return bytes([0x2A]) + per + bytes([(d >> 8) & 0xFF, d & 0xFF])
        if code == 0x2C:
            d = hv("did")
            per = uds.parse_hex(p["period"].get()) if "period" in p else b""
            return bytes([0x2C, hv("ctrl") & 0xFF, (d >> 8) & 0xFF,
                          d & 0xFF, hv("sub") & 0xFF]) + per
        if code == 0x2F:
            d = hv("did")
            return bytes([0x2F, (d >> 8) & 0xFF, d & 0xFF,
                          hv("opt") & 0xFF, hv("val") & 0xFF])
        if code == 0x34:
            addr = uds.parse_hex(p["addr"].get()) if "addr" in p else b""
            size = uds.parse_hex(p["size"].get()) if "size" in p else b""
            return (bytes([0x34, hv("dfi") & 0xFF, hv("alfid") & 0xFF])
                    + addr + size)
        if code == 0x35:
            addr = uds.parse_hex(p["addr"].get()) if "addr" in p else b""
            size = uds.parse_hex(p["size"].get()) if "size" in p else b""
            return (bytes([0x35, hv("dfi") & 0xFF, hv("alfid") & 0xFF])
                    + addr + size)
        if code == 0x36:
            raw = p["data"].get() if "data" in p else ""
            data = uds.parse_hex(raw) if (raw or "").strip() else b""
            bsc = self._pick_hex("bsc")
            if bsc is None:
                bsc = self.session_mgr.bsc if self.session_mgr else 0
                bsc = (bsc % 0xFF) + 1
            return bytes([0x36, bsc & 0xFF]) + data
        if code == 0x38:
            mode = hv("mode") & 0xFF
            plen = uds.parse_hex(p["pathlen"].get()) if "pathlen" in p else b"\x00"
            if len(plen) < 2:
                plen = bytes([plen[0] if plen else 0, 0])
            path = (p["path"].get() if "path" in p else "") or ""
            pb = path.encode("utf-8", "ignore")
            out = bytes([0x38, mode]) + plen[:1] + bytes([hv("dfi") & 0xFF])
            size = uds.parse_hex(p["size"].get()) if "size" in p else b""
            out += size + pb
            if mode in (0x01, 0x03):
                raw = p["data"].get() if "data" in p else ""
                out += uds.parse_hex(raw) if (raw or "").strip() else b""
            return out
        if code == 0x37:
            return bytes([0x37])
        if code == 0x3D:
            addr = uds.parse_hex(p["addr"].get()) if "addr" in p else b""
            size = uds.parse_hex(p["size"].get()) if "size" in p else b""
            data = uds.parse_hex(p["data"].get()) if "data" in p else b""
            return (bytes([0x3D, hv("alfid") & 0xFF])
                    + addr + size + data)
        if code == 0x83:
            return bytes([0x83, hv("sub") & 0xFF, hv("param") & 0xFF])
        if code == 0x84:
            return bytes([0x84, hv("sub") & 0xFF])
        if code == 0x86:
            return bytes([0x86, hv("sub") & 0xFF, hv("etype") & 0xFF])
        if code == 0x87:
            return bytes([0x87, hv("sub") & 0xFF])
        return None

    def _fill_pdu(self, code):
        try:
            pdu = self._params_to_pdu(code)
        except Exception as e:
            self._say(f"构造失败：{e}", warn=True)
            return
        if not pdu:
            self._say("该服务需要两步（如 27），请直接点「执行」", warn=True)
            return
        self.req_var.set(uds.hex_str(pdu))
        self._say(f"已生成 PDU：{uds.hex_str(pdu)}")

    def _execute(self, code):
        if self._bus() is None:
            self._say("通道未连接：请先到「⚡ 实时」页连接", warn=True)
            return
        try:
            cli = self._ensure_client()
        except Exception as e:
            self._say(f"初始化失败：{e}", warn=True)
            return
        cli.set_ids(self._selected_addr(), self._rx_id())
        cli.set_timeout(self._timeout())
        self._busy = True
        try:
            if code == 0x10:
                res = self.session_mgr.diag_session(
                    self._pick_hex("mode") or 0x03)
                self._update_state()
                self._say(f"{res['status'].upper()} · {res['note']}",
                          warn=(res["status"] == "fail"))
                return
            if code == 0x27:
                alg = (self.param_vars["alg"].get()
                       if "alg" in self.param_vars else "")
                level = self._pick_hex("level") or 0x01
                out = self.session_mgr.security_access(level, alg)
                self._log(out["seed_req"], self._selected_addr())
                if out["key_req"] is not None:
                    self._log(out["key_req"], self._selected_addr())
                self.stats["total"] += 1
                key = out["status"] if out["status"] in (
                    "pass", "fail", "unchecked") else "fail"
                self.stats[key] += 1
                self._update_stats()
                self._update_state()
                self._say(f"{key.upper()} · {out['note']}",
                          warn=(key == "fail"))
                return
            if code == 0x34 or code == 0x35:
                addr = uds.parse_hex(self.param_vars["addr"].get()) \
                    if "addr" in self.param_vars else b""
                size = uds.parse_hex(self.param_vars["size"].get()) \
                    if "size" in self.param_vars else b""
                dfi = self._pick_hex("dfi") or 0
                alf = self._pick_hex("alfid") or 0x44
                fn = (self.session_mgr.request_download if code == 0x34
                      else self.session_mgr.request_upload)
                res = fn(dfi, alf, addr, size)
                self._update_state()
                self._do_log_result(res, self._selected_addr())
                return
            if code == 0x36:
                self._exec_transfer_data()
                return
            if code == 0x37:
                res = self.session_mgr.transfer_exit()
                self.session_mgr.max_block = None
                self.session_mgr.bsc = 0
                self._update_state()
                self._do_log_result(res, self._selected_addr())
                return
            pdu = self._params_to_pdu(code)
            if not pdu:
                self._say("无法构造 PDU", warn=True)
                return
            self._do_request(pdu, self._selected_addr(), self.check_var.get())
        finally:
            self._busy = False

    # ---------------- 状态与保活 ----------------
    def _update_state(self):
        try:
            self.state_lbl.configure(text=self.session_mgr.state_text())
        except Exception:
            pass

    def _toggle_keepalive(self):
        if self.keep_var.get():
            self._say("保活已开启（每 2 秒发 3E 00）")
            self._keepalive_tick()
        else:
            self._say("保活已关闭")

    def _keepalive_tick(self):
        if not self.keep_var.get():
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        if self._bus() is not None and not self._busy:
            try:
                cli = self._ensure_client()
                cli.set_ids(self._selected_addr(), self._rx_id())
                cli.set_timeout(0.5)
                self.session_mgr.tester_present(suppress=True)
            except Exception:
                pass
        self.after(2000, self._keepalive_tick)

    def _send_now(self):
        try:
            pdu = uds.parse_hex(self.req_var.get())
        except ValueError as e:
            self._say(f"请求 PDU 格式错误：{e}", warn=True)
            return
        if not pdu:
            self._say("请求 PDU 为空", warn=True)
            return
        self._do_request(pdu, self._selected_addr(), self.check_var.get())

    def _do_log_result(self, res, tx_id):
        """把一条结果记进日志与统计（供不走 _do_request 的分支复用）"""
        self.resp_lbl.configure(text=(uds.hex_str(res["resp"])
                                      if res["resp"] else "（无响应）"))
        self.stats["total"] += 1
        key = res["status"] if res["status"] in ("pass", "fail", "unchecked") else "fail"
        self.stats[key] += 1
        self._update_stats()
        self._log(res, tx_id)
        if res["resp"]:
            dtcs = uds.parse_dtc(res["resp"])
            if dtcs:
                self._log_dtc(dtcs)
        self._update_state()
        self._say(f"{key.upper()} · {res['note']}", warn=(key == "fail"))
        return res

    def _load_payload(self):
        """给 0x36 载入整包数据（.bin/.hex/.s19 等），由程序按块长分块"""
        path = filedialog.askopenfilename(
            parent=self, title="选择要传输的数据文件",
            filetypes=[("刷写/数据文件", "*.bin *.hex *.s19 *.srec *.mot"),
                       ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception as e:
            self._say(f"读取失败：{e}", warn=True)
            return
        if not data:
            self._say("文件是空的", warn=True)
            return
        self.session_mgr.set_pending(data)
        # 载入文件后清空「数据」框，让执行时走 pending 分支
        if "data" in self.param_vars:
            self.param_vars["data"].set("")
        self._say(f"已载入 {os.path.basename(path)} · {len(data)} 字节，"
                  f"点「执行」开始传输")

    def _exec_transfer_data(self):
        """0x36：有数据就发（超块长自动切分），没数据就从待传缓冲取"""
        raw = self.param_vars["data"].get() if "data" in self.param_vars else ""
        data = uds.parse_hex(raw) if (raw or "").strip() else b""
        bsc = self._pick_hex("bsc")
        mgr = self.session_mgr

        if not data and not mgr.pending_len():
            self._say("没有待传数据：在「数据」里粘贴整包，或先执行 34 后载入文件",
                      warn=True)
            return

        if data:
            # 手动模式：超过 ECU 给的块长就自动切块，块序号连续
            blk = mgr.max_block or 0
            chunks = ([data[i:i + blk] for i in range(0, len(data), blk)]
                      if blk and len(data) > blk else [data])
        else:
            blk = max(mgr.max_block or mgr.pending_len(), 1)
            chunks = []
            while mgr.pending_len():
                chunks.append(mgr.take_pending(blk))

        total = len(chunks)
        last = None
        for i, ch in enumerate(chunks):
            res = mgr.transfer_data(
                ch, bsc=(bsc if (bsc is not None and i == 0) else None))
            last = res
            if res["status"] != "pass":
                self._do_log_result(res, self._selected_addr())
                self._say(f"第 {i + 1}/{total} 块失败，已停止", warn=True)
                return
            if total > 3 and (i + 1) % 20 == 0:
                self._say(f"已传 {i + 1}/{total} 块…")
        self._do_log_result(last, self._selected_addr())
        if total > 1:
            self._append_log(f"    （本次共发送 {total} 块，"
                             f"每块 {len(chunks[0])} 字节）\n")
        self._update_state()

    def _do_request(self, pdu, tx_id, check):
        if self._bus() is None:
            self._say("通道未连接：请先到「⚡ 实时」页连接", warn=True)
            return None
        try:
            cli = self._ensure_client()
        except Exception as e:
            self._say(f"初始化失败：{e}", warn=True)
            return None
        cli.set_ids(tx_id, self._rx_id())
        cli.set_timeout(self._timeout())

        functional = (self.addr_mode.get() == "功能地址")
        if functional:
            cli.set_functional(True)
            results = cli.request_multi(pdu, window=min(self._timeout(), 1.0),
                                        check_response=check)
            for res in results:
                rx = res.get("rx_id")
                self._log(res, tx_id, rx_id=rx)
                self.stats["total"] += 1
                key = (res["status"] if res["status"] in
                       ("pass", "fail", "unchecked") else "fail")
                self.stats[key] += 1
            self._update_stats()
            self._update_state()
            ok = sum(1 for r in results if r["status"] == "pass")
            self._say(f"功能寻址：{len(results)} 个 ECU 应答，通过 {ok}",
                      warn=(ok == 0))
            if results:
                self.resp_lbl.configure(
                    text=uds.hex_str(results[0]["resp"]) if results[0]["resp"] else "（无响应）")
            return results

        cli.set_functional(False)
        res = cli.request(pdu, check_response=check)
        self.resp_lbl.configure(text=(uds.hex_str(res["resp"])
                                      if res["resp"] else "（无响应）"))
        self.stats["total"] += 1
        key = res["status"] if res["status"] in ("pass", "fail", "unchecked") else "fail"
        self.stats[key] += 1
        self._update_stats()
        self._log(res, tx_id)
        if HAS_UDS and res["resp"]:
            dtcs = uds.parse_dtc(res["resp"])
            if dtcs:
                self._log_dtc(dtcs)
            cnt = uds.parse_dtc_count(res["resp"])
            if cnt is not None:
                self._append_log(f"    DTC 数量：{cnt}\n")
            if len(pdu) > 3 and pdu[0] == 0x22:
                dids = uds.parse_did(res["resp"])
                if dids:
                    for did, data in dids:
                        self._append_log(f"    DID 0x{did:04X} = {uds.hex_str(data)}\n")
        self._update_state()
        self._say(f"{key.upper()} · {res['note']}", warn=(key == "fail"))
        return res

    def _log(self, res, tx_id, rx_id=None):
        arrow_req = f"→ [{tx_id:03X}] {uds.hex_str(res['req'])}"
        rid = self._rx_id() if rx_id is None else rx_id
        arrow_resp = (f"← [{rid:03X}] {uds.hex_str(res['resp'])}"
                      if res["resp"] else "← （无响应）")
        line = (f"[{time.strftime('%H:%M:%S')}] {arrow_req}\n"
                f"    {arrow_resp}\n"
                f"    {res['status'].upper()} · {res['note']}"
                f" · {res['elapsed'] * 1000:.0f} ms\n")
        self._append_log(line)

    def _append_log(self, text):
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", text)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except Exception:
            pass
        f = getattr(self, "_log_file", None)
        if f is not None:
            try:
                f.write(text)
                f.flush()
            except Exception:
                pass

    def _log_dtc(self, dtcs):
        lines = [f"    DTC 共 {len(dtcs)} 条："]
        for d in dtcs:
            bits = "/".join(d["bits"]) if d["bits"] else "-"
            lines.append(f"      {d['code']}  status=0x{d['status']:02X}  {bits}")
        self._append_log("\n".join(lines) + "\n")

    def _export_log(self):
        txt = self.log_text.get("1.0", "end").strip()
        if not txt:
            self._say("日志为空", warn=True)
            return
        name = f"UDS日志_{time.strftime('%Y%m%d_%H%M')}.txt"
        path = _ask_save(self, name)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(txt)
        except Exception as e:
            self._say(f"导出失败：{e}", warn=True)
            return
        self._say(f"已导出 → {os.path.basename(path)}")

    def _toggle_live_save(self):
        if not self.live_save_var.get():
            f = getattr(self, "_log_file", None)
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass
            self._log_file = None
            self._say("实时保存已关闭")
            return
        name = f"UDS实时日志_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        path = _ask_save(self, name)
        if not path:
            self.live_save_var.set(False)
            return
        try:
            self._log_file = open(path, "a", encoding="utf-8")
        except Exception as e:
            self.live_save_var.set(False)
            self._say(f"无法写入：{e}", warn=True)
            return
        self._say(f"实时保存 → {os.path.basename(path)}")

    def _clear_log(self):
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
        except Exception:
            pass

    def _update_stats(self):
        s = self.stats
        try:
            self.stat_lbl.configure(
                text=(f"测试 {s['total']}  ·  通过 {s['pass']}"
                      f"  ·  未通过 {s['fail']}  ·  未检查 {s['unchecked']}"))
        except Exception:
            pass

    def _reset_stats(self):
        self.stats = {"total": 0, "pass": 0, "fail": 0, "unchecked": 0}
        self._update_stats()
        self._say("统计已重置")

    # ---------------- 列表 ----------------
    def _add_row(self):
        try:
            pdu = uds.parse_hex(self.req_var.get())
        except ValueError as e:
            self._say(f"请求 PDU 格式错误：{e}", warn=True)
            return
        if not pdu:
            self._say("请求 PDU 为空", warn=True)
            return
        name = uds.SERVICE_TEMPLATES[0][0]
        sid = pdu[0]
        for n, _t in (uds.SERVICE_TEMPLATES if HAS_UDS else []):
            if n.upper().startswith(f"{sid:02X}"):
                name = n
                break
        self.rows.append({
            "pdu": uds.hex_str(pdu), "name": name,
            "tx": self._selected_addr(), "check": bool(self.check_var.get()),
            "gap": 50, "enabled": True, "result": ""})
        self._rebuild()
        self._say("已加入发送列表")

    def _remove_row(self):
        sel = self.tree.selection()
        if not sel:
            self._say("先选一行", warn=True)
            return
        for iid in sorted((int(x) for x in sel), reverse=True):
            if 0 <= iid < len(self.rows):
                self.rows.pop(iid)
        self._rebuild()

    def _row_values(self, r):
        return ("☑" if r["enabled"] else "☐", r["name"], r["pdu"],
                f"{r['tx']:03X}", r["gap"], r["result"] or "-")

    def _rebuild(self):
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.rows):
            self.tree.insert("", "end", iid=str(i), values=self._row_values(r),
                             tags=("alt",) if i % 2 else ())

    def _refresh_row(self, idx):
        if 0 <= idx < len(self.rows) and self.tree.exists(str(idx)):
            self.tree.item(str(idx), values=self._row_values(self.rows[idx]))

    def _on_tree_dbl(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        col = self.tree.identify_column(event.x)
        idx = int(iid)
        r = self.rows[idx]
        if col == "#1":
            r["enabled"] = not r["enabled"]
            self._refresh_row(idx)
        elif col == "#5":
            self._inline(iid, "gap", idx)

    def _inline(self, iid, col_key, idx):
        if self._edit is not None:
            try:
                self._edit.destroy()
            except Exception:
                pass
            self._edit = None
        cols = list(self.tree["columns"])
        bbox = self.tree.bbox(iid, f"#{cols.index(col_key) + 1}")
        if not bbox:
            return
        x, y, w, h = bbox
        e = tk.Entry(self.tree, justify="center", font=("Consolas", 11),
                     relief="solid", borderwidth=1)
        e.place(x=x, y=y, width=max(w, 56), height=h)
        e.insert(0, self.tree.set(iid, col_key))
        e.select_range(0, "end")
        e.focus_set()
        self._edit = e

        def commit(_e=None):
            txt = e.get()
            try:
                e.destroy()
            except Exception:
                pass
            self._edit = None
            try:
                self.rows[idx]["gap"] = max(int(float(txt)), 0)
            except (TypeError, ValueError):
                self._say("延时必须是数字", warn=True)
                return
            self._refresh_row(idx)

        e.bind("<Return>", commit)
        e.bind("<FocusOut>", commit)
        e.bind("<Escape>", lambda _e: e.destroy())

    # ---------------- 列表发送 ----------------
    def _toggle_list_send(self):
        if self.running:
            self.running = False
            self.btn_list.configure(text="列表发送", fg_color="#1668DC",
                                    hover_color="#0E4FA8")
            self._say("已停止")
            return
        if not [r for r in self.rows if r["enabled"]]:
            self._say("列表里没有勾选的行", warn=True)
            return
        self.running = True
        self._idx = 0
        self.btn_list.configure(text="停止", fg_color="#C0392B",
                                hover_color="#A93226")
        self._say("列表发送中…")
        self._tick_list()

    def _tick_list(self):
        if not self.running:
            return
        if self._idx >= len(self.rows):
            self.running = False
            self.btn_list.configure(text="列表发送", fg_color="#1668DC",
                                    hover_color="#0E4FA8")
            self._say("列表发送完成")
            if self.loop_var.get():
                self._idx = 0
                self.running = True
                self.btn_list.configure(text="停止", fg_color="#C0392B")
                self.after(50, self._tick_list)
            return
        r = self.rows[self._idx]
        idx = self._idx
        self._idx += 1
        if not r["enabled"]:
            self.after(1, self._tick_list)
            return
        try:
            pdu = uds.parse_hex(r["pdu"])
        except ValueError:
            r["result"] = "格式错"
            self._refresh_row(idx)
            self.after(1, self._tick_list)
            return
        res = self._do_request(pdu, r["tx"], r["check"])
        r["result"] = res["status"] if res else "fail"
        self._refresh_row(idx)
        self.after(max(int(r.get("gap", 0)), 1), self._tick_list)

    # ---------------- 收尾 ----------------
    def on_live_disconnect(self):
        try:
            if self.winfo_exists():
                self.running = False
                self._say("通道已断开", warn=True)
        except Exception:
            pass

    def _on_close(self):
        self.running = False
        self.keep_var.set(False)
        try:
            if getattr(self, "_log_file", None) is not None:
                self._log_file.close()
                self._log_file = None
        except Exception:
            pass
        self._unsubscribe()
        try:
            save_paned_layout(self, "uds", self._paneds)
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


class ReplayWindow(ctk.CTkToplevel):
    """
    数据回放 —— 读取录制文件（TRC / ASC / BLF / CSV），按原始时间戳发回总线。

    解析交给 python-can 自带的 LogReader，按扩展名自动分派，不自己造轮子。
    支持速度倍率、循环回放、暂停/继续；通道复用「实时」页已打开的那一个。
    """

    SPEEDS = ["0.5x", "1x", "2x", "5x", "10x", "最快"]

    def __init__(self, master, app, live_tab):
        super().__init__(master)
        self.app = app
        self.live = live_tab
        self.frames = []          # [(相对时间秒, Message)]
        self.path = None
        self.idx = 0
        self.playing = False
        self.paused = False
        self._t_base = 0.0
        self._t_off = 0.0
        self._sent = 0
        self.C = _palette()

        self.title("数据回放")
        self.geometry("760x430")
        self.minsize(660, 400)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build()

    # ---------------- 小工具 ----------------
    def _label(self, master, text, size=11, muted=True, bold=False):
        return ctk.CTkLabel(
            master, text=text, anchor="w",
            font=("Microsoft YaHei UI", size, "bold" if bold else "normal"),
            text_color=self.C["muted"] if muted else self.C["text"])

    def _say(self, text, warn=False):
        try:
            self.status.configure(
                text=text, text_color="#C08A2A" if warn else self.C["muted"])
        except Exception:
            pass

    def _bus(self):
        src = getattr(self.live, "src", None)
        if src is None or getattr(src, "bus", None) is None:
            return None
        return src.bus

    @staticmethod
    def _fmt_size(path):
        try:
            s = os.path.getsize(path)
        except OSError:
            return ""
        for unit in ("B", "KB", "MB", "GB"):
            if s < 1024:
                return f"{s:.0f} {unit}"
            s /= 1024.0
        return f"{s:.1f} TB"

    # ---------------- 构件 ----------------
    def _build(self):
        c = self.C
        root = ctk.CTkFrame(self, fg_color=c["card"], corner_radius=6)
        root.pack(fill="both", expand=True, padx=12, pady=12)

        r1 = ctk.CTkFrame(root, fg_color="transparent")
        r1.pack(fill="x", padx=12, pady=(12, 4))
        self._label(r1, "回放文件", size=11).pack(side="left")
        self.file_lbl = self._label(r1, "未选择", size=11, muted=False, bold=True)
        self.file_lbl.pack(side="left", padx=(8, 12))
        ctk.CTkButton(r1, text="浏览…", width=80, height=30,
                      fg_color=c["neutral"], hover_color="#6B6B6B",
                      font=("Microsoft YaHei UI", 11),
                      command=self._browse).pack(side="left")

        self.info_lbl = self._label(root, "—", size=11)
        self.info_lbl.pack(anchor="w", padx=12, pady=(0, 8))

        r2 = ctk.CTkFrame(root, fg_color="transparent")
        r2.pack(fill="x", padx=12, pady=4)
        self._label(r2, "进度", size=11).pack(side="left")
        self.prog = ctk.CTkProgressBar(r2, height=16)
        self.prog.pack(side="left", padx=(8, 8), fill="x", expand=True)
        self.prog.set(0)
        self.prog_lbl = self._label(r2, "0%", size=11)
        self.prog_lbl.pack(side="left", padx=(0, 12))
        self.pos_lbl = self._label(r2, "0 / 0", size=11)
        self.pos_lbl.pack(side="left")

        r3 = ctk.CTkFrame(root, fg_color="transparent")
        r3.pack(fill="x", padx=12, pady=(8, 4))
        self._label(r3, "回放速度", size=11).pack(side="left")
        self.speed_var = ctk.StringVar(value="1x")
        ctk.CTkOptionMenu(r3, values=self.SPEEDS, variable=self.speed_var,
                          width=92, height=30, font=("Consolas", 11),
                          command=lambda _v: self._on_speed()
                          ).pack(side="left", padx=(8, 16))

        self.loop_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(r3, text="循环回放", variable=self.loop_var,
                        font=("Microsoft YaHei UI", 11),
                        checkbox_width=18, checkbox_height=18
                        ).pack(side="left", padx=(0, 16))

        self.btn_stop = ctk.CTkButton(r3, text="停止", width=76, height=32,
                                      fg_color=c["neutral"], hover_color="#6B6B6B",
                                      font=("Microsoft YaHei UI", 11),
                                      command=lambda: self._stop("已停止"))
        self.btn_stop.pack(side="right", padx=(8, 0))
        self.btn_play = ctk.CTkButton(
            r3, text="开始", width=96, height=32,
            fg_color="#1668DC", hover_color="#0E4FA8",
            font=("Microsoft YaHei UI", 12, "bold"),
            command=self._toggle)
        self.btn_play.pack(side="right")

        r4 = ctk.CTkFrame(root, fg_color="transparent")
        r4.pack(fill="x", padx=12, pady=(10, 12))
        self.status = ctk.CTkLabel(r4, text="选择录制文件后点「开始」", anchor="w",
                                   font=("Microsoft YaHei UI", 11),
                                   text_color=c["muted"])
        self.status.pack(side="left")

    # ---------------- 载入 ----------------
    def _browse(self):
        p = filedialog.askopenfilename(
            parent=self, title="选择回放文件",
            filetypes=[("CAN 录制", "*.trc *.asc *.blf *.csv *.log"),
                       ("TRC (Vector)", "*.trc"), ("ASC (Vector)", "*.asc"),
                       ("BLF (Vector)", "*.blf"), ("所有文件", "*.*")])
        if p:
            self.load(p)

    def load(self, path):
        self._stop()
        try:
            reader = can.LogReader(path)
            msgs = list(reader)
        except Exception as e:
            self._say(f"读取失败：{type(e).__name__}: {e}", warn=True)
            return False
        if not msgs:
            self._say("文件里没有可回放的帧", warn=True)
            return False
        t0 = msgs[0].timestamp or 0.0
        frames = []
        for m in msgs:
            t = (m.timestamp or t0) - t0
            frames.append((max(float(t), 0.0), m))
        self.frames = frames
        self.path = path
        self.idx = 0
        self._t_off = 0.0
        self._t_base = 0.0
        dur = frames[-1][0]
        self.file_lbl.configure(text=os.path.basename(path))
        self.info_lbl.configure(
            text=f"{len(frames)} 帧 · 时长 {dur:.3f} s · {self._fmt_size(path)}")
        self._update_progress()
        self._say(f"已载入 {len(frames)} 帧，点「开始」回放")
        return True

    # ---------------- 回放 ----------------
    def _speed(self):
        v = self.speed_var.get()
        if v == "最快":
            return 0.0
        try:
            return float(v.rstrip("x"))
        except ValueError:
            return 1.0

    def _on_speed(self):
        # 变速时把已播放时长折算成新的基准，避免跳帧
        if self.playing and not self.paused:
            self._t_off = self._elapsed()
            self._t_base = time.time()

    def _elapsed(self):
        if self.paused:
            return self._t_off
        sp = self._speed()
        if sp == 0.0:
            return self._t_off
        return self._t_off + (time.time() - self._t_base) * sp

    def _ready(self):
        if getattr(self.live, "read_only", False):
            self._say("当前是只读（只监听）连接，不能回放", warn=True)
            return False
        if self._bus() is None:
            self._say("通道未连接：请先到「⚡ 实时」页连接", warn=True)
            return False
        return True

    def _toggle(self):
        if self.playing and not self.paused:
            self._pause()
        elif self.playing and self.paused:
            self._resume()
        else:
            self._start()

    def _start(self):
        if not self.frames:
            self._say("先选择回放文件", warn=True)
            return
        if not self._ready():
            return
        self.idx = 0
        self._t_off = 0.0
        self._t_base = time.time()
        self.playing = True
        self.paused = False
        self.btn_play.configure(text="暂停", fg_color="#C08A2A",
                                hover_color="#A87520")
        self._say("回放中…")
        self._update_progress()
        self._tick()

    def _pause(self):
        if not self.playing:
            return
        self._t_off = self._elapsed()
        self.paused = True
        self.btn_play.configure(text="继续", fg_color="#1668DC",
                                hover_color="#0E4FA8")
        self._say(f"已暂停 @ {self.idx} 帧")

    def _resume(self):
        if not self.playing:
            return
        self._t_base = time.time()
        self.paused = False
        self.btn_play.configure(text="暂停", fg_color="#C08A2A",
                                hover_color="#A87520")
        self._say("回放中…")
        self._tick()

    def _stop(self, reason=None):
        was = self.playing
        self.playing = False
        self.paused = False
        try:
            self.btn_play.configure(text="开始", fg_color="#1668DC",
                                    hover_color="#0E4FA8")
        except Exception:
            pass
        if was:
            self._say(reason or "已停止", warn=bool(reason))

    def _tick(self):
        if not self.playing or self.paused:
            return
        if self._bus() is None:
            self._stop("通道已断开，回放中止")
            return
        sp = self._speed()
        try:
            if sp == 0.0:
                n = 0
                while self.idx < len(self.frames) and n < 200:
                    self._emit(self.frames[self.idx][1])
                    self.idx += 1
                    n += 1
            else:
                el = self._elapsed()
                while (self.idx < len(self.frames)
                       and self.frames[self.idx][0] <= el):
                    self._emit(self.frames[self.idx][1])
                    self.idx += 1
        except Exception as e:
            self._stop(f"回放中止：{e}")
            return
        self._update_progress()
        if self.idx >= len(self.frames):
            if self.loop_var.get():
                self.idx = 0
                self._t_off = 0.0
                self._t_base = time.time()
            else:
                self._stop("回放完成")
                return
        self.after(5, self._tick)

    def _emit(self, m):
        bus = self._bus()
        if bus is None:
            raise RuntimeError("通道已断开")
        bus.send(can.Message(
            arbitration_id=m.arbitration_id, data=m.data,
            is_extended_id=m.is_extended_id,
            is_remote_frame=m.is_remote_frame,
            is_fd=m.is_fd, is_rx=False))
        self._sent += 1

    def _update_progress(self):
        n = len(self.frames)
        p = (self.idx / n) if n else 0.0
        try:
            self.prog.set(min(p, 1.0))
            self.prog_lbl.configure(text=f"{p * 100:.0f}%")
            self.pos_lbl.configure(text=f"{self.idx} / {n}")
        except Exception:
            pass

    # ---------------- 收尾 ----------------
    def on_live_disconnect(self):
        try:
            if self.winfo_exists():
                self._stop("通道已断开，回放中止")
        except Exception:
            pass

    def _on_close(self):
        self._stop()
        try:
            self.destroy()
        except Exception:
            pass


class RawSendWindow(ctk.CTkToplevel):
    """
    普通发送 —— 原始帧，不依赖 DBC。界面照图莫斯 TCANLINPro「普通发送」。

    上半：设备通道 / 帧类型 / 帧格式 / ID / 数据长度 / 数据(Hex) /
          ID 递增 / 数据递增 / 发送次数 / 每次发送帧数 / 每次间隔时间
          + 发送进度条与耗时
    下半：列表发送（选择 / ID / 数据长度 / 数据 / 帧类型 / 帧格式 / 延时 / 次数 / 状态 / 备注）
    底部：发送模式（顺序/并行）· 失败处理 · 发送次数 · 循环发送 · 列表发送

    通道复用「实时」页已打开的那一个。
    """

    def __init__(self, master, app, live_tab):
        super().__init__(master)
        self.app = app
        self.live = live_tab
        self.rows = []
        self.running = False
        self.running_list = False
        self._rounds = 1
        self._frames_per = 1
        self._interval = 0
        self._done = 0
        self._total = 0
        self._t0 = 0.0
        self._seq_idx = 0
        self._seq_due = 0.0
        self._edit = None
        self.C = _palette()

        self.title("普通发送")
        self.geometry("1180x680")
        self.minsize(980, 560)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build()
        self._sync_chan()

    # ---------------- 小工具 ----------------
    def _label(self, master, text, size=11, muted=True, bold=False):
        return ctk.CTkLabel(
            master, text=text, anchor="w",
            font=("Microsoft YaHei UI", size, "bold" if bold else "normal"),
            text_color=self.C["muted"] if muted else self.C["text"])

    def _say(self, text, warn=False):
        try:
            self.status.configure(
                text=text, text_color="#C08A2A" if warn else self.C["muted"])
        except Exception:
            pass

    def _bus(self):
        src = getattr(self.live, "src", None)
        if src is None or getattr(src, "bus", None) is None:
            return None
        return src.bus

    def _sync_chan(self):
        try:
            cfg = getattr(self.live, "pcan_cfg", None)
            if cfg is None:
                self.chan_lbl.configure(text="未配置")
                return
            rd = " · 只读" if getattr(self.live, "read_only", False) else ""
            self.chan_lbl.configure(text=cfg.summary() + rd)
        except Exception:
            pass

    # ---- 数据解析 ----
    @staticmethod
    def _fmt_hex(data):
        return " ".join(f"{b:02X}" for b in data)

    @staticmethod
    def _parse_hex(txt):
        t = (txt or "").replace(",", " ").replace("0x", "").replace("0X", "")
        t = "".join(t.split())
        if not t:
            return b""
        if len(t) % 2:
            t = "0" + t
        try:
            return bytes.fromhex(t)
        except ValueError:
            raise ValueError("不是合法的十六进制")

    def _parse_id(self):
        t = (self.id_var.get() or "").strip().replace("0x", "").replace("0X", "")
        t = t.replace(" ", "")
        if not t:
            raise ValueError("ID 不能为空")
        try:
            v = int(t, 16)
        except ValueError:
            raise ValueError("ID 必须是十六进制")
        if self.ftype_var.get() == "扩展帧":
            if not 0 <= v <= 0x1FFFFFFF:
                raise ValueError("扩展帧 ID 范围 0 ~ 0x1FFFFFFF")
        else:
            if not 0 <= v <= 0x7FF:
                raise ValueError("标准帧 ID 范围 0 ~ 0x7FF")
        return v

    # ---------------- 构件 ----------------
    def _build(self):
        c = self.C

        # 参数区与列表区分成可拖拽的上下两栏
        vp = ctk.CTkFrame(self, fg_color="transparent")
        vp.pack(fill="both", expand=True, padx=10, pady=(10, 6))
        self.v_paned = ttk.PanedWindow(vp, orient="vertical")
        self.v_paned.pack(fill="both", expand=True)

        top = ctk.CTkFrame(self.v_paned, fg_color=c["card"], corner_radius=6)
        self.v_paned.add(top, weight=0)
        th = ctk.CTkFrame(top, fg_color="transparent")
        th.pack(fill="x", padx=12, pady=(8, 2))
        self._label(th, "普通发送", size=12, muted=False, bold=True).pack(side="left")
        ctk.CTkButton(th, text="复位布局", width=76, height=24,
                      fg_color=c["neutral"], hover_color="#6B6B6B",
                      font=("Microsoft YaHei UI", 10),
                      command=self._reset_layout).pack(side="right")

        # 行 1
        r1 = ctk.CTkFrame(top, fg_color="transparent")
        r1.pack(fill="x", padx=12, pady=3)
        self._label(r1, "设备通道", size=11).pack(side="left")
        self.chan_lbl = self._label(r1, "—", size=11, muted=False, bold=True)
        self.chan_lbl.pack(side="left", padx=(6, 16))

        self._label(r1, "帧类型", size=11).pack(side="left")
        self.ftype_var = ctk.StringVar(value="标准帧")
        ctk.CTkOptionMenu(r1, values=["标准帧", "扩展帧"], variable=self.ftype_var,
                          width=92, height=28, font=("Microsoft YaHei UI", 11)
                          ).pack(side="left", padx=(6, 16))

        self._label(r1, "帧格式", size=11).pack(side="left")
        self.ffmt_var = ctk.StringVar(value="数据帧")
        ctk.CTkOptionMenu(r1, values=["数据帧", "远程帧"], variable=self.ffmt_var,
                          width=92, height=28, font=("Microsoft YaHei UI", 11)
                          ).pack(side="left", padx=(6, 16))

        self._label(r1, "ID(Hex)", size=11).pack(side="left")
        self.id_var = ctk.StringVar(value="0x100")
        ctk.CTkEntry(r1, textvariable=self.id_var, width=104, height=28,
                     font=("Consolas", 11)).pack(side="left", padx=(6, 16))

        self.id_inc_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(r1, text="ID递增", variable=self.id_inc_var,
                        font=("Microsoft YaHei UI", 11),
                        checkbox_width=18, checkbox_height=18
                        ).pack(side="left", padx=(0, 16))

        self._label(r1, "周期(ms)", size=11).pack(side="left")
        self.cycle_var = ctk.StringVar(value="0")
        ctk.CTkEntry(r1, textvariable=self.cycle_var, width=62, height=28,
                     font=("Consolas", 11)).pack(side="left", padx=(6, 0))

        # 行 2
        r2 = ctk.CTkFrame(top, fg_color="transparent")
        r2.pack(fill="x", padx=12, pady=3)
        self._label(r2, "数据长度", size=11).pack(side="left")
        self.len_var = ctk.StringVar(value="8")
        ctk.CTkOptionMenu(r2, values=[str(i) for i in range(1, 65)],
                          variable=self.len_var, width=64, height=28,
                          font=("Consolas", 11),
                          command=lambda _v: self._on_len()
                          ).pack(side="left", padx=(6, 16))

        self._label(r2, "数据(Hex)", size=11).pack(side="left")
        self.data_var = ctk.StringVar(value="00 11 22 33 44 55 66 77")
        ctk.CTkEntry(r2, textvariable=self.data_var, height=28,
                     font=("Consolas", 11)).pack(side="left", padx=(6, 8),
                                                 fill="x", expand=True)
        ctk.CTkButton(r2, text="编辑数据", width=76, height=28,
                      fg_color=c["neutral"], hover_color="#6B6B6B",
                      font=("Microsoft YaHei UI", 11),
                      command=self._edit_data).pack(side="left", padx=(0, 12))

        self.data_inc_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(r2, text="数据递增", variable=self.data_inc_var,
                        font=("Microsoft YaHei UI", 11),
                        checkbox_width=18, checkbox_height=18).pack(side="left")

        # 行 3
        r3 = ctk.CTkFrame(top, fg_color="transparent")
        r3.pack(fill="x", padx=12, pady=3)
        self._label(r3, "发送次数", size=11).pack(side="left")
        self.count_var = ctk.StringVar(value="1")
        ctk.CTkEntry(r3, textvariable=self.count_var, width=62, height=28,
                     font=("Consolas", 11)).pack(side="left", padx=(6, 16))

        self._label(r3, "每次发送帧数", size=11).pack(side="left")
        self.frames_var = ctk.StringVar(value="1")
        ctk.CTkEntry(r3, textvariable=self.frames_var, width=62, height=28,
                     font=("Consolas", 11)).pack(side="left", padx=(6, 16))

        self._label(r3, "每次间隔时间(ms)", size=11).pack(side="left")
        self.interval_var = ctk.StringVar(value="0")
        ctk.CTkEntry(r3, textvariable=self.interval_var, width=62, height=28,
                     font=("Consolas", 11)).pack(side="left", padx=(6, 0))

        # 行 4
        r4 = ctk.CTkFrame(top, fg_color="transparent")
        r4.pack(fill="x", padx=12, pady=(4, 10))
        self._label(r4, "发送进度", size=11).pack(side="left")
        self.prog = ctk.CTkProgressBar(r4, height=16)
        self.prog.pack(side="left", padx=(6, 8), fill="x", expand=True)
        self.prog.set(0)
        self.prog_lbl = self._label(r4, "0%", size=11)
        self.prog_lbl.pack(side="left", padx=(0, 16))
        self.cost_lbl = self._label(r4, "发送耗时(秒): 0", size=11)
        self.cost_lbl.pack(side="left", padx=(0, 16))

        ctk.CTkButton(r4, text="添加到列表", width=96, height=30,
                      fg_color=c["neutral"], hover_color="#6B6B6B",
                      font=("Microsoft YaHei UI", 11),
                      command=self._add_current).pack(side="right", padx=(8, 0))
        self.btn_send = ctk.CTkButton(
            r4, text="发送", width=96, height=30,
            fg_color="#1668DC", hover_color="#0E4FA8",
            font=("Microsoft YaHei UI", 12, "bold"),
            command=self._toggle_send)
        self.btn_send.pack(side="right")

        # ---- 列表发送 ----
        mid = ctk.CTkFrame(self.v_paned, fg_color=c["card"], corner_radius=6)
        self.v_paned.add(mid, weight=1)
        lh = ctk.CTkFrame(mid, fg_color="transparent")
        lh.pack(fill="x", padx=12, pady=(8, 4))
        self._label(lh, "列表发送", size=12, muted=False, bold=True).pack(side="left")
        self._label(lh, "双击「延时」「次数」「选择」可改", size=10).pack(
            side="left", padx=(8, 0))
        ctk.CTkButton(lh, text="移除", width=52, height=24,
                      fg_color=c["neutral"], hover_color="#6B6B6B",
                      font=("Microsoft YaHei UI", 10),
                      command=self._remove_row).pack(side="right", padx=(6, 0))
        self.btn_listops = ctk.CTkButton(
            lh, text="列表操作 ▾", width=88, height=24,
            fg_color=c["neutral"], hover_color="#6B6B6B",
            font=("Microsoft YaHei UI", 10),
            command=self._list_menu)
        self.btn_listops.pack(side="right")

        tc = ctk.CTkFrame(mid, fg_color="transparent")
        tc.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        cols = (("on", "选择", 44, "center"), ("id", "ID(Hex)", 84, "w"),
                ("ln", "数据长度", 66, "e"), ("data", "数据(Hex)", 300, "w"),
                ("ft", "帧类型", 66, "w"), ("ff", "帧格式", 66, "w"),
                ("gap", "延时(ms)", 66, "e"), ("times", "发送次数", 70, "e"),
                ("st", "状态", 60, "w"), ("note", "备注", 110, "w"),
                ("_f", "", 14, "w"))
        self.tree = ttk.Treeview(tc, show="headings", style="Live.Treeview",
                                 columns=[x[0] for x in cols])
        for key, text, w, anchor in cols:
            self.tree.heading(key, text=text, anchor=anchor)
            self.tree.column(key, width=w, minwidth=24, anchor=anchor,
                             stretch=(key in ("_f", "data", "note")))
        sv = ttk.Scrollbar(tc, orient="vertical", command=self.tree.yview,
                           style="Live.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=sv.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sv.pack(side="right", fill="y")
        self.tree.tag_configure("alt", background=c["row_alt"])
        self.tree.bind("<Double-1>", self._on_tree_dbl)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_sel)

        # ---- 底部 ----
        foot = ctk.CTkFrame(self, fg_color=c["card"], corner_radius=6)
        foot.pack(fill="x", padx=10, pady=(0, 10))
        self._label(foot, "发送模式", size=11).pack(side="left", padx=(12, 4), pady=8)
        self.lmode_var = ctk.StringVar(value="顺序发送")
        ctk.CTkOptionMenu(foot, values=["顺序发送", "并行发送"],
                          variable=self.lmode_var, width=100, height=28,
                          font=("Microsoft YaHei UI", 11)
                          ).pack(side="left", padx=(0, 12))

        self._label(foot, "失败处理", size=11).pack(side="left")
        self.fail_var = ctk.StringVar(value="失败终止")
        ctk.CTkOptionMenu(foot, values=["失败终止", "失败继续"],
                          variable=self.fail_var, width=100, height=28,
                          font=("Microsoft YaHei UI", 11)
                          ).pack(side="left", padx=(6, 12))

        self._label(foot, "发送次数", size=11).pack(side="left")
        self.ltimes_var = ctk.StringVar(value="1")
        ctk.CTkEntry(foot, textvariable=self.ltimes_var, width=58, height=28,
                     font=("Consolas", 11)).pack(side="left", padx=(6, 10))

        self.lloop_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(foot, text="循环发送", variable=self.lloop_var,
                        font=("Microsoft YaHei UI", 11),
                        checkbox_width=18, checkbox_height=18
                        ).pack(side="left", padx=(0, 12))

        self.btn_list = ctk.CTkButton(
            foot, text="列表发送", width=104, height=32,
            fg_color="#1668DC", hover_color="#0E4FA8",
            font=("Microsoft YaHei UI", 12, "bold"),
            command=self._toggle_list_send)
        self.btn_list.pack(side="right", padx=(8, 12), pady=8)

        self.status = ctk.CTkLabel(foot, text="", anchor="w",
                                   font=("Microsoft YaHei UI", 11),
                                   text_color=c["muted"])
        self.status.pack(side="left", padx=(12, 10))

        self._paneds = [(self.v_paned, "v")]
        self._paned_defaults = [330]
        bind_paned_save(self, "raw", self._paneds)
        apply_paned_layout(self, "raw", self._paneds, self._paned_defaults)

    def _reset_layout(self):
        """恢复默认分栏与默认窗口尺寸，并清掉本窗口的存档"""
        try:
            reset_paned_layout(self, "raw", self._paneds,
                               self._paned_defaults, "1180x680")
            self._say("布局已恢复默认")
        except Exception:
            pass

    # ---------------- 数据长度 ----------------
    def _on_len(self):
        try:
            n = int(self.len_var.get())
        except ValueError:
            return
        try:
            cur = self._parse_hex(self.data_var.get())
        except ValueError:
            cur = b""
        cur = (cur + bytes(max(n - len(cur), 0)))[:n]
        self.data_var.set(self._fmt_hex(cur))

    def _edit_data(self):
        top = ctk.CTkToplevel(self)
        top.title("编辑数据")
        top.geometry("460x320")
        top.transient(self)
        present_window(top, self)
        txt = tk.Text(top, font=("Consolas", 12), wrap="char",
                      undo=True, relief="solid", borderwidth=1)
        txt.pack(fill="both", expand=True, padx=10, pady=10)
        txt.insert("1.0", self.data_var.get())

        def ok():
            self.data_var.set(txt.get("1.0", "end").strip())
            top.destroy()

        bar = ctk.CTkFrame(top, fg_color="transparent")
        bar.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkButton(bar, text="确定", width=80, height=30,
                      command=ok).pack(side="right")
        ctk.CTkButton(bar, text="取消", width=80, height=30,
                      fg_color="#8A8A8A", hover_color="#6B6B6B",
                      command=top.destroy).pack(side="right", padx=(0, 8))
        txt.focus_set()

    # ---------------- 单帧发送 ----------------
    def _ready(self):
        if getattr(self.live, "read_only", False):
            self._say("当前是只读（只监听）连接，不能发送", warn=True)
            return False
        if self._bus() is None:
            self._say("通道未连接：请先到「⚡ 实时」页连接", warn=True)
            return False
        return True

    def _build_frame(self, offset=0):
        mid = self._parse_id()
        data = self._parse_hex(self.data_var.get())
        if self.ffmt_var.get() == "远程帧":
            data = b""
        ext = self.ftype_var.get() == "扩展帧"
        if self.id_inc_var.get() and offset:
            mid = (mid + offset) & (0x1FFFFFFF if ext else 0x7FF)
        if self.data_inc_var.get() and offset and data:
            n = int.from_bytes(data, "big")
            n = (n + offset) & ((1 << (8 * len(data))) - 1)
            data = n.to_bytes(len(data), "big")
        return can.Message(
            arbitration_id=mid, data=data,
            is_extended_id=ext,
            is_remote_frame=(self.ffmt_var.get() == "远程帧"),
            is_fd=(len(data) > 8), is_rx=False)

    def _send_frame(self, msg):
        bus = self._bus()
        if bus is None:
            raise RuntimeError("通道已断开")
        bus.send(msg)

    def _toggle_send(self):
        if self.running:
            self._stop()
            return
        if not self._ready():
            return
        try:
            self._parse_id()
            self._parse_hex(self.data_var.get())
            self._rounds = max(int(float(self.count_var.get())), 1)
            self._frames_per = max(int(float(self.frames_var.get())), 1)
            self._interval = max(int(float(self.interval_var.get())), 0)
        except ValueError as e:
            self._say(f"参数错误：{e}", warn=True)
            return
        self._done = 0
        self._total = self._rounds * self._frames_per
        self._t0 = time.time()
        self.running = True
        self.btn_send.configure(text="停止", fg_color="#C0392B",
                                hover_color="#A93226")
        self._say("发送中…")
        self._tick_send()

    def _tick_send(self):
        if not self.running:
            return
        if self._bus() is None:
            self._stop("通道已断开，发送中止")
            return
        try:
            self._send_frame(self._build_frame(self._done))
        except Exception as e:
            self._stop(f"发送失败：{e}")
            return
        self._done += 1
        pct = self._done / self._total if self._total else 1.0
        try:
            self.prog.set(min(pct, 1.0))
            self.prog_lbl.configure(text=f"{pct * 100:.0f}%")
            self.cost_lbl.configure(
                text=f"发送耗时(秒): {time.time() - self._t0:.1f}")
        except Exception:
            pass
        if self._done >= self._total:
            self._stop("发送完成")
            return
        self.after(max(self._interval, 1), self._tick_send)

    def _stop(self, reason=None):
        was = self.running or self.running_list
        self.running = False
        self.running_list = False
        try:
            self.btn_send.configure(text="发送", fg_color="#1668DC",
                                    hover_color="#0E4FA8")
            self.btn_list.configure(text="列表发送", fg_color="#1668DC",
                                    hover_color="#0E4FA8")
        except Exception:
            pass
        if was:
            self._say(reason or "已停止", warn=bool(reason))

    # ---------------- 列表 ----------------
    def _add_current(self):
        try:
            mid = self._parse_id()
            data = self._parse_hex(self.data_var.get())
        except ValueError as e:
            self._say(f"参数错误：{e}", warn=True)
            return
        try:
            gap = max(int(float(self.cycle_var.get())), 1)
        except ValueError:
            gap = 10
        self.rows.append({
            "id": mid, "data": data,
            "ext": self.ftype_var.get() == "扩展帧",
            "rtr": self.ffmt_var.get() == "远程帧",
            "gap": gap, "times": 1, "enabled": True, "sent": 0, "note": ""})
        self._rebuild()
        self._say(f"已加入列表：0x{mid:X}")

    def _row_values(self, r):
        return ("☑" if r["enabled"] else "☐", f"0x{r['id']:X}",
                len(r["data"]), self._fmt_hex(r["data"]) if r["data"] else "（远程帧）",
                "扩展帧" if r["ext"] else "标准帧",
                "远程帧" if r["rtr"] else "数据帧",
                r["gap"], r["times"],
                "已发" if r["sent"] >= r["times"] else f"{r['sent']}/{r['times']}",
                r["note"])

    def _rebuild(self):
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.rows):
            self.tree.insert("", "end", iid=str(i), values=self._row_values(r),
                             tags=("alt",) if i % 2 else ())

    def _refresh_row(self, idx):
        if 0 <= idx < len(self.rows) and self.tree.exists(str(idx)):
            self.tree.item(str(idx), values=self._row_values(self.rows[idx]))

    def _on_tree_sel(self, _e=None):
        pass

    def _remove_row(self):
        sel = self.tree.selection()
        if not sel:
            self._say("先选一行", warn=True)
            return
        for iid in sorted((int(x) for x in sel), reverse=True):
            if 0 <= iid < len(self.rows):
                self.rows.pop(iid)
        self._rebuild()

    def _clear_rows(self):
        self._stop()
        self.rows = []
        self._rebuild()
        self._say("列表已清空")

    # ---------------- 列表导入导出 ----------------
    def _list_menu(self):
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="导出列表…", command=self._export_list)
        m.add_command(label="导入列表…", command=self._import_list)
        m.add_separator()
        m.add_command(label="清空列表", command=self._clear_rows)
        try:
            x = self.btn_listops.winfo_rootx()
            y = self.btn_listops.winfo_rooty() + self.btn_listops.winfo_height()
            m.tk_popup(x, y)
        finally:
            m.grab_release()

    def _export_list(self):
        if not self.rows:
            self._say("列表为空", warn=True)
            return
        name = f"普通发送列表_{time.strftime('%Y%m%d_%H%M')}.csv"
        path = _ask_save(self, name)
        if not path:
            return
        header = ["ID", "数据长度", "数据Hex", "帧类型", "帧格式",
                  "延时ms", "次数", "启用", "备注"]
        rows = []
        for r in self.rows:
            rows.append([f"0x{r['id']:X}", len(r["data"]),
                         self._fmt_hex(r["data"]),
                         "扩展帧" if r["ext"] else "标准帧",
                         "远程帧" if r["rtr"] else "数据帧",
                         r["gap"], r["times"],
                         "1" if r["enabled"] else "0", r.get("note", "")])
        try:
            _write_csv(path, header, rows)
        except Exception as e:
            self._say(f"导出失败：{e}", warn=True)
            return
        self._say(f"已导出 {len(rows)} 行 → {os.path.basename(path)}")

    def _import_list(self):
        path = _ask_open(self)
        if not path:
            return
        try:
            rows = _read_csv(path)
        except Exception as e:
            self._say(f"读取失败：{e}", warn=True)
            return
        added = bad = 0
        for row in rows:
            try:
                mid = int((row.get("ID") or "").strip(), 16) & 0x1FFFFFFF
            except ValueError:
                bad += 1
                continue
            try:
                data = self._parse_hex(row.get("数据Hex") or "")
            except ValueError:
                bad += 1
                continue
            n = row.get("数据长度")
            if n:
                try:
                    n = int(float(n))
                    data = (data + bytes(max(n - len(data), 0)))[:n]
                except ValueError:
                    pass
            try:
                gap = max(int(float(row.get("延时ms") or 10)), 1)
            except ValueError:
                gap = 10
            try:
                times = max(int(float(row.get("次数") or 1)), 1)
            except ValueError:
                times = 1
            self.rows.append({
                "id": mid, "data": data,
                "ext": (row.get("帧类型") or "") == "扩展帧",
                "rtr": (row.get("帧格式") or "") == "远程帧",
                "gap": gap, "times": times,
                "enabled": _truthy(row.get("启用"), True),
                "sent": 0, "note": row.get("备注") or ""})
            added += 1
        self._rebuild()
        self._say(f"导入 {added} 行" + (f"，{bad} 行无法识别" if bad else ""),
                  warn=bool(bad))

    def _on_tree_dbl(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        col = self.tree.identify_column(event.x)
        idx = int(iid)
        r = self.rows[idx]
        if col == "#1":
            r["enabled"] = not r["enabled"]
            self._refresh_row(idx)
        elif col == "#7":
            self._inline(iid, "gap", lambda t: self._set_num(idx, "gap", t, 1))
        elif col == "#8":
            self._inline(iid, "times", lambda t: self._set_num(idx, "times", t, 1))

    def _set_num(self, idx, key, txt, lo):
        try:
            v = int(float(txt))
        except (TypeError, ValueError):
            self._say("必须是数字", warn=True)
            return
        self.rows[idx][key] = max(v, lo)
        self.rows[idx]["sent"] = 0
        self._refresh_row(idx)

    def _inline(self, iid, col_key, on_commit):
        self._cancel_edit()
        cols = list(self.tree["columns"])
        if col_key not in cols:
            return
        bbox = self.tree.bbox(iid, f"#{cols.index(col_key) + 1}")
        if not bbox:
            return
        x, y, w, h = bbox
        cur = self.tree.set(iid, col_key)
        e = tk.Entry(self.tree, justify="center", font=("Consolas", 11),
                     relief="solid", borderwidth=1)
        e.place(x=x, y=y, width=max(w, 56), height=h)
        e.insert(0, cur)
        e.select_range(0, "end")
        e.focus_set()
        self._edit = e

        def commit(_e=None):
            t = e.get()
            self._cancel_edit()
            on_commit(t)

        e.bind("<Return>", commit)
        e.bind("<FocusOut>", commit)
        e.bind("<Escape>", lambda _e: self._cancel_edit())

    def _cancel_edit(self):
        if self._edit is not None:
            try:
                self._edit.destroy()
            except Exception:
                pass
            self._edit = None

    # ---------------- 列表发送 ----------------
    def _toggle_list_send(self):
        if self.running_list:
            self._stop()
            return
        if not self._ready():
            return
        active = [r for r in self.rows if r["enabled"]]
        if not active:
            self._say("列表里没有可发送的行", warn=True)
            return
        self.running_list = True
        now = time.time()
        for r in self.rows:
            r["sent"] = 0
        self._seq_idx = 0
        self._seq_due = now
        self.btn_list.configure(text="停止", fg_color="#C0392B",
                                hover_color="#A93226")
        self._say("列表发送中…")
        self._tick_list()

    def _list_target(self):
        if self.lloop_var.get():
            return 0
        try:
            return max(int(float(self.ltimes_var.get())), 1)
        except (TypeError, ValueError):
            return 1

    def _emit_row(self, r):
        msg = can.Message(
            arbitration_id=r["id"], data=(b"" if r["rtr"] else r["data"]),
            is_extended_id=r["ext"], is_remote_frame=r["rtr"],
            is_fd=(len(r["data"]) > 8), is_rx=False)
        self._send_frame(msg)
        r["sent"] += 1

    def _tick_list(self):
        if not self.running_list:
            return
        if self._bus() is None:
            self._stop("通道已断开，发送中止")
            return
        now = time.time()
        try:
            if self.lmode_var.get() == "并行发送":
                for r in self.rows:
                    if not r["enabled"]:
                        continue
                    if now >= r.get("due", 0):
                        self._emit_row(r)
                        r["due"] = now + max(r["gap"], 1) / 1000.0
                        self._refresh_row(self.rows.index(r))
            else:
                if now >= self._seq_due:
                    pool = [r for r in self.rows if r["enabled"]]
                    if pool:
                        r = pool[self._seq_idx % len(pool)]
                        self._seq_idx = (self._seq_idx + 1) % len(pool)
                        self._emit_row(r)
                        self._refresh_row(self.rows.index(r))
                        self._seq_due = now + max(r["gap"], 1) / 1000.0
        except Exception as e:
            if self.fail_var.get() == "失败终止":
                self._stop(f"发送失败：{e}")
                return
            self._say(f"发送失败（继续）：{e}", warn=True)

        target = self._list_target()
        if target:
            if all(r["sent"] >= target for r in self.rows if r["enabled"]):
                self._stop("列表发送完成")
                return
        self.after(10, self._tick_list)

    # ---------------- 收尾 ----------------
    def on_live_disconnect(self):
        try:
            if self.winfo_exists():
                self._sync_chan()
                self._stop("通道已断开，发送中止")
        except Exception:
            pass

    def _on_close(self):
        self._stop()
        try:
            save_paned_layout(self, "raw", self._paneds)
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


class SendWindow(ctk.CTkToplevel):
    """
    DBC 信号发送 —— 独立窗口，界面照图莫斯 TCANLINPro「发送CAN数据」重做。

      左栏      消息列表：序号 / 消息名称 / ID(Hex) / 发送者 / 周期
      右上栏    发送列表：选择 / 消息名称 / ID(Hex) / DLC / 原始数据(Hex) / 间隔(ms) / 已发
      右下栏    信号：序号 / 信号名称 / 原始值 / 实际值 / 单位 / 转换比 / 偏移 / 起始位 / 位宽 / 最小 / 最大

    实际值双击就地编辑（不弹窗），改完整行「原始数据(Hex)」立刻重算，发出去的就是什么一眼可见。
    通道复用「实时」页已经打开的那一个：PCAN 通道独占，另开必然失败。
    """

    def __init__(self, master, app, live_tab, parse_fn=None, pack_fn=None):
        super().__init__(master)
        self.app = app
        self.live = live_tab
        self.parse_fn = parse_fn
        self.pack_fn = pack_fn      # dbc_parser.pack_message，避免循环 import
        self.rows = []
        self.running = False
        self.sel_row = None
        self.sig_by_iid = {}
        self._seq_idx = 0
        self._seq_due = 0.0
        self._edit = None
        self.C = _palette()

        self.title("发送CAN数据")
        self.geometry("1240x720")
        self.minsize(1000, 560)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build()
        self._refresh_dbc_list()

    # ---------------- 小工具 ----------------
    def _label(self, master, text, muted=True, size=11, bold=False):
        return ctk.CTkLabel(
            master, text=text, anchor="w",
            font=("Microsoft YaHei UI", size, "bold" if bold else "normal"),
            text_color=self.C["muted"] if muted else self.C["text"])

    @property
    def messages(self):
        return getattr(self.live, "messages", {}) or {}

    def _say(self, text, warn=False):
        try:
            self.status.configure(
                text=text, text_color="#C08A2A" if warn else self.C["muted"])
        except Exception:
            pass

    def _bus(self):
        src = getattr(self.live, "src", None)
        if src is None or getattr(src, "bus", None) is None:
            return None
        return src.bus

    @staticmethod
    def _num(v):
        if v is None:
            return "-"
        try:
            f = float(v)
        except (TypeError, ValueError):
            return str(v)
        return str(int(f)) if f == int(f) else f"{f:g}"

    @staticmethod
    def _sig_range(sig):
        """DBC 写了 [min|max] 就用它；没写按位宽 + factor/offset 推算"""
        lo, hi = sig.get("min"), sig.get("max")
        if lo is not None or hi is not None:
            return lo, hi, False
        length = sig.get("length") or 0
        if length <= 0:
            return None, None, False
        if sig.get("is_signed"):
            rlo, rhi = -(1 << (length - 1)), (1 << (length - 1)) - 1
        else:
            rlo, rhi = 0, (1 << length) - 1
        f = sig.get("factor")
        f = 1 if f in (None, 0) else f
        o = sig.get("offset") or 0
        a, b = rlo * f + o, rhi * f + o
        return min(a, b), max(a, b), True

    @staticmethod
    def _phys_to_raw(sig, phys):
        factor = sig.get("factor") or 0
        if not factor:
            return None
        return int(round((phys - sig.get("offset", 0)) / factor))

    # ---------------- 构件 ----------------
    def _build(self):
        c = self.C

        # ---- 顶栏 ----
        bar = ctk.CTkFrame(self, fg_color=c["card"], corner_radius=6)
        bar.pack(fill="x", padx=10, pady=(10, 6))

        self._label(bar, "通道", size=11).pack(side="left", padx=(12, 4), pady=8)
        self.chan_lbl = self._label(bar, "—", muted=False, size=11, bold=True)
        self.chan_lbl.pack(side="left")
        ctk.CTkButton(bar, text="复位布局", width=76, height=24,
                      fg_color=c["neutral"], hover_color="#6B6B6B",
                      font=("Microsoft YaHei UI", 10),
                      command=self._reset_layout).pack(side="left", padx=(14, 0), pady=10)

        # 右侧从右往左排：列表发送 / 循环发送 / 次数 / 模式
        self.btn_send = ctk.CTkButton(
            bar, text="列表发送", width=104, height=32,
            fg_color="#1668DC", hover_color="#0E4FA8",
            font=("Microsoft YaHei UI", 12, "bold"),
            command=self._toggle_send)
        self.btn_send.pack(side="right", padx=(8, 12), pady=8)

        self.loop_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(bar, text="循环发送", variable=self.loop_var,
                        font=("Microsoft YaHei UI", 11),
                        checkbox_width=18, checkbox_height=18,
                        command=self._on_loop_toggle
                        ).pack(side="right", padx=(0, 12))

        self.times_var = ctk.StringVar(value="1")
        self.times_entry = ctk.CTkEntry(bar, textvariable=self.times_var,
                                        width=58, height=30,
                                        font=("Consolas", 11))
        self.times_entry.pack(side="right", padx=(4, 8))
        self._label(bar, "发送次数", size=11).pack(side="right")

        self.mode_var = ctk.StringVar(value="并行发送")
        ctk.CTkOptionMenu(bar, values=["并行发送", "顺序发送"],
                          variable=self.mode_var, width=104, height=30,
                          font=("Microsoft YaHei UI", 11),
                          command=lambda _v: self._on_mode()
                          ).pack(side="right", padx=(4, 10))
        self._label(bar, "发送模式", size=11).pack(side="right")

        # ---- 主体 ----
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        # 左 / 右、右上 / 右下 都做成可拖拽分栏；位置按窗口名存进配置
        self.h_paned = ttk.PanedWindow(body, orient="horizontal")
        self.h_paned.pack(fill="both", expand=True)

        # 左：消息列表
        left = ctk.CTkFrame(self.h_paned, fg_color=c["card"], corner_radius=6)
        self.h_paned.add(left, weight=0)
        lh = ctk.CTkFrame(left, fg_color="transparent")
        lh.pack(fill="x", padx=8, pady=(8, 4))
        self._label(lh, "消息列表", muted=False, size=12, bold=True).pack(side="left")
        self._label(lh, "双击 / 右键加入", size=10).pack(side="left", padx=(8, 0))

        lc = ctk.CTkFrame(left, fg_color="transparent")
        lc.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        dcols = (("idx", "#", 32, "e"), ("name", "消息名称", 158, "w"),
                 ("id", "ID(Hex)", 70, "w"), ("tx", "发送者", 62, "w"),
                 ("cyc", "周期", 50, "e"), ("_f", "", 14, "w"))
        self.dbc_tree = ttk.Treeview(lc, show="headings", style="Live.Treeview",
                                     columns=[x[0] for x in dcols])
        for key, text, w, anchor in dcols:
            self.dbc_tree.heading(key, text=text, anchor=anchor)
            self.dbc_tree.column(key, width=w, minwidth=26, anchor=anchor,
                                 stretch=(key == "_f"))
        sv = ttk.Scrollbar(lc, orient="vertical", command=self.dbc_tree.yview,
                           style="Live.Vertical.TScrollbar")
        self.dbc_tree.configure(yscrollcommand=sv.set)
        self.dbc_tree.pack(side="left", fill="both", expand=True)
        sv.pack(side="right", fill="y")
        self.dbc_tree.tag_configure("alt", background=c["row_alt"])
        self.dbc_tree.bind("<Double-1>", self._on_dbc_dbl)
        self.dbc_tree.bind("<Button-3>", self._dbc_menu)

        # 右侧容器：发送列表与信号表再分一次
        rwrap = ctk.CTkFrame(self.h_paned, fg_color="transparent")
        self.h_paned.add(rwrap, weight=1)
        self.v_paned = ttk.PanedWindow(rwrap, orient="vertical")
        self.v_paned.pack(fill="both", expand=True)

        # 右上：发送列表
        rtop = ctk.CTkFrame(self.v_paned, fg_color=c["card"], corner_radius=6)
        self.v_paned.add(rtop, weight=3)
        th = ctk.CTkFrame(rtop, fg_color="transparent")
        th.pack(fill="x", padx=8, pady=(8, 4))
        self._label(th, "发送列表", muted=False, size=12, bold=True).pack(side="left")
        self._label(th, "双击「选择」切换启用，双击「间隔」改周期", size=10
                    ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(th, text="移除", width=52, height=24,
                      fg_color=c["neutral"], hover_color="#6B6B6B",
                      font=("Microsoft YaHei UI", 10),
                      command=self._remove_row).pack(side="right")
        self.btn_listops = ctk.CTkButton(
            th, text="列表操作 ▾", width=88, height=24,
            fg_color=c["neutral"], hover_color="#6B6B6B",
            font=("Microsoft YaHei UI", 10),
            command=self._list_menu)
        self.btn_listops.pack(side="right", padx=(0, 6))

        tc = ctk.CTkFrame(rtop, fg_color="transparent")
        tc.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        scols = (("on", "启用", 68, "center"), ("name", "消息名称", 160, "w"),
                 ("id", "ID(Hex)", 70, "w"), ("dlc", "DLC", 42, "e"),
                 ("data", "原始数据(Hex)", 280, "w"),
                 ("gap", "间隔(ms)", 62, "e"), ("cnt", "已发", 50, "e"),
                 ("_f", "", 14, "w"))
        self.send_tree = ttk.Treeview(tc, show="headings", style="Send.Treeview",
                                      columns=[x[0] for x in scols])
        for key, text, w, anchor in scols:
            self.send_tree.heading(key, text=text, anchor=anchor)
            self.send_tree.column(key, width=w, minwidth=48 if key == "on" else 24,
                                  anchor=anchor,
                                  stretch=(key in ("_f", "data")))
        sv2 = ttk.Scrollbar(tc, orient="vertical", command=self.send_tree.yview,
                            style="Live.Vertical.TScrollbar")
        self.send_tree.configure(yscrollcommand=sv2.set)
        self.send_tree.pack(side="left", fill="both", expand=True)
        sv2.pack(side="right", fill="y")
        self.send_tree.tag_configure("hover", background=c["card_alt"])
        self.send_tree.tag_configure("alt", background=c["row_alt"])
        self.send_tree.bind("<<TreeviewSelect>>", self._on_send_select)
        self.send_tree.bind("<Button-1>", self._on_send_click)
        self.send_tree.bind("<Motion>", self._on_send_motion)
        self.send_tree.bind("<Leave>", lambda _e: self._clear_hover())
        self.send_tree.bind("<Double-1>", self._on_send_dbl)

        # 右下：信号
        rbot = ctk.CTkFrame(self.v_paned, fg_color=c["card"], corner_radius=6)
        self.v_paned.add(rbot, weight=4)
        sh = ctk.CTkFrame(rbot, fg_color="transparent")
        sh.pack(fill="x", padx=8, pady=(8, 4))
        self.sig_hdr = self._label(sh, "信号", muted=False, size=12, bold=True)
        self.sig_hdr.pack(side="left")
        self.sig_meta = self._label(sh, "", size=10)
        self.sig_meta.pack(side="left", padx=(8, 0))

        scc = ctk.CTkFrame(rbot, fg_color="transparent")
        scc.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        gcols = (("idx", "#", 28, "e"), ("name", "信号名称", 176, "w"),
                 ("raw", "原始值", 68, "e"), ("act", "实际值", 84, "e"),
                 ("unit", "单位", 50, "w"), ("fac", "转换比", 60, "e"),
                 ("off", "偏移", 54, "e"), ("bit", "起始位", 52, "e"),
                 ("len", "位宽", 44, "e"), ("mn", "最小值", 58, "e"),
                 ("mx", "最大值", 58, "e"), ("_f", "", 14, "w"))
        self.sig_tree = ttk.Treeview(scc, show="headings", style="Live.Treeview",
                                     columns=[x[0] for x in gcols])
        for key, text, w, anchor in gcols:
            self.sig_tree.heading(key, text=text, anchor=anchor)
            self.sig_tree.column(key, width=w, minwidth=22, anchor=anchor,
                                 stretch=(key in ("_f", "name")))
        sv3 = ttk.Scrollbar(scc, orient="vertical", command=self.sig_tree.yview,
                            style="Live.Vertical.TScrollbar")
        self.sig_tree.configure(yscrollcommand=sv3.set)
        self.sig_tree.pack(side="left", fill="both", expand=True)
        sv3.pack(side="right", fill="y")
        self.sig_tree.tag_configure("alt", background=c["row_alt"])
        self.sig_tree.bind("<Double-1>", self._on_sig_dbl)

        # 底部状态
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=12, pady=(0, 8))
        self.status = ctk.CTkLabel(foot, text="", anchor="w", justify="left",
                                   font=("Microsoft YaHei UI", 11),
                                   text_color=c["muted"])
        self.status.pack(side="left")

        self._on_mode()
        self._sync_chan_label()

        # 分栏：拖动时防抖保存，打开时套用上次的位置
        self._paneds = [(self.h_paned, "h"), (self.v_paned, "v")]
        self._paned_defaults = [430, 300]
        bind_paned_save(self, "send", self._paneds)
        apply_paned_layout(self, "send", self._paneds, self._paned_defaults)

    def _reset_layout(self):
        """恢复默认分栏与默认窗口尺寸，并清掉本窗口的存档"""
        try:
            reset_paned_layout(self, "send", self._paneds,
                               self._paned_defaults, "1240x720")
            self._say("布局已恢复默认")
        except Exception:
            pass

    def _sync_chan_label(self):
        try:
            cfg = getattr(self.live, "pcan_cfg", None)
            if cfg is None:
                self.chan_lbl.configure(text="未配置")
                return
            rd = " · 只读" if getattr(self.live, "read_only", False) else ""
            self.chan_lbl.configure(text=cfg.summary() + rd)
        except Exception:
            pass

    # ---------------- 消息列表 ----------------
    def _refresh_dbc_list(self):
        self.dbc_tree.delete(*self.dbc_tree.get_children())
        msgs = self.messages
        for i, (mid, m) in enumerate(sorted(msgs.items())):
            cyc = m.get("cycle_ms")
            self.dbc_tree.insert(
                "", "end", iid=f"m{mid}",
                values=(i + 1, m.get("name", ""), f"0x{mid:X}",
                        m.get("transmitter", ""), f"{cyc}" if cyc else "-"),
                tags=("alt",) if i % 2 else ())
        if not msgs:
            self._say("未加载 DBC：请先在「⚡ 实时」页连接通道并指定 DBC", warn=True)

    @staticmethod
    def _mid_of(iid):
        if not iid:
            return None
        s = str(iid)
        if s.startswith("m"):
            s = s[1:]
        try:
            return int(s)
        except ValueError:
            return None

    def _on_dbc_dbl(self, event):
        iid = self.dbc_tree.identify_row(event.y)
        mid = self._mid_of(iid)
        if mid is not None:
            self._add_row(mid)

    def _dbc_menu(self, event):
        iid = self.dbc_tree.identify_row(event.y)
        mid = self._mid_of(iid)
        if mid is None:
            return
        self.dbc_tree.selection_set(iid)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="添加到发送列表",
                         command=lambda: self._add_row(mid))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ---------------- 发送列表 ----------------
    def _add_row(self, mid):
        if any(r["mid"] == mid for r in self.rows):
            self._say(f"0x{mid:X} 已在发送列表中", warn=True)
            return
        m = self.messages.get(mid)
        if m is None:
            return
        values = {}
        for sig in m.get("signals", []):
            dv = sig.get("start_value")
            if dv is None:
                lo = sig.get("min")
                dv = lo if isinstance(lo, (int, float)) and lo > 0 else 0.0
            values[sig["name"]] = float(dv)
        gap = m.get("cycle_ms") or 100
        self.rows.append({
            "mid": mid, "name": m.get("name", ""),
            "gap_ms": max(int(gap), 10),
            "want": True, "done": False, "count": 0,
            "due": 0.0, "values": values})
        self._rebuild_send_tree()
        self._say(f"已加入 0x{mid:X} · 间隔 {gap}ms（取自 DBC 周期）")

    def _remove_row(self):
        if self.sel_row is None:
            self._say("先在发送列表里选一行", warn=True)
            return
        self.rows = [r for r in self.rows if r is not self.sel_row]
        self.sel_row = None
        self.sig_by_iid = {}
        self.sig_tree.delete(*self.sig_tree.get_children())
        self.sig_hdr.configure(text="信号")
        self.sig_meta.configure(text="")
        self._rebuild_send_tree()

    def _clear_rows(self):
        self._stop()
        self.rows = []
        self.sel_row = None
        self.sig_by_iid = {}
        self.sig_tree.delete(*self.sig_tree.get_children())
        self.sig_hdr.configure(text="信号")
        self.sig_meta.configure(text="")
        self._rebuild_send_tree()
        self._say("发送列表已清空")

    # ---------------- 列表导入导出 ----------------
    def _list_menu(self):
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="导出列表…", command=self._export_list)
        m.add_command(label="导入列表…", command=self._import_list)
        m.add_separator()
        m.add_command(label="清空列表", command=self._clear_rows)
        try:
            x = self.btn_listops.winfo_rootx()
            y = self.btn_listops.winfo_rooty() + self.btn_listops.winfo_height()
            m.tk_popup(x, y)
        finally:
            m.grab_release()

    def _export_list(self):
        if not self.rows:
            self._say("发送列表为空", warn=True)
            return
        name = f"发送列表_{time.strftime('%Y%m%d_%H%M')}.csv"
        path = _ask_save(self, name)
        if not path:
            return
        header = ["ID", "名称", "间隔ms", "启用", "信号值"]
        rows = []
        for r in self.rows:
            sv = ";".join(f"{k}={self._num(v)}" for k, v in r["values"].items())
            rows.append([f"0x{r['mid']:X}", r["name"], r["gap_ms"],
                         "1" if r.get("want", True) else "0", sv])
        try:
            _write_csv(path, header, rows)
        except Exception as e:
            self._say(f"导出失败：{e}", warn=True)
            return
        self._say(f"已导出 {len(rows)} 行 → {os.path.basename(path)}")

    def _import_list(self):
        path = _ask_open(self)
        if not path:
            return
        try:
            rows = _read_csv(path)
        except Exception as e:
            self._say(f"读取失败：{e}", warn=True)
            return
        added = dup = bad = 0
        for row in rows:
            idtxt = (row.get("ID") or "").strip()
            try:
                mid = int(idtxt, 16) & 0x7FFFFFFF
            except ValueError:
                bad += 1
                continue
            if any(r["mid"] == mid for r in self.rows):
                dup += 1
                continue
            m = self.messages.get(mid)
            if m is None:
                bad += 1
                continue
            values = {}
            for sig in m.get("signals", []):
                dv = sig.get("start_value")
                if dv is None:
                    lo = sig.get("min")
                    dv = lo if isinstance(lo, (int, float)) and lo > 0 else 0.0
                values[sig["name"]] = float(dv)
            for pair in (row.get("信号值") or "").split(";"):
                if "=" not in pair:
                    continue
                k, v = pair.split("=", 1)
                try:
                    values[k.strip()] = float(v)
                except ValueError:
                    pass
            try:
                gap = max(int(float(row.get("间隔ms") or 100)), 1)
            except ValueError:
                gap = 100
            want = _truthy(row.get("启用"), True)
            self.rows.append({
                "mid": mid, "name": m.get("name", ""),
                "gap_ms": gap, "want": want, "done": False,
                "count": 0, "due": 0.0, "values": values})
            added += 1
        self._rebuild_send_tree()
        msg = f"导入 {added} 行"
        if dup:
            msg += f"，跳过重复 {dup}"
        if bad:
            msg += f"，{bad} 行无法识别"
        self._say(msg, warn=bool(bad))

    def _hex_of(self, r):
        m = self.messages.get(r["mid"])
        if not m or not self.pack_fn:
            return ""
        try:
            data, _missed = self.pack_fn(m, r["values"])
            return data.hex(" ").upper()
        except Exception:
            return ""

    def _row_values(self, r):
        m = self.messages.get(r["mid"]) or {}
        return ("☑" if r.get("want") else "☐", r["name"], f"0x{r['mid']:X}",
                m.get("dlc", ""), self._hex_of(r), r["gap_ms"], r["count"])

    def _rebuild_send_tree(self):
        self.send_tree.delete(*self.send_tree.get_children())
        for i, r in enumerate(self.rows):
            self.send_tree.insert("", "end", iid=str(i),
                                  values=self._row_values(r),
                                  tags=("alt",) if i % 2 else ())

    def _refresh_send_row(self, idx):
        if 0 <= idx < len(self.rows) and self.send_tree.exists(str(idx)):
            self.send_tree.item(str(idx), values=self._row_values(self.rows[idx]))

    def _paint_on_cell(self, idx, ch):
        """只改「启用」这一格，不整行重绘"""
        if self.send_tree.exists(str(idx)):
            vals = list(self.send_tree.item(str(idx), "values"))
            if vals:
                vals[0] = ch
                self.send_tree.item(str(idx), values=vals)

    def _toggle_want(self, idx):
        """切换启用状态：勾选意图只存 want；done 只表示本轮已发够"""
        if not (0 <= idx < len(self.rows)):
            return
        r = self.rows[idx]
        r["want"] = not r.get("want")
        r["done"] = False
        # 中间帧做一次 70ms 过渡，勾上/取消都不是生硬跳变
        self._paint_on_cell(idx, "▣" if r["want"] else "▢")
        self.after(70, lambda i=idx: self._refresh_send_row(i))
        self._say("已启用" if r["want"] else "已停用")

    def _on_send_select(self, _event=None):
        sel = self.send_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx >= len(self.rows):
            return
        self.sel_row = self.rows[idx]
        self._refresh_signal_table()

    def _on_send_click(self, event):
        """点「启用」格直接切换——不用双击，命中面积也够大"""
        iid = self.send_tree.identify_row(event.y)
        if not iid:
            return
        if self.send_tree.identify_column(event.x) != "#1":
            return
        self._toggle_want(int(iid))

    def _on_send_motion(self, event):
        """悬停整行轻微高亮，鼠标在哪一行一目了然"""
        iid = self.send_tree.identify_row(event.y)
        if iid == getattr(self, "_hover_iid", None):
            return
        prev = getattr(self, "_hover_iid", None)
        if prev and self.send_tree.exists(prev):
            tags = [t for t in self.send_tree.item(prev, "tags") if t != "hover"]
            self.send_tree.item(prev, tags=tags)
        if iid:
            tags = list(self.send_tree.item(iid, "tags"))
            if "hover" not in tags:
                tags.append("hover")
            self.send_tree.item(iid, tags=tags)
        self._hover_iid = iid

    def _clear_hover(self):
        prev = getattr(self, "_hover_iid", None)
        if prev and self.send_tree.exists(prev):
            tags = [t for t in self.send_tree.item(prev, "tags") if t != "hover"]
            self.send_tree.item(prev, tags=tags)
        self._hover_iid = None

    def _on_send_dbl(self, event):
        iid = self.send_tree.identify_row(event.y)
        if not iid:
            return
        col = self.send_tree.identify_column(event.x)
        idx = int(iid)
        if col == "#6":                       # 间隔
            self._begin_edit(self.send_tree, iid, "gap",
                             lambda txt: self._commit_gap(idx, txt))

    def _commit_gap(self, idx, txt):
        try:
            v = int(float(txt))
        except (TypeError, ValueError):
            self._say("间隔必须是数字", warn=True)
            return
        self.rows[idx]["gap_ms"] = max(v, 1)
        self._refresh_send_row(idx)

    # ---------------- 信号表 ----------------
    def _refresh_signal_table(self):
        self.sig_tree.delete(*self.sig_tree.get_children())
        self.sig_by_iid = {}
        r = self.sel_row
        if r is None:
            return
        m = self.messages.get(r["mid"])
        if m is None:
            return
        self.sig_hdr.configure(text=f"信号 · 0x{r['mid']:X} {r['name']}")
        self.sig_meta.configure(text="双击「实际值」直接改，改完整行原始数据立即更新")
        for i, sig in enumerate(m.get("signals", [])):
            name = sig["name"]
            act = r["values"].get(name, 0.0)
            raw = self._phys_to_raw(sig, act)
            lo, hi, derived = self._sig_range(sig)
            mn = self._num(lo)
            mx = self._num(hi)
            if derived:
                mn = "≈" + mn
                mx = "≈" + mx
            self.sig_tree.insert(
                "", "end", iid=name,
                values=(i + 1, name,
                        f"0x{raw:X}" if raw is not None else "-",
                        self._num(act), sig.get("unit", ""),
                        self._num(sig.get("factor")), self._num(sig.get("offset")),
                        sig.get("start_bit", ""), sig.get("length", ""),
                        mn, mx),
                tags=("alt",) if i % 2 else ())
            self.sig_by_iid[name] = sig

    def _on_sig_dbl(self, event):
        iid = self.sig_tree.identify_row(event.y)
        if not iid or iid not in self.sig_by_iid:
            return
        col = self.sig_tree.identify_column(event.x)
        if col != "#4":                       # 只允许改「实际值」
            return
        self._begin_edit(self.sig_tree, iid, "act",
                         lambda txt: self._commit_sig(iid, txt))

    def _commit_sig(self, name, txt):
        sig = self.sig_by_iid.get(name)
        r = self.sel_row
        if sig is None or r is None:
            return
        try:
            val = float(txt)
        except (TypeError, ValueError):
            self._say("实际值必须是数字", warn=True)
            return
        lo = sig.get("min")
        hi = sig.get("max")
        if lo is not None and val < lo:
            val = lo
            self._say(f"{name} 已按最小值 {self._num(lo)} 处理", warn=True)
        if hi is not None and val > hi:
            val = hi
            self._say(f"{name} 已按最大值 {self._num(hi)} 处理", warn=True)
        r["values"][name] = val
        # 就地更新这一行的原始值
        raw = self._phys_to_raw(sig, val)
        if self.sig_tree.exists(name):
            vals = list(self.sig_tree.item(name, "values"))
            vals[2] = f"0x{raw:X}" if raw is not None else "-"
            vals[3] = self._num(val)
            self.sig_tree.item(name, values=vals)
        # 发送列表的原始数据整体重算
        try:
            idx = self.rows.index(r)
            self._refresh_send_row(idx)
        except ValueError:
            pass

    # ---------------- 就地编辑 ----------------
    def _begin_edit(self, tree, iid, col_key, on_commit):
        self._cancel_edit()
        cols = list(tree["columns"])
        if col_key not in cols:
            return
        bbox = tree.bbox(iid, f"#{cols.index(col_key) + 1}")
        if not bbox:
            return
        x, y, w, h = bbox
        try:
            cur = tree.set(iid, col_key)
        except Exception:
            cur = ""
        e = tk.Entry(tree, justify="center", font=("Consolas", 11),
                     relief="solid", borderwidth=1)
        e.place(x=x, y=y, width=max(w, 60), height=h)
        e.insert(0, cur)
        e.select_range(0, "end")
        e.focus_set()
        self._edit = e

        def commit(_evt=None):
            txt = e.get()
            self._cancel_edit()
            on_commit(txt)

        def cancel(_evt=None):
            self._cancel_edit()

        e.bind("<Return>", commit)
        e.bind("<FocusOut>", commit)
        e.bind("<Escape>", cancel)

    def _cancel_edit(self):
        if self._edit is not None:
            try:
                self._edit.destroy()
            except Exception:
                pass
            self._edit = None

    # ---------------- 发送 ----------------
    def _on_mode(self):
        seq = self.mode_var.get() == "顺序发送"
        self._say("顺序发送：按列表顺序逐条发，间隔取该行设置"
                  if seq else "并行发送：每行按自己的间隔独立发")

    def _on_loop_toggle(self):
        try:
            self.times_entry.configure(
                state="disabled" if self.loop_var.get() else "normal")
        except Exception:
            pass
        self._say("循环发送：一直发到手动停止"
                  if self.loop_var.get() else f"按次数发送")

    def _target_times(self):
        if self.loop_var.get():
            return 0
        try:
            n = int(float(self.times_var.get()))
        except (TypeError, ValueError):
            n = 1
        return max(n, 1)

    def _ready(self):
        if self.live is None:
            self._say("实时页不可用", warn=True)
            return False
        if getattr(self.live, "read_only", False):
            self._say("当前是只读（只监听）连接，不能发送。"
                      "请关闭占用程序后以「正常收发」重新连接", warn=True)
            return False
        if self._bus() is None:
            self._say("通道未连接：请先到「⚡ 实时」页连接通道", warn=True)
            return False
        if not self.rows:
            self._say("发送列表为空", warn=True)
            return False
        if not any(r.get("want", True) for r in self.rows):
            self._say("没有任何一行被勾选", warn=True)
            return False
        return True

    def _toggle_send(self):
        if self.running:
            self._stop()
            return
        if not self._ready():
            return
        self.running = True
        now = time.time()
        for r in self.rows:
            r["done"] = False
            r["count"] = 0
            r["due"] = now
        self._seq_idx = 0
        self._seq_due = now
        self.btn_send.configure(text="停止发送", fg_color="#C0392B",
                                hover_color="#A93226")
        tgt = self._target_times()
        self._say("发送中…" + ("（循环）" if not tgt else f"（每行 {tgt} 次）"))
        self._tick()

    def _stop(self, reason=None):
        was = self.running
        self.running = False
        try:
            self.btn_send.configure(text="列表发送", fg_color="#1668DC",
                                    hover_color="#0E4FA8")
        except Exception:
            pass
        if was:
            self._say(reason or "已停止发送", warn=bool(reason))

    def _tick(self):
        if not self.running:
            return
        if self._bus() is None:
            self._stop("连接已断开，发送中止")
            return
        target = self._target_times()
        now = time.time()
        try:
            if self.mode_var.get() == "顺序发送":
                if now >= self._seq_due:
                    live_rows = [r for r in self.rows
                                 if r.get("want") and not r.get("done")]
                    if live_rows:
                        r = live_rows[self._seq_idx % len(live_rows)]
                        self._seq_idx = (self._seq_idx + 1) % len(live_rows)
                        self._emit(r, target)
                        self._seq_due = now + max(r["gap_ms"], 1) / 1000.0
            else:
                for r in self.rows:
                    if not r.get("want") or r.get("done"):
                        continue
                    if now >= r["due"]:
                        self._emit(r, target)
                        r["due"] = now + max(r["gap_ms"], 1) / 1000.0
        except Exception as e:
            self._stop(f"发送中止：{e}")
            return
        if self.rows and not any(r.get("want") and not r.get("done")
                                 for r in self.rows):
            self._stop("全部发送完成")
            return
        self.after(10, self._tick)

    def _emit(self, r, target):
        bus = self._bus()
        if bus is None:
            raise RuntimeError("通道已断开")
        m = self.messages.get(r["mid"])
        if m is None:
            return
        data, missed = self.pack_fn(m, r["values"])
        if missed and not r.get("_warned"):
            r["_warned"] = True
            self._say(f"0x{r['mid']:X} 有信号未写入：" + "；".join(missed[:3]),
                      warn=True)
        bus.send(can.Message(
            arbitration_id=r["mid"], data=data,
            is_extended_id=bool(m.get("ext")), is_fd=(len(data) > 8),
            is_rx=False))
        r["count"] += 1
        if target and r["count"] >= target:
            r["done"] = True          # 只标本轮发完，不动用户的勾选
        try:
            self._refresh_send_row(self.rows.index(r))
        except ValueError:
            pass

    # ---------------- 收尾 ----------------
    def on_live_disconnect(self):
        try:
            if self.winfo_exists():
                self._sync_chan_label()
                self._stop("通道已断开，发送中止")
        except Exception:
            pass

    def _on_close(self):
        self._stop()
        try:
            save_paned_layout(self, "send", self._paneds)
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


# ------------------------- 窗口布局（分栏 + 尺寸） -------------------------
# 各弹窗的 sash 位置与窗口尺寸统一存进 config.json，按窗口名分组：
#   {"last_dbc": "...", "layout": {"parse": {...}, "send": {...}, "raw": {...}, "uds": {...}}}
# 主程序只写自己的 "parse" 组；两边都是读-改-写，谁也不整块覆盖 layout，
# 否则后写的会把先写的设置抹掉。

def _cfg_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "BusLens")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.dirname(os.path.abspath(sys.argv[0]))
    return os.path.join(d, "config.json")


def _cfg_read():
    try:
        with open(_cfg_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _cfg_write(cfg):
    try:
        with open(_cfg_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def load_layout(key):
    """取某个窗口的布局；数据坏了当没有，不影响启动"""
    lay = _cfg_read().get("layout")
    v = lay.get(key) if isinstance(lay, dict) else None
    return v if isinstance(v, dict) else {}


def save_layout(key, vals):
    """读-改-写：只动自己这一组"""
    cfg = _cfg_read()
    lay = cfg.get("layout")
    if not isinstance(lay, dict):
        lay = {}
    old = lay.get(key)
    merged = dict(old) if isinstance(old, dict) else {}
    merged.update(vals)
    lay[key] = merged
    cfg["layout"] = lay
    _cfg_write(cfg)


def drop_layout(key):
    cfg = _cfg_read()
    lay = cfg.get("layout")
    if isinstance(lay, dict) and key in lay:
        lay.pop(key, None)
        cfg["layout"] = lay
        _cfg_write(cfg)


def apply_paned_layout(win, key, pairs, defaults):
    """套用保存的分栏位置与窗口尺寸。

    pairs = [(paned, 名字), ...]。sashpos 在窗口 map 之前无效，
    CTk 建窗后还有一串延迟回调会把 sash 重排回去，所以分三拍，
    最后一拍落在 1000ms 之后。
    """
    saved = load_layout(key)
    g = saved.get("geom")
    if isinstance(g, str) and g.strip():
        try:
            win.geometry(g)
        except Exception:
            pass

    def put():
        for (paned, name), dflt in zip(pairs, defaults):
            try:
                paned.sashpos(0, max(int(saved.get(name, dflt)), 120))
            except Exception:
                pass

    for ms in (120, 500, 1100):
        win.after(ms, put)


def save_paned_layout(win, key, pairs):
    """记录 sash 位置与当前窗口尺寸"""
    vals = {}
    for paned, name in pairs:
        try:
            vals[name] = int(paned.sashpos(0))
        except Exception:
            pass
    try:
        if win.winfo_exists() and win.state() == "normal":
            vals["geom"] = win.geometry()
    except Exception:
        pass
    if vals:
        save_layout(key, vals)


def bind_paned_save(win, key, pairs):
    """拖 sash 时防抖保存：松手 400ms 后落盘，不是每移动一像素写一次文件"""
    def _later(_e=None):
        job = getattr(win, "_paned_job", None)
        if job:
            try:
                win.after_cancel(job)
            except Exception:
                pass
        win._paned_job = win.after(400, lambda: save_paned_layout(win, key, pairs))

    for paned, _name in pairs:
        paned.bind("<B1-Motion>", _later)
        paned.bind("<ButtonRelease-1>", _later)


def reset_paned_layout(win, key, pairs, defaults, geom=None):
    """恢复默认分栏与默认尺寸，并清掉本窗口的存档"""
    for (paned, _name), dflt in zip(pairs, defaults):
        try:
            paned.sashpos(0, dflt)
        except Exception:
            pass
    if geom:
        try:
            win.geometry(geom)
        except Exception:
            pass
    drop_layout(key)


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_HWND_TOP = 0


def _center_window_native(win, master, raise_it=False):
    """
    用 Win32 SetWindowPos 把弹窗放到主窗口正中。

    不走 tkinter 的 geometry / wm_geometry：CTk 会按 DPI 缩放二次换算坐标，
    而且它自己在 10ms / 200ms 还有改标题栏、设图标的延迟回调，
    会把窗口位置打断（表现为所有弹窗叠在同一处）。
    这里直接按物理像素操作外框窗口，绕开全部缩放与竞争。

    raise_it=True 时同一次调用顺带把 z-order 提到最前：
    CTk 的 Toplevel 在 10ms withdraw、15ms 再 deiconify，
    那一次 deiconify 会把窗口重新压回主窗口下面，
    创建瞬间的 lift() 顶不到那个时候。定位与提层合并成一次 API，
    免得多拍之间再多一轮竞争。
    """
    try:
        u = ctypes.windll.user32
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                   ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
        mh = int(master.wm_frame(), 16)
        wh = int(win.wm_frame(), 16)
        if not mh or not wh:
            return False
        mr, wr = _RECT(), _RECT()
        u.GetWindowRect(wintypes.HWND(mh), ctypes.byref(mr))
        u.GetWindowRect(wintypes.HWND(wh), ctypes.byref(wr))
        w = wr.right - wr.left
        h = wr.bottom - wr.top
        sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        x = mr.left + ((mr.right - mr.left) - w) // 2
        y = mr.top + ((mr.bottom - mr.top) - h) // 2
        x = max(0, min(x, max(sw - w, 0)))
        y = max(0, min(y, max(sh - h, 0)))
        if raise_it:
            u.SetWindowPos(wintypes.HWND(wh), wintypes.HWND(_HWND_TOP),
                           x, y, 0, 0, _SWP_NOSIZE | _SWP_NOACTIVATE)
        else:
            u.SetWindowPos(wintypes.HWND(wh), wintypes.HWND(0),
                           x, y, 0, 0, _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE)
        return True
    except Exception:
        return False


def _raise_native(win):
    """只把窗口提到 z-order 最前：不动位置、不动尺寸、不抢焦点"""
    try:
        u = ctypes.windll.user32
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                   ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
        wh = int(win.wm_frame(), 16)
        if not wh:
            return False
        u.SetWindowPos(wintypes.HWND(wh), wintypes.HWND(_HWND_TOP),
                       0, 0, 0, 0,
                       _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOACTIVATE)
        return True
    except Exception:
        return False


def _release_topmost(win):
    """临时置顶的延时收回：只求弹窗跳一次前台，不长期压住别的窗口"""
    try:
        if win.winfo_exists():
            win.attributes("-topmost", False)
    except Exception:
        pass


def present_window(win, master):
    """
    首次打开弹窗：跳到前台 + 居中到主窗口。

    必须分拍（50 / 180 / 360ms），原因在 CTk 的 Toplevel 有一串延迟回调：
        10ms   withdraw()          改标题栏颜色
        15ms   deiconify()         重新 map —— z-order 在这一刻被重排，会压回主窗口下面
        200ms  iconbitmap()        设图标
        1000ms _set_scaled_min_max 设缩放后最小尺寸
    创建瞬间的 lift()/topmost 只顶到 withdraw 之前，拦不住 15ms 那次重新 map；
    之前所有弹窗都出现在主窗口后面，就是这一条。
    所以定位与提 z-order 一起延后到 deiconify 之后，走同一次 SetWindowPos（物理像素），
    避开 CTk 的 DPI 换算与它的位置改写。

    topmost 只保持到 700ms 就收回：目的是让窗口「弹出来那一下」绝不被压住，
    之后仍靠 HWND_TOP 停在主窗口之上，不去长期压住别的程序。
    刻意不用 transient()：加了之后窗口被钉在主窗口上、拖不到副屏。
    """
    try:
        win.deiconify()
        win.lift()
        win.focus_force()
        win.attributes("-topmost", True)
    except Exception:
        return

    for ms in (50, 180, 360):
        try:
            win.after(ms, lambda: _center_window_native(win, master, True))
        except Exception:
            pass

    try:
        win.after(700, lambda: _release_topmost(win))
        win.after(760, lambda: _raise_native(win))
    except Exception:
        pass


def bring_to_front(win):
    """
    已开着的弹窗再次唤起：提到最前，不重新居中
    —— 位置可能是用户自己摆到副屏去的，不该被拉回来。

    同样要分拍：deiconify 那一刻的 z-order 由 Windows 决定，创建时的 lift() 靠不住。
    """
    try:
        win.deiconify()
        win.lift()
        win.focus_force()
        win.attributes("-topmost", True)
    except Exception:
        return win

    for ms in (50, 220):
        try:
            win.after(ms, lambda: _raise_native(win))
        except Exception:
            pass

    try:
        win.after(520, lambda: _release_topmost(win))
        win.after(580, lambda: _raise_native(win))
    except Exception:
        pass
    return win


def open_uds_window(master, app, live_tab):
    """打开 UDS 诊断窗口；已开着就提到前面"""
    win = getattr(live_tab, "uds_win", None)
    try:
        if win is not None and win.winfo_exists():
            return bring_to_front(win)
    except Exception:
        pass
    win = UdsWindow(master, app, live_tab)
    live_tab.uds_win = win
    present_window(win, master)
    return win


def open_replay_window(master, app, live_tab):
    """打开数据回放窗口；已开着就提到前面"""
    win = getattr(live_tab, "replay_win", None)
    try:
        if win is not None and win.winfo_exists():
            return bring_to_front(win)
    except Exception:
        pass
    win = ReplayWindow(master, app, live_tab)
    live_tab.replay_win = win
    present_window(win, master)
    return win


def open_raw_send_window(master, app, live_tab):
    """打开普通发送窗口；已开着就提到前面"""
    win = getattr(live_tab, "raw_send_win", None)
    try:
        if win is not None and win.winfo_exists():
            return bring_to_front(win)
    except Exception:
        pass
    win = RawSendWindow(master, app, live_tab)
    live_tab.raw_send_win = win
    present_window(win, master)
    return win


def open_send_window(master, app, live_tab, parse_fn=None, pack_fn=None):
    """打开发送窗口；已开着就把它提到前面，不重复创建"""
    win = getattr(live_tab, "send_win", None)
    try:
        if win is not None and win.winfo_exists():
            return bring_to_front(win)
    except Exception:
        pass
    win = SendWindow(master, app, live_tab, parse_fn, pack_fn)
    live_tab.send_win = win
    present_window(win, master)
    return win


def add_live_tab(app, decode_fn, parse_fn=None, pack_fn=None):
    """在主窗口挂载「实时」标签页，返回 LiveTab 实例。

    传入 parse_fn（dbc_parser.parse_dbc）后，实时页可独立加载 DBC，
    报文表与值表都不再与主界面共用。
    传入 pack_fn（dbc_parser.pack_message）后，工具条的「发送」按钮可以
    打开发送窗口。
    """
    tab = app.tabs.add("监控", "monitor")
    live = LiveTab(tab, app, decode_fn, parse_fn)
    live.pack(fill="both", expand=True)
    if pack_fn is not None:
        def _open(kind):
            if kind == "raw":
                return open_raw_send_window(app, app, live)
            if kind == "replay":
                return open_replay_window(app, app, live)
            if kind == "uds":
                return open_uds_window(app, app, live)
            return open_send_window(app, app, live, parse_fn, pack_fn)
        live.open_send_fn = _open
    return live
