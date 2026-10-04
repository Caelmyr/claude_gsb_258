"""主色提取：从图像中抽取若干主色，输出十六进制、占比、色块映射图与色卡。

流程（全部在低分辨率工作副本上完成，统计开销与原图大小无关）：

1. 量化：最长边压到 224 后用 MEDIANCUT 量化到至多 256 色（不抖动），
   逐色桶统计像素数，避免被 JPEG 噪声/抗锯齿拆出海量相近颜色。
2. 背景识别：取图像四边像素的中值色作为「背景色」，统计与其 Lab 距离
   小于阈值的像素占比；占比足够大且忽略后仍有内容时才剔除（单色图不会被误删）。
3. 层次合并：在 CIE Lab 空间对色桶做加权凝聚聚类——
   - 距离小于「合并强度」对应的 ΔE 阈值时，即使颜色数已达标也继续合并，
     保证几乎一样的颜色不会被拆成多种；
   - 颜色数仍多于目标数量时，按最近距离继续合并到目标数量。
   色彩单一的图最终颜色数允许少于请求数量，不会凭空造色。
4. 占比按有效像素归一，恒为 1.0；百分比用最大余数法修正到合计 100.0%。
"""
import heapq
import math
import os

from PIL import Image, ImageDraw, ImageFont

from . import util

ANALYSIS_MAXDIM = 224  # 统计用工作副本最长边


# ---------------------------------------------------------------------------
# 颜色空间：sRGB -> CIE Lab（D65），距离用 CIE76 ΔE
# ---------------------------------------------------------------------------
def _srgb_to_linear(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_lab(rgb):
    r, g, b = (_srgb_to_linear(v) for v in rgb)
    x = r * 0.4124564 + g * 0.3575761 + b * 0.1804375
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = r * 0.0193339 + g * 0.1191920 + b * 0.9503041
    x /= 0.95047
    z /= 1.08883

    def f(t):
        return t ** (1.0 / 3.0) if t > 216.0 / 24389.0 else (841.0 / 108.0) * t + 16.0 / 116.0

    fx, fy, fz = f(x), f(y), f(z)
    return (116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz))


def _lab_to_rgb(lab):
    L, a, b = lab
    fy = (L + 16.0) / 116.0
    fx = a / 500.0 + fy
    fz = fy - b / 200.0

    def finv(t):
        t3 = t ** 3
        return t3 if t3 > 216.0 / 24389.0 else (t - 16.0 / 116.0) * 108.0 / 841.0

    x = 0.95047 * finv(fx)
    y = finv(fy)
    z = 1.08883 * finv(fz)
    r = x * 3.2404542 + y * -1.5371385 + z * -0.4985314
    g = x * -0.9692660 + y * 1.8760108 + z * 0.0415560
    bl = x * 0.0556434 + y * -0.2040259 + z * 1.0572252

    def enc(c):
        c = max(0.0, min(1.0, c))
        v = 1.055 * c ** (1.0 / 2.4) - 0.055 if c > 0.0031308 else 12.92 * c
        return util.clamp(v * 255.0)

    return (enc(r), enc(g), enc(bl))


def _delta_e(lab1, lab2):
    return math.sqrt((lab1[0] - lab2[0]) ** 2 + (lab1[1] - lab2[1]) ** 2 + (lab1[2] - lab2[2]) ** 2)


def _luma(rgb):
    """sRGB 相对亮度（0..1），用于决定色块上的文字用深色还是浅色。"""
    return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255.0


def _hex(rgb):
    return "#{:02X}{:02X}{:02X}".format(*(util.clamp(v) for v in rgb))


# ---------------------------------------------------------------------------
# 背景识别
# ---------------------------------------------------------------------------
def _border_median_rgb(img):
    """四边像素各通道中值：对白底/黑边这类均匀边框很稳，不受角上小物体影响。"""
    w, h = img.size
    t = max(1, min(w, h) // 50)
    px = img.load()
    samples_r, samples_g, samples_b = [], [], []
    for y in range(h):
        for x in range(w):
            if x < t or x >= w - t or y < t or y >= h - t:
                r, g, b = px[x, y][:3]
                samples_r.append(r)
                samples_g.append(g)
                samples_b.append(b)

    def median(seq):
        seq = sorted(seq)
        n = len(seq)
        return seq[n // 2] if n % 2 else (seq[n // 2 - 1] + seq[n // 2]) / 2.0

    return (median(samples_r), median(samples_g), median(samples_b))


# ---------------------------------------------------------------------------
# 凝聚聚类（带惰性失效的堆优化，O(n^2 log n)，n <= 256）
# ---------------------------------------------------------------------------
def _agglomerate(labs, counts, target, threshold, min_fraction=0.02):
    """返回按权重降序排列的簇：{members, count, centroid(lab)}。

    收敛后再把权重小于 min_fraction（默认 2%）的碎屑簇并入最近邻——
    这些通常是抗锯齿/重采样/JPEG 噪声产生的过渡色，独立成色没有意义。
    """
    n = len(labs)
    members = [[i] for i in range(n)]
    weights = list(counts)
    centroids = list(labs)
    version = [0] * n
    alive = set(range(n))
    total_w = float(sum(weights))

    heap = []
    for i in range(n):
        for j in range(i + 1, n):
            heapq.heappush(heap, (_delta_e(centroids[i], centroids[j]), i, 0, j, 0))

    def _pop_valid():
        while heap:
            dd, ii, vi, jj, vj = heapq.heappop(heap)
            if ii in alive and jj in alive and version[ii] == vi and version[jj] == vj:
                return dd, ii, jj
        return None

    def _merge(i, j):
        tw = weights[i] + weights[j]
        if tw <= 0:  # 防御：空色桶不应到达这里
            alive.remove(j)
            return
        centroids[i] = tuple(
            (centroids[i][c] * weights[i] + centroids[j][c] * weights[j]) / tw
            for c in range(3)
        )
        weights[i] = tw
        members[i].extend(members[j])
        alive.remove(j)
        version[i] += 1
        for k in alive:
            if k != i:
                heapq.heappush(heap, (_delta_e(centroids[i], centroids[k]),
                                      i, version[i], k, version[k]))

    while len(alive) > 1:
        item = _pop_valid()
        if item is None:
            break
        d, i, j = item
        # 颜色数已达标且最近的一对也不够相近 -> 停止
        if len(alive) <= target and d >= threshold:
            break
        _merge(i, j)

    # 碎屑吸收：过渡色/噪声小桶并入最近的存活簇（整图都很碎时除外）
    while len(alive) > 1:
        dust = [i for i in alive if weights[i] < min_fraction * total_w]
        if not dust:
            break
        i = min(dust, key=lambda k: weights[k])
        nearest = min((k for k in alive if k != i),
                      key=lambda k: _delta_e(centroids[i], centroids[k]))
        _merge(i, nearest)

    clusters = [{"members": members[i], "count": weights[i], "centroid": centroids[i]}
                for i in alive]
    clusters.sort(key=lambda c: c["count"], reverse=True)
    return clusters


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def extract(image, params):
    """提取主色。

    参数：
      colors            期望主色数 1..16（色彩单一时实际数量可能更少）
      merge             相近色合并强度 0..100，映射 ΔE 阈值约 3..35
      ignore_background 是否忽略大面积背景色
      bg_threshold      判定为「大面积背景」的最小像素占比（默认 0.25）
      bg_tolerance      背景色 Lab 容差 ΔE（默认 18）

    返回 dict：image（色块映射图）+ colors/background 等统计 meta。
    """
    target = max(1, min(16, int(params.get("colors", 6))))
    merge = max(0, min(100, int(params.get("merge", 50))))
    ignore_bg = bool(params.get("ignore_background", True))
    bg_threshold = max(0.0, min(0.95, float(params.get("bg_threshold", 0.25))))
    bg_tolerance = max(1.0, min(60.0, float(params.get("bg_tolerance", 18))))
    # ΔE <5 肉眼几乎不可辨；强度 100 时放到 35（同色系归并）
    merge_threshold = 3.0 + merge / 100.0 * 32.0

    rgb = util.ensure_rgb(image)
    small = util.downscale_to_max(rgb, ANALYSIS_MAXDIM)

    # 1) 量化到至多 256 个色桶
    q = small.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    palette_flat = q.getpalette() or []
    n_entries = min(256, len(palette_flat) // 3)
    hist = q.histogram()[:n_entries]
    bins_rgb = [tuple(palette_flat[i * 3:i * 3 + 3]) for i in range(n_entries)]
    bins_lab = [rgb_to_lab(c) for c in bins_rgb]
    bin_total = sum(hist)  # 量化覆盖的像素数（=total），作为占比分母依据

    # 2) 背景识别
    ignored_bg = False
    bg_bin_set = set()
    bg_rep = None
    bg_count = 0
    if ignore_bg:
        border_rgb = _border_median_rgb(small)
        border_lab = rgb_to_lab(border_rgb)
        bg_bin_set = {i for i in range(n_entries)
                      if _delta_e(bins_lab[i], border_lab) <= bg_tolerance}
        bg_count = sum(hist[i] for i in bg_bin_set)
        # 占比够大、且忽略后至少还剩 10% 内容，才算背景（保护纯色图）
        if bg_count / max(bin_total, 1) >= bg_threshold and bg_count <= bin_total * 0.9:
            ignored_bg = True
            bg_bins = [i for i in bg_bin_set]
            bg_rep = _weighted_rgb(bins_rgb, hist, bg_bins)
        else:
            bg_bin_set = set()

    fg_bins = [i for i in range(n_entries) if i not in bg_bin_set and hist[i] > 0]
    fg_count = sum(hist[i] for i in fg_bins)

    # 3) 在前景色桶（或全图）上凝聚合并（quantize 可能返回未使用的色桶，已按 count>0 过滤）
    work_bins = fg_bins if ignored_bg else [i for i in range(n_entries) if hist[i] > 0]
    denom = fg_count if ignored_bg and fg_count > 0 else bin_total
    clusters = _agglomerate([bins_lab[i] for i in work_bins],
                            [hist[i] for i in work_bins], target, merge_threshold)

    colors = []
    bin_cluster_rgb = {}
    for cl in clusters:
        member_bins = [work_bins[m] for m in cl["members"]]
        rep = _weighted_rgb(bins_rgb, hist, member_bins)
        ratio = cl["count"] / max(denom, 1)
        for b in member_bins:
            bin_cluster_rgb[b] = rep
        colors.append({"hex": _hex(rep), "rgb": [rep[0], rep[1], rep[2]],
                       "ratio": round(ratio, 6), "pixel_count": int(cl["count"])})
    colors.sort(key=lambda c: c["ratio"], reverse=True)

    # 4) 百分比：保留一位小数，最大余数法修正到合计 100.0%
    tenths = _largest_remainder([c["ratio"] for c in colors], 1000)
    for c, t in zip(colors, tenths):
        c["percent"] = f"{t / 10:.1f}"

    background = None
    if ignored_bg:
        bg_ratio = bg_count / max(bin_total, 1)
        background = {"hex": _hex(bg_rep), "rgb": [bg_rep[0], bg_rep[1], bg_rep[2]],
                      "ratio": round(bg_ratio, 6),
                      "percent": f"{bg_ratio * 100:.1f}",
                      "pixel_count": int(bg_count)}
        for b in bg_bin_set:
            bin_cluster_rgb[b] = bg_rep

    # 5) 色块映射图：每个像素替换为所属主色（背景像素 -> 背景代表色）
    lut = [bin_cluster_rgb.get(i, bins_rgb[i]) for i in range(n_entries)]
    mapped = Image.new("RGB", q.size)
    mapped.putdata([lut[i] for i in q.getdata()])

    return {
        "image": mapped,
        "colors": colors,
        "count": len(colors),
        "requested_count": target,
        "ignored_background": ignored_bg,
        "background": background,
        "total_pixels": int(bin_total),
        "covered_pixels": int(denom),
    }


def _weighted_rgb(bins_rgb, hist, indices):
    total = sum(hist[i] for i in indices) or 1
    return tuple(
        int(round(sum(bins_rgb[i][c] * hist[i] for i in indices) / total))
        for c in range(3)
    )


def _largest_remainder(ratios, units):
    """把占比按最大余数法分成 units 个整数份（这里 units=1000，即一位小数）。"""
    raw = [r * units for r in ratios]
    floors = [int(x) for x in raw]
    leftover = units - sum(floors)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - floors[i], reverse=True)
    out = list(floors)
    for k in range(max(0, leftover)):
        out[order[k % len(order)]] += 1
    return out


# ---------------------------------------------------------------------------
# 色卡图片渲染 / 文本导出
# ---------------------------------------------------------------------------
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
)
_FONT_BOLD_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
)


def _font(size, bold=False):
    for p in (_FONT_BOLD_CANDIDATES if bold else _FONT_CANDIDATES):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def render_swatch(colors, background=None, width=1080):
    """把提取结果渲染成可分享的色卡 PNG：顶部比例条 + 下方等宽色块。"""
    pad = 36
    gap = 12
    title_h, bar_h = 34, 116
    block_h, meta_h = 128, 58
    n = max(1, len(colors))
    cols = min(n, 6)
    rows = (n + cols - 1) // cols
    card_w = (width - pad * 2 - gap * (cols - 1)) // cols
    height = pad + title_h + 18 + bar_h + 26 + rows * (block_h + meta_h + gap) + pad

    img = Image.new("RGB", (width, height), (255, 255, 255))
    d = ImageDraw.Draw(img)
    f_title, f_bar, f_bar_pct = _font(22, True), _font(15, True), _font(12)
    f_hex, f_pct, f_rgb, f_rank = _font(15, True), _font(15, True), _font(12), _font(11, True)
    ink, faint = (28, 33, 42), (130, 138, 150)

    d.text((pad, pad - 4), "Color Palette", font=f_title, fill=ink)
    subtitle = f"{len(colors)} color" + ("s" if len(colors) != 1 else "")
    if background:
        subtitle += f"   ·   background {background['hex']} ({background['percent']}%) ignored"
    tw = d.textbbox((0, 0), subtitle, font=f_bar_pct)[2]
    d.text((width - pad - tw, pad + 2), subtitle, font=f_bar_pct, fill=faint)

    # 比例条
    bar_top = pad + title_h + 18
    x = pad
    for c in colors:
        seg_w = max(1, int(round(c["ratio"] * (width - pad * 2))))
        rgb = tuple(c["rgb"])
        d.rectangle([x, bar_top, x + seg_w, bar_top + bar_h], fill=rgb)
        d.line([x, bar_top, x, bar_top + bar_h], fill=(255, 255, 255), width=1)
        if seg_w >= 92:
            tc = (255, 255, 255) if _luma(rgb) < 0.6 else (20, 24, 30)
            label = c["hex"].upper()
            lw = d.textbbox((0, 0), label, font=f_bar)[2]
            d.text((x + (seg_w - lw) / 2, bar_top + bar_h / 2 - 17), label, font=f_bar, fill=tc)
            pct = f"{c['percent']}%"
            pw = d.textbbox((0, 0), pct, font=f_bar_pct)[2]
            d.text((x + (seg_w - pw) / 2, bar_top + bar_h / 2 + 6), pct, font=f_bar_pct, fill=tc)
        x += seg_w
    d.rectangle([pad, bar_top, width - pad, bar_top + bar_h], outline=(210, 214, 220))

    # 等宽色块卡片
    grid_top = bar_top + bar_h + 26
    for idx, c in enumerate(colors):
        r, col = divmod(idx, cols)
        x0 = pad + col * (card_w + gap)
        y0 = grid_top + r * (block_h + meta_h + gap)
        rgb = tuple(c["rgb"])
        d.rectangle([x0, y0, x0 + card_w, y0 + block_h], fill=rgb)
        d.rectangle([x0, y0, x0 + card_w, y0 + block_h], outline=(210, 214, 220))
        tc = (255, 255, 255) if _luma(rgb) < 0.6 else (20, 24, 30)
        rank = f"{idx + 1:02d}"
        d.text((x0 + 12, y0 + 10), rank, font=f_rank, fill=tc)
        hexs = c["hex"].upper()
        hw = d.textbbox((0, 0), hexs, font=f_hex)[2]
        d.text((x0 + (card_w - hw) / 2, y0 + block_h / 2 - 9), hexs, font=f_hex, fill=tc)

        d.rectangle([x0, y0 + block_h, x0 + card_w, y0 + block_h + meta_h],
                    fill=(247, 248, 250), outline=(210, 214, 220))
        pct = f"{c['percent']}%"
        d.text((x0 + 12, y0 + block_h + 9), pct, font=f_pct, fill=ink)
        rgbs = f"rgb({c['rgb'][0]}, {c['rgb'][1]}, {c['rgb'][2]})"
        rw = d.textbbox((0, 0), rgbs, font=f_rgb)[2]
        d.text((x0 + card_w - rw - 12, y0 + block_h + 11), rgbs, font=f_rgb, fill=faint)

    return img


def palette_text(meta):
    """把提取结果导出为纯文本色卡。"""
    lines = [
        "# 主色卡 Color Palette",
        f"# 主色数量: {meta['count']}"
        + ("" if meta["count"] == meta.get("requested_count", meta["count"])
           else f"（请求 {meta.get('requested_count')}，相近颜色已合并）"),
        f"# 有效像素: {meta['covered_pixels']} / {meta['total_pixels']}",
    ]
    bg = meta.get("background")
    if meta.get("ignored_background") and bg:
        lines.append(f"# 已忽略背景: {bg['hex']}  rgb({bg['rgb'][0]}, {bg['rgb'][1]}, {bg['rgb'][2]})"
                     f"  {bg['percent']}%")
    lines.append("#")
    lines.append("# 序号\tHEX\tRGB\t占比")
    for i, c in enumerate(meta["colors"], 1):
        lines.append(f"{i}\t{c['hex']}\trgb({c['rgb'][0]}, {c['rgb'][1]}, {c['rgb'][2]})"
                     f"\t{c['percent']}%")
    return "\n".join(lines) + "\n"
