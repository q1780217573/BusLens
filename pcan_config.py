# -*- coding: utf-8 -*-
"""
PCAN 通道配置底层
=================
提供三件事，与界面完全解耦：

1. 位定时反算：从「时钟 + 波特率 + 目标采样点 + SJW」求 brp / tseg1 / tseg2，
   并给出实际采样点与偏差。无精确解时返回最接近的可行解，绝不静默。
2. 通道枚举：列出实际存在的通道及其占用状态、是否支持 FD。
3. 占用状态查询：PCAN_CHANNEL_CONDITION，用于打开前判断能否正常连接。

依赖 PCANBasic.dll（PEAK-System 驱动或随程序附带的 x64 DLL）。
DLL 不可用时所有查询返回 (None, 原因)，由界面决定如何提示，不抛异常。
"""

import ctypes
import os
import sys

# ============================ 常量 ============================

# 参数号以 PCAN-Basic 官方 TPCANParameter 为准（已核对 python-can 4.6.1 basic.py）
# 旧值 0x09 / 0x0B 实际指向 PCAN_LOG_LOCATION / PCAN_LOG_CONFIGURE，属误用：
# 用 1 字节缓冲查日志路径参数必然失败，状态判定因此全废。
PCAN_API_VERSION = 0x05
PCAN_CHANNEL_VERSION = 0x06
PCAN_CHANNEL_CONDITION = 0x0D
PCAN_CHANNEL_FEATURES = 0x16
PCAN_HARDWARE_NAME = 0x0E
PCAN_CHANNEL_FEATURE_FD = 0x01

PCAN_ATTACHED_CHANNELS_COUNT = 0x2A
PCAN_ATTACHED_CHANNELS = 0x2B
# 初始化前设置：不指定波特率，直接沿用通道上已有那一套。
# PEAK 官方论坛确认，这是让第二个应用接入已被 PCAN-View 占用的通道的正规做法。
PCAN_BITRATE_ADAPTING = 0x17

MAX_CHANNEL_BUFFER = 80 * 400
# 通道表结构体：4.x 为 48 字节（无 channel_condition），
# 5.x 为 52 字节（末尾新增 channel_condition）。句柄/features/名字偏移两者一致。
# 第二个元素标记设备名是否 UTF-16；ANSI 优先。
_CHANNEL_LAYOUTS = [(52, False), (48, False), (52, True), (48, True)]

_NAME_OFFSET = 8          # handle(2)+type(1)+controller(1)+features(4)
_NAME_LEN = 33            # MAX_LENGTH_HARDWARE_NAME：32 字符 + 终止符
_ID_OFFSET = 44           # 8+33=41，向上对齐到 44
_COND_OFFSET = 48         # 仅 5.x 布局存在

COND_UNAVAILABLE = 0x00
COND_AVAILABLE = 0x01
COND_OCCUPIED = 0x02
COND_PCANVIEW = 0x03

COND_TEXT = {
    COND_UNAVAILABLE: "不可用",
    COND_AVAILABLE: "空闲",
    COND_OCCUPIED: "已被占用",
    COND_PCANVIEW: "被 PCAN-View 占用",
}

# PCAN 句柄编码：通道 1-9 = 基址+n；通道 10-16 = 高基址+n
#   0x40/0x400 = PCAN_PCIBUSn   （PCIe）
#   0x50/0x500 = PCAN_USBBUSn   （USB）
#   0x30/0x300 = PCAN_DNGBUSn   （DNG）
#   0x800      = PCAN_LANBUSn   （LAN，连续到 16）
_HANDLE_SEGS = [
    (0x40, 0x400, "PCAN_PCIBUS", "PCIe"),
    (0x50, 0x500, "PCAN_USBBUS", "USB"),
    (0x30, 0x300, "PCAN_DNGBUS", "DNG"),
    (0x800, None, "PCAN_LANBUS", "LAN"),
]

# 兼容旧引用（区间上界按官方编号修正：1-8 号段止于 0x48/0x58/0x38/0x808）
SEGMENTS = [(0x41, 0x48, "PCAN_PCIBUS", "PCIe"),
            (0x51, 0x58, "PCAN_USBBUS", "USB"),
            (0x31, 0x38, "PCAN_DNGBUS", "DNG"),
            (0x801, 0x808, "PCAN_LANBUS", "LAN")]

# PCAN-View 默认参数（见方案第 7 节）
DEFAULT_CLASSIC = {"bitrate": 500000, "sample_point": 87.5, "sjw": 1}
DEFAULT_FD_NOMINAL = {"bitrate": 500000, "sample_point": 80.0, "sjw": 1}
DEFAULT_FD_DATA = {"bitrate": 2000000, "sample_point": 75.0, "sjw": 1}
DEFAULT_F_CLOCK = 80_000_000

BITRATE_PRESETS = [
    (1_000_000, "1 Mbit/s"),
    (800_000, "800 kbit/s"),
    (500_000, "500 kbit/s"),
    (250_000, "250 kbit/s"),
    (125_000, "125 kbit/s"),
    (100_000, "100 kbit/s"),
    (50_000, "50 kbit/s"),
    (33_333, "33.3 kbit/s"),
    (20_000, "20 kbit/s"),
    (10_000, "10 kbit/s"),
    (5_000, "5 kbit/s"),
]

# CAN FD 数据段可使用更高波特率
DATA_BITRATE_PRESETS = [
    (8_000_000, "8 Mbit/s"),
    (5_000_000, "5 Mbit/s"),
    (4_000_000, "4 Mbit/s"),
    (2_000_000, "2 Mbit/s"),
    (1_000_000, "1 Mbit/s"),
    (500_000, "500 kbit/s"),
]

ALL_BITRATE_PRESETS = DATA_BITRATE_PRESETS + [
    p for p in BITRATE_PRESETS if p[0] not in {v for v, _ in DATA_BITRATE_PRESETS}
]

F_CLOCK_OPTIONS = [(80_000_000, "80 MHz"), (40_000_000, "40 MHz")]


# ============================ 通道名 ↔ 句柄 ============================

def channel_to_handle(name):
    """'PCAN_PCIBUS1' → 0x41；无法识别返回 None

    官方编号：PCIe/USB/DNG 的 1-8 号用 0x4n/0x5n/0x3n，
    9-16 号切到 0x40n/0x50n/0x30n；LAN 从 0x801 连续到 0x810。
    """
    if not name:
        return None
    for lo, hi, prefix, _ in _HANDLE_SEGS:
        if not name.startswith(prefix):
            continue
        try:
            n = int(name[len(prefix):])
        except ValueError:
            return None
        if not 1 <= n <= 16:
            return None
        if hi is None or n <= 8:
            return lo + n
        return hi + n
    return None


def handle_to_channel(handle):
    """0x41 → ('PCAN_PCIBUS1', 'PCIe', 1)；越界返回 (None, None, None)"""
    for lo, hi, prefix, bus in _HANDLE_SEGS:
        if lo < handle <= lo + 8:
            n = handle - lo
            return f"{prefix}{n}", bus, n
        if hi is not None:
            if hi < handle <= hi + 16:
                n = handle - hi
                if 9 <= n <= 16:
                    return f"{prefix}{n}", bus, n
        elif lo + 8 < handle <= lo + 16:
            n = handle - lo
            return f"{prefix}{n}", bus, n
    return None, None, None


def channel_label(name):
    """'PCAN_PCIBUS1' → 'PCAN_PCIBUS1（通道 1 · PCIe）'"""
    for lo, hi, prefix, bus in _HANDLE_SEGS:
        if name and name.startswith(prefix):
            try:
                n = int(name[len(prefix):])
            except ValueError:
                return name
            return f"{name}（通道 {n} · {bus}）"
    return name


# ============================ DLL 加载 ============================

_lib = None
_lib_error = None
_lib_path = None        # 实际加载成功的 DLL 完整路径
_lib_source = None      # 来源标识：内嵌 / 程序目录 / 自带包 / 系统目录
_lib_tried = []         # 逐个候选的失败记录，用于诊断（不静默丢弃）


def _app_dir():
    """exe / 脚本所在真实目录（onefile 下是 exe 目录，不是 _MEIPASS）"""
    try:
        if getattr(sys, "frozen", False):
            return os.path.dirname(os.path.abspath(sys.executable))
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    except Exception:
        return os.getcwd()


def _meipass_dir():
    """PyInstaller onefile 运行时解压目录，无则 None"""
    return getattr(sys, "_MEIPASS", None)


def _dll_candidates():
    """返回 [(路径, 来源标识)]，按优先级排列；内嵌优先，系统目录最后"""
    out = []
    seen = set()

    def add(path, kind):
        try:
            key = os.path.normcase(os.path.abspath(path))
        except Exception:
            return
        if key in seen or not os.path.isfile(path):
            return
        seen.add(key)
        out.append((path, kind))

    mp = _meipass_dir()
    if mp:
        add(os.path.join(mp, "PCANBasic.dll"), "内嵌")
        add(os.path.join(mp, "PCAN-Basic", "x64", "PCANBasic.dll"), "内嵌")

    base = _app_dir()
    add(os.path.join(base, "PCANBasic.dll"), "程序目录")
    add(os.path.join(base, "x64", "PCANBasic.dll"), "程序目录")
    add(os.path.join(base, "PCAN-Basic", "x64", "PCANBasic.dll"), "自带包")

    # 系统目录放最后：工控机上 System32 可能是旧版，与新版驱动不配套
    sysdir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32")
    add(os.path.join(sysdir, "PCANBasic.dll"), "系统目录")

    return out


def _file_version(path):
    """读取 DLL 文件版本号（不加载），失败返回 '?'"""
    try:
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return "?"
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return "?"
        pv = ctypes.c_void_p()
        ln = ctypes.c_uint()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(pv), ctypes.byref(ln)):
            return "?"
        info = ctypes.cast(pv, ctypes.POINTER(ctypes.c_uint32 * 4)).contents
        ms, ls = info[2], info[3]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return "?"


def load_lib():
    """加载 PCANBasic.dll，失败返回 None（结果缓存在模块级）"""
    global _lib, _lib_error, _lib_path, _lib_source, _lib_tried
    if _lib is not None:
        return _lib
    if _lib_error is not None:
        return None

    _lib_tried = []
    for path, kind in _dll_candidates():
        try:
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(os.path.dirname(path))
                except Exception:
                    pass
            lib = ctypes.WinDLL(path)
            lib.CAN_GetValue.argtypes = [ctypes.c_ushort, ctypes.c_uint32,
                                         ctypes.c_void_p, ctypes.c_uint32]
            lib.CAN_GetValue.restype = ctypes.c_uint32
            _lib, _lib_path, _lib_source = lib, path, kind
            return _lib
        except Exception as e:
            _lib_tried.append(f"{kind} {path} → {type(e).__name__}: {e}")
            continue

    # 不再回退到裸名 "PCANBasic.dll"：那会静默加载系统目录的版本，
    # 用错 DLL 后的症状（API 版本空、CONDITION 全 0x1C00）会被误判成
    # 驱动或硬件故障。系统目录已作为最后一个候选参与尝试并带来源标记。
    _lib_error = "未找到可用的 PCANBasic.dll"
    return None


def dll_info():
    """返回实际加载的 DLL 详情 dict，供界面与诊断显示"""
    lib = load_lib()
    if lib is None:
        return {"ok": False, "error": _lib_error, "tried": list(_lib_tried),
                "path": None, "source": None, "version": None,
                "api_version": None}
    return {"ok": True, "error": None, "tried": list(_lib_tried),
            "path": _lib_path, "source": _lib_source,
            "version": _file_version(_lib_path),
            "api_version": api_version()}


def api_version():
    """向已加载的 DLL 询问其自报版本，失败返回 None"""
    lib = load_lib()
    if lib is None:
        return None
    try:
        buf = ctypes.create_string_buffer(256)
        res = lib.CAN_GetValue(0, PCAN_API_VERSION, ctypes.byref(buf), 256)
        if res != 0:
            return None
        text = buf.value.decode("ascii", "ignore").strip("\x00").strip()
        return text or None
    except Exception:
        return None


def dll_status():
    """返回 (是否可用, 说明文本)"""
    info = dll_info()
    if not info["ok"]:
        detail = info["tried"][-1] if info["tried"] else ""
        return False, (f"{info['error']}（最后一次尝试：{detail}）"
                       if detail else info["error"])
    return True, (f"已加载 [{info['source']}] {info['version']} · "
                  f"API {info['api_version'] or '无应答'}\n{info['path']}")


# ============================ 状态查询 ============================

def query_condition_handle(handle):
    """按句柄查询占用状态（PCAN_CHANNEL_CONDITION 0x0D，参数类型为 DWORD）"""
    lib = load_lib()
    if lib is None:
        return None, _lib_error or "未找到 PCANBasic.dll"
    try:
        buf = ctypes.c_uint32(0)
        status = lib.CAN_GetValue(handle, PCAN_CHANNEL_CONDITION,
                                  ctypes.byref(buf), 4)
        if status != 0:
            return COND_UNAVAILABLE, f"不可用（0x{status:08X}）"
        cond = int(buf.value)
        if cond not in COND_TEXT:
            return COND_UNAVAILABLE, f"未知状态 0x{cond:02X}"
        return cond, COND_TEXT[cond]
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def query_condition(channel_name):
    """
    查询通道占用状态（按通道名）。
    返回 (cond: int|None, text: str)
    """
    handle = channel_to_handle(channel_name)
    if handle is None:
        return None, "通道名无法识别"
    return query_condition_handle(handle)


def query_fd_capable_handle(handle):
    """按句柄查询是否支持 CAN FD（PCAN_CHANNEL_FEATURES 0x16）"""
    lib = load_lib()
    if lib is None:
        return None, _lib_error or "未找到 PCANBasic.dll"
    try:
        buf = ctypes.c_uint32(0)
        status = lib.CAN_GetValue(handle, PCAN_CHANNEL_FEATURES,
                                  ctypes.byref(buf), 4)
        if status != 0:
            return False, f"不可用（0x{status:08X}）"
        return bool(int(buf.value) & PCAN_CHANNEL_FEATURE_FD), "OK"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def query_fd_capable(channel_name):
    """返回 (是否支持 FD, 说明)"""
    handle = channel_to_handle(channel_name)
    if handle is None:
        return None, "通道名无法识别"
    return query_fd_capable_handle(handle)


def set_bitrate_adapting(channel_name, enable=True):
    """
    初始化前设置 PCAN_BITRATE_ADAPTING(0x17)。

    语义：不指定波特率，直接沿用该通道上已经生效的那一套。
    这是 PEAK 官方论坛给出的两种共享办法之一（另一种是让 PCAN-Basic
    使用与对方完全相同的 BTR0/BTR1）。

    适用边界（必须分清，否则会把「本来就不可行」误判成「代码没生效」）：
      · PCAN-View 占用 + 本应用接入 → 可行
      · 另一个 PCAN-Basic 应用占用 → 不可行。PCAN-Basic 的模型就是
        一个通道同时只有一个 PCAN-Basic 客户端，要多个独立客户端
        得用 PCAN-Developer

    返回 (ok, 说明)。
    """
    lib = load_lib()
    if lib is None:
        return False, _lib_error or "未找到 PCANBasic.dll"
    handle = channel_to_handle(channel_name)
    if handle is None:
        return False, "通道名无法识别"
    try:
        lib.CAN_SetValue.argtypes = [ctypes.c_ushort, ctypes.c_uint32,
                                     ctypes.c_void_p, ctypes.c_uint32]
        lib.CAN_SetValue.restype = ctypes.c_uint32
        val = 1 if enable else 0
        # 文档标称 bool，实际驱动对 1/2/4 字节都有接受的情况，逐个试
        for size in (1, 4, 2):
            buf = ctypes.create_string_buffer(size)
            ctypes.memset(buf, 0, size)
            buf[0] = bytes([val & 0xFF])
            st = lib.CAN_SetValue(handle, PCAN_BITRATE_ADAPTING, buf, size)
            if st == 0:
                # 注意：CAN_SetValue 对并不存在的通道也可能返回 OK，
                # 所以这里只能说「已下发」，是否真生效要看随后的初始化能否成功。
                return True, f"已下发波特率自适应（{size} 字节）"
        return False, "驱动不接受 PCAN_BITRATE_ADAPTING"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------- 通道表解析（ATTACHED_CHANNELS） ----------

def _parse_channel_table(raw, stride, wide):
    """
    按固定偏移解析 TPCANChannelInformation：
        +0  handle(WORD)  +2 device_type(BYTE)  +3 controller(BYTE)
        +4  device_features(DWORD)
        +8  device_name(char[33])
        +44 device_id(DWORD)
        +48 channel_condition(DWORD)   ← 仅 5.x（stride=52）
    """
    out = []
    if stride < _NAME_OFFSET + _NAME_LEN:
        return out
    n = len(raw) // stride
    for i in range(n):
        off = i * stride
        handle = int.from_bytes(raw[off:off + 2], "little")
        if handle == 0:
            continue
        chunk = raw[off + _NAME_OFFSET: off + _NAME_OFFSET + _NAME_LEN]
        try:
            name = chunk.decode("utf-16-le" if wide else "ascii", "ignore")
        except Exception:
            name = ""
        features = int.from_bytes(raw[off + 4:off + 8], "little")
        cond = None
        if stride >= _COND_OFFSET + 4:
            cond = int.from_bytes(
                raw[off + _COND_OFFSET:off + _COND_OFFSET + 4], "little")
            if cond not in COND_TEXT:
                cond = None          # 越界即说明步长猜错，交给逐通道查询兜底
        out.append({
            "handle": handle,
            "device_type": raw[off + 2],
            "controller": raw[off + 3],
            "features": features,
            "fd_capable": bool(features & PCAN_CHANNEL_FEATURE_FD),
            "device_id": int.from_bytes(
                raw[off + _ID_OFFSET:off + _ID_OFFSET + 4], "little"),
            "condition": cond,
            "name": name.split("\x00")[0].strip(),
        })
    return out


def _score_channel_table(entries, declared):
    """打分：句柄能映射到已知段 + 名字可读 + 状态字段有效 + 条数吻合"""
    s = 0
    for e in entries:
        if handle_to_channel(e["handle"])[0] is None:
            continue
        name = e["name"]
        if name and all(0x20 <= ord(c) < 0x7F for c in name):
            s += 2
        elif not name:
            s -= 1
        if e["controller"] <= 8:
            s += 1
        if e["condition"] is not None:
            s += 1
    if declared is not None and len(entries) == declared:
        s += 5
    return s


def attached_channels():
    """
    向驱动查询当前实际枚举出的通道表。
    返回 (ok, entries, message)，entries 每项含 handle / controller /
    features / condition / name。
    """
    lib = load_lib()
    if lib is None:
        return False, [], _lib_error or "未找到 PCANBasic.dll"

    try:
        cnt = ctypes.c_uint32(0)
        st = lib.CAN_GetValue(0, PCAN_ATTACHED_CHANNELS_COUNT,
                              ctypes.byref(cnt), ctypes.sizeof(cnt))
        declared = int(cnt.value) if st == 0 else None
        if declared == 0:
            return False, [], "本机未检测到 PCAN 通道（驱动未响应或未接设备）"

        n = declared or 64
        size = 52 * n
        buf = ctypes.create_string_buffer(size)
        st2 = lib.CAN_GetValue(0, PCAN_ATTACHED_CHANNELS,
                               ctypes.byref(buf), size)
        if st2 != 0:
            return False, [], (f"读取通道表失败（0x{st2:08X}，"
                               f"驱动自报 {declared} 个）")
        raw = buf.raw

        best, best_score, best_stride = None, -1, None
        for stride, wide in _CHANNEL_LAYOUTS:
            limit = stride * declared if declared else len(raw)
            ent = _parse_channel_table(raw[:limit], stride, wide)
            score = _score_channel_table(ent, declared)
            if score > best_score:
                best_score, best, best_stride = score, ent, stride

        if not best:
            return False, [], (f"通道表解析失败（驱动自报 {declared} 个，"
                               f"原始前 32 字节：{raw[:32].hex(' ')}）")
        return True, best, (f"驱动自报 {declared} 个，按 {best_stride} 字节"
                            f"步长解析出 {len(best)} 个")
    except Exception as e:
        return False, [], f"{type(e).__name__}: {e}"


def list_channels(include_unavailable=False):
    """
    枚举实际存在的通道。
    返回 dict: {"ok": bool, "error": str, "channels": [ {...}, ... ], "note": str}
    """
    ok, msg = dll_status()
    if not ok:
        return {"ok": False, "error": msg, "channels": [], "note": ""}

    ok2, entries, note = attached_channels()
    if not ok2:
        return {"ok": False, "error": note, "channels": [], "note": ""}

    channels = []
    for e in entries:
        h = e["handle"]
        name, bus, idx = handle_to_channel(h)
        if name is None:
            name = e["name"] or f"0x{h:03X}"
            bus, idx = "未知", 0

        # 优先用通道表自带的状态（5.x 结构体末尾的 channel_condition），
        # 省掉逐通道查询；字段缺失或值越界时才回退单独查。
        cond = e.get("condition")
        if cond is None:
            cond, ctext = query_condition_handle(h)
        else:
            ctext = COND_TEXT[cond]

        fd = e.get("fd_capable")
        if fd is None:
            fd, _ = query_fd_capable_handle(h)

        if cond == COND_UNAVAILABLE and not include_unavailable:
            continue

        channels.append({
            "handle": h,
            "name": name,
            "bus": bus,
            "index": idx,
            "label": f"{name}（通道 {idx} · {bus}）" if idx else name,
            "device_name": e["name"],
            "controller": e["controller"],
            "condition": cond,
            "condition_text": ctext,
            "fd_capable": fd,
        })
    return {"ok": True, "error": "", "channels": channels, "note": note}


# ============================ 位定时反算 ============================

def _divisors(n):
    out = []
    i = 1
    while i * i <= n:
        if n % i == 0:
            out.append(i)
            if i != n // i:
                out.append(n // i)
        i += 1
    return sorted(out)


def calc_bit_timing(f_clock, bitrate, sample_point=87.5, sjw=1,
                    min_tq=8, max_tq=25):
    """
    从时钟与目标采样点反算位定时。

    返回 dict:
      brp, tseg1, tseg2, sjw      —— 可直接写入驱动
      actual_sp                    —— 实际采样点 %
      deviation                    —— 与目标的偏差 %
      actual_bitrate               —— 实际波特率
      exact                        —— 偏差 < 0.05% 视为精确
    无可行解返回 None。
    """
    if not f_clock or not bitrate or bitrate <= 0:
        return None

    total = f_clock / float(bitrate)
    if abs(total - round(total)) > 0.5:
        # 时钟不能被波特率整除，仍按最接近的整数 tq 计算，并在 actual_bitrate 体现偏差
        pass
    total = int(round(total))
    if total < min_tq:
        return None

    best = None
    for brp in _divisors(total):
        n = total // brp
        if n < min_tq or n > max_tq:
            continue
        tseg1 = int(round(n * sample_point / 100.0)) - 1
        tseg2 = n - 1 - tseg1
        if tseg1 < 2 or tseg2 < 1:
            continue
        s = min(int(sjw) if sjw else 1, tseg1, tseg2)
        if s < 1:
            continue
        actual_sp = (1 + tseg1) / float(n) * 100.0
        dev = abs(actual_sp - sample_point)
        cand = {
            "brp": brp,
            "tseg1": tseg1,
            "tseg2": tseg2,
            "sjw": s,
            "actual_sp": round(actual_sp, 2),
            "deviation": round(dev, 2),
            "actual_bitrate": int(f_clock // (brp * n)),
            "exact": dev < 0.05,
        }
        if best is None or dev < best["deviation"] - 1e-9:
            best = cand
        elif abs(dev - best["deviation"]) < 1e-9 and n > (1 + best["tseg1"] + best["tseg2"]):
            best = cand
    return best


def timing_from_parts(f_clock, brp, tseg1, tseg2, sjw=1):
    """
    手动模式：由分频参数反算位定时，不再走自动搜索。

        bitrate      = f_clock / (brp * (1 + tseg1 + tseg2))
        sample_point = (1 + tseg1) / (1 + tseg1 + tseg2) * 100

    返回结构与 calc_bit_timing 一致；参数非法返回 None。
    deviation 置 0——手动模式下没有"目标偏差"可言，用户填什么就是什么。
    """
    try:
        f_clock = int(f_clock)
        brp = int(brp)
        tseg1 = int(tseg1)
        tseg2 = int(tseg2)
        sjw = max(int(sjw), 1)
    except (TypeError, ValueError):
        return None
    if f_clock <= 0 or brp < 1 or tseg1 < 1 or tseg2 < 1:
        return None
    total = 1 + tseg1 + tseg2
    if total < 3:
        return None
    bitrate = f_clock / (brp * total)
    sp = (1 + tseg1) / total * 100.0
    return {
        "brp": brp, "tseg1": tseg1, "tseg2": tseg2, "sjw": sjw,
        "actual_sp": round(sp, 4), "deviation": 0.0,
        "actual_bitrate": int(round(bitrate)), "exact": True,
        "manual": True,
    }


def describe_timing(t):
    """把反算结果转成一行可读文本"""
    if not t:
        return "无可行解"
    s = (f"brp={t['brp']}  tseg1={t['tseg1']}  tseg2={t['tseg2']}  "
         f"实际 {t['actual_sp']:.2f}%")
    if t["deviation"] >= 1.0:
        s += f"  ⚠ 偏差 {t['deviation']:.2f}%"
    elif not t["exact"]:
        s += f"  （偏差 {t['deviation']:.2f}%）"
    return s


# ============================ 配置模型 ============================

class CanConfig:
    """一次连接的完整参数。所有派生值由 recompute() 计算，界面只读写字段。"""

    def __init__(self):
        self.channel = "PCAN_PCIBUS1"
        self.frame_mode = "classic"       # classic | fd
        self.bus_mode = "active"          # active | listen_only（默认正常收发）
        self.bus_mode_forced = False      # 是否因占用被强制降级
        self.f_clock = DEFAULT_F_CLOCK

        # 手动位定时：勾选后直接用 brp/seg1/seg2，不再自动反算
        self.nom_manual = False
        self.nom_brp = None
        self.nom_seg1 = None
        self.nom_seg2 = None
        self.data_manual = False
        self.data_brp = None
        self.data_seg1 = None
        self.data_seg2 = None

        self.nom_bitrate = DEFAULT_CLASSIC["bitrate"]
        self.nom_sample_point = DEFAULT_CLASSIC["sample_point"]
        self.nom_sjw = DEFAULT_CLASSIC["sjw"]

        self.data_bitrate = DEFAULT_FD_DATA["bitrate"]
        self.data_sample_point = DEFAULT_FD_DATA["sample_point"]
        self.data_sjw = DEFAULT_FD_DATA["sjw"]

        self.nom_timing = None
        self.data_timing = None
        self.recompute()

    # ---- 派生 ----

    @property
    def is_fd(self):
        return self.frame_mode == "fd"

    @property
    def listen_only(self):
        return self.bus_mode == "listen_only"

    def recompute(self):
        # 经典 CAN 的硬件时钟固定 8 MHz，界面上选的 80/40 MHz 只对 FD 有意义。
        # 这里按实际硬件时钟算，保证显示值与真正下发的位定时一致。
        clock = self.f_clock if self.is_fd else 8_000_000
        if self.nom_manual and self.nom_brp and self.nom_seg1 and self.nom_seg2:
            self.nom_timing = timing_from_parts(
                clock, self.nom_brp, self.nom_seg1,
                self.nom_seg2, self.nom_sjw)
        else:
            self.nom_timing = calc_bit_timing(
                clock, self.nom_bitrate, self.nom_sample_point, self.nom_sjw)
        if self.is_fd:
            if (self.data_manual and self.data_brp and self.data_seg1
                    and self.data_seg2):
                self.data_timing = timing_from_parts(
                    self.f_clock, self.data_brp, self.data_seg1,
                    self.data_seg2, self.data_sjw)
            else:
                self.data_timing = calc_bit_timing(
                    self.f_clock, self.data_bitrate,
                    self.data_sample_point, self.data_sjw)
        else:
            self.data_timing = None
        return self.nom_timing, self.data_timing

    def set_fd_defaults(self):
        """切到 CAN FD 时，仲裁段沿用经典默认、数据段用 FD 默认"""
        self.nom_sample_point = DEFAULT_FD_NOMINAL["sample_point"]
        self.data_bitrate = DEFAULT_FD_DATA["bitrate"]
        self.data_sample_point = DEFAULT_FD_DATA["sample_point"]

    def problems(self):
        """返回会阻止连接的严重问题列表"""
        out = []
        if self.nom_timing is None:
            out.append("仲裁段位定时无可行解，请调整波特率或采样点")
        elif (not self.nom_manual
              and self.nom_timing["actual_bitrate"] != self.nom_bitrate):
            out.append(f"仲裁段实际速率为 {self.nom_timing['actual_bitrate']}，"
                       f"与目标 {self.nom_bitrate} 不一致")
        if self.is_fd:
            if self.data_timing is None:
                out.append("数据段位定时无可行解")
            elif (not self.data_manual
                  and self.data_timing["actual_bitrate"] != self.data_bitrate):
                out.append(f"数据段实际速率为 {self.data_timing['actual_bitrate']}，"
                           f"与目标 {self.data_bitrate} 不一致")
        return out

    def summary(self):
        """工具条摘要：PCAN_PCIBUS1 · 500k/2M · 只监听"""
        t = self.nom_timing
        nb = (t["actual_bitrate"] if t else self.nom_bitrate)
        speed = f"{nb // 1000}k"
        if self.is_fd:
            dt = self.data_timing
            db = (dt["actual_bitrate"] if dt else self.data_bitrate)
            if db >= 1_000_000:
                speed += f"/{db // 1_000_000}M"
            else:
                speed += f"/{db // 1000}k"
        mode = "只监听" if self.listen_only else "正常"
        manual = "·手动" if (self.nom_manual or (self.is_fd and self.data_manual)) else ""
        return f"{self.channel} · {speed}{manual} · {mode}"

    def copy(self):
        c = CanConfig()
        c.__dict__.update(self.__dict__)
        c.recompute()
        return c

