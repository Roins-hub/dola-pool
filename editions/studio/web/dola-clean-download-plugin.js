/* Optional Dola task-detail control for verified platform originals only.
 * The host page injects its authenticated API and file downloader. This module
 * never reads a Dola cookie, signed media URL, or admin credential.
 */
window.DolaCleanDownloadPlugin = (() => {
  'use strict';

  const mounted = new WeakMap();

  function taskBase(taskId) {
    if (typeof taskId !== 'string' || !/^video_[A-Za-z0-9_-]{8,128}$/.test(taskId)) {
      throw new Error('无效的任务 ID');
    }
    return '/api/admin/tasks/' + encodeURIComponent(taskId) + '/clean-download';
  }

  function fileUrl(taskId, kind) {
    if (kind !== 'official') throw new Error('只支持下载已核验的官方原片');
    return taskBase(taskId) + '/file?kind=official';
  }

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function mount({container, task, api, downloadFile, notify} = {}) {
    if (!(container instanceof Element)) throw new Error('缺少插件容器');
    mounted.get(container)?.destroy();
    if (!task || (task.platform || 'dola') !== 'dola') {
      container.replaceChildren();
      return null;
    }
    if (typeof api !== 'function' || typeof downloadFile !== 'function') {
      throw new Error('需要注入已鉴权的 API 与文件下载函数');
    }
    const base = taskBase(task.id);
    let destroyed = false;
    let busy = false;
    let status = null;
    let statusRequest = 0;
    const announce = typeof notify === 'function' ? notify : () => {};

    const section = element('section', undefined, 'detail-block');
    section.setAttribute('aria-label', '官方无水印原片下载');
    section.append(element('h3', '官方无水印原片'));
    section.append(element('p', '只下载平台提供并核验过的原片；查询沿用此任务的账号和绑定 ISP，不会重新提交提示词。', 'hint'));
    const row = element('div', undefined, 'toolbar-actions');
    const checkButton = element('button', '同账号同线路查询官方原片', 'btn');
    checkButton.type = 'button';
    const downloadButton = element('button', '下载官方无水印原片', 'btn');
    downloadButton.type = 'button';
    downloadButton.disabled = true;
    row.append(checkButton, downloadButton);
    section.append(row);
    const officialNote = element('p', '正在查询官方原片状态…', 'hint');
    section.append(officialNote);
    const feedback = element('p', '', 'hint');
    feedback.setAttribute('role', 'status');
    section.append(feedback);
    container.replaceChildren(section);

    function verified() {
      return status?.official?.available === true && status.official.verified === true;
    }

    function showStatus() {
      const isVerified = verified();
      container.setAttribute('aria-busy', String(busy));
      downloadButton.disabled = busy || !isVerified;
      checkButton.disabled = busy || isVerified || !task.conversation_id;
      officialNote.textContent = isVerified
        ? '平台已核验这条视频的官方无水印原片，可以下载。'
        : String(status?.official?.reason || '平台尚未提供已核验的无水印原片。');
    }

    function showError(error) {
      const message = error?.status === 404
        ? '插件页面已更新，后台仍是旧版；请应用项目更新后重试。'
        : String(error?.message || error || '操作失败');
      feedback.textContent = message;
      announce(message, true);
    }

    async function refresh() {
      if (destroyed) return;
      const request = ++statusRequest;
      try {
        const next = await api(base + '/status');
        if (destroyed || request !== statusRequest) return;
        status = next || {};
        showStatus();
      } catch (error) {
        if (destroyed || request !== statusRequest) return;
        status = null;
        showStatus();
        showError(error);
      }
    }

    async function checkOfficial() {
      if (destroyed || busy || verified() || !task.conversation_id) return;
      busy = true;
      showStatus();
      feedback.textContent = '正在同账号、同 ISP 查询已有视频；不会重新提交生成请求。';
      try {
        await api('/api/admin/tasks/' + encodeURIComponent(task.id) + '/download', {method: 'POST'});
        if (destroyed) return;
        await refresh();
        if (!destroyed) feedback.textContent = verified()
          ? '官方原片已核验，可以下载。'
          : '查询完成；平台尚未提供可核验的官方原片。';
      } catch (error) {
        if (!destroyed) {
          showError(error);
          await refresh();
        }
      } finally {
        busy = false;
        if (!destroyed) showStatus();
      }
    }

    async function downloadOfficial() {
      if (destroyed || busy || !verified()) return;
      busy = true;
      showStatus();
      feedback.textContent = '正在下载官方原片…';
      try {
        await downloadFile(fileUrl(task.id, 'official'), task.id + '-official.mp4');
        if (!destroyed) feedback.textContent = '官方原片下载已开始。';
      } catch (error) {
        if (!destroyed) showError(error);
      } finally {
        busy = false;
        if (!destroyed) showStatus();
      }
    }

    checkButton.addEventListener('click', checkOfficial);
    downloadButton.addEventListener('click', downloadOfficial);
    const controller = {
      refresh,
      destroy() {
        destroyed = true;
        statusRequest++;
        if (mounted.get(container) === controller) mounted.delete(container);
      }
    };
    mounted.set(container, controller);
    controller.ready = refresh();
    return controller;
  }

  return {mount, taskBase, fileUrl};
})();
