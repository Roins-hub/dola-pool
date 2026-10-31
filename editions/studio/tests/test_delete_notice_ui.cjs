/* Actual shared UI in Chromium; restoration is an isolated local callback. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('./browser-runtime.cjs');

async function until(page,predicate){
  for(let i=0;i<100;i++){
    if(await page.evaluate(predicate))return;
    await new Promise(resolve=>setTimeout(resolve,20));
  }
  throw new Error('delete notice did not reach the expected state');
}

async function fixture(){
  const browser=await chromium.launch({headless:true});
  const page=await browser.newPage({viewport:{width:455,height:900}});
  page.setDefaultTimeout(3000);
  await page.setContent('<main><h1>任务列表</h1><button id="list-action">查看任务</button></main>');
  await page.addStyleTag({path:path.join(__dirname,'../web/ui.css')});
  await page.evaluate(fs.readFileSync(path.join(__dirname,'../web/ui.js'),'utf8'));
  await page.evaluate(fs.readFileSync(path.join(__dirname,'../web/task-results-ui.js'),'utf8'));
  await page.evaluate(()=>{
    window.restored=[];
    window.undoTimers=new Map();
    let nextTimer=0;
    const originalSet=window.setTimeout,originalClear=window.clearTimeout;
    window.setTimeout=(callback,delay,...args)=>{
      if(delay!==60000)return originalSet(callback,delay,...args);
      const id=--nextTimer;undoTimers.set(id,()=>callback(...args));return id;
    };
    window.clearTimeout=id=>{if(undoTimers.has(id))undoTimers.delete(id);else originalClear(id);};
    window.expireUndo=()=>{const callbacks=[...undoTimers.values()];undoTimers.clear();callbacks.forEach(fn=>fn());};
  });
  return {browser,page};
}

test('repeated deletions show one compact notice and preserve every undo callback',async()=>{
  const {browser,page}=await fixture();
  try{
    await page.evaluate(()=>{
      for(let i=1;i<=12;i++)TaskResultsUI.undoNotice(async()=>{restored.push(i);});
    });
    assert.equal(await page.locator('.task-delete-undo').count(),1,'continuous deletes must not cover the task list with separate notices');
    assert.match(await page.locator('.task-delete-undo').innerText(),/12/);
    const box=await page.locator('.task-delete-undo').boundingBox();
    assert.ok(box.height<150,'the notice stays compact on a narrow screen');
    await page.getByRole('button',{name:'撤销全部',exact:true}).click();
    await until(page,()=>window.restored.length===12);
    assert.deepEqual(await page.evaluate(()=>restored),[1,2,3,4,5,6,7,8,9,10,11,12]);
    await page.locator('.task-delete-undo').waitFor({state:'detached'});
  }finally{await browser.close();}
});

test('closing or expiring the delete notice does not restore deleted tasks',async()=>{
  const {browser,page}=await fixture();
  try{
    await page.evaluate(()=>TaskResultsUI.undoNotice(async()=>restored.push('closed')));
    await page.getByRole('button',{name:'关闭删除提示',exact:true}).click();
    assert.equal(await page.locator('.task-delete-undo').count(),0);
    await page.evaluate(()=>{TaskResultsUI.undoNotice(async()=>restored.push('expired'));expireUndo();});
    assert.equal(await page.locator('.task-delete-undo').count(),0);
    assert.deepEqual(await page.evaluate(()=>restored),[]);
  }finally{await browser.close();}
});

test('a failed grouped undo keeps only failed deletions available for retry',async()=>{
  const {browser,page}=await fixture();
  try{
    await page.evaluate(()=>{
      window.attempts=0;
      TaskResultsUI.undoNotice(async()=>restored.push('first'));
      TaskResultsUI.undoNotice(async()=>{if(++attempts===1)throw new Error('恢复暂时失败');restored.push('second');});
      TaskResultsUI.undoNotice(async()=>restored.push('third'));
    });
    await page.getByRole('button',{name:'撤销全部',exact:true}).click();
    await until(page,()=>window.restored.length===2);
    assert.equal(await page.locator('.task-delete-undo').count(),1);
    await page.getByRole('button',{name:'撤销',exact:true}).click();
    await until(page,()=>window.restored.length===3);
    assert.deepEqual(await page.evaluate(()=>restored),['first','third','second']);
    await page.locator('.task-delete-undo').waitFor({state:'detached'});
  }finally{await browser.close();}
});

test('a new deletion arriving during undo remains available after that undo completes',async()=>{
  const {browser,page}=await fixture();
  try{
    await page.evaluate(()=>TaskResultsUI.undoNotice(async()=>{await new Promise(resolve=>window.finishFirstUndo=resolve);restored.push('first');}));
    await page.getByRole('button',{name:'撤销',exact:true}).click();
    await until(page,()=>typeof window.finishFirstUndo==='function');
    await page.evaluate(()=>{TaskResultsUI.undoNotice(async()=>restored.push('second'));finishFirstUndo();});
    await until(page,()=>window.restored.length===1);
    assert.equal(await page.locator('.task-delete-undo').count(),1);
    await page.getByRole('button',{name:'撤销',exact:true}).click();
    await until(page,()=>window.restored.length===2);
    assert.deepEqual(await page.evaluate(()=>restored),['first','second']);
    await page.locator('.task-delete-undo').waitFor({state:'detached'});
  }finally{await browser.close();}
});
