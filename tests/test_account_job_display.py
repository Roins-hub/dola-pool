import re
import shutil
import subprocess
from pathlib import Path

import pytest


def test_add_failure_is_historical_only_after_new_successful_verification():
    node=shutil.which('node')
    if not node: pytest.skip('node required')
    html=(Path(__file__).resolve().parents[1]/'web/index.html').read_text(encoding='utf8')
    start=html.index('function statusBadge(a)')
    end=html.index('function table(heads,rows)',start)
    code=html[start:end]
    script=r"""
const assert=require('assert'),pendingVerifications=new Map();
const esc=s=>String(s).replaceAll('<','&lt;'),noTr=esc;
""" + code + r"""
const job={kind:'add',status:'failed',finished_at:100,error:'<old error>'};
const a={name:'acc25',busy:false,login_ok:1,login_checked_at:200};
assert(!accountRunBadge(a,job).includes('加号失败'));
assert(accountJobNote(a,job).includes('之后验证已通过'));
assert(accountJobNote(a,job).includes('&lt;old error>'));
assert(accountRunBadge({...a,login_checked_at:99},job).includes('加号失败'));
assert(accountRunBadge({...a,login_ok:0},job).includes('加号失败'));
assert(accountRunBadge(a,{...job,finished_at:undefined}).includes('加号失败'));
assert(accountRunBadge(a,{...job,status:'running'}).includes('加号中'));
assert(accountRunBadge({...a,busy:true,operation:'verifying'},job).includes('验证中'));
assert(!accountRunBadge({...a,busy:true,operation:'verifying'},job).includes('加号失败'));
pendingVerifications.set('acc25',Date.now());
assert(accountRunBadge(a,job).includes('验证中'));
"""
    subprocess.run([node],input=script,text=True,encoding='utf8',capture_output=True,check=True,timeout=15)


def test_doubao_work_account_is_labeled_and_test_dialog_defaults_to_work_mode():
    node=shutil.which('node')
    if not node: pytest.skip('node required')
    html=(Path(__file__).resolve().parents[1]/'web/index.html').read_text(encoding='utf8')
    badge=html[html.index('function srcBadge(x)'):html.index('/** 同一个邮箱加了两遍')]
    start=html.index('function openTestGen(name)')
    end=html.index('function syncTestGenDurations()',start)
    code=html[start:end]
    script=r"""
const assert=require('assert');
const allAccounts=[{name:'acc153',site:'doubao',doubao_account_type:'work'}];
let rendered='';
let testGenPollTimer=null;
const noTr=x=>x;
function stopTestGenPoll(){testGenPollTimer=null;}
function showDlg(x){rendered=x;}
""" + badge + code + r"""
assert(srcBadge(allAccounts[0]).includes('工作号 · 仅工作模式'));
openTestGen('acc153');
assert(rendered.includes('value="doubao-seedance-2.0-fast" selected'));
assert(rendered.includes('value="auto" disabled'));
assert(rendered.includes('value="work" selected'));
assert(rendered.includes('<option value="15" selected>15</option>'));
allAccounts.push({name:'acc154',site:'doubao',doubao_account_type:'personal'});
openTestGen('acc154');
assert(rendered.includes('value="doubao-seedance-2.0-mini" selected'));
assert(rendered.includes('value="auto" selected'));
assert(rendered.includes('value="work" disabled'));
"""
    subprocess.run([node],input=script,text=True,encoding='utf8',capture_output=True,check=True,timeout=15)


def test_work_mode_option_only_disables_dola_25():
    node=shutil.which('node')
    if not node: pytest.skip('node required')
    html=(Path(__file__).resolve().parents[1]/'web/index.html').read_text(encoding='utf8')
    start=html.index('const TEST_GEN_DURATIONS=')
    end=html.index('function syncTestGenCount()',start)
    code=html[start:end]
    script=r"""
const assert=require('assert');
const model={value:'doubao-seedance-2.5'};
const option={disabled:false};
const mode={value:'work',querySelector:()=>option};
const duration={value:'15',innerHTML:''},hint={innerHTML:''};
const q=s=>s==='#tg_model'?model:s==='#tg_mode'?mode:s==='#tg_duration'?duration:hint;
function syncTestGenCount(){}
"""+code+r"""
syncTestGenDurations();
assert(mode.value==='work'&&!option.disabled);
model.value='seedance-2.5';
mode.value='work';
syncTestGenDurations();
assert(mode.value==='auto'&&option.disabled);
"""
    subprocess.run([node],input=script,text=True,encoding='utf8',capture_output=True,check=True,timeout=15)
