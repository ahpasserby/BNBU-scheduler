async (page) => {
  if (!page.url().startsWith('http://127.0.0.1:5019/')) throw new Error('Use the local application.');
  const origin = 'http://127.0.0.1:5019';
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const fixture = 'tests/fixtures/print-portal.pdf';
  // Every API request in this browser session is intercepted. No credentials
  // reach school authentication and no request reaches a real print agent.
  const allFeatures = ['color', 'duplex', 'copies', 'convert'];
  // A small valid PDF stands in for the device's conversion result.
  const convertedPdf = (() => {
    const content = '0.2 0.4 0.8 rg 72 560 450 200 re f';
    const objects = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
      '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R >>',
      `<< /Length ${content.length} >>\nstream\n${content}\nendstream`];
    let out = '%PDF-1.4\n';
    const offsets = objects.map((body, index) => { const at = out.length; out += `${index + 1} 0 obj\n${body}\nendobj\n`; return at; });
    const xref = out.length;
    out += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n` + offsets.map(at => String(at).padStart(10, '0') + ' 00000 n \n').join('');
    out += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF`;
    return Buffer.from(out).toString('base64');
  })();
  const docx = { name: 'report.docx', mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer: Buffer.from('PK\u0003\u0004synthetic-docx') };
  const converts = [];
  let user = null, jobs = [], online = true, failLogin = false, reply = 'submitted', features = allFeatures;
  let loginCalls = 0, inspectionCalls = 0;
  let holdLogin = null;
  const posts = [];
  let holdHistory = null;
  let historyStarted = false;
  await page.route('**/api/**', async route => {
    const req = route.request();
    const path = req.url().split(origin)[1]?.split('?')[0];
    const json = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    if (path === '/api/print/session') return json({
      csrf_token: 'browser-test-token', user,
      service: { enabled: true, ready: online, online, busy: false, demo: false, features: online ? features : [] },
      limits: { max_bytes: 10485760, max_pages: 50, max_impressions: 200 },
      capabilities: { paper: 'A4', color: features.includes('color') ? ['grayscale', 'color'] : ['grayscale'],
        sides: features.includes('duplex') ? ['one-sided', 'two-sided-long-edge', 'two-sided-short-edge'] : ['one-sided'],
        copies: { min: 1, max: features.includes('copies') ? 20 : 1 }, max_impressions: 200 },
    });
    if (path === '/api/login/ispace') {
      loginCalls++;
      if (holdLogin) await holdLogin;
      if (failLogin) return json({ error: 'Invalid credentials' }, 401);
      const input = req.postDataJSON();
      user = { id: input.username === 't_test2' ? 2 : 1, school_username: input.username };
      return json({ success: true });
    }
    if (path === '/api/print/convert') {
      converts.push(req.postDataJSON().name);
      return json({ pdf: convertedPdf, pages: 1 });
    }
    if (path === '/api/print/inspect') {
      inspectionCalls++;
      return json({ pages: 1, inspection_token: 'browser-inspection-ticket' });
    }
    if (path === '/api/print/jobs' && req.method() === 'POST') {
      const input = req.postDataJSON();
      posts.push({ key: input.idempotency_key, passwordPresent: input.password === 'test-only', user: req.headers()['x-print-user'], options: input.options, pdf: input.pdf });
      if (reply === 'timeout') return route.fulfill({ status: 504, contentType: 'text/plain', body: 'Gateway timeout' });
      const job = { id: String(posts.length).padStart(32, '0'), idempotency_key: input.idempotency_key,
        state: reply, pages: 1, options: input.options, created_at: Date.now() / 1000, updated_at: Date.now() / 1000 };
      jobs = [job, ...jobs];
      return json({ job });
    }
    if (path === '/api/print/jobs') {
      const snapshot = jobs.slice();
      if (holdHistory) { const pending = holdHistory; holdHistory = null; historyStarted = true; await pending; }
      return json({ jobs: snapshot });
    }
    return json({ error: 'Blocked test request' }, 404);
  });
  const reset = async () => {
    user = null; jobs = []; online = true; failLogin = false; reply = 'submitted'; features = allFeatures;
    await page.goto(origin + '/print/');
    await page.getByText('已连接', { exact: true }).waitFor();
  };
  const fill = async (account = 't_test1') => {
    await page.locator('#file-input').setInputFiles(fixture);
    await page.locator('#doc-sub').filter({hasText:/[KM]B/}).waitFor();
    await page.locator('#go-print').click();
    await page.getByLabel('学号', { exact: true }).fill(account);
    await page.getByLabel('密码', { exact: true }).fill('test-only');
  };
  await reset();
  assert(await page.locator('#school-username').isHidden(), 'Account requested before document selection');
  assert(await page.locator('#school-password').isHidden(), 'Password requested before printing');
  assert(await page.locator('#history').isHidden(), 'Empty history should not clutter the page');
  assert(!/演示同学|本地演示|demo/.test(await page.locator('body').innerText()), 'Demo content in product');
  assert(await page.locator('#doc-panel').isHidden(), 'Preview must wait for a document');
  assert(posts.length === 0, 'Validation submitted a print job');
  // The page opens on an introduction; the workspace follows the call to action and Back returns.
  assert(await page.locator('#intro-view').isVisible(), 'Introduction missing');
  assert(await page.locator('#upload-stage').isHidden(), 'Workspace shown before starting');
  await page.locator('#start-print').click();
  await page.locator('#upload-stage').waitFor({ state: 'visible' });
  assert(page.url().endsWith('#start'), 'Workspace has no history entry');
  await page.goBack();
  await page.locator('#intro-view').waitFor({ state: 'visible' });

  await fill();
  assert(await page.locator('#confirm-name').textContent() === 'print-portal.pdf', 'Confirmation lost filename');
  assert(loginCalls === 0 && inspectionCalls === 0 && posts.length === 0, 'Opening account step sent credentials or PDF');
  await page.locator('#account-back').click();
  await page.locator('#account-dialog').waitFor({ state: 'hidden', timeout: 2000 });
  assert(await page.locator('#doc-name').textContent() === 'print-portal.pdf', 'Back lost selected file');
  assert(await page.locator('#school-password').inputValue() === '', 'Back retained password');
  await page.locator('#go-print').click();
  await page.getByLabel('密码', { exact:true }).fill('test-only');
  await page.keyboard.press('Escape');
  await page.locator('#account-dialog').waitFor({ state: 'hidden', timeout: 2000 });
  assert(await page.locator('#school-password').inputValue() === '', 'Escape retained password');
  await page.locator('#go-print').click();
  await page.getByLabel('密码', { exact:true }).fill('test-only');
  let releaseLogin;
  holdLogin = new Promise(resolve => { releaseLogin = resolve; });
  const beforeLogin = loginCalls;
  await page.locator('#submit-form').evaluate(form => { form.requestSubmit(); form.requestSubmit(); });
  await page.getByText('正在验证学校账号…', {exact:true}).first().waitFor();
  await page.keyboard.press('Escape');
  assert(await page.locator('#account-dialog').isVisible(), 'In-flight submission was dismissed');
  assert(await page.locator('#account-back').isDisabled(), 'In-flight back control must be disabled');
  releaseLogin(); holdLogin = null;
  await page.getByRole('heading', { name: '文件已提交，去刷卡取件吧' }).waitFor();
  assert(posts.length === 1 && loginCalls === beforeLogin + 1 && inspectionCalls === 1, 'Expected single login-inspect-submit pipeline');
  assert(posts[0].passwordPresent && posts[0].user === '1', 'Wrong dispatch credentials or owner');
  assert(await page.locator('#school-password').inputValue() === '', 'Password retained after dispatch');
  assert(await page.evaluate(() => !localStorage.length && !sessionStorage.length), 'Unexpected browser persistence');
  assert(await page.locator('#doc-open').getAttribute('href') === null, 'Submitted file blob not released');

  await reset(); await fill(); reply = 'timeout';
  await page.locator('#submit-btn').click();
  await page.getByRole('heading', { name: '提交结果待确认' }).waitFor();
  const ambiguous = posts.at(-1).key;
  const countBeforeQuery = posts.length;
  await page.locator('#receipt-query').click();
  await page.locator('#receipt-retry').waitFor({ state: 'visible' });
  assert(posts.length === countBeforeQuery, 'Query must not send another job');
  await page.locator('#receipt-retry').click();
  assert(await page.locator('#school-username').isDisabled(), 'Retry identity must stay bound');
  assert(await page.getByRole('radio', { name: '彩色' }).isDisabled(), 'Retry settings must stay bound');
  reply = 'submitted';
  await page.getByLabel('密码', { exact: true }).fill('test-only');
  await page.locator('#submit-btn').click();
  await page.getByRole('heading', { name: '文件已提交，去刷卡取件吧' }).waitFor();
  assert(posts.at(-1).key === ambiguous, 'Uncertain dispatch retry changed ID');

  await reset(); await fill(); failLogin = true;
  const beforeFailure = posts.length;
  await page.locator('#submit-btn').click();
  await page.getByText('学号或密码验证失败，请核对后重试。', { exact: true }).waitFor();
  assert(posts.length === beforeFailure && await page.locator('#school-password').inputValue() === '', 'Failed auth sent document or retained password');
  assert(await page.locator('#doc-panel').isVisible(), 'Failed auth lost selected document');
  failLogin = false;
  await page.getByLabel('密码', { exact: true }).fill('test-only');
  await page.locator('#password-toggle').click();
  assert(await page.locator('#school-password').getAttribute('type') === 'text', 'Password reveal failed');
  await page.getByLabel('学号', { exact: true }).fill('t_test2');
  assert(await page.locator('#school-password').inputValue() === '' && await page.locator('#school-password').getAttribute('type') === 'password', 'Account change retained password');

  await reset(); online = false;
  await page.reload(); await page.getByText('未连接', { exact: true }).waitFor();
  await fill();
  assert(await page.locator('#submit-btn').isDisabled(), 'Offline submission should be disabled');
  await page.locator('#account-back').click();
  await page.locator('#account-dialog').waitFor({ state: 'hidden', timeout: 2000 });
  online = true; await page.locator('#service-refresh').click();
  await page.getByText('已连接', { exact: true }).waitFor();

  await page.locator('#doc-remove').click();
  await page.locator('#file-input').setInputFiles('.codex/broken.pdf');
  await page.locator('#file-error').filter({hasText:/无法生成|无法读取/}).waitFor();
  await page.locator('#file-input').setInputFiles('.codex/too-big.pdf');
  await page.getByText('文件超过 10 MB，请压缩后再试。', { exact: true }).waitFor();

  // Exercise delayed FileReader completion after a newer selection.
  await page.evaluate(() => {
    const original = FileReader.prototype.readAsDataURL;
    FileReader.prototype.readAsDataURL = function(file) {
      if (file.name === 'slow.pdf') setTimeout(() => original.call(this, file), 400);
      else original.call(this, file);
    };
  });
  await page.locator('#file-input').setInputFiles('.codex/slow.pdf');
  await page.locator('#file-input').setInputFiles('.codex/fast.pdf');
  await page.locator('#doc-name').filter({hasText:'fast.pdf'}).waitFor();
  await page.waitForTimeout(600);
  await page.locator('#preview-canvas[data-page="1"]').waitFor({state:'visible'});
  assert(await page.locator('#doc-name').textContent() === 'fast.pdf', 'Late read replaced current file');

  // Real local renderer: multi-page navigation, A4 output, zoom and validation.
  await page.locator('#file-input').setInputFiles('tests/fixtures/print-preview-pages.pdf');
  await page.locator('#doc-sub').filter({hasText:'2 页'}).waitFor();
  await page.locator('#preview-canvas[data-page="1"]').waitFor({state:'visible'});
  await page.waitForFunction(() => document.getElementById('print-total-sheets').textContent === '2');
  assert(await page.locator('#print-total').textContent() === '2 页 × 1 份', 'Incorrect print summary');
  const firstPixels = await page.locator('#preview-canvas').evaluate(canvas => canvas.toDataURL());
  await page.locator('#page-next').click();
  await page.locator('#preview-canvas[data-page="2"]').waitFor({state:'visible'});
  const secondPixels = await page.locator('#preview-canvas').evaluate(canvas => canvas.toDataURL());
  assert(firstPixels !== secondPixels, 'Page navigation did not change rendered content');
  await page.locator('#page-current').fill('999');
  await page.locator('#page-current').press('Enter');
  assert(await page.locator('#page-current').inputValue() === '2', 'Page bounds not reflected in control');
  assert(await page.locator('#preview-canvas').evaluate(c => c.height > c.width), 'Landscape source was not fitted to portrait A4');
  await page.locator('#zoom-in').click();
  await page.locator('#zoom-fit').filter({hasText:'100%'}).waitFor();
  await page.locator('#zoom-in').click();
  await page.locator('#zoom-fit').filter({hasText:'150%'}).waitFor();
  await page.waitForFunction(() => parseFloat(document.getElementById('preview-canvas').style.width) > 800);
  await page.locator('#zoom-fit').click();
  await page.locator('#zoom-fit').filter({hasText:'适合'}).waitFor();
  await page.locator('#page-current').fill('1');
  await page.locator('#page-current').press('Enter');
  await page.locator('#preview-canvas[data-page="1"]').waitFor({state:'visible'});
  assert(await page.locator('#account-dialog').isHidden(), 'Page jump unexpectedly opened account confirmation');
  await page.locator('#file-input').setInputFiles('.codex/encrypted.pdf');
  await page.locator('#file-error').filter({hasText:'加密 PDF'}).waitFor();
  assert(await page.locator('#doc-panel').isHidden(), 'Encrypted document remained printable');
  await page.locator('#file-input').setInputFiles('.codex/too-many-pages.pdf');
  await page.locator('#file-error').filter({hasText:'51 页'}).waitFor();
  assert(await page.locator('#doc-panel').isHidden(), 'Over-limit document remained printable');

  // Output options change the preview and travel with the print intent.
  await reset();
  await page.locator('#file-input').setInputFiles('tests/fixtures/print-preview-pages.pdf');
  await page.locator('#preview-canvas[data-page="1"]').waitFor();
  await page.getByRole('radio', { name: '彩色' }).check();
  assert(await page.locator('#sheet').evaluate(s => s.classList.contains('is-color')), 'Colour did not reach the preview');
  await page.getByRole('radio', { name: '双面长边翻页' }).check();
  await page.locator('#flip-group').waitFor({ state: 'visible' });
  await page.locator('#back-canvas[data-page="2"]').waitFor({ state: 'attached' });
  await page.getByRole('radio', { name: '短边翻页' }).check();
  assert(await page.locator('#sheet').getAttribute('data-flip') === 'short', 'Short-edge flip not previewed');
  await page.locator('#copies-inc').click();
  await page.locator('#copies-inc').click();
  assert(await page.locator('#copies').inputValue() === '3', 'Copies stepper failed');
  assert(await page.locator('#sheet-wrap').getAttribute('data-copies') === '3', 'Copies stack missing');
  await page.waitForFunction(() => document.getElementById('print-total-sheets').textContent === '3');
  assert(await page.locator('#print-total').textContent() === '2 页 × 3 份', 'Duplex summary incorrect');
  await page.waitForTimeout(2400); // Let the one-time duplex demonstration flip settle.
  assert(await page.locator('#flip-sheet').getAttribute('aria-pressed') === 'false', 'Demonstration flip did not return');
  await page.locator('#flip-sheet').click();
  assert(await page.locator('#page-current').inputValue() === '2', 'Back side did not show page 2');
  assert(await page.locator('#sheet-caption').textContent() === '背面', 'Back side caption missing');
  await page.locator('#go-print').click();
  assert((await page.locator('#confirm-specs').textContent()).includes('彩色 · 双面 · 短边 · 3 份'), 'Confirmation lost options');
  await page.getByLabel('学号', { exact: true }).fill('t_test1');
  await page.getByLabel('密码', { exact: true }).fill('test-only');
  await page.locator('#submit-btn').click();
  await page.getByRole('heading', { name: '文件已提交，去刷卡取件吧' }).waitFor();
  const sent = posts.at(-1).options;
  assert(sent.color === 'color' && sent.sides === 'two-sided-short-edge' && sent.copies === 3, 'Options were not submitted');
  assert((await page.locator('#receipt-details').textContent()).includes('彩色 · 双面 · 短边 · 3 份'), 'Receipt lost options');
  await page.screenshot({ path:'output/playwright/print-portal/options-receipt.png', fullPage:true });

  // Word documents are converted on the device, previewed, then printed as that PDF.
  await reset();
  await page.locator('#file-input').setInputFiles(docx);
  await page.locator('#preview-canvas[data-page="1"]').waitFor();
  assert(converts.length === 1 && converts[0] === 'report.docx', 'Conversion request missing');
  assert(await page.locator('#doc-name').textContent() === 'report.docx', 'Converted file lost its name');
  assert((await page.locator('#doc-sub').textContent()).includes('已转为 PDF'), 'Conversion not indicated');
  await page.locator('#go-print').click();
  await page.getByLabel('学号', { exact: true }).fill('t_test1');
  await page.getByLabel('密码', { exact: true }).fill('test-only');
  await page.locator('#submit-btn').click();
  await page.getByRole('heading', { name: '文件已提交，去刷卡取件吧' }).waitFor();
  assert(posts.at(-1).pdf === convertedPdf, 'Converted PDF was not the printed document');
  // Without the device conversion feature only PDF is accepted.
  await reset(); features = ['color', 'duplex', 'copies'];
  await page.reload(); await page.getByText('已连接', { exact: true }).waitFor();
  assert(await page.locator('#drop-sub').textContent() === 'PDF 文件', 'Formats not limited to PDF');
  await page.locator('#file-input').setInputFiles(docx);
  await page.locator('#file-error').filter({ hasText: '暂不支持转换' }).waitFor();
  assert(converts.length === 1, 'Unsupported conversion was attempted');

  // Older agents without output features keep the fixed defaults and say so.
  await reset(); features = [];
  await page.reload(); await page.getByText('已连接', { exact: true }).waitFor();
  await page.locator('#file-input').setInputFiles(fixture);
  await page.locator('#preview-canvas[data-page="1"]').waitFor();
  assert(await page.getByRole('radio', { name: '彩色' }).isDisabled(), 'Unsupported colour stayed selectable');
  assert(await page.getByRole('radio', { name: '双面长边翻页' }).isDisabled(), 'Unsupported duplex stayed selectable');
  assert(await page.locator('#copies-inc').isDisabled(), 'Unsupported copies stayed adjustable');
  assert(await page.locator('#color-note').textContent() === '设备暂不支持彩色', 'Missing unsupported note');
  // Options chosen while offline revert once the device reports its features.
  online = false; await page.locator('#doc-remove').click();
  await page.reload(); await page.getByText('未连接', { exact: true }).waitFor();
  await page.locator('#file-input').setInputFiles(fixture);
  await page.locator('#preview-canvas[data-page="1"]').waitFor();
  await page.getByRole('radio', { name: '彩色' }).check();
  online = true; await page.locator('#service-refresh').click();
  await page.getByText('已连接', { exact: true }).waitFor();
  assert(await page.getByRole('radio', { name: '黑白' }).isChecked(), 'Unsupported colour was not reverted');

  // The copy cap follows the 200-face limit for long documents.
  await reset();
  await page.locator('#file-input').setInputFiles('.codex/fifty-pages.pdf');
  await page.locator('#doc-sub').filter({hasText:'50 页'}).waitFor();
  await page.locator('#copies').fill('9');
  await page.locator('#copies').press('Enter');
  assert(await page.locator('#copies').inputValue() === '4', 'Copies not capped at 200 faces');
  assert(await page.locator('#copies-inc').isDisabled(), 'Copy cap still adjustable');
  assert(await page.locator('#copies-note').textContent() === '最多 4 份', 'Copy cap note missing');

  await reset();
  // Realistic authenticated history remains collapsed and follows the typed ID.
  user = { id: 1, school_username: 't_test1' };
  jobs = [{ id:'a'.repeat(32), state:'submitted', pages:1, created_at:Date.now()/1000 }];
  await page.reload();
  await page.locator('#history').waitFor({ state: 'visible' });
  assert(await page.locator('#history').getAttribute('open') === null, 'History should be collapsed');
  let release;
  holdHistory = new Promise(resolve => { release = resolve; });
  await page.locator('#history > summary').click();
  await page.locator('#jobs-refresh').click();
  for (let i=0; i<100 && !historyStarted; i++) await page.waitForTimeout(20);
  assert(historyStarted, 'History request did not start');
  await page.locator('#file-input').setInputFiles(fixture);
  await page.locator('#doc-sub').filter({hasText:/[KM]B/}).waitFor();
  await page.locator('#go-print').click();
  await page.getByLabel('学号', { exact: true }).fill('t_test2');
  release(); await page.waitForTimeout(100);
  assert(await page.locator('#history').isHidden(), 'Old-account history reappeared');

  await reset();
  await page.locator('#help-open').click();
  assert(await page.locator('#help-dialog').isVisible(), 'Help modal failed');
  await page.keyboard.press('Escape');
  await page.locator('#help-dialog').waitFor({ state: 'hidden', timeout: 2000 });
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.screenshot({ path:'output/playwright/print-portal/redesign-desktop.png', fullPage:true });
  await page.locator('#file-input').setInputFiles(fixture);
  await page.locator('#doc-sub').filter({hasText:/[KM]B/}).waitFor();
  assert(await page.locator('#school-username').isHidden(), 'Selecting a file automatically requested credentials');
  assert(await page.locator('#preview-canvas').isVisible(), 'Actual PDF preview missing');
  await page.screenshot({ path:'output/playwright/print-portal/redesign-selected.png', fullPage:true });
  await page.locator('#go-print').click();
  await page.screenshot({ path:'output/playwright/print-portal/two-step-account.png', fullPage:true });
  for (const width of [375, 768, 1024]) {
    await page.setViewportSize({ width, height: 900 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Horizontal overflow at '+width);
  }
  await page.setViewportSize({ width:375, height:812 });
  await page.waitForFunction(() => { const c=document.getElementById('preview-canvas'),h=document.getElementById('page-stage'); return !c.hidden && parseFloat(c.style.width) <= h.clientWidth; });
  await page.screenshot({ path:'output/playwright/print-portal/two-step-mobile-account.png', fullPage:true });
  await page.locator('#account-back').click();
  await page.screenshot({ path:'output/playwright/print-portal/redesign-mobile.png', fullPage:true });
  await page.emulateMedia({ reducedMotion:'reduce' });
  assert(await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches), 'Reduced motion missing');
  assert(errors.length === 0, 'Browser errors: '+errors.join('; '));
  // Restore the real local application before handing the page to a user.
  await page.unroute('**/api/**');
  await page.setViewportSize({ width:1440, height:960 });
  await page.goto(origin+'/print/');
  await page.getByText('未连接', { exact:true }).waitFor();
  await page.screenshot({ path:'output/playwright/print-portal/redesign-real.png', fullPage:true });
  return { passed:true, scenarios:27, realPrintJobs:0, realSchoolLogins:0, consoleErrors:errors.length };
}
