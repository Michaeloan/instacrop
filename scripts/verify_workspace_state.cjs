/* Test response ordering and operation scopes with isolated synthetic sources. */
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'outputs/bug-audit');
const cropOnly = process.argv.includes('--crop-only') || !fs.existsSync(path.join(root, 'scripts/serve_workspace_pages_fixture.py'));
const localPython = path.join(root, '.runtime/Scripts/python.exe'), parentPython = path.join(root, '../.runtime/Scripts/python.exe');
const python = process.env.UI_TEST_PYTHON || (fs.existsSync(localPython) ? localPython : fs.existsSync(parentPython) ? parentPython : 'python');
const gate = () => { let resolve; const promise = new Promise(r => resolve = r); return { promise, resolve }; };
(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ ...(process.env.UI_TEST_BROWSER === 'chromium' ? {} : { channel: 'msedge' }), headless: true });
  const results = [];
  try {
    for (const edition of cropOnly ? ['crop'] : ['full', 'crop']) {
      const directory = edition === 'full' || cropOnly ? root : path.join(root, 'polascan-crop');
      const script = edition === 'full' ? 'scripts/serve_workspace_pages_fixture.py' : 'scripts/serve_ui_fixture.py';
      const server = spawn(python, [path.join(directory, script)], { cwd: directory, env: { ...process.env, PYTHONUTF8: '1' }, windowsHide: true });
      let page;
      try {
        const fixture = await new Promise((resolve, reject) => {
          let received = '';
          server.stdout.on('data', chunk => {
            received += chunk.toString();
            for (const line of received.split('\n')) if (line.startsWith('{')) { try { resolve(JSON.parse(line)); } catch {} }
          });
          server.once('error', reject); server.once('exit', code => reject(new Error(`Fixture exited ${code}`)));
        });
        page = await browser.newPage({ viewport: { width: 1380, height: 920 } });
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.addInitScript(() => { const original = window.setInterval; window.__intervals = []; window.setInterval = (...args) => { const id = original(...args); window.__intervals.push(id); return id; }; });
        await page.route('**/api/live/capabilities', route => route.fulfill({ json: { available: false, reasons: ['Synthetic audit'] } }));
        await page.goto(fixture.url);
        await page.waitForFunction(() => window.Gallery && Workspace.state.id && token);
        await page.evaluate(() => { window.__intervals.forEach(clearInterval); });
        if (edition === 'full') { await page.locator('.mode-nav [data-mode="crop"]').click(); await page.locator('[data-category="photo"]').click(); }
        else await page.locator('.mode-nav [data-mode="photo"]').click();
        const check = async (name, value) => { const ok = await value; results.push({ edition, name, ok }); if (!ok) throw new Error(`${edition}: ${name}`); };

        const original = await page.evaluate(() => JSON.parse(JSON.stringify(Workspace.state)));
        const id = original.pages.find(p => original.sources.find(s => s.id === p.source_id)?.kind === 'photo').photos[0].id;
        const newer = structuredClone(original);
        newer.pages.flatMap(p => p.photos).find(p => p.id === id).enabled = false;
        const oldStarted = gate(), oldRelease = gate(); let reads = 0;
        await page.route('**/api/workspace', async route => {
          if (++reads === 1) { oldStarted.resolve(); await oldRelease.promise; await route.fulfill({ json: original }); }
          else await route.fulfill({ json: newer });
        });
        const oldRefresh = page.evaluate(() => Workspace.refresh());
        await oldStarted.promise;
        await page.evaluate(() => Workspace.refresh());
        oldRelease.resolve(); await oldRefresh;
        await check('Older refresh cannot overwrite a newer selection snapshot', page.evaluate(id => !Workspace.photos().find(x => x.photo.id === id).photo.enabled, id));
        await page.unroute('**/api/workspace'); await page.evaluate(() => Workspace.refresh());

        const selectionStarted = gate(), selectionRelease = gate();
        await page.route('**/api/workspace/select', async route => { selectionStarted.resolve(); await selectionRelease.promise; await route.continue(); });
        await page.evaluate(id => { window.__selection = Workspace.select([id], false); }, id);
        await selectionStarted.promise;
        await check('Export and batch actions wait for selection persistence', Promise.all([page.locator('#export').isDisabled(), page.locator('#batchEdit').isDisabled()]).then(values => values.every(Boolean)));
        selectionRelease.resolve(); await page.evaluate(() => window.__selection);
        await check('Selection completes with the selected identifier excluded', page.evaluate(id => !Workspace.ids().includes(id), id));
        await page.unroute('**/api/workspace/select'); await page.evaluate(id => Workspace.select([id], true), id);

        const batchIds = await page.evaluate(() => Workspace.ids());
        await page.locator('#batchEdit').click();
        await page.evaluate(() => { const page = Workspace.state.pages.find(p => p.photos.length); const photo = JSON.parse(JSON.stringify(page.photos[0])); photo.id = 'f'.repeat(32); photo.enabled = true; page.photos.push(photo); });
        let applied;
        await page.route('**/api/workspace/apply', async route => { applied = route.request().postDataJSON().photo_ids; await route.fulfill({ json: { undo_token: 'synthetic-batch' } }); });
        await page.locator('#batchFields .batch-field').first().locator('input').first().check();
        await page.locator('#batchApply').click();
        await page.locator('#batchDialog').waitFor({ state: 'hidden' });
        await check('Batch applies only to the identifiers shown when opened', JSON.stringify(applied) === JSON.stringify(batchIds));
        await page.unroute('**/api/workspace/apply');
        await page.evaluate(() => Workspace.refresh());

        await page.locator('.photo-card .adjust-button').first().click(); await page.locator('#editDialog').waitFor({ state: 'visible' });
        await page.evaluate(() => document.getElementById('applyRepairAll').click());
        await check('Unsaved single-photo edits cannot be overwritten by a batch dialog', !(await page.locator('#batchDialog').isVisible()));
        await page.locator('#cancelEdit').click();

        const activationStarted = gate(), activationRelease = gate();
        let response;
        await page.route('**/api/workspace/page', async route => { response = await (await route.fetch()).json(); activationStarted.resolve(); await activationRelease.promise; await route.fulfill({ json: response }); });
        const pageId = await page.evaluate(id => Workspace.photos().find(x => x.photo.id === id).page.id, id);
        await page.evaluate(id => { window.__scanBefore = scan; window.__activation = Workspace.activate(id); }, pageId);
        await activationStarted.promise;
        await page.evaluate(async () => { await Workspace.request('/api/workspace/new', { cancel_tasks: true }); await Workspace.refresh(); });
        activationRelease.resolve();
        const discarded = await page.evaluate(() => window.__activation);
        await check('Batch replacement discards the pending page activation', discarded === null && await page.evaluate(() => scan === window.__scanBefore));
        await check('No JavaScript runtime errors', errors.length === 0);
      } finally {
        if (page) await page.close(); server.kill();
      }
    }
    fs.writeFileSync(path.join(output, 'workspace-state.json'), JSON.stringify({ status: 'ok', checks: results }, null, 2));
    console.log(JSON.stringify({ status: 'ok', checks: results.length }));
  } catch (error) {
    fs.writeFileSync(path.join(output, 'workspace-state.json'), JSON.stringify({ status: 'failed', checks: results, error: error.stack }, null, 2));
    throw error;
  } finally { await browser.close(); }
})();
