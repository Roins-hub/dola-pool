(() => {
  let files = [], running = false;
  function openBatchImportCK() {
    if (running) return;
    files = [];
    showDlg(`<h2>批量导入 CK</h2>
      <p class="hint">选择多个 TXT / JSON，或选择整个 CK 文件夹。支持完整 CK、浏览器 JSON、Netscape 文件和一个 TXT 内的多个账号；重复账号自动跳过。</p>
      <label for="batchCKFiles">选择 CK 文件（可多选）</label>
      <input id="batchCKFiles" type="file" accept=".txt,.json,text/plain,application/json" multiple onchange="selectBatchCK(this)">
      <label for="batchCKFolder">或选择 CK 文件夹</label>
      <input id="batchCKFolder" type="file" webkitdirectory multiple onchange="selectBatchCK(this)">
      <p id="batchCKSelection" class="hint">尚未选择文件。每批最多 100 个，每个不超过 128 KB。</p>
      <label for="batchCKCategory">新账号分类</label><select id="batchCKCategory"><option value="long_term">接码长期号</option><option value="disposable">日抛号</option></select>
      <p class="hint">本批新账号归入所选分类；重复账号保留原分类。</p>
      <p class="hint">新账号自动随机绑定已配置的 ISP；没有线路时使用默认代理。导入后可在列表验证登录状态。</p>
      <p id="batchCKStatus" role="status" aria-live="polite"></p>
      <div id="batchCKResults" style="max-height:260px;overflow:auto;overflow-wrap:anywhere"></div>
      <div class="row"><button id="batchCKClose" class="btn" onclick="closeBatchCK()">关闭</button>
      <button id="batchCKSubmit" class="btn btn-p" onclick="submitBatchCK()" disabled>一键导入</button></div>`);
  }
  function selectBatchCK(input) {
    if (running) return;
    files = Array.from(input.files || []).filter(file => /\.(txt|json)$/i.test(file.name));
    const other = q(input.id === 'batchCKFiles' ? '#batchCKFolder' : '#batchCKFiles');
    other.value = '';
    q('#batchCKSelection').textContent = `已选择 ${files.length} 个 CK 文件。`;
    q('#batchCKStatus').textContent = files.length > 100 ? '每批最多 100 个，请重新选择。' : '';
    q('#batchCKResults').replaceChildren();
    q('#batchCKSubmit').disabled = files.length === 0 || files.length > 100;
  }
  function closeBatchCK() {
    if (running) return;
    files = [];
    hide('dlg'); q('#dlgBox').replaceChildren();
  }
  async function submitBatchCK() {
    if (running || !files.length || files.length > 100) return;
    running = true;
    const selected = files.slice();
    const category = q('#batchCKCategory').value || 'long_term';
    const status = q('#batchCKStatus'), results = q('#batchCKResults');
    const controls = ['#batchCKFiles', '#batchCKFolder', '#batchCKClose', '#batchCKCategory', '#batchCKSubmit'].map(q);
    controls.forEach(el => { el.disabled = true; });
    results.replaceChildren();
    let imported = 0, skipped = 0, failed = 0;
    try {
      for (let i = 0; i < selected.length; i++) {
        const file = selected[i];
        status.textContent = `正在导入 ${i + 1}/${selected.length}：${file.name}`;
        let detail;
        try {
          if (file.size > 131072) throw new Error('超过 128 KB，已跳过');
          const ck = (await file.text()).replace(/^\uFEFF/, '').trim();
          if (!ck) throw new Error('文件为空');
          const stem = file.name.replace(/\.(txt|json)$/i, '').replace(/^cookie_/i, '').replace(/[^A-Za-z0-9_-]/g, '_');
          const name = ('ck_' + stem).slice(0, 64);
          const result = await api('/api/admin/accounts/import-ck-batch', {method:'POST', body:{name, ck, category}});
          imported += result.imported; skipped += result.skipped; failed += result.failed;
          detail = result.results.map(item => item.error ? `失败：${item.error}` :
            item.existing ? `重复跳过（已有：${item.name}）` : `导入成功：${item.name}`).join('；');
        } catch (error) {
          failed++;
          detail = `失败：${error.message || '导入失败'}`;
        }
        const line = document.createElement('p');
        line.textContent = `${file.name} — ${detail}`;
        results.append(line);
      }
      status.textContent = `处理完成：导入 ${imported} 个，重复跳过 ${skipped} 个，失败 ${failed} 个。`;
      try { await refresh(); } catch { /* Results remain visible if list refresh fails. */ }
    } finally {
      files = [];
      running = false;
      controls.slice(0, -1).forEach(el => { el.disabled = false; });
      q('#batchCKSubmit')?.setAttribute('disabled', '');
    }
  }
  Object.assign(window, {openBatchImportCK, selectBatchCK, closeBatchCK, submitBatchCK});
})();
