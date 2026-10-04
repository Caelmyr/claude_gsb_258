/* 视图：主色提取（调色板）。数量可调、相近色合并、忽略背景，导出图片/文本。 */
window.Views = window.Views || {};
window.Views.palette = (function () {
  const C = window.Common;
  let imgId = null;
  let lastResult = null;

  return {
    mount(el) {
      el.innerHTML = `
        <div class="split">
          <div>
            <div class="panel">
              <div class="panel-title">选择图像</div>
              <div id="pl-gallery"></div>
            </div>
            <div class="panel">
              <div class="panel-title">提取参数</div>
              <div class="field">
                <label>主色数量<span class="hint">色彩单一时实际数量可能更少</span></label>
                <div class="range-row">
                  <input type="range" id="pl-colors" min="1" max="16" step="1" value="6">
                  <span class="range-val" data-val="colors">6</span>
                </div>
              </div>
              <div class="field">
                <label>相近色合并<span class="hint">值越大，越接近的颜色越会被归并</span></label>
                <div class="range-row">
                  <input type="range" id="pl-merge" min="0" max="100" step="1" value="50">
                  <span class="range-val" data-val="merge">50</span>
                </div>
              </div>
              <div class="field">
                <label><input type="checkbox" id="pl-bg" checked> 忽略大面积背景色
                  <span class="hint">自动识别白/黑等边框背景</span></label>
              </div>
              <button class="btn btn-primary" id="pl-run" style="width:100%">提取主色</button>
            </div>
          </div>

          <div>
            <div class="panel">
              <div class="panel-title">色卡
                <span class="dim" id="pl-summary"></span>
                <div style="float:right;display:flex;gap:6px">
                  <button class="btn btn-sm" id="pl-export-png" disabled>导出图片 PNG</button>
                  <button class="btn btn-sm" id="pl-export-txt" disabled>导出文本 TXT</button>
                </div>
              </div>
              <div id="pl-strip" class="palette-strip"><span class="dim">选择图像后提取主色</span></div>
              <div id="pl-colors"></div>
            </div>
            <div class="panel">
              <div class="panel-title">色块映射预览<span class="dim">每个像素替换为所属主色</span></div>
              <div class="stage" id="pl-stage"><span class="dim">提取后显示</span></div>
            </div>
          </div>
        </div>`;

      loadGallery(el);

      el.querySelectorAll('input[type="range"]').forEach((inp) => {
        inp.addEventListener("input", () => {
          el.querySelector(`[data-val="${inp.id.slice(3)}"]`).textContent = inp.value;
        });
      });

      el.querySelector("#pl-run").onclick = async () => {
        if (!imgId) { C.toast("请先选择一张图像", "error"); return; }
        const stage = el.querySelector("#pl-stage");
        stage.innerHTML = `<div class="loading">提取中…</div>`;
        try {
          const r = await Api.post("/api/palette", {
            image_id: imgId,
            colors: Number(el.querySelector("#pl-colors").value),
            merge: Number(el.querySelector("#pl-merge").value),
            ignore_background: el.querySelector("#pl-bg").checked,
          });
          lastResult = r;
          renderResult(el, r);
        } catch (e) {
          stage.innerHTML = `<span class="dim">提取失败：${C.esc(e.message)}</span>`;
        }
      };

      el.querySelector("#pl-export-png").onclick = () => {
        if (lastResult) window.open(`/api/palette/results/${lastResult.result_id}/swatch?t=${Date.now()}`, "_blank");
      };
      el.querySelector("#pl-export-txt").onclick = () => {
        if (lastResult) window.location.href = `/api/palette/results/${lastResult.result_id}/text?t=${Date.now()}`;
      };
    },

    refresh() {
      const el = document.querySelector('.view[data-view="palette"]');
      if (el && this.mounted) loadGallery(el);
    },
  };

  async function loadGallery(el) {
    const images = await C.fetchImages();
    el.querySelector("#pl-gallery").innerHTML = C.galleryHTML(images, { compact: true });
    C.bindGallery(el.querySelector("#pl-gallery"), images, (id) => { imgId = id; }, imgId);
  }

  function renderResult(el, r) {
    const colors = r.colors || [];
    el.querySelector("#pl-export-png").disabled = false;
    el.querySelector("#pl-export-txt").disabled = false;

    let summary = `共 ${colors.length} 种主色`;
    if (r.ignored_background && r.background) {
      summary += ` · 已忽略背景 ${r.background.hex}（${r.background.percent}%）`;
    }
    if (r.count < r.requested_count) {
      summary += ` · 颜色较少，相近色已归并`;
    }
    el.querySelector("#pl-summary").textContent = summary;

    // 按占比的比例条
    el.querySelector("#pl-strip").innerHTML =
      `<div class="pal-bar">` + colors.map((c) =>
        `<div class="pal-bar-seg" title="${C.esc(c.hex)} ${c.percent}%"
           style="flex:${Math.max(c.ratio, 0.0001)} 1 0;background:${C.esc(c.hex)}"></div>`
      ).join("") + `</div>`;

    // 色块列表
    el.querySelector("#pl-colors").innerHTML =
      `<div class="pal-list">` + colors.map((c, i) => `
        <div class="pal-item">
          <div class="pal-swatch" style="background:${C.esc(c.hex)}"><span>${i + 1}</span></div>
          <div class="pal-info">
            <div class="pal-hex mono">${C.esc(c.hex)}</div>
            <div class="pal-rgb mono">rgb(${c.rgb[0]}, ${c.rgb[1]}, ${c.rgb[2]})</div>
          </div>
          <div class="pal-pct mono">${c.percent}%</div>
          <button class="btn btn-sm pal-copy" data-hex="${C.esc(c.hex)}">复制</button>
        </div>`).join("") + `</div>`;

    el.querySelectorAll(".pal-copy").forEach((btn) => {
      btn.onclick = async () => {
        const hex = btn.dataset.hex;
        try {
          await navigator.clipboard.writeText(hex);
          C.toast(`已复制 ${hex}`, "success");
        } catch (e) {
          // file:// 或非安全上下文兜底
          const ta = document.createElement("textarea");
          ta.value = hex; document.body.appendChild(ta); ta.select();
          document.execCommand("copy"); ta.remove();
          C.toast(`已复制 ${hex}`, "success");
        }
      };
    });

    el.querySelector("#pl-stage").innerHTML =
      `<img src="/api/results/${r.result_id}/file?t=${Date.now()}">
       <div class="caption">色块映射图 · ${colors.length} 色 · 占比合计
         ${colors.reduce((s, c) => s + parseFloat(c.percent), 0).toFixed(1)}%</div>`;
  }
})();
