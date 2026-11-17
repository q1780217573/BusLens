# -*- coding: utf-8 -*-
"""
生成 BusLens · 观澜 应用图标（方向 A：双绞线穿过透镜）。

画法：
  1. 深蓝圆角方底，自上而下渐变
  2. 两条交叉线（双绞线）从左上/左下穿到对角
  3. 交点处一个亮点，表示节点
  4. 外套一个圆环，表示透镜「看见总线」

输出：app.ico（16/24/32/48/64/128/256 七档）与 icon_preview.png（1024 预览）
"""

import os
import sys

from PIL import Image, ImageDraw

S = 1024                 # 超采样画布
PAD = 54                 # 外留白
RADIUS = 200             # 圆角半径
TOP = (20, 58, 98)
BOT = (8, 26, 46)
LINE = (56, 189, 248, 255)      # 双绞线：天蓝
RING = (125, 211, 252, 255)     # 透镜环：浅青
NODE = (224, 242, 254, 255)     # 节点：近白


def build(size=S):
    # --- 渐变底 ---
    bg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(bg)
    for y in range(PAD, size - PAD):
        t = (y - PAD) / max(size - 2 * PAD - 1, 1)
        c = tuple(int(TOP[i] + (BOT[i] - TOP[i]) * t) for i in range(3))
        d.line([(PAD, y), (size - PAD, y)], fill=c + (255,))

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [PAD, PAD, size - PAD, size - PAD], radius=RADIUS, fill=255)
    bg.putalpha(mask)

    d = ImageDraw.Draw(bg)
    cx = cy = size // 2
    k = size / S                      # 所有尺寸按比例缩放

    # --- 双绞线（两条交叉斜线）---
    span_x, span_y = int(238 * k), int(222 * k)
    w = max(int(36 * k), 2)
    d.line([(cx - span_x, cy + span_y), (cx + span_x, cy - span_y)],
           fill=LINE, width=w)
    d.line([(cx - span_x, cy - span_y), (cx + span_x, cy + span_y)],
           fill=LINE, width=w)

    # --- 中心节点 ---
    r = max(int(40 * k), 2)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=NODE)

    # --- 透镜圆环 ---
    rr = int(318 * k)
    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
              outline=RING, width=max(int(46 * k), 2))
    return bg


def main():
    out_dir = os.path.dirname(os.path.abspath(__file__))
    master = build(S)

    png = os.path.join(out_dir, "icon_preview.png")
    master.save(png, "PNG")

    ico = os.path.join(out_dir, "app.ico")
    master.save(ico, format="ICO",
                sizes=[(256, 256), (128, 128), (64, 64), (48, 48),
                       (32, 32), (24, 24), (16, 16)])
    print(f"已生成 {png}")
    print(f"已生成 {ico}  {os.path.getsize(ico)} 字节")

    # 顺带导出 16px 与 32px 预览，确认小尺寸不糊
    for s in (16, 32, 48):
        p = os.path.join(out_dir, f"icon_preview_{s}.png")
        master.resize((s, s), Image.LANCZOS).save(p, "PNG")
        print(f"已生成 {p}")


if __name__ == "__main__":
    sys.exit(main())