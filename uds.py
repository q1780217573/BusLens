# -*- coding: utf-8 -*-
"""
UDS 诊断（ISO 14229）—— 基于 ISO-TP（ISO 15765-2）在经典 CAN / CAN FD 上收发。

与界面完全解耦：
  · 发送通过注入的 send_fn(arbitration_id, data)
  · 接收通过 on_frame(msg) 由外部接收线程分发（不直接碰 can.Bus）

支持：
  · 经典 CAN（8 字节帧）与 CAN FD（8/12/16/20/24/32/48/64 字节帧）
  · 单帧 DL ≤ 7 / ≤ 62，首帧 12-bit 与 32-bit 转义长度，连续帧 7 / 63 字节
  · 功能寻址（多 ECU 应答汇总）
  · 0x34 → 0x36（块序号自动递增）→ 0x37 刷写链路
"""

import threading
import time

# ---- ISO-TP PCI ----
PCI_SF = 0x0
PCI_FF = 0x1
PCI_CF = 0x2
PCI_FC = 0x3

FS_CTS = 0x0
FS_WAIT = 0x1
FS_OVFLW = 0x2

# CAN FD 有效帧长（DLC 对应的字节数）
FD_FRAME_LENS = (8, 12, 16, 20, 24, 32, 48, 64)

NRC_TEXT = {
    0x10: "通用拒绝",
    0x11: "服务不支持",
    0x12: "子功能不支持或格式不对",
    0x13: "长度或格式不对",
    0x21: "忙，请重试",
    0x22: "条件不满足",
    0x24: "请求序列错误",
    0x25: "请求序列错误（无响应本请求）",
    0x31: "请求超出范围",
    0x33: "安全访问被拒",
    0x34: "认证失败",
    0x35: "密钥无效",
    0x36: "超过尝试次数",
    0x37: "延时未到",
    0x38: "超过延时次数",
    0x3A: "所需时间未到",
    0x50: "安全访问未通过",
    0x70: "上传下载不被接受",
    0x71: "传输数据暂停",
    0x72: "编程失败",
    0x73: "块序号错误",
    0x78: "响应挂起",
    0x7E: "当前会话不支持该子功能",
    0x7F: "当前会话不支持该服务",
    0x92: "电压过高",
    0x93: "电压过低",
}

SERVICE_GROUPS = [
    ("会话与安全", [
        ("10 诊断会话控制", "10 03"),
        ("11 ECU 重置", "11 01"),
        ("27 安全访问（请求种子）", "27 01"),
        ("29 认证", "29 01"),
        ("3E 待机握手", "3E 00"),
        ("28 通讯控制", "28 00 01"),
        ("83 访问计时参数", "83 00"),
        ("85 控制 DTC 设置", "85 02"),
    ]),
    ("数据读写", [
        ("22 按标识符读数据", "22 F1 90"),
        ("2E 按标识符写数据", "2E F1 90 00"),
        ("2F 按标识符 IO 控制", "2F F1 90 00 00"),
        ("2A 按周期标识读取", "2A 00 64 F1 90"),
        ("24 按标识符读缩放信息", "24 F1 90"),
        ("2C 动态定义数据标识", "2C 03 F1 90 01 64"),
        ("23 按地址读内存", "23 24 00 00 00 10 00 02"),
        ("3D 按地址写内存", "3D 24 00 00 00 10 00 02 00"),
    ]),
    ("DTC", [
        ("14 清除诊断信息", "14 FF FF FF"),
        ("19 读取 DTC 信息", "19 02 FF"),
        ("86 基于事件响应", "86 00"),
    ]),
    ("例程与传输", [
        ("31 例程控制", "31 01 FF 00"),
        ("34 请求下载", "34 00 44 00 00 00 10 00 00 00 10"),
        ("35 请求上传", "35 00 44 00 00 00 10 00 00 00 10"),
        ("36 传输数据", "36 01 00 00 00 00 00 00 00"),
        ("37 请求传输退出", "37"),
        ("38 请求文件传输", "38 01 00 00"),
        ("84 受保护数据", "84 00"),
        ("87 链路控制", "87 02"),
    ]),
]

SERVICE_TEMPLATES = [(n, t) for _, items in SERVICE_GROUPS for n, t in items]


class IsoTpError(Exception):
    pass


def hex_str(b, sep=" "):
    return sep.join(f"{x:02X}" for x in b)


def parse_hex(txt):
    t = (txt or "").replace(",", " ").replace("0x", "").replace("0X", "")
    t = "".join(t.split())
    if not t:
        return b""
    if len(t) % 2:
        raise ValueError("十六进制位数必须是偶数")
    return bytes.fromhex(t)


def frame_len_for(use_fd, length):
    """把要发的字节数对齐到合法的 CAN FD 帧长；经典 CAN 恒为 8"""
    if not use_fd:
        return 8
    for n in FD_FRAME_LENS:
        if length <= n:
            return n
    return 64


class IsoTpChannel:
    """
    ISO-TP 通道。frame_len=8 走经典 CAN；>8 走 CAN FD（单帧最多 62 字节、
    连续帧每帧 63 字节）。padding=True 时不足帧长补 0x00，多数 ECU 接受。

    functional=True 时不校验 rx_id，收集窗口内所有 ECU 的应答（0x7DF 广播）。
    """

    def __init__(self, send_fn, tx_id, rx_id, timeout=1.0, padding=True,
                 frame_len=8, functional=False):
        self.send_fn = send_fn
        self.tx_id = tx_id
        self.rx_id = rx_id
        self.timeout = timeout
        self.padding = padding
        self.frame_len = frame_len
        self.functional = functional

        self._buf = bytearray()
        self._expect = 0
        self._done = False
        self._evt = threading.Event()
        self._fc_evt = threading.Event()
        self._fc_status = None
        self._got_pending = False       # 是否收到过 7F xx 78
        self._multi = {}                # functional: rx_id -> 已完成 payload
        self._multi_evt = None

    # ---- 发送 ----
    def _frame(self, b):
        if self.padding:
            n = frame_len_for(self.frame_len > 8, len(b))
            if len(b) < n:
                b = bytes(b) + bytes(n - len(b))
        self.send_fn(self.tx_id, bytes(b))

    def _send_fc(self):
        try:
            fl = frame_len_for(self.frame_len > 8, 8)
            self._frame(bytes([0x30, 0x00, 0x00, 0, 0, 0, 0, 0])[:fl])
        except Exception:
            pass

    def _finish(self, payload):
        """收齐一帧响应。7F xx 78 是「响应挂起」，要接着等而不是当最终结果"""
        payload = bytes(payload)
        if len(payload) == 3 and payload[0] == 0x7F and payload[2] == 0x78:
            self._buf = bytearray()
            self._expect = 0
            self._got_pending = True
            return
        self._buf = bytearray(payload)
        self._done = True
        self._evt.set()

    # ---- 接收 ----
    def on_frame(self, msg):
        if not self.functional and msg.arbitration_id != self.rx_id:
            return
        d = bytes(msg.data)
        if not d:
            return
        pci = d[0] >> 4
        if pci == PCI_SF:
            n = d[0] & 0x0F
            if n == 0 and len(d) > 1:
                # CAN FD 单帧转义：0x00 + 长度字节
                n = d[1]
                payload = d[2:2 + n]
            else:
                payload = d[1:1 + n] if n else b""
            if self.functional:
                self._multi[msg.arbitration_id] = bytes(payload)
                if self._multi_evt is not None:
                    self._multi_evt.set()
            else:
                self._finish(payload)
        elif pci == PCI_FF:
            ln = ((d[0] & 0x0F) << 8) | d[1]
            if ln == 0 and len(d) >= 6:
                # CAN FD 首帧 32-bit 转义长度
                ln = int.from_bytes(d[2:6], "big")
                self._expect = ln
                self._buf = bytearray(d[6:])
            else:
                self._expect = ln
                self._buf = bytearray(d[2:])
            self._send_fc()
            if self._expect and len(self._buf) >= self._expect:
                self._finish(bytes(self._buf[:self._expect]))
        elif pci == PCI_CF:
            if not self._expect:
                return
            self._buf.extend(d[1:])
            if len(self._buf) >= self._expect:
                self._finish(bytes(self._buf[:self._expect]))
        elif pci == PCI_FC:
            self._fc_status = d[0] & 0x0F
            self._fc_evt.set()

    # ---- 请求 ----
    def _send_pdu(self, payload):
        """只负责把 PDU 拆成 ISO-TP 帧发出去，不等响应"""
        n = len(payload)
        if n == 0:
            raise IsoTpError("请求 PDU 为空")

        max_sf = 7 if self.frame_len <= 8 else 62
        if n > max_sf:
            if n <= 0xFFF:
                head = bytes([0x10 | ((n >> 8) & 0x0F), n & 0xFF])
                first = payload[:6]
                off = 6
            else:
                head = bytes([0x10, 0x00])
                first = n.to_bytes(4, "big") + payload[:self.frame_len - 6]
                off = self.frame_len - 6
            self._frame(head + first)
            got_fc = self._fc_evt.wait(min(self.timeout, 0.2))
            if got_fc and self._fc_status not in (None, FS_CTS, FS_WAIT):
                raise IsoTpError(f"对端拒绝接收：FS={self._fc_status}")
            rest = payload[off:]
            seq = 1
            i = 0
            step = max(self.frame_len - 1, 1)
            while i < len(rest):
                chunk = rest[i:i + step]
                self._frame(bytes([0x20 | (seq & 0x0F)]) + chunk)
                seq = (seq + 1) & 0x0F
                i += step
                time.sleep(0.001)
        elif n <= 7:
            self._frame(bytes([n]) + payload)
        else:
            # CAN FD 单帧转义：PCI=0x00 + 长度字节
            self._frame(bytes([0x00, n]) + payload)

    def request(self, payload, timeout=None):
        to = self.timeout if timeout is None else timeout
        self._buf = bytearray()
        self._expect = 0
        self._done = False
        self._got_pending = False
        self._evt.clear()
        self._fc_evt.clear()
        self._fc_status = None

        self._send_pdu(bytes(payload))

        # 收到 7F xx 78（响应挂起）时要继续等，最多延长 5 轮，
        # 每轮按 P2* 给足 2 秒——刷写类服务经常这么回应。
        deadline = time.time() + to
        rounds = 0
        while True:
            remaining = max(deadline - time.time(), 0.01)
            if self._evt.wait(remaining):
                return bytes(self._buf)
            if self._got_pending and rounds < 5:
                rounds += 1
                self._got_pending = False
                deadline = time.time() + max(to, 2.0)
                continue
            raise IsoTpError("等待响应超时")

    # ---- 功能寻址：收集窗口内的多 ECU 应答 ----
    def request_multi(self, payload, window=0.5):
        self._multi = {}
        self._multi_evt = threading.Event()
        try:
            self._send_pdu(bytes(payload))
        except IsoTpError:
            self._multi_evt = None
            raise
        # 发完就开窗口收，不白等单个响应
        deadline = time.time() + window
        while time.time() < deadline:
            self._multi_evt.clear()
            self._multi_evt.wait(max(deadline - time.time(), 0.01))
        self._multi_evt = None
        return dict(self._multi)


class UdsClient:
    """UDS 请求/响应。request() 阻塞到收到响应或超时。"""

    def __init__(self, send_fn, tx_id=0x704, rx_id=0x705,
                 timeout=1.0, padding=True, frame_len=8, functional=False):
        self.tp = IsoTpChannel(send_fn, tx_id, rx_id, timeout, padding,
                               frame_len, functional)
        self.timeout = timeout

    def set_ids(self, tx_id, rx_id):
        self.tp.tx_id = tx_id
        self.tp.rx_id = rx_id

    def set_timeout(self, timeout):
        self.timeout = timeout
        self.tp.timeout = timeout

    def set_frame_len(self, frame_len):
        self.tp.frame_len = max(int(frame_len), 8)

    def set_functional(self, on):
        self.tp.functional = bool(on)

    def on_frame(self, msg):
        self.tp.on_frame(msg)

    def _wrap(self, pdu, resp, t0, check_response=True):
        el = time.time() - t0
        if not resp:
            return {"req": pdu, "resp": b"", "status": "fail",
                    "note": "空响应", "elapsed": el}
        if resp[0] == 0x7F:
            nrc = resp[2] if len(resp) > 2 else 0
            note = f"负响应 NRC=0x{nrc:02X}"
            if nrc in NRC_TEXT:
                note += f" {NRC_TEXT[nrc]}"
            return {"req": pdu, "resp": resp, "status": "fail",
                    "note": note, "elapsed": el}
        if not check_response:
            return {"req": pdu, "resp": resp, "status": "unchecked",
                    "note": "未校验", "elapsed": el}
        if (pdu[0] + 0x40) == resp[0]:
            return {"req": pdu, "resp": resp, "status": "pass",
                    "note": "正响应", "elapsed": el}
        return {"req": pdu, "resp": resp, "status": "fail",
                "note": f"响应 SID 0x{resp[0]:02X} 与请求不匹配", "elapsed": el}

    def request(self, pdu, check_response=True):
        """
        返回 dict：req / resp / status / note / elapsed
        status: pass（正响应）/ fail（负响应、超时、SID 不匹配）/ unchecked（没校验）
        """
        t0 = time.time()
        pdu = bytes(pdu)
        try:
            resp = self.tp.request(pdu)
        except IsoTpError as e:
            return {"req": pdu, "resp": b"", "status": "fail",
                    "note": str(e), "elapsed": time.time() - t0}
        except Exception as e:
            return {"req": pdu, "resp": b"", "status": "fail",
                    "note": f"{type(e).__name__}: {e}",
                    "elapsed": time.time() - t0}
        return self._wrap(pdu, resp, t0, check_response)

    def request_multi(self, pdu, window=0.5, check_response=True):
        """功能寻址：返回多条结果，每条带 rx_id"""
        t0 = time.time()
        pdu = bytes(pdu)
        try:
            got = self.tp.request_multi(pdu, window)
        except Exception as e:
            return [{"req": pdu, "resp": b"", "status": "fail",
                     "note": f"{type(e).__name__}: {e}",
                     "elapsed": time.time() - t0, "rx_id": None}]
        if not got:
            return [{"req": pdu, "resp": b"", "status": "fail",
                     "note": "窗口内无任何 ECU 应答",
                     "elapsed": time.time() - t0, "rx_id": None}]
        out = []
        for rx_id, resp in sorted(got.items()):
            d = self._wrap(pdu, resp, t0, check_response)
            d["rx_id"] = rx_id
            out.append(d)
        return out


# ============================ 会话与安全访问 ============================

SESSION_NAME = {0x01: "默认会话", 0x02: "编程会话", 0x03: "扩展会话"}

# 各服务的参数表单定义：key / 标签 / 类型 / 选项 / 默认
# type: "select" 下拉, "text" 输入
SERVICE_UI = {
    0x10: [("mode", "会话类型", "select",
            ["01 默认会话", "02 编程会话", "03 扩展会话"], "03")],
    0x11: [("kind", "复位类型", "select",
            ["01 硬复位", "02 钥匙 OFF/ON", "03 软复位"], "01")],
    0x14: [("group", "DTC 组(hex)", "text", None, "FFFFFF")],
    0x19: [("sub", "子功能", "select",
            ["01 按掩码报数量", "02 按状态掩码", "03 快照标识",
             "04 快照记录", "06 扩展数据记录", "0A 支持的 DTC",
             "0B 首个失败 DTC", "0C 首个已确认 DTC",
             "0D 最近失败 DTC", "0E 最近已确认 DTC",
             "0F 本次循环失败 DTC", "10 本次循环已确认 DTC"], "02"),
           ("mask", "状态掩码(hex)", "text", None, "FF"),
           ("rec", "记录号/DTC(hex)", "text", None, "01")],
    0x22: [("did", "DID(hex，空格分隔多个)", "text", None, "F190")],
    0x27: [("level", "安全等级", "select",
            ["01", "03", "05", "07", "09", "0B", "0D", "0F",
             "11", "13", "15", "17"], "01"),
           ("alg", "Key 算法(Python)", "text", None,
            "bytes([(b ^ 0x5A) for b in seed])")],
    0x28: [("ctrl", "控制类型", "select",
            ["00 使能 RX/TX", "01 禁止 RX/TX", "02 禁止 RX", "03 使能 RX"], "00"),
           ("comm", "通信类型(hex，可多字节)", "text", None, "01")],
    0x29: [("sub", "子功能", "select",
            ["00 取消认证", "01 请求证书", "02 发送挑战",
             "03 发送证明", "04 发送令牌", "05 校验令牌"], "01"),
           ("data", "数据(hex)", "text", None, "")],
    0x2E: [("did", "DID(hex)", "text", None, "F190"),
           ("data", "数据(hex)", "text", None, "00")],
    0x31: [("kind", "例程类型", "select",
            ["01 启动", "02 停止", "03 查询结果"], "01"),
           ("rid", "例程 ID(hex)", "text", None, "FF00"),
           ("data", "附加数据(hex)", "text", None, "")],
    0x3E: [("sub", "子功能", "select",
            ["00 需要响应", "80 抑制响应"], "00")],
    0x85: [("kind", "控制类型", "select", ["01 ON", "02 OFF"], "02")],
    0x23: [("alfid", "ALFID(hex)", "text", None, "24"),
           ("addr", "地址(hex)", "text", None, "00000010"),
           ("size", "长度(hex)", "text", None, "0002")],
    0x24: [("did", "DID(hex)", "text", None, "F190")],
    0x2A: [("period", "周期(hex)", "text", None, "0064"),
           ("did", "DID(hex)", "text", None, "F190")],
    0x2C: [("ctrl", "控制类型", "select",
            ["01 添加", "02 删除", "03 清除"], "03"),
           ("did", "DID(hex)", "text", None, "F190"),
           ("sub", "子作用域节点数(hex)", "text", None, "01"),
           ("period", "周期(hex)", "text", None, "0064")],
    0x2F: [("did", "DID(hex)", "text", None, "F190"),
           ("opt", "控制参数(hex)", "text", None, "00"),
           ("val", "状态值(hex)", "text", None, "00")],
    0x34: [("dfi", "数据格式标识(hex)", "text", None, "00"),
           ("alfid", "ALFID(hex)", "text", None, "44"),
           ("addr", "地址(hex)", "text", None, "00000010"),
           ("size", "大小(hex)", "text", None, "00000010")],
    0x35: [("dfi", "数据格式标识(hex)", "text", None, "00"),
           ("alfid", "ALFID(hex)", "text", None, "44"),
           ("addr", "地址(hex)", "text", None, "00000010"),
           ("size", "大小(hex)", "text", None, "00000010")],
    0x36: [("bsc", "块序号(hex，留空自动递增)", "text", None, ""),
           ("data", "数据(hex，留空则按上次 34 的块长自动分块)", "text", None, "")],
    0x37: [],
    0x38: [("mode", "模式", "select",
            ["01 添加文件", "02 删除文件", "03 替换文件",
             "04 读取文件", "05 读取目录"], "01"),
           ("pathlen", "长度格式(hex)", "text", None, "40"),
           ("dfi", "数据格式标识(hex)", "text", None, "00"),
           ("size", "文件大小(hex)", "text", None, "00000010"),
           ("path", "文件路径(ASCII)", "text", None, ""),
           ("data", "数据(hex，可选)", "text", None, "")],
    0x3D: [("alfid", "ALFID(hex)", "text", None, "24"),
           ("addr", "地址(hex)", "text", None, "00000010"),
           ("size", "大小(hex)", "text", None, "0002"),
           ("data", "数据(hex)", "text", None, "00")],
    0x83: [("sub", "子功能", "select",
            ["00 读取", "01 设置", "02 恢复默认"], "00"),
           ("param", "参数(hex)", "text", None, "01")],
    0x84: [("sub", "子功能", "select", ["00 读取", "01 设置"], "00")],
    0x86: [("sub", "子功能", "select", ["00 停止", "01 开始"], "00"),
           ("etype", "事件类型(hex)", "text", None, "01")],
    0x87: [("sub", "链路控制", "select",
            ["01 验证固定波特率", "02 转换波特率", "03 固定波特率"], "02")],
}


# ---- DTC 解析 ----
DTC_STATUS_BITS = [
    (0x01, "testFailed"),
    (0x02, "testFailedThisCycle"),
    (0x04, "pendingDTC"),
    (0x08, "confirmedDTC"),
    (0x10, "notCompletedSinceClear"),
    (0x20, "failedSinceClear"),
    (0x40, "notCompletedThisCycle"),
    (0x80, "warningIndicator"),
]


def dtc_code(b1, b2, b3):
    """3 字节 DTC → P0301 这种可读写法"""
    letter = "PCBU"[(b1 >> 6) & 0x03]
    d1 = (b1 >> 4) & 0x03
    rest = ((b1 & 0x0F) << 8) | b2
    return f"{letter}{d1}{rest:03X}{b3:02X}"


def parse_dtc(resp):
    """19 02 / 0A / 0B 等响应 → [{"code","status","bits"}]"""
    if len(resp) < 7 or resp[0] != 0x59:
        return []
    out = []
    i = 3
    # 19 01 的响应是「计数」格式：59 01 mask count(2字节)
    if len(resp) == 6 and resp[1] in (0x01,):
        return []
    while i + 4 <= len(resp):
        b1, b2, b3, st = resp[i], resp[i + 1], resp[i + 2], resp[i + 3]
        out.append({
            "code": dtc_code(b1, b2, b3),
            "status": st,
            "bits": [n for m, n in DTC_STATUS_BITS if st & m],
        })
        i += 4
    return out


def parse_dtc_count(resp):
    """19 01 响应 → DTC 个数"""
    if len(resp) >= 6 and resp[0] == 0x59 and resp[1] == 0x01:
        return (resp[3] << 8) | resp[4]
    return None


def parse_did(resp):
    """22 响应 → [(did, data)]；支持一次读多个 DID"""
    if len(resp) < 3 or resp[0] != 0x62:
        return []
    out = []
    i = 1
    while i + 2 <= len(resp):
        did = (resp[i] << 8) | resp[i + 1]
        i += 2
        # 下一个 DID 的边界不可知，整段归当前 DID：只取第一段最可靠
        out.append((did, bytes(resp[i:])))
        break
    return out


def parse_transfer_max_block(resp):
    """
    0x74 响应里的 maxNumberOfBlockLength：
      74 <lenFormatId> <maxBlockLength 1~4 字节>
    lenFormatId 高 4 位 = maxBlockLength 的字节数，低 4 位 = 地址/长度参数长度
    返回 (max_block, extra)
    """
    if len(resp) < 3 or resp[0] != 0x74:
        return None, 0
    lf = resp[1]
    n = (lf >> 4) & 0x0F
    if n == 0:
        return None, 0
    n = min(n, 4)
    body = resp[2:2 + n]
    if len(body) < n:
        return None, 0
    return int.from_bytes(body, "big"), n


class UdsSession:
    """
    会话与安全访问状态 + 常用服务封装。

    只负责"正确地把服务组织成 PDU 并维护状态"，收发仍走 UdsClient。
    """

    def __init__(self, client):
        self.client = client
        self.session = None          # 当前会话子功能值
        self.unlocked = False        # 是否已通过安全访问
        self.level = None            # 已解锁的安全等级
        self.bsc = 0                 # 0x36 块序号
        self.max_block = None        # 0x34 返回的 maxNumberOfBlockLength
        self._pending = b""          # 待传输数据（34 之后由 36 分块取用）

    # ---- 状态 ----
    def reset_state(self):
        self.session = None
        self.unlocked = False
        self.level = None
        self.bsc = 0
        self.max_block = None
        self._pending = b""

    def state_text(self):
        s = SESSION_NAME.get(self.session, "未知" if self.session else "未连接")
        if self.session is None:
            s = "—"
        sec = (f"已解锁 L{self.level:02X}" if self.unlocked
               else "未解锁")
        extra = ""
        if self.max_block:
            extra = f" · 块长 {self.max_block}"
        return f"会话 {s} · 安全 {sec}{extra}"

    # ---- 服务 ----
    def diag_session(self, mode):
        res = self.client.request(bytes([0x10, mode & 0xFF]))
        if res["status"] == "pass" and len(res["resp"]) >= 2:
            self.session = res["resp"][1]
            if self.session == 0x01:
                self.unlocked = False
                self.level = None
        return res

    def tester_present(self, suppress=True):
        sub = 0x80 if suppress else 0x00
        return self.client.request(bytes([0x3E, sub]))

    def ecu_reset(self, kind=0x01):
        return self.client.request(bytes([0x11, kind & 0xFF]))

    def clear_dtc(self, group=0xFFFFFF):
        return self.client.request(bytes([0x14, (group >> 16) & 0xFF,
                                          (group >> 8) & 0xFF, group & 0xFF]))

    def read_dtc(self, sub=0x02, mask=0xFF):
        return self.client.request(bytes([0x19, sub & 0xFF, mask & 0xFF]))

    def read_did(self, did):
        return self.client.request(bytes([0x22, (did >> 8) & 0xFF, did & 0xFF]))

    def read_dids(self, dids):
        pdu = bytes([0x22])
        for d in dids:
            pdu += bytes([(d >> 8) & 0xFF, d & 0xFF])
        return self.client.request(pdu)

    def write_did(self, did, data):
        return self.client.request(bytes([0x2E, (did >> 8) & 0xFF,
                                          did & 0xFF]) + bytes(data))

    def routine_control(self, kind, rid, data=b""):
        return self.client.request(bytes([0x31, kind & 0xFF,
                                          (rid >> 8) & 0xFF, rid & 0xFF])
                                   + bytes(data))

    def comm_control(self, ctrl, comm=0x01):
        return self.client.request(bytes([0x28, ctrl & 0xFF, comm & 0xFF]))

    def control_dtc_setting(self, kind):
        return self.client.request(bytes([0x85, kind & 0xFF]))

    # ---- 刷写链路 ----
    def request_download(self, dfi, alfid, addr, size):
        res = self.client.request(bytes([0x34, dfi & 0xFF, alfid & 0xFF])
                                  + bytes(addr) + bytes(size))
        if res["status"] == "pass":
            mb, _ = parse_transfer_max_block(res["resp"])
            self.max_block = mb
            self.bsc = 0
        return res

    def request_upload(self, dfi, alfid, addr, size):
        res = self.client.request(bytes([0x35, dfi & 0xFF, alfid & 0xFF])
                                  + bytes(addr) + bytes(size))
        if res["status"] == "pass":
            mb, _ = parse_transfer_max_block(res["resp"])
            self.max_block = mb
            self.bsc = 0
        return res

    def set_pending(self, data):
        self._pending = bytes(data or b"")

    def take_pending(self, n):
        """从待传缓冲取 n 字节"""
        n = max(int(n or 1), 1)
        out = self._pending[:n]
        self._pending = self._pending[n:]
        return out

    def pending_len(self):
        return len(self._pending)

    def next_bsc(self):
        self.bsc = (self.bsc % 0xFF) + 1
        return self.bsc

    def transfer_data(self, data=b"", bsc=None):
        """0x36 传输数据。bsc 留空自动递增（1..255 循环）"""
        seq = self.next_bsc() if bsc is None else (bsc & 0xFF)
        return self.client.request(bytes([0x36, seq]) + bytes(data))

    def transfer_exit(self, data=b""):
        return self.client.request(bytes([0x37]) + bytes(data))

    def security_access(self, level, key_source=None):
        """
        0x27：请求种子 → 算 key → 发送 key。
        key_source 可以是 callable(seed)->bytes，也可以是 Python 表达式字符串
        （环境里提供 seed 与 bytes）。返回 dict 汇总两步结果。
        """
        seed_res = self.client.request(bytes([0x27, level & 0xFF]))
        out = {"seed_req": seed_res, "key_req": None, "seed": b"",
               "key": b"", "status": "fail", "note": ""}
        if seed_res["status"] != "pass" or len(seed_res["resp"]) < 2:
            out["note"] = "请求种子失败：" + seed_res["note"]
            return out
        seed = seed_res["resp"][2:]
        out["seed"] = seed
        if seed and all(b == 0 for b in seed):
            self.unlocked = True
            self.level = level
            out["status"] = "pass"
            out["note"] = "ECU 返回全 0 种子，已处于解锁状态"
            return out

        key = None
        if callable(key_source):
            try:
                key = bytes(key_source(seed))
            except Exception as e:
                out["note"] = f"密钥算法出错：{type(e).__name__}: {e}"
                return out
        elif isinstance(key_source, str) and key_source.strip():
            try:
                val = eval(key_source, {"__builtins__": {}},
                           {"seed": seed, "bytes": bytes, "bytearray": bytearray})
                key = bytes(val) if val is not None else None
            except Exception as e:
                out["note"] = f"密钥算法表达式出错：{type(e).__name__}: {e}"
                return out
        if not key:
            out["note"] = "没有可用的 key（算法为空或结果为空）"
            return out
        out["key"] = key

        key_res = self.client.request(bytes([0x27, (level + 1) & 0xFF]) + key)
        out["key_req"] = key_res
        out["status"] = key_res["status"]
        out["note"] = key_res["note"]
        if key_res["status"] == "pass":
            self.unlocked = True
            self.level = level
        return out