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
