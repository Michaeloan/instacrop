/* npm install --no-save playwright; node scripts/verify_ui_pages.cjs */
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'outputs/ui-pages');

(async () => {
  const localPython = path.join(root, '.runtime/Scripts/python.exe');
  const parentPython = path.join(root, '../.runtime/Scripts/python.exe');
  const python = process.env.UI_TEST_PYTHON || (fs.existsSync(localPython) ? localPython : fs.existsSync(parentPython) ? parentPython : 'python');
  const server = spawn(python, [path.join(root, 'scripts/serve_ui_fixture.py')], { cwd: root, env: { ...process.env, PYTHONUTF8: '1' }, windowsHide: true });
  let browser;
  const checks = [], errors = [];
  const check = (name, ok) => { assert.ok(ok, name); checks.push(name); };
  try {
    const fixture = await new Promise((resolve, reject) => {
      let received = '';
      server.stdout.on('data', chunk => {
        received += chunk.toString();
        for (const line of received.split('\n')) if (line.startsWith('{')) { try { resolve(JSON.parse(line)); } catch {} }
      });
      server.once('error', reject);
      server.once('exit', code => reject(new Error(`UI fixture exited: ${code}`)));
    });
    browser = await chromium.launch({ ...(process.env.UI_TEST_BROWSER === 'chromium' ? {} : { channel: 'msedge' }), headless: true });
    const page = await browser.newPage({ viewport: { width: 1380, height: 920 } });
    page.on('pageerror', error => errors.push(error.message));
    const mode = async name => { await page.locator(`.mode-nav [data-mode="${name}"]`).click(); await page.waitForFunction(name => Gallery.mode === name, name); };
    const category = name => page.locator(`[data-category="${name}"]`).click();
    const screenshot = async name => {
      for (const image of await page.locator('#sourceGrid img:visible, #galleryGrid img:visible').all()) {
        await image.scrollIntoViewIfNeeded();
        await page.waitForFunction(alt => [...document.querySelectorAll('img')].some(img => img.alt === alt && img.getAttribute('src') && img.complete && img.naturalWidth), await image.getAttribute('alt'));
      }
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: path.join(output, name), fullPage: true });
    };
    await page.goto(fixture.url);
    await page.waitForFunction(() => window.Gallery && Workspace.state.id && token);
    check('Default library shows source files rather than crop duplicates', await page.locator('.source-card').count() === 4 && await page.getAttribute('body', 'data-mode') === 'library');
    check('Only crop-edition modes and scripts are present', await page.evaluate(() => [...document.querySelectorAll('.mode-nav [data-mode]')].map(node => node.dataset.mode).join(',') === 'library,crop,photo' && !window.LiveUI && !document.querySelector('script[src="/live.js"]')));
    await category('scan');
    check('Scan files form a separate library category', await page.locator('.source-card').count() === 2);
    await category('photo');
    check('Direct photos form a separate library category', await page.locator('.source-card').count() === 2);
    await category('all');
    await screenshot('library.png');
    await mode('crop');
    check('Crop page contains only scanned results', await page.evaluate(() => Gallery.visible().length === 7 && Gallery.visible().every(item => item.source.kind === 'scan')));
    check('Scan import and export are available here', await page.locator('#openButton').isVisible() && !(await page.locator('#openPhotos').isVisible()) && await page.locator('#export').isVisible());
    await page.locator('#sourceFilter').selectOption(fixture.empty_id);
    check('Empty scanned source keeps manual crop recovery', await page.locator('#emptyManual').isVisible());
    await page.locator('#emptyManual').click();
    await page.locator('#editDialog').waitFor({ state: 'visible' });
    check('Manual crop uses the selected source', await page.evaluate(id => scan.source_id === id && drawing === 'outer', fixture.empty_id));
    await page.locator('#cancelEdit').click();
    await page.locator('#clearSource').click();
    await page.locator('.photo-card .adjust-button').first().click();
    await page.locator('#editDialog').waitFor({ state: 'visible' });
    await page.locator('#stepRepair').click();
    check('Repair remains accessible within the crop editor', await page.getAttribute('body', 'data-editor-tab') === 'repair');
    await page.locator('#stepExport').click();
    check('Color remains accessible within the crop editor', await page.getAttribute('body', 'data-editor-tab') === 'color');
    await page.locator('#cancelEdit').click();
    await screenshot('crop.png');
    await mode('photo');
    check('Direct photos remain editable in their own page', await page.evaluate(() => Gallery.visible().length === 2 && Gallery.visible().every(item => item.source.kind === 'photo')));
    check('Photo import does not use the scan entry', await page.locator('#openPhotos').isVisible() && !(await page.locator('#openButton').isVisible()));
    await page.locator('#photoSearch').fill('Lake');
    check('Export selection excludes hidden sources', await page.evaluate(() => Workspace.ids().length === 1 && Gallery.visible()[0].source.name === 'Lake.png'));
    const exported = page.waitForRequest(request => request.url().endsWith('/api/workspace/export'));
    await page.route('**/api/workspace/export', route => route.fulfill({ json: { task_id: 'test-export' } }));
    await page.locator('#export').click();
    const ids = (await exported).postDataJSON().photo_ids;
    check('Export request carries only the filtered photo identifier', await page.evaluate(ids => JSON.stringify(ids) === JSON.stringify(Workspace.ids()), ids));
    await page.locator('#batchEdit').click();
    check('Batch changes show the filtered affected count', (await page.locator('#batchCount').textContent()).includes('1'));
    await page.locator('#batchCancel').click();
    await mode('crop');
    check('Switching modules restores independent filters', await page.locator('#photoSearch').inputValue() === '' && await page.evaluate(() => Workspace.ids().length === 7));
    await mode('photo');
    check('Photo page preserves its own search', await page.locator('#photoSearch').inputValue() === 'Lake');
    const chooser = page.waitForEvent('filechooser');
    await page.locator('#openPhotos').click();
    check('New import resets old filters without changing type', (await chooser).isMultiple() && await page.locator('#photoSearch').inputValue() === '');
    await screenshot('photo.png');
    let imported;
    await page.route('**/api/workspace/import?**', route => { imported = new URL(route.request().url()).searchParams.get('kind'); return route.fulfill({ json: { task_id: 'test-import' } }); });
    await mode('crop');
    await page.evaluate(() => { const transfer = new DataTransfer(); transfer.items.add(new File(['fixture'], 'test.png', { type: 'image/png' })); document.body.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: transfer })); });
    await page.waitForFunction(() => document.getElementById('status').textContent.includes('文件已加入'));
    check('Drop type follows current module rather than last importer', imported === 'scan');
    await mode('library');
    await category('photo');
    await page.locator('#selectAll').click();
    check('Library bulk selection counts source files', (await page.locator('#removeSelectedSources').textContent()).includes('2'));
    await category('scan');
    check('Hidden selected source files cannot be removed from another category', await page.locator('#removeSelectedSources').isDisabled());
    await category('photo');
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#removeSelectedSources').click();
    await page.waitForFunction(() => Workspace.state.sources.length === 2);
    check('Removing selected sources retains unrelated scan files', await page.evaluate(() => Workspace.state.sources.every(source => source.kind === 'scan')));
    await category('all');
    await page.setViewportSize({ width: 980, height: 700 });
    check('Minimum desktop window has no horizontal overflow', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.setViewportSize({ width: 600, height: 850 });
    check('Narrow layout keeps working navigation', await page.locator('.mode-nav').isVisible() && await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await mode('crop');
    await category('pending');
    let switches = 0;
    await page.route('**/api/workspace/new', async route => {
      check('New batch requests cancellation of unfinished work', route.request().postDataJSON().cancel_tasks === true);
      if (++switches === 1) await route.fulfill({ json: { pending: true, active_tasks: [{ id: 'pending-fixture', kind: 'import', state: 'cancelling', done: 39, total: 62 }] } });
      else await route.continue();
    });
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#moreMenu summary').click();
    await page.locator('#newBatch').click();
    await page.waitForFunction(() => document.getElementById('status').textContent.includes('导入图片（39/62）'));
    check('Pending batch switch names its task and prevents new imports', await page.evaluate(() => document.querySelector('.home').inert && document.querySelector('.header-actions').inert));
    await page.waitForFunction(() => Workspace.state.sources.length === 0 && !document.querySelector('.home').inert);
    check('New batch proceeds automatically after pending work settles', switches === 2);
    check('New batch clears old category and source filters', await page.locator('#welcome').isVisible() && await page.locator('[data-category="all"]').getAttribute('aria-pressed') === 'true');
    check('No JavaScript runtime errors', errors.length === 0);
    fs.writeFileSync(path.join(output, 'verification.json'), JSON.stringify({ status: 'ok', checks, errors }, null, 2));
    console.log(JSON.stringify({ status: 'ok', checks: checks.length, errors }));
  } catch (error) {
    fs.mkdirSync(output, { recursive: true });
    fs.writeFileSync(path.join(output, 'verification.json'), JSON.stringify({ status: 'failed', checks, errors, error: error.stack }, null, 2));
    throw error;
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
})();
