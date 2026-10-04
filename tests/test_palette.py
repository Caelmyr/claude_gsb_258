"""主色提取算法测试：单色、彩色、相近色合并、背景忽略、占比、色卡渲染。

运行：python tests/test_palette.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw  # noqa: E402

from server.algorithms import palette  # noqa: E402


def _solid(size=(200, 160), color=(30, 120, 220), noise=3):
    """近乎纯色的图（带轻微噪声，模拟 JPEG/抗锯齿）。"""
    img = Image.new("RGB", size, color)
    px = img.load()
    step = max(1, noise)
    for y in range(0, size[1], 3):
        for x in range(0, size[0], 3):
            j = ((x * 7 + y * 13) % (2 * step + 1)) - step
            px[x, y] = (max(0, min(255, color[0] + j)),
                        max(0, min(255, color[1] + j)),
                        max(0, min(255, color[2] + j)))
    return img


def _blocks(spec, size=(300, 200)):
    """按 (x0,y0,x1,y1,rgb) 列表拼一张硬色块图。"""
    img = Image.new("RGB", size)
    d = ImageDraw.Draw(img)
    for x0, y0, x1, y1, rgb in spec:
        d.rectangle([x0, y0, x1, y1], fill=rgb)
    return img


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise AssertionError(name)


def main():
    print("== 单色图：必须给出有意义的结果，不凭空造色 ==")
    img = _solid()
    r = palette.extract(img, {"colors": 8, "merge": 50, "ignore_background": True})
    check("颜色数 <= 请求数", r["count"] <= 8)
    check("只得到 1 种主色", r["count"] == 1)
    check("占比合计为 1", abs(sum(c["ratio"] for c in r["colors"]) - 1.0) < 1e-6)
    check("显示百分比合计 100.0", sum(float(c["percent"]) for c in r["colors"]) == 100.0)
    top = r["colors"][0]
    check(f"主色接近 (30,120,220): {top['hex']}",
          all(abs(top["rgb"][i] - (30, 120, 220)[i]) <= 12 for i in range(3)))
    check("输出映射图", isinstance(r["image"], Image.Image) and r["image"].size)

    print("\n== 色彩丰富图：四块各 1/4 ==")
    img = _blocks([
        (0, 0, 150, 100, (220, 40, 40)),
        (150, 0, 300, 100, (40, 180, 70)),
        (0, 100, 150, 200, (40, 80, 200)),
        (150, 100, 300, 200, (230, 200, 40)),
    ])
    r = palette.extract(img, {"colors": 4, "merge": 50, "ignore_background": False})
    check("得到 4 种主色", r["count"] == 4)
    for c in r["colors"]:
        check(f"各色约 25%（{c['hex']}={c['percent']}%）", abs(c["ratio"] - 0.25) < 0.03)
    check("占比合计为 1", abs(sum(c["ratio"] for c in r["colors"]) - 1.0) < 1e-6)
    check("百分比合计 100.0", sum(float(c["percent"]) for c in r["colors"]) == 100.0)

    print("\n== 相近色合并：四块里两块是同色微差，不应拆成两种 ==")
    img = _blocks([
        (0, 0, 100, 200, (200, 50, 50)),
        (100, 0, 200, 200, (208, 56, 54)),   # 肉眼几乎一样的红
        (200, 0, 260, 200, (50, 60, 180)),
        (260, 0, 300, 200, (40, 170, 90)),
    ])
    r_merged = palette.extract(img, {"colors": 6, "merge": 60, "ignore_background": False})
    check("相近红被归并（颜色数 <= 3）", r_merged["count"] <= 3)
    red = max(r_merged["colors"], key=lambda c: c["ratio"])
    check(f"归并后的红约占 2/3（{red['percent']}%）", abs(red["ratio"] - 2 / 3) < 0.06)
    # 强度 0 时允许拆开
    r_split = palette.extract(img, {"colors": 6, "merge": 0, "ignore_background": False})
    check("合并强度 0 时颜色更多", r_split["count"] >= r_merged["count"])

    print("\n== 忽略大面积背景：白底 + 中心物体 ==")
    img = Image.new("RGB", (300, 200), (248, 248, 248))
    d = ImageDraw.Draw(img)
    d.rectangle([90, 50, 210, 160], fill=(200, 60, 60))  # 物体占 120*110/60000 = 22%
    r = palette.extract(img, {"colors": 4, "merge": 50, "ignore_background": True})
    check("背景被识别并忽略", r["ignored_background"] is True and r["background"] is not None)
    check(f"背景色接近白（{r['background']['hex']}）",
          all(v >= 225 for v in r["background"]["rgb"]))
    check(f"背景占比约 78%（{r['background']['percent']}%）",
          abs(r["background"]["ratio"] - 0.78) < 0.05)
    check("主色里没有白", all(not all(v > 220 for v in c["rgb"]) for c in r["colors"]))
    red = r["colors"][0]
    check(f"主体红占前景主导（{red['percent']}%）", red["ratio"] > 0.8)
    check("主色占比按前景归一且合计为 1",
          abs(sum(c["ratio"] for c in r["colors"]) - 1.0) < 1e-6)

    print("\n== 关闭背景忽略时白底应作为主色 ==")
    r2 = palette.extract(img, {"colors": 4, "merge": 50, "ignore_background": False})
    check("未忽略背景", r2["ignored_background"] is False)
    check("白色是占比最大的主色", all(v > 220 for v in r2["colors"][0]["rgb"]))
    check(r2["colors"][0]["ratio"] > 0.6, "白色占比 > 60%")

    print("\n== 纯色图不能被背景识别误删 ==")
    r3 = palette.extract(_solid(color=(245, 245, 245)),
                         {"colors": 4, "merge": 50, "ignore_background": True})
    check("整图一个颜色时保留为内容", r3["count"] == 1 and not r3["ignored_background"])

    print("\n== 目标数量裁剪：8 色图请求 3 色 ==")
    spec, cols = [], 8
    for i in range(cols):
        hue = i * 30
        spec.append((i * 300 // cols, 0, (i + 1) * 300 // cols, 200,
                     (60 + hue, 120, 240 - hue * 2)))
    img = _blocks(spec)
    r4 = palette.extract(img, {"colors": 3, "merge": 10, "ignore_background": False})
    check("结果不超过 3 种", r4["count"] <= 3)
    check("所有像素都被覆盖", abs(sum(c["ratio"] for c in r4["colors"]) - 1.0) < 1e-6)

    print("\n== 色卡图片与文本导出 ==")
    r5 = palette.extract(_blocks([
        (0, 0, 150, 100, (220, 40, 40)), (150, 0, 300, 100, (40, 180, 70)),
        (0, 100, 150, 200, (40, 80, 200)), (150, 100, 300, 200, (230, 200, 40)),
    ]), {"colors": 4, "merge": 50, "ignore_background": False})
    sw = palette.render_swatch(r5["colors"])
    check("色卡为 RGB PNG", sw.mode == "RGB" and sw.size[0] == 1080 and sw.size[1] > 100)
    txt = palette.palette_text({"count": r5["count"], "requested_count": 4,
                                "covered_pixels": r5["covered_pixels"],
                                "total_pixels": r5["total_pixels"],
                                "colors": r5["colors"], "background": None,
                                "ignored_background": False})
    check("文本含 HEX 表头", "#" in txt and "HEX" in txt)
    pct_sum = sum(float(line.split("\t")[-1].rstrip("%"))
                  for line in txt.splitlines()
                  if line and line[0].isdigit() and "\t" in line)
    check("文本占比合计 100.0%", pct_sum == 100.0)
    check("文本每种颜色一行",
          sum(1 for line in txt.splitlines() if line and line[0].isdigit()) == 4)

    print("\n主色提取测试全部通过 ✔")


if __name__ == "__main__":
    main()
