/* Isolated synthetic fixtures only. Run both editions with the bundled Node runtime. */
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'outputs/bug-audit');
const cropOnly = process.argv.includes('--crop-only') || !fs.existsSync(path.join(root, 'scripts/serve_workspace_pages_fixture.py'));
const reproduceSnap = process.argv.includes('--reproduce-snap');
const localPython = path.join(root, '.runtime/Scripts/python.exe'), parentPython = path.join(root, '../.runtime/Scripts/python.exe');
const python = process.env.UI_TEST_PYTHON || (fs.existsSync(localPython) ? localPython : fs.existsSync(parentPython) ? parentPython : 'python');

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
          server.once('error', reject);
          server.once('exit', code => reject(new Error(`Fixture exited ${code}`)));
        });
        page = await browser.newPage({ viewport: { width: 1380, height: 920 } });
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        if (reproduceSnap) {
          // Reproduce the pre-fix snap handler in this isolated browser only.
          const source = fs.readFileSync(path.join(directory, 'web/crop.js'), 'utf8').replace('if(busy||!$("edgeSnap").checked)', 'if(!$("edgeSnap").checked)').replace('if(busy||id!==operation', 'if(id!==operation');
          await page.route('**/crop.js', route => route.fulfill({ contentType: 'text/javascript', body: source }));
        }
        await page.route('**/api/live/capabilities', route => route.fulfill({ json: { available: false, reasons: ['Synthetic audit: no generation'] } }));
        await page.goto(fixture.url);
        await page.waitForFunction(() => window.Gallery && Workspace.state.id && token);
        const check = (name, passed, details) => results.push({ edition, name, passed: !!passed, ...(details ? { details } : {}) });
        const photoMode = edition === 'full' ? 'live' : 'photo';
        const directSource = await page.evaluate(() => Workspace.state.sources.find(source => source.kind === 'photo').id);
        await page.evaluate(mode => Gallery.setMode(mode), photoMode);
        await page.locator('#sourceFilter').selectOption(directSource);
        await page.evaluate(() => Gallery.setMode('crop'));
        await page.locator('#sourceFilter').selectOption(fixture.scan_id);
        await page.evaluate(mode => Gallery.setMode(mode), photoMode);
        check('Module switch restores direct-photo source filter', await page.locator('#sourceFilter').inputValue() === directSource);
        await page.evaluate(() => Gallery.setMode('crop'));
        check('Module switch restores scan source filter', await page.locator('#sourceFilter').inputValue() === fixture.scan_id);

        await page.route('**/api/workspace/select', route => route.fulfill({ status: 500, json: { error: 'Synthetic selection failure' } }));
        const checkBox = page.locator('#galleryGrid .photo-select').first();
        const original = await checkBox.isChecked();
        await checkBox.click();
        await page.waitForFunction(() => document.getElementById('status').textContent.includes('Synthetic selection failure'));
        check('Failed selection is retryable and restores server value', !(await checkBox.isDisabled()) && await checkBox.isChecked() === original);
        await page.unroute('**/api/workspace/select');
        await page.evaluate(() => Gallery.render());

        await page.locator('#galleryGrid .adjust-button').first().click();
        await page.locator('#editDialog').waitFor({ state: 'visible' });
        const presentationFollows = await page.evaluate(() => {
          scan.photos[1].presentation = { ...scan.photos[1].presentation, trim: 7, occupancy: .61 };
          document.querySelectorAll('#photoList button')[1].click();
          const follows = document.getElementById('trim').value === '7' && document.getElementById('occupancy').value === '61' && document.getElementById('occupancyValue').textContent === '61%';
          document.querySelectorAll('#photoList button')[0].click();
          return follows;
        });
        check('Editor photo navigation loads that photo presentation controls', presentationFollows);
        const selectionStayed = await page.evaluate(() => {
          const old = selected;
          setBusy(true);
          document.querySelectorAll('#photoList button')[1].click();
          const same = selected === old;
          setBusy(false);
          return same;
        });
        check('Editor navigation cannot switch photo during save or analysis', selectionStayed);
        await page.locator('#cancelEdit').click();

        for (const activation of ['photo card', 'manual add', 'page switch']) {
        let releaseActivation, signalActivation;
        const activationGate = new Promise(resolve => { releaseActivation = resolve; });
        const activationStarted = new Promise(resolve => { signalActivation = resolve; });
        await page.route('**/api/workspace/page', async route => {
          signalActivation();
          await activationGate;
          await route.continue();
        });
        await page.evaluate(activation => {
          window.auditActivation = activation === 'photo card' ? document.querySelector('#galleryGrid .adjust-button').onclick() : activation === 'manual add' ? document.getElementById('manualAdd').onclick() : document.getElementById('homePage').onchange();
        }, activation);
        await activationStarted;
        await page.evaluate(mode => Gallery.setMode(mode), photoMode);
        releaseActivation();
        await page.evaluate(() => window.auditActivation);
        check(`Navigation during ${activation} activation does not open a stale dialog`, !(await page.locator('#editDialog').isVisible()) && await page.getAttribute('body', 'data-mode') === photoMode);
        if (await page.locator('#editDialog').isVisible()) await page.locator('#cancelEdit').click();
        await page.unroute('**/api/workspace/page');
        await page.evaluate(() => Gallery.setMode('crop'));
        }
        const discardedInstall = await page.evaluate(async () => {
          const before = scan, title = document.getElementById('scanName').textContent;
          const result = await installScan({ ...scan, name: 'Discarded activation' }, null, () => false);
          return result === false && scan === before && document.getElementById('scanName').textContent === title;
        });
        check('Decoded stale activation cannot replace current scan', discardedInstall);
        await page.locator('#galleryGrid .adjust-button').first().click();
        await page.locator('#stepRepair').click();
        await page.waitForFunction(() => !previewRunning && !document.getElementById('repairCanvas').hidden && !document.getElementById('repairHint').textContent.includes('预览更新中'));
        const brushBounds = await page.locator('#repairCanvas').boundingBox();
        await page.mouse.move(brushBounds.x + 35, brushBounds.y + 35);
        await page.mouse.down();
        await page.evaluate(() => document.querySelectorAll('#photoList button')[1].click());
        await page.waitForFunction(() => !previewRunning && !document.getElementById('repairCanvas').hidden && !document.getElementById('repairHint').textContent.includes('预览更新中'));
        await page.mouse.move(brushBounds.x + 55, brushBounds.y + 55);
        await page.mouse.up();
        check('Switching repair photos cancels the previous brush safely', errors.length === 0 && await page.evaluate(() => !currentPhoto().restoration.strokes.length), errors);
        await page.locator('#cancelEdit').click();

        // A pending edge snap must not mutate a clone after its save payload is sent.
        await page.locator('#galleryGrid .adjust-button').first().click();
        await page.locator('#editDialog').waitFor({ state: 'visible' });
        let releaseSnap, signalSnap, releaseSave, signalSave;
        const snapGate = new Promise(resolve => { releaseSnap = resolve; });
        const snapStarted = new Promise(resolve => { signalSnap = resolve; });
        const saveGate = new Promise(resolve => { releaseSave = resolve; });
        const saveStarted = new Promise(resolve => { signalSave = resolve; });
        let snappedPoint;
        await page.route('**/api/crop-snap', async route => {
          const body = route.request().postDataJSON();
          snappedPoint = [body.point[0] + 3, body.point[1]];
          signalSnap();
          await snapGate;
          await route.fulfill({ json: { snapped: true, point: snappedPoint } });
        });
        await page.route('**/api/workspace/update', async route => {
          signalSave();
          await saveGate;
          await route.fulfill({ status: 500, json: { error: 'Synthetic delayed save failure' } });
        });
        // Release the active drag through the real production pointerup handlers.
        await page.evaluate(() => {
          editMode = 'outer';
          drag = { index: 0, start: currentPhoto().outer[0].slice() };
          canvas.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
        });
        await snapStarted;
        const beforeSnap = await page.evaluate(() => JSON.stringify(currentPhoto().outer));
        await page.locator('#editorDone').click();
        await saveStarted;
        check('All crop mutation controls disable during save', await page.evaluate(() => ['rotateLeft', 'rotateHalf', 'resetGeometry', 'applyOrientation', 'orientationConfirmed', 'fineAngle', 'perspectiveX', 'perspectiveY', 'trimTop', 'trimRight', 'trimBottom', 'trimLeft'].every(id => document.getElementById(id).disabled)));
        // Observe the snap handler finishing after JSON decode, not just its HTTP response.
        await page.evaluate(() => {
          // The request already holds a response promise, so instrument Response decoding.
          window.auditJson = Response.prototype.json;
          Response.prototype.json = async function() {
            const result = await window.auditJson.call(this);
            if (this.url.endsWith('/api/crop-snap')) setTimeout(() => { window.auditSnapSettled = true; }, 0);
            return result;
          };
          window.auditSnapSettled = false;
        });
        releaseSnap();
        await page.waitForFunction(() => window.auditSnapSettled);
        check('Late edge snap does not mutate a saving photo', await page.evaluate(before => JSON.stringify(currentPhoto().outer) === before, beforeSnap));
        await page.evaluate(() => { Response.prototype.json = window.auditJson; });
        releaseSave();
        await page.waitForFunction(() => !busy);
        await page.unroute('**/api/crop-snap');
        await page.unroute('**/api/workspace/update');
        await page.locator('#cancelEdit').click();

        // A failed save must leave the edited clone and dialog available for retry.
        await page.locator('#galleryGrid .adjust-button').first().click();
        const baseline = await page.evaluate(() => currentPhoto().rotation || 0);
        await page.locator('#rotate').click();
        await page.route('**/api/workspace/update', route => route.fulfill({ status: 500, json: { error: 'Synthetic save failure' } }));
        await page.locator('#editorDone').click();
        await page.waitForFunction(() => !busy && document.getElementById('status').textContent.includes('Synthetic save failure'));
        check('Failed save keeps edits and allows retry', await page.evaluate(rotation => document.getElementById('editDialog').open && currentPhoto().rotation === (rotation + 1) % 4 && !document.getElementById('editorDone').disabled, baseline));
        await page.unroute('**/api/workspace/update');
        await page.locator('#cancelEdit').click();
        await page.locator('#galleryGrid .adjust-button').first().click();
        check('Cancel restores unsaved geometry on reopening', await page.evaluate(rotation => currentPhoto().rotation === rotation, baseline));
        await page.locator('#cancelEdit').click();
      } finally {
        if (page) await page.close();
        server.kill();
      }
    }
  } finally { await browser.close(); }
  const status = results.every(result => result.passed) ? 'ok' : 'failed';
  fs.writeFileSync(path.join(output, reproduceSnap ? 'crop-ui-snap-before.json' : 'crop-ui-verification.json'), JSON.stringify({ status, results }, null, 2));
  console.log(JSON.stringify({ status, checks: results.length, failures: results.filter(result => !result.passed) }));
  if (status !== 'ok') process.exitCode = 1;
})();
