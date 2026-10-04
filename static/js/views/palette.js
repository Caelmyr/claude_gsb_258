/* 视图：主色提取（取色）。 */
window.Views = window.Views || {};
window.Views.palette = (function () {
  const C = window.Common;
  let imgId = null;
  let lastResult = null;   // 最近一次提取结果（导出用）

  function swatchRow(p) {
    const pct = (p.share * 100).toFixed(1);
    return `
      <div class="swatch-row" data-hex="${C.esc(p.hex)}" title="点击复制 ${C.esc(p.hex)}">
        <span class="swatch-chip" style="background:${C.esc(p.hex)}"></span>
        <span class="mono swatch-hex">${C.esc(p.hex.toUpperCase())}</span>
        <span class="swatch-rgb dim">rgb(${p.rgb.join(", ")})</span>
        <span class="swatch-bar"><i style="width:${pct}%;background:${C.esc(p.hex)}"></i></span>
        <span class="mono swatch-pct">${pct}%</span>
      </div>`;
  }

  function renderResult(el, r) {
    const d = r.diagnostics || {};
    const bgNote = d.background_ignored
      ? `已忽略背景色 <span class="mono">${C.esc(d.background_hex || "")}</span>（占 ${(d.background_share * 100).toFixed(1)}%），占比已按剩余像素归一`
      : "未检测到可忽略的大面积背景色";

    el.querySelector("#pl-card").innerHTML =
      `<img src="/api/results/${r.result_id}/file?t=${Date.now()}">
       <div class="caption">色卡图 · 可点下方「导出 PNG」保存</div>`;

    el.querySelector("#pl-swatches").innerHTML =
      `<div style="margin-bottom:8px">${r.palette.map(swatchRow).join("")}</div>
       <div class="dim" style="font-size:12px">${bgNote} · 色彩丰富度 ${(d.richness * 100).toFixed(0)}%
         · 聚类 ${d.clusters_before_merge} → ${d.returned_colors} 色 · 占比合计 ${
         (r.palette.reduce((s, p) => s + p.share, 0) * 100).toFixed(1)}%</div>`;

    el.querySelector("#pl-export").style.display = "";
  }

  async function doExport(fmt, filename) {
    if (!lastResult) { C.toast("请先提取主色", "error"); return; }
    const blob = await Api.post("/api/palette/export", {
      palette: lastResult.palette,
      diagnostics: lastResult.diagnostics,
      format: fmt,
      title: (lastResult._filename || "提取主色"),
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `palette.${fmt}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    C.toast(`已导出 palette.${fmt}`, "success");
  }

  return {
    mount(el) {
      el.innerHTML = `
        <div class="split">
          <div class="col">
            <div class="panel"><div class="panel-title">选择图像</div><div id="pl-gallery" style="max-height:380px;overflow:auto"></div></div>
            <div class="panel">
              <div class="panel-title">取色参数</div>
              <div class="field"><label>主色数量</label>
                <div class="range-row"><input type="range" id="pl-colors" min="2" max="12" value="6"><span class="range-val">6</span></div>
              </div>
              <div class="field"><label>忽略大面积背景色<span class="hint">白底/纯色底图自动剔除底色</span></label>
                <div><input type="checkbox" id="pl-ignore-bg" checked></div>
              </div>
              <div class="field"><label>合并相近颜色<span class="hint">避免几乎一样的颜色拆成好几种</span></label>
                <div><input type="checkbox" id="pl-merge" checked></div>
              </div>
              <div class="field"><label>合并阈值（越大合并越激进）</label>
                <div class="range-row"><input type="range" id="pl-merge-th" min="4" max="60" value="12"><span class="range-val">12</span></div>
              </div>
              <button class="btn btn-primary" id="pl-run">提取主色</button>
            </div>
          </div>
          <div class="col">
            <div class="panel"><div class="panel-title">色卡预览</div><div class="stage" id="pl-card"><span class="dim">选择图像并提取</span></div></div>
            <div class="panel">
              <div class="panel-title">主色列表 <span class="dim">点击色块复制 HEX</span></div>
              <div id="pl-swatches"><span class="dim">暂无</span></div>
            </div>
            <div class="panel" id="pl-export" style="display:none">
              <div class="panel-title">导出色卡</div>
              <div class="toolbar" style="margin-bottom:0">
                <button class="btn" data-exp="png">导出 PNG 色卡</button>
                <button class="btn" data-exp="txt">导出文本</button>
                <button class="btn" data-exp="css">导出 CSS 变量</button>
                <button class="btn" data-exp="json">导出 JSON</button>
              </div>
            </div>
          </div>
        </div>`;

      C.fetchImages().then((images) => {
        el.querySelector("#pl-gallery").innerHTML = C.galleryHTML(images);
        C.bindGallery(el.querySelector("#pl-gallery"), images, (id, rec) => {
          imgId = id;
          el.dataset.filename = rec ? rec.filename : "";
        });
      });

      el.querySelectorAll('input[type="range"]').forEach((inp) => {
        inp.addEventListener("input", () => { inp.nextElementSibling.textContent = inp.value; });
      });

      el.querySelector("#pl-run").onclick = async () => {
        if (!imgId) { C.toast("请选择图像", "error"); return; }
        el.querySelector("#pl-card").innerHTML = `<div class="loading">提取中…</div>`;
        try {
          const r = await Api.post("/api/palette", {
            image_id: imgId,
            colors: Number(el.querySelector("#pl-colors").value),
            ignore_bg: el.querySelector("#pl-ignore-bg").checked,
            merge_similar: el.querySelector("#pl-merge").checked,
            merge_threshold: Number(el.querySelector("#pl-merge-th").value),
          });
          r._filename = el.dataset.filename || "提取主色";
          lastResult = r;
          renderResult(el, r);
        } catch (e) {
          el.querySelector("#pl-card").innerHTML = `<span class="dim">提取失败：${C.esc(e.message)}</span>`;
        }
      };

      // 点击色块复制 HEX
      el.querySelector("#pl-swatches").addEventListener("click", (e) => {
        const row = e.target.closest(".swatch-row");
        if (!row) return;
        const hex = row.dataset.hex;
        if (navigator.clipboard) {
          navigator.clipboard.writeText(hex).then(
            () => C.toast(`已复制 ${hex}`, "success"),
            () => C.toast("复制失败", "error"));
        }
      });

      el.querySelector("#pl-export").addEventListener("click", (e) => {
        const btn = e.target.closest("[data-exp]");
        if (btn) doExport(btn.dataset.exp);
      });
    },
  };
})();
