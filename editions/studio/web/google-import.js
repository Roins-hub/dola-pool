(() => {
  let running = false;
  function openGoogleImport() {
    if (running) return;
    showDlg(`<h2>导入 Google 账号</h2>
      <p class="hint">每行一个：邮箱----密码----备用邮箱。第三段也可填写 TOTP 验证器密钥；没有额外验证资料可只填邮箱和密码。</p>
      <label for="googleImportText">账号资料</label>
      <textarea id="googleImportText" rows="8" autocomplete="off" spellcheck="false" placeholder="邮箱----密码----备用邮箱"></textarea>
      <label for="googleImportCategory">新账号分类</label>
      <select id="googleImportCategory"><option value="disposable">日抛号</option><option value="long_term">接码长期号</option></select>
      <p class="hint">后台逐个登录 Dola，确认成功后才参与视频生成。Google 登录使用项目默认代理；视频生成使用分配给账号的 ISP。密码在本机加密保存，成功后清除。需要验证码的账号显示“登录待处理”。</p>
      <p class="hint">导入进度可在账号管理的“已停用”列表查看，成功后会按分类开关进入“已启用”列表。</p>
      <p id="googleImportStatus" role="status" aria-live="polite"></p>
      <div class="row"><button class="btn" onclick="hide('dlg');q('#dlgBox').replaceChildren()">关闭</button>
      <button id="googleImportSubmit" class="btn btn-p" onclick="submitGoogleImport()">导入并登录</button></div>`);
  }
  async function submitGoogleImport() {
    if (running) return;
    const input=q('#googleImportText'), button=q('#googleImportSubmit'), status=q('#googleImportStatus');
    const text=input.value.trim();
    if (!text) {status.textContent='请填写账号资料。';return;}
    if (text.length>262144) {status.textContent='内容过大，请分批导入。';return;}
    const category=q('#googleImportCategory').value||'disposable';
    running=true;button.disabled=true;status.textContent='正在保存账号并加入登录队列…';
    try {
      const result=await api('/api/admin/accounts/import-google',{method:'POST',body:{text,category}});
      input.value='';
      status.textContent=`已导入 ${result.imported} 个，重复跳过 ${result.skipped} 个。正在后台逐个登录；导入数量不代表可用数量。`;
      try {await refresh();} catch {}
    } catch (error) {
      status.textContent=error.message||'导入失败，请重试。';
    } finally {running=false;button.disabled=false;}
  }
  async function retryGoogleLogin(name,button) {
    button.disabled=true;
    try {await api('/api/admin/accounts/'+encodeURIComponent(name)+'/retry-google',{method:'POST'});await refresh();}
    catch(error) {UI.toast(error.message||'重试失败',true);}
    finally {button.disabled=false;}
  }
  Object.assign(window,{openGoogleImport,submitGoogleImport,retryGoogleLogin});
})();
