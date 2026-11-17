# -*- coding: utf-8 -*-
"""
生成导航栏图标：解析 / 导入 / 转换 / 监控。

统一规格：
  · 24 单位网格、4 倍超采样（96px）绘制再降采样，线条锐利
  · 圆头线条，线宽 2 单位（=8px @96）
  · 两种颜色：未选中 #6B7785，选中 #FFFFFF
  · 输出 56px 源图，界面按 28px 显示（2x 屏也不糊）

不用 emoji：系统字体在不同机器/版本下渲染差异大，还会被强制上色。
"""

import os

from PIL import Image, ImageDraw

S = 96                  # 4x 画布（24 单位 × 4）
LW = 8                  # 线宽 2 单位
GREY = (107, 119, 133, 255)
WHITE = (255, 255, 255, 255)


def _new():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _dot(d, x, y, r, color):
    d.ellipse([x - r, y - r, x + r, y + r], fill=color)


def _line(d, pts, color, w=LW):
    d.line(pts, fill=color, width=w, joint="curve")
    for x, y in (pts[0], pts[-1]):
        _dot(d, x, y, w // 2, color)


def icon_parse(color):
    """放大镜 + 内嵌三条数据行"""
    img, d = _new()
    cx, cy, r = 40, 40, 21
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=LW)
    # 手柄
    _line(d, [(56, 56), (78, 78)], color)
    # 镜内三条数据行
    for y in (32, 40, 48):
        _line(d, [(30, y), (50, y)], color, 6)
    return img


def icon_import(color):
    """向下箭头 + 承接横线"""
    img, d = _new()
    _line(d, [(48, 16), (48, 60)], color)
    _line(d, [(32, 46), (48, 64), (64, 46)], color)
    _line(d, [(20, 80), (76, 80)], color)
    return img


def icon_convert(color):
    """上下双向循环箭头"""
    img, d = _new()
    box = [22, 22, 74, 74]
    arc_w = LW
    # 上弧：左 → 右（顶部）
    d.arc(box, 190, 350, fill=color, width=arc_w)
    # 上弧右端箭头（向下）
    _line(d, [(66, 40), (74, 52), (82, 40)], color, 7)
    # 下弧：右 → 左（底部）
    d.arc(box, 10, 170, fill=color, width=arc_w)
    # 下弧左端箭头（向上）
    _line(d, [(30, 56), (22, 44), (14, 56)], color, 7)
    return img


def icon_monitor(color):
    """方波：一眼看出是总线上的信号"""
    img, d = _new()
    _line(d, [(14, 66), (30, 66), (30, 34), (48, 34),
              (48, 66), (66, 66), (66, 46), (82, 46)], color, 7)
    return img


ICONS = {
    "parse": icon_parse,
    "import": icon_import,
    "convert": icon_convert,
    "monitor": icon_monitor,
}


def main():
    out_dir = os.path.dirname(os.path.abspath(__file__))
    made = []
    for key, fn in ICONS.items():
        for suffix, color in (("", GREY), ("_on", WHITE)):
            big = fn(color)
            img = big.resize((56, 56), Image.LANCZOS)
            path = os.path.join(out_dir, f"nav_{key}{suffix}.png")
            img.save(path, "PNG")
            made.append(path)
    # 拼一张对照图，肉眼看清楚
    sheet = Image.new("RGBA", (56 * 4 * 2 + 30, 56 * 2 + 30), (18, 26, 36, 255))
    for i, key in enumerate(ICONS):
        for j, suffix in enumerate(("", "_on")):
            p = os.path.join(out_dir, f"nav_{key}{suffix}.png")
            im = Image.open(p)
            sheet.alpha_composite(im, (10 + i * 64, 10 + j * 66))
    preview = os.path.join(out_dir, "nav_icons_preview.png")
    sheet.save(preview)
    print(f"已生成 {len(made)} 张图标，对照图 → {preview}")
    for p in made:
        print("  ", os.path.basename(p), os.path.getsize(p), "字节")


if __name__ == "__main__":
    main()