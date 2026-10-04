"""主色提取（取色）：从图像提取配色方案用的主色卡。

纯 Pillow 实现，流程：

1. 降采样到工作副本（避免逐像素大循环），统计量化颜色直方图。
2. 边缘密度估计背景：图像边缘一圈通常被背景占据，边缘颜色占比越集中，
   越说明存在大面积纯色背景（如白底产品图）；超过阈值的主边缘色可忽略。
3. 自适应聚类数：按颜色丰富度（显著颜色占比）在 [n, 2n] 间调整实际聚类数，
   色彩单一的图不会强行凑数，色彩丰富的图能拆出更多主色。
4. 中位切分量化聚类，统计各簇占比与簇内原始像素均值（比桶中心更准）。
5. 相近色合并：加权 CIE76 色差（暗色对色相差异更敏感），小于阈值的簇
   按占比加权合并，避免把几乎一样的颜色拆成好几种。
6. 占比归一：忽略背景后重新归一，所有主色占比之和恒为 100%。

输出：色卡图（横向色条 + 色块/HEX/占比标注）+ 每色 hex/rgb/占比 + 诊断信息。
"""
import math

from PIL import Image, ImageDraw

from . import util

# 工作副本最长边：取色是统计型任务，不需要高分辨率
_WORK_DIM = 240
# 量化桶数：16^3 = 4096 桶，足够区分相近色又不至于太稀疏
_QUANTIZE_LEVELS = 16
# 边缘带宽度（占短边比例）与背景判定阈值
_BORDER_FRAC = 0.06
_BG_DOMINANCE = 0.55
# 相近色合并的加权 CIE76 阈值（≈“看起来几乎一样”的界限，可在参数里调）
_MERGE_DELTA = 12.0
# 显著颜色阈值：占比超过该值的桶计入“颜色丰富度”
_SIGNIFICANT_SHARE = 0.004


# ---------------------------------------------------------------------------
# 颜色空间换算
# ---------------------------------------------------------------------------
def _rgb_to_hex(rgb):
    return "#{:02x}{:02x}{:02x}".format(*[int(round(c)) for c in rgb])


def _srgb_to_linear(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _rgb_to_lab(rgb):
    """sRGB -> CIE L*a*b*（D65）。用于感知色差。"""
    r, g, b = (_srgb_to_linear(c) for c in rgb)
    # XYZ（D65）
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _color_distance(lab1, lab2):
    """加权 CIE76：暗色区域人眼对色相差异更敏感，权重随明度降低。"""
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2
    luma_w = 1.0 + (100 - min(l1, l2)) / 220.0  # 暗色差异权重略增
    dl = (l1 - l2) * luma_w
    return math.sqrt(dl * dl + (a1 - a2) ** 2 + (b1 - b2) ** 2)


def _luminance(rgb):
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


# ---------------------------------------------------------------------------
# 直方图与背景检测
# ---------------------------------------------------------------------------
def _bucketize(work):
    """单趟扫描：把像素归入 16^3 桶。

    返回 (hist, pixel_sums, total)：
    - hist       : {bucket_key: 像素数}
    - pixel_sums : {bucket_key: (r累计, g累计, b累计, n)}，用于簇内原始像素均值
    """
    shift = 8 - int(round(math.log2(_QUANTIZE_LEVELS)))  # 4
    hist = {}
    pixel_sums = {}
    for r, g, b in work.getdata():
        key = ((r >> shift) << 8) | ((g >> shift) << 4) | (b >> shift)
        hist[key] = hist.get(key, 0) + 1
        s = pixel_sums.get(key)
        if s is None:
            pixel_sums[key] = (r, g, b, 1)
        else:
            pixel_sums[key] = (s[0] + r, s[1] + g, s[2] + b, s[3] + 1)
    return hist, pixel_sums, work.size[0] * work.size[1]


def _bucket_center(key):
    step = 256 // _QUANTIZE_LEVELS
    r = ((key >> 8) & 0xF) * step + step // 2
    g = ((key >> 4) & 0xF) * step + step // 2
    b = (key & 0xF) * step + step // 2
    return (r, g, b)


def _border_and_center_keys(work):
    """统计边缘一圈与中心区域的量化桶计数。

    返回 (border_counts, center_counts)。中心区域取中间 50%×50% 的盒子。
    """
    w, h = work.size
    bw = max(2, int(round(min(w, h) * _BORDER_FRAC)))
    cx0, cx1 = int(w * 0.25), int(w * 0.75)
    cy0, cy1 = int(h * 0.25), int(h * 0.75)
    shift = 8 - int(round(math.log2(_QUANTIZE_LEVELS)))
    border, center = {}, {}
    px = util.ensure_rgb(work).load()
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y][:3]
            key = ((r >> shift) << 8) | ((g >> shift) << 4) | (b >> shift)
            if x < bw or x >= w - bw or y < bw or y >= h - bw:
                border[key] = border.get(key, 0) + 1
            elif cx0 <= x < cx1 and cy0 <= y < cy1:
                center[key] = center.get(key, 0) + 1
    return border, center


def _bucket_mean(key, pixel_sums):
    """桶内原始像素均值；没有统计时退回桶中心。"""
    s = pixel_sums.get(key)
    if s and s[3] > 0:
        return (s[0] / s[3], s[1] / s[3], s[2] / s[3])
    return _bucket_center(key)


def _detect_background(hist, total, border_counts, center_counts, pixel_sums):
    """判定可忽略的大面积背景色，返回背景桶集合（一个颜色家族）。

    判定以“颜色家族”为单位（相近桶先归并），避免背景有轻微渐变/噪点时被
    拆成多个桶而漏判。条件（同时满足）：
    - 家族在边缘带中占主导（>= _BG_DOMINANCE），说明它从四边包围画面；
    - 家族整图占比足够大（>= 25%），说明是大面积背景而非边框装饰；
    - 中心区域存在非背景内容（>= 2%）：背景是“框住主体”的；
      若中心也被同族颜色填满（如全幅纹理），说明它本身就是画面内容。
    """
    if not border_counts:
        return set()
    border_total = sum(border_counts.values())
    center_total = sum(center_counts.values()) or 1

    keys = set(hist) | set(border_counts)
    labs = {k: _rgb_to_lab(_bucket_mean(k, pixel_sums)) for k in keys}

    def family_of(key):
        return [j for j in keys if _color_distance(labs[key], labs[j]) < _MERGE_DELTA]

    best_family, best_edge = None, 0.0
    for key in border_counts:
        fam = family_of(key)
        edge = sum(border_counts.get(j, 0) for j in fam) / border_total
        img = sum(hist.get(j, 0) for j in fam) / max(total, 1)
        center = sum(center_counts.get(j, 0) for j in fam) / center_total
        if (edge >= _BG_DOMINANCE and img >= 0.25
                and (1.0 - center) >= 0.02 and edge > best_edge):
            best_family, best_edge = fam, edge
    return set(best_family) if best_family else set()


# ---------------------------------------------------------------------------
# 聚类（中位切分，作用在桶直方图上）
# ---------------------------------------------------------------------------
def _median_cut(hist, n_clusters):
    """对 {bucket_key: count} 做加权中位切分，返回 [{key: count}, ...] 簇列表。"""
    boxes = [dict(hist)]
    while len(boxes) < n_clusters:
        # 选“像素数最多且通道跨度最大”的箱子继续切
        best_i, best_score = -1, -1
        for i, box in enumerate(boxes):
            if len(box) < 2:
                continue
            count = sum(box.values())
            keys = list(box.keys())
            rs = [(k >> 8) & 0xF for k in keys]
            gs = [(k >> 4) & 0xF for k in keys]
            bs = [k & 0xF for k in keys]
            span = max(max(rs) - min(rs), max(gs) - min(gs), max(bs) - min(bs))
            score = count * (1 + span)
            if score > best_score:
                best_i, best_score = i, score
        if best_i < 0:
            break
        box = boxes.pop(best_i)
        keys = list(box.keys())
        rs = [(k >> 8) & 0xF for k in keys]
        gs = [(k >> 4) & 0xF for k in keys]
        bs = [k & 0xF for k in keys]
        spans = (max(rs) - min(rs), max(gs) - min(gs), max(bs) - min(bs))
        channel = spans.index(max(spans))

        def ch_val(k):
            return (k >> 8) & 0xF if channel == 0 else ((k >> 4) & 0xF if channel == 1 else k & 0xF)

        keys.sort(key=ch_val)
        half = sum(box.values()) / 2.0
        acc, cut = 0, 0
        for cut, k in enumerate(keys):
            acc += box[k]
            if acc >= half:
                break
        cut = max(1, min(len(keys) - 1, cut + 1))
        boxes.append({k: box[k] for k in keys[:cut]})
        boxes.append({k: box[k] for k in keys[cut:]})
    return boxes


def _cluster_mean(cluster, pixel_sums):
    """簇的加权均值颜色：优先用原始像素累计值（更准），否则用桶中心。"""
    total = sum(cluster.values())
    if total == 0:
        return (0, 0, 0), 0
    r = g = b = 0.0
    for key, cnt in cluster.items():
        if key in pixel_sums:
            sr, sg, sb, n = pixel_sums[key]
            r += sr
            g += sg
            b += sb
        else:
            cr, cg, cb = _bucket_center(key)
            r += cr * cnt
            g += cg * cnt
            b += cb * cnt
    return (r / total, g / total, b / total), total


# ---------------------------------------------------------------------------
# 相近色合并
# ---------------------------------------------------------------------------
def _merge_similar(colors, threshold=_MERGE_DELTA):
    """把相近色合并（按占比加权平均）。colors 按占比降序。

    采用全连接（complete-linkage）：候选色必须与组内所有原始簇都相近才合并，
    避免渐变图里“每个都和下一个像”导致整条链被并掉。
    """
    merged = []
    for c in colors:
        target = None
        for m in merged:
            if all(_color_distance(c["lab"], mem) < threshold for mem in m["members"]):
                target = m
                break
        if target is None:
            entry = dict(c)
            entry["members"] = [c["lab"]]
            merged.append(entry)
        else:
            t = target["count"] + c["count"]
            for ch in ("rgb", "lab"):
                target[ch] = tuple(
                    (target[ch][i] * target["count"] + c[ch][i] * c["count"]) / t
                    for i in range(3)
                )
            target["count"] = t
            target["members"].append(c["lab"])
    merged.sort(key=lambda c: c["count"], reverse=True)
    return merged


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def extract_palette(image, params):
    """提取主色，返回 {palette, diagnostics, image(色卡图)}。

    params:
      colors          期望主色数量（2~12，默认 6）
      ignore_bg       是否忽略大面积背景色（默认 True）
      merge_similar   是否合并相近颜色（默认 True）
      merge_threshold 合并阈值（加权 CIE76，默认 12，越大合并越激进）
    """
    n_colors = max(2, min(12, int(params.get("colors", 6))))
    ignore_bg = bool(params.get("ignore_bg", True))
    do_merge = bool(params.get("merge_similar", True))
    merge_th = float(params.get("merge_threshold", _MERGE_DELTA))
    merge_th = max(4.0, min(60.0, merge_th))

    rgb = util.ensure_rgb(image)
    work = util.downscale_to_max(rgb, _WORK_DIM)

    hist, pixel_sums, total_px = _bucketize(work)
    full_hist = hist  # 留一份完整直方图，供“整图都是背景”时回退

    # ---- 背景检测与忽略 ----
    bg_keys = set()
    bg_share = 0.0
    if ignore_bg:
        border_counts, center_counts = _border_and_center_keys(work)
        bg_keys = _detect_background(hist, total_px, border_counts, center_counts, pixel_sums)
        if bg_keys:
            bg_share = sum(hist.get(k, 0) for k in bg_keys) / total_px
            hist = {k: v for k, v in hist.items() if k not in bg_keys}

    remaining = sum(hist.values())
    if remaining == 0:
        # 整图都是背景色（极端情况）：退回不忽略背景
        hist = full_hist
        bg_keys, bg_share = set(), 0.0
        remaining = total_px

    # ---- 自适应聚类数 ----
    # 颜色丰富度 = 显著桶的占比；丰富度低说明色彩单一，少拆几类
    significant = sum(c for c in hist.values() if c / remaining >= _SIGNIFICANT_SHARE)
    richness = significant / remaining
    if richness >= 0.75:
        target = min(n_colors * 2, 24)
    elif richness >= 0.4:
        target = int(round(n_colors * 1.5))
    else:
        target = n_colors
    target = max(n_colors, target)

    clusters = _median_cut(hist, target)

    colors = []
    for cl in clusters:
        mean, count = _cluster_mean(cl, pixel_sums)
        colors.append({
            "rgb": mean,
            "lab": _rgb_to_lab(mean),
            "count": count,
        })
    colors.sort(key=lambda c: c["count"], reverse=True)

    # ---- 相近色合并 ----
    if do_merge:
        colors = _merge_similar(colors, threshold=merge_th)

    # 保留前 n_colors 个；被裁掉的小色占比并入最近的主色，保证占比和为 100%
    if len(colors) > n_colors:
        kept, dropped = colors[:n_colors], colors[n_colors:]
        for d in dropped:
            nearest = min(kept, key=lambda k: _color_distance(k["lab"], d["lab"]))
            nearest["count"] += d["count"]
        colors = kept
    colors.sort(key=lambda c: c["count"], reverse=True)

    grand = sum(c["count"] for c in colors) or 1
    palette = []
    for c in colors:
        hexv = _rgb_to_hex(c["rgb"])
        palette.append({
            "hex": hexv,
            "rgb": [int(round(v)) for v in c["rgb"]],
            "share": round(c["count"] / grand, 4),
        })

    bg_hex = None
    if bg_keys:
        # 背景家族占比加权均值，作为被忽略背景的代表色
        bg_total = sum(full_hist.get(k, 0) for k in bg_keys) or 1
        bg_mean = tuple(
            sum(_bucket_mean(k, pixel_sums)[i] * full_hist.get(k, 0) for k in bg_keys) / bg_total
            for i in range(3)
        )
        bg_hex = _rgb_to_hex(bg_mean)

    diagnostics = {
        "requested_colors": n_colors,
        "returned_colors": len(palette),
        "richness": round(richness, 4),
        "clusters_before_merge": len(clusters),
        "background_ignored": bool(bg_keys),
        "background_share": round(bg_share, 4),
        "background_hex": bg_hex,
    }

    card = render_palette_card(palette, title="Color Palette")
    return {
        "palette": palette,
        "diagnostics": diagnostics,
        "image": card,
    }


# ---------------------------------------------------------------------------
# 色卡渲染 / 导出
# ---------------------------------------------------------------------------
def _contrast_text(rgb):
    """根据底色亮度选黑/白文字。"""
    return (20, 22, 28) if _luminance(rgb) > 150 else (255, 255, 255)


_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
)


def _font(size=13):
    """尽量用 TrueType 字体（默认位图字体不支持中文且偏小）。"""
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _safe_title(title):
    """色卡图内只放 ASCII 标题（无中文字体时避免方块字）；中文标题由前端展示。"""
    if title and all(ord(c) < 128 for c in title):
        return title
    return "Color Palette"


def render_palette_card(palette, title=None, width=880, swatch_h=180, label_h=64):
    """把色卡渲染成一张图：顶部横向色条 + 每色 HEX/占比标注。"""
    n = max(1, len(palette))
    pad = 16
    title = _safe_title(title) if title else None
    title_h = 40 if title else 0
    height = pad * 2 + title_h + swatch_h + label_h
    card = Image.new("RGB", (width, height), (24, 27, 34))
    draw = ImageDraw.Draw(card)
    font = _font(13)
    font_sm = _font(12)

    y0 = pad
    if title:
        draw.text((pad, y0 + 6), title, fill=(235, 238, 245), font=_font(16))
        y0 += title_h

    x = pad
    avail = width - pad * 2
    # 色块宽度按占比分配，先给最小宽度兜底，再整体归一到 avail，保证不溢出
    min_w = max(28, avail // (n * 3))
    raw = [max(avail * p.get("share", 1.0 / n), min_w) for p in palette]
    scale = avail / sum(raw)
    widths = [w_i * scale for w_i in raw]
    for i, p in enumerate(palette):
        w_i = widths[i]
        if i == len(palette) - 1:
            w_i = pad + avail - x  # 末块补齐，吸收舍入误差
        rgb = tuple(p["rgb"])
        draw.rectangle([x, y0, x + w_i, y0 + swatch_h], fill=rgb)
        if w_i >= 64:  # 太窄的色块不写文字（底部图例有完整信息），避免溢出到邻块
            txt = _contrast_text(rgb)
            draw.text((x + 8, y0 + 8), p["hex"].upper(), fill=txt, font=font)
            draw.text((x + 8, y0 + 28), "{:.1f}%".format(p.get("share", 0) * 100), fill=txt, font=font_sm)
        x += w_i

    # 底部图例：色块 + hex（第一行）+ 占比（第二行）
    ly = y0 + swatch_h + 10
    col_w = avail // n
    for i, p in enumerate(palette):
        lx = pad + i * col_w
        draw.rectangle([lx, ly + 2, lx + 12, ly + 14], fill=tuple(p["rgb"]))
        draw.text((lx + 17, ly + 2), p["hex"].upper(), fill=(200, 205, 215), font=font_sm)
        draw.text((lx + 17, ly + 18), "{:.1f}%".format(p.get("share", 0) * 100),
                  fill=(140, 146, 158), font=font_sm)
    return card


def palette_to_text(palette, diagnostics=None, fmt="list"):
    """把色卡导出为文本。fmt: list（每行一条）/ css（CSS 变量）/ json。"""
    if fmt == "css":
        lines = [":root {"]
        for i, p in enumerate(palette, 1):
            lines.append("  --color-{}: {}; /* {:.1f}% */".format(i, p["hex"], p.get("share", 0) * 100))
        lines.append("}")
        return "\n".join(lines)
    if fmt == "json":
        import json
        return json.dumps({"palette": palette, "diagnostics": diagnostics or {}},
                          ensure_ascii=False, indent=2)
    lines = []
    if diagnostics and diagnostics.get("background_ignored"):
        lines.append("# 已忽略背景色 {}（占 {:.1f}%）".format(
            diagnostics.get("background_hex") or "?",
            diagnostics.get("background_share", 0) * 100))
    for i, p in enumerate(palette, 1):
        r, g, b = p["rgb"]
        lines.append("{:>2}. {}  rgb({},{},{})  {:.1f}%".format(
            i, p["hex"].upper(), r, g, b, p.get("share", 0) * 100))
    return "\n".join(lines)
