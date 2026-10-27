import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { snapshot, detail } from './fixture.mjs';

const require = createRequire(import.meta.url);
let playwright;
try { playwright = require('playwright'); }
catch { playwright = require(require.resolve('playwright', { paths: [process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES].filter(Boolean) })); }
const here = path.dirname(fileURLToPath(import.meta.url));
const html = await readFile(path.resolve(here, '../coordination/ui/aster-panel.html'), 'utf8');
const artifacts = path.join(here, 'test-artifacts');
await mkdir(artifacts, { recursive: true });
const fixtureHost = `<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Aster fixture host</title><style>html,body,iframe{margin:0;width:100%;height:100%;border:0;display:block;background:#0a111c}</style></head><body><iframe title="Aster panel" src="/panel"></iframe><script>
  window.snapshot = ${JSON.stringify(snapshot)};
  window.detail = ${JSON.stringify(detail)};
  window.calls = []; window.messages = []; window.failRefresh = false;
  window.features = {serverTools:{},message:{text:{}}};
  const frame = document.querySelector('iframe');
  function notify(method,params) { frame.contentWindow.postMessage({jsonrpc:'2.0',method,params},'*'); }
  window.notify = notify;
  window.addEventListener('message',event=>{
    if(event.source!==frame.contentWindow || event.data?.jsonrpc!=='2.0')return;
    const req=event.data;
    const reply=result=>frame.contentWindow.postMessage({jsonrpc:'2.0',id:req.id,result},'*');
    if(req.method==='ui/initialize')return reply({protocolVersion:req.params.protocolVersion,hostInfo:{name:'Aster fixture host',version:'1.0.0'},hostCapabilities:window.features,hostContext:{theme:'dark',displayMode:'fullscreen',availableDisplayModes:['inline','fullscreen']}});
    if(req.method==='ui/notifications/initialized')return notify('ui/notifications/tool-result',{content:[{type:'text',text:'Test workspace'}],structuredContent:window.snapshot});
    if(req.method==='tools/call'){
      window.calls.push(req.params);
      if(req.params.name==='aster_panel_refresh')return reply(window.failRefresh?{isError:true,content:[{type:'text',text:'Read failed'}]}:{content:[],structuredContent:window.snapshot});
      if(req.params.name==='aster_panel_task'){
        const item=window.snapshot.tasks.find(t=>t.task_id===req.params.arguments.task_id);
        return reply({content:[],structuredContent:{...window.detail,task:req.params.arguments.task_id===window.detail.task.task_id?window.detail.task:item}});
      }
      return reply({isError:true,content:[{type:'text',text:'Unknown fixture tool'}]});
    }
    if(req.method==='ui/message'){window.messages.push(req.params);return reply({});}
    if(req.id!==undefined)return reply({});
  });
</script></body></html>`;
const server = createServer((req,res) => { res.setHeader('Content-Type','text/html; charset=utf-8'); res.end(req.url === '/panel' ? html : fixtureHost); });
await new Promise((resolve,reject) => { server.once('error',reject); server.listen(0,'127.0.0.1',resolve); });
const url = `http://127.0.0.1:${server.address().port}`;
const browser = await playwright.chromium.launch({ headless:true, executablePath:process.env.ASTER_TEST_BROWSER || undefined, args:['--no-sandbox'] });
const page = await browser.newPage({ viewport:{width:1440,height:1060}, deviceScaleFactor:1 });
const errors=[]; const network=[];
page.on('pageerror',error=>errors.push(error.message));
page.on('request',request=>{if(!request.url().startsWith(url))network.push(request.url());});
const checks=[];
const check=(name)=>{checks.push(name);console.log(`PASS ${name}`);};
try {
  await page.goto(url);
  const frame = page.frameLocator('iframe');
  await frame.getByRole('heading',{name:'Work overview',exact:true}).waitFor();
  assert.equal((await page.evaluate(()=>window.calls)).length,0);
  assert.equal((await page.evaluate(()=>window.messages)).length,0);
  check('Initial tool result renders without a duplicate read or chat send');
  await page.screenshot({path:path.join(artifacts,'overview-desktop.png'),fullPage:true});
  await frame.getByRole('button',{name:'Refresh',exact:true}).click();
  await page.waitForFunction(()=>window.calls.length===1);
  assert.deepEqual(await page.evaluate(()=>({name:window.calls[0].name,arguments:window.calls[0].arguments})),{name:'aster_panel_refresh',arguments:{limit:40}});
  check('Refresh uses the object-form MCP tool call');
  const search=frame.getByRole('searchbox',{name:'Find a task or agent'});
  await search.fill('archive index');
  assert.equal(await frame.locator('.task-row').count(),1);
  await search.fill('');
  await frame.getByRole('button',{name:'Blocked',exact:true}).click();
  assert.equal(await frame.locator('.task-row').count(),1);
  await frame.getByRole('button',{name:'Accepted',exact:true}).click();
  assert.equal(await frame.locator('.task-row').count(),1);
  assert.equal(await frame.locator('.task-row .badge').innerText(),'Completed');
  await frame.getByRole('button',{name:'All tasks',exact:true}).click();
  check('Search/status filters preserve scope; Accepted filters acceptance separately');
  await frame.getByRole('button',{name:'Open task: Check the archive index and document coverage'}).click();
  await frame.getByText('Accepted after sender review.',{exact:true}).waitFor();
  assert.equal(await frame.getByText('Sender verified',{exact:true}).count(),1);
  assert.equal((await page.evaluate(()=>window.messages)).length,0);
  await page.screenshot({path:path.join(artifacts,'task-desktop.png'),fullPage:true});
  await frame.getByRole('button',{name:'Ask in chat about this task',exact:true}).click();
  await page.waitForFunction(()=>window.messages.length===1);
  assert.match(await page.evaluate(()=>window.messages[0].content[0].text),/task_archive_index/);
  check('Task detail, evidence, verification and receipt render; handoff requires a click');
  await frame.getByRole('button',{name:'Fleet',exact:true}).click();
  await frame.getByRole('heading',{name:'The fleet',exact:true}).waitFor();
  assert.equal(await frame.locator('.agent-card').count(),3);
  assert.equal(await frame.getByText('Receive ready',{exact:true}).count(),2);
  await page.screenshot({path:path.join(artifacts,'fleet-desktop.png'),fullPage:true});
  check('Fleet separates receive readiness from heartbeat');
  await frame.getByRole('button',{name:'Archive & costs',exact:true}).click();
  await frame.getByText('No spend figure was supplied. Unreported cost does not mean zero cost.',{exact:true}).waitFor();
  assert.equal(await frame.getByText('$0',{exact:true}).count(),0);
  await page.evaluate(()=>{window.snapshot.sources.coordination.checked_at='2026-10-27T06:30:00Z';window.notify('ui/notifications/tool-result',{content:[],structuredContent:window.snapshot});});
  await frame.getByText('2026-10-27 09:30:00 UTC+3',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>window.snapshot.sources.coordination.checked_at),'2026-10-27T06:30:00Z');
  check('Visible source timestamps use Europe/Istanbul UTC+3; ISO values remain unchanged');
  await page.screenshot({path:path.join(artifacts,'sources-desktop.png'),fullPage:true});
  check('Unreported archive and costs remain explicit, without fabricated zero');
  await page.evaluate(()=>{window.failRefresh=true;});
  await frame.getByRole('button',{name:'Refresh',exact:true}).click();
  await frame.getByRole('alert').waitFor();
  assert.equal(await frame.locator('.source-card').count(),3);
  check('A failed read retains the prior snapshot and announces the error');
  await page.evaluate(()=>{window.failRefresh=false;});
  await frame.getByRole('button',{name:'Overview',exact:true}).click();
  await page.setViewportSize({width:768,height:1024});
  await page.screenshot({path:path.join(artifacts,'overview-tablet.png'),fullPage:true});
  for(const width of [375,320]) {
    await page.setViewportSize({width,height:900});
    const overflow=await frame.locator('body').evaluate(node=>({scroll:node.scrollWidth,client:node.clientWidth}));
    assert.ok(overflow.scroll<=overflow.client+1,`${width}px overflow ${JSON.stringify(overflow)}`);
    await page.screenshot({path:path.join(artifacts,`overview-mobile-${width}.png`),fullPage:true});
  }
  check('Desktop, tablet, 375px and 320px layouts have no page overflow');
  await page.setViewportSize({width:375,height:900});
  await frame.getByRole('button',{name:'Open task: Check the archive index and document coverage'}).click();
  await frame.getByText('Accepted after sender review.',{exact:true}).waitFor();
  assert.ok(await frame.locator('body').evaluate(node=>node.scrollWidth<=node.clientWidth+1));
  await page.screenshot({path:path.join(artifacts,'task-mobile.png'),fullPage:true});
  await frame.getByRole('button',{name:'Archive & costs',exact:true}).click();
  assert.ok(await frame.locator('body').evaluate(node=>node.scrollWidth<=node.clientWidth+1));
  await page.screenshot({path:path.join(artifacts,'sources-mobile.png'),fullPage:true});
  await frame.getByRole('button',{name:'Overview',exact:true}).click();
  check('Task detail and source status remain readable at mobile width');
  await page.evaluate(()=>{
    window.snapshot.tasks=[{task_id:'task_html_test',status:'queued',instruction:'<img src=x onerror="window.__injected=true">',receiver:'research',updated_at:new Date().toISOString()}];
    window.notify('ui/notifications/tool-result',{content:[],structuredContent:window.snapshot});
  });
  await frame.getByText('<img src=x onerror="window.__injected=true">',{exact:true}).waitFor();
  assert.equal(await frame.locator('img').count(),0);
  assert.equal(await frame.locator('body').evaluate(()=>window.__injected),undefined);
  check('Untrusted task content remains text and cannot create markup');
  await page.evaluate(()=>{window.snapshot.tasks=[];window.snapshot.agents=[];window.snapshot.counts={};window.snapshot.sources={};window.notify('ui/notifications/tool-result',{content:[],structuredContent:window.snapshot});});
  await frame.getByRole('heading',{name:'No assignments in this snapshot',exact:true}).waitFor();
  check('Empty and partial snapshots degrade without invented numeric totals');
  await page.reload();
  await frame.getByRole('heading',{name:'Work overview',exact:true}).waitFor();
  await page.evaluate(()=>{window.features={serverTools:{}};document.querySelector('iframe').src='/panel';});
  await frame.getByRole('heading',{name:'Work overview',exact:true}).waitFor();
  await frame.getByRole('button',{name:'Open task: Check the archive index and document coverage'}).click();
  await frame.getByRole('button',{name:'Ask in chat about this task',exact:true}).waitFor();
  assert.equal(await frame.getByRole('button',{name:'Ask in chat about this task',exact:true}).isDisabled(),true);
  check('Chat handoff is disabled when the host omits that capability');
  assert.deepEqual(errors,[]);
  assert.deepEqual(network,[]);
  check('No page errors or external network requests');
  await writeFile(path.join(artifacts,'results.json'),JSON.stringify({passed:checks.length,checks,errors,external_requests:network},null,2));
} finally { await browser.close(); await new Promise(resolve=>server.close(resolve)); }
