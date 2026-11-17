# BusLens

> CAN / CAN FD 总线分析与诊断工具 ｜ CANova · 观澜
>
> A Windows GUI tool for CAN / CAN FD bus analysis, DBC decoding, live monitoring and UDS diagnostics.

BusLens 是一款面向汽车电子测试的 CAN 总线工具，把日常排查中分散的几件事收在一个界面里：DBC 解析、实时监控、报文发送、数据回放、UDS 诊断、日志格式转换。

设计上对标 TUMOS TCANLINPro 与 PCAN-View，保持单文件绿色运行：不需要安装，不写注册表。

---

## 功能

| 功能 | 说明 |
|---|---|
| **DBC 解析** | 按 DBC 定义把原始字节还原成信号物理值，支持 Intel / Motorola 位序、有符号数、多路复用、值表枚举 |
| **实时监控** | 聚合视图（按 ID 统计周期/计数/变化次数）与逐帧视图（完整流水），信号表附原始字节条 |
| **报文发送** | 按 DBC 信号组帧发送，或直接发原始帧；支持并行/顺序模式、循环发送、列表导入导出 |
| **数据回放** | 把 TRC / ASC / BLF 按原时间戳重放，支持速度倍率、循环、暂停 |
| **CAN FD** | 完整支持，含数据段独立波特率与采样点配置 |
| **UDS 诊断** | ISO-TP 分包收发，27 个诊断服务图形化，支持功能寻址多 ECU 应答、0x78 挂起、DTC 解析 |
| **格式转换** | TRC / ASC / BLF 互转 |
| **通道配置** | 自动反算位定时，也可手动指定 BRP / SEG1 / SEG2 / SJW；支持只监听模式 |
| **文件拖放** | DBC 与日志文件拖入窗口任意位置即可导入（原生 WM_DROPFILES） |

---

## 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11（64 位） |
| Python | 3.10+（仅源码运行需要） |
| 硬件驱动 | PEAK PCAN 驱动（使用 PCAN 硬件时必需） |
| 权限 | **普通权限运行**，不要用管理员（原因见「已知限制」） |

依赖见 `requirements.txt`：

```
python-can>=4.6.1
customtkinter>=5.2.2
pillow>=11.0.0
tkinterdnd2>=0.6.2
```

---

## 快速开始

### 方式一：源码运行

```bash
git clone https://github.com/q1780217573/BusLens.git
cd BusLens
pip install -r requirements.txt
python dbc_parser.py
```

### 方式二：打包成单文件 exe

```bash
pip install pyinstaller
pyinstaller dbc_parser.spec --noconfirm
```

产物在 `dist/BusLens.exe`，约 30 MB，可单独拷走运行。

### 关于 PCANBasic.dll

本仓库**不包含** PEAK 的 `PCANBasic.dll`——它是第三方组件，授权归 PEAK-System，不应随本仓库分发。

使用 PCAN 硬件时，二选一：

1. **安装 PCAN-Basic**（推荐）：从 [PEAK-System 官网](https://www.peak-system.com/) 下载安装，程序会自动使用系统目录中的 DLL。
2. **便携方式**：把 `x64/PCANBasic.dll` 放到项目的 `PCAN-Basic/x64/` 目录下。

> 程序按「内嵌 → 程序目录 → PCAN-Basic/x64 → 系统目录」的顺序查找 DLL。
>
> 没有 PCAN 硬件时，界面仍可正常打开，只是无法连接通道。

---

## 目录结构

```
BusLens/
├── dbc_parser.py        # 主程序：界面、DBC 解析、日志导入、格式转换
├── live_monitor.py      # 实时监控、发送窗口、回放、UDS 窗口
├── pcan_config.py       # PCAN 通道枚举、状态查询、位定时计算
├── uds.py               # ISO-TP 与 UDS 协议层（与界面解耦）
├── dbc_parser.spec      # PyInstaller 打包配置
├── make_icon.py         # 生成应用图标
├── make_nav_icons.py    # 生成导航栏图标
├── app.ico              # 应用图标
├── nav_*.png            # 导航图标（灰 / 白两套）
├── requirements.txt
├── LICENSE
└── docs/
    └── 使用说明书.md
```

---

## 使用说明

完整的功能说明、参数解释与常见问题见 **[docs/使用说明书.md](docs/使用说明书.md)**。

几个高频问题：

**Q：拖文件进窗口显示禁止光标？**
程序以管理员权限运行时会这样——Windows 的 UIPI 隔离阻止普通资源管理器向高权限进程拖放。用普通权限双击启动即可，状态栏右下角会显示「拖拽就绪」。

**Q：通道已被占用，连不上？**
PCAN-Basic 的通道是独占的。占用者是另一个 PCAN-Basic 程序（如别的上位机）时无法共享，只能换物理通道；占用者是 PCAN-View 时可用「只监听 + 波特率自适应」只读接入。

**Q：UDS 安全访问返回 `7F 27 35`？**
`0x27` 的 Key 算法是厂商私有，仓库里默认填的是示例算法。拿到真实算法后填进界面即可。

**Q：经典 CAN 设了 80 MHz 不生效？**
PCAN 驱动的经典 CAN 时钟固定 8 MHz，80 / 40 MHz 只对 CAN FD 有效。

---

## 已知限制

| 限制 | 说明 |
|---|---|
| PCAN 通道独占 | 同一通道同时只能被一个 PCAN-Basic 客户端持有 |
| 安全访问算法 | `0x27` 需厂商提供，仓库内为示例值 |
| UDS 依赖实时页通道 | UDS 窗口复用监控页已连接的通道 |
| 管理员权限下拖放失效 | UIPI 限制，非程序问题 |
| 仅 Windows | 界面基于 customtkinter，未在 Linux / macOS 验证 |

---

## 技术栈

- [python-can](https://python-can.readthedocs.io/) —— CAN 总线访问与日志读写
- [customtkinter](https://github.com/TomSchimansky/CustomTkinter) —— 界面
- [tkinterdnd2](https://github.com/pmgagne/tkinterdnd2) —— 拖放支持（原生 WM_DROPFILES 为主，tkdnd 兜底）
- [Pillow](https://python-pillow.org/) —— 图标生成与渲染

---

## 许可证

[MIT](LICENSE)

---

## 免责声明

本工具用于汽车电子测试与学习研究。CAN 总线上的写操作可能影响被测设备或整车通信，请自行评估风险并在受控环境中使用。作者不对使用本工具造成的任何损失承担责任。
