async (page) => {
  if (!page.url().startsWith('http://127.0.0.1:5019/')) throw new Error('Use the local application.');
  const origin = 'http://127.0.0.1:5019';
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const fixture = 'tests/fixtures/print-portal.pdf';
  // Every API request in this browser session is intercepted. No credentials
  // reach school authentication and no request reaches a real print agent.
  let user = null, jobs = [], online = true, failLogin = false, reply = 'submitted';
  let loginCalls = 0, inspectionCalls = 0;
  const posts = [];
  let holdHistory = null;
  let historyStarted = false;
  await page.route('**/api/**', async route => {
    const req = route.request();
    const path = req.url().split(origin)[1]?.split('?')[0];
    const json = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    if (path === '/api/print/session') return json({
      csrf_token: 'browser-test-token', user,
      service: { enabled: true, ready: online, online, busy: false, demo: false },
      limits: { max_bytes: 10485760, max_pages: 50 },
      capabilities: { paper: 'A4', color: 'grayscale', sides: 'one-sided', copies: 1 },
    });
    if (path === '/api/login/ispace') {
      loginCalls++;
      if (failLogin) return json({ error: 'Invalid credentials' }, 401);
      const input = req.postDataJSON();
      user = { id: input.username === 't_test2' ? 2 : 1, school_username: input.username };
      return json({ success: true });
    }
    if (path === '/api/print/inspect') {
      inspectionCalls++;
      return json({ pages: 1, inspection_token: 'browser-inspection-ticket' });
    }
    if (path === '/api/print/jobs' && req.method() === 'POST') {
      const input = req.postDataJSON();
      posts.push({ key: input.idempotency_key, passwordPresent: input.password === 'test-only', user: req.headers()['x-print-user'] });
      if (reply === 'timeout') return route.fulfill({ status: 504, contentType: 'text/plain', body: 'Gateway timeout' });
      const job = { id: String(posts.length).padStart(32, '0'), idempotency_key: input.idempotency_key,
        state: reply, pages: 1, created_at: Date.now() / 1000, updated_at: Date.now() / 1000 };
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
    user = null; jobs = []; online = true; failLogin = false; reply = 'submitted';
    await page.goto(origin + '/print/');
    await page.getByText('打印服务已连接', { exact: true }).waitFor();
  };
  const fill = async (account = 't_test1') => {
    await page.locator('#file-input').setInputFiles(fixture);
    await page.locator('#doc-sub').filter({hasText:/MB/}).waitFor();
    await page.getByLabel('学号', { exact: true }).fill(account);
    await page.getByLabel('密码', { exact: true }).fill('test-only');
  };
  await reset();
  assert(await page.locator('#school-username').isVisible(), 'School account is hidden behind login');
  assert(await page.locator('#school-password').isVisible(), 'Password should be immediately visible');
  assert(await page.locator('#history').isHidden(), 'Empty history should not clutter the page');
  assert(!/演示同学|本地演示|demo/.test(await page.locator('body').innerText()), 'Demo content in product');
  await page.getByRole('button', { name: '提交打印', exact: true }).click();
  await page.getByText('先选择一份需要打印的 PDF。', { exact: true }).waitFor();
  assert(posts.length === 0, 'Validation submitted a print job');

  await fill();
  const beforeLogin = loginCalls;
  await page.locator('#submit-form').evaluate(form => { form.requestSubmit(); form.requestSubmit(); });
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
  await page.reload(); await page.getByText('打印服务未连接', { exact: true }).waitFor();
  await fill();
  assert(await page.locator('#submit-btn').isDisabled(), 'Offline submission should be disabled');
  online = true; await page.locator('#service-refresh').click();
  await page.getByText('打印服务已连接', { exact: true }).waitFor();

  await page.locator('#doc-remove').click();
  await page.locator('#file-input').setInputFiles('.codex/broken.pdf');
  await page.getByText('无法读取这份 PDF，请重新选择或导出文件。', { exact: true }).waitFor();
  await page.locator('#file-input').setInputFiles('.codex/too-big.pdf');
  await page.getByText('文件超过 10 MB，请压缩 PDF 后再试。', { exact: true }).waitFor();

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
  await page.getByText('fast.pdf', { exact: true }).waitFor();
  await page.waitForTimeout(600);
  assert(await page.locator('#doc-name').textContent() === 'fast.pdf', 'Late read replaced current file');

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
  await page.getByLabel('学号', { exact: true }).fill('t_test2');
  release(); await page.waitForTimeout(100);
  assert(await page.locator('#history').isHidden(), 'Old-account history reappeared');

  await reset();
  await page.locator('#help-open').click();
  assert(await page.locator('#help-dialog').isVisible(), 'Help modal failed');
  await page.keyboard.press('Escape');
  assert(await page.locator('#help-dialog').isHidden(), 'Escape did not dismiss help');
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.screenshot({ path:'output/playwright/print-portal/redesign-desktop.png', fullPage:true });
  await fill();
  await page.screenshot({ path:'output/playwright/print-portal/redesign-selected.png', fullPage:true });
  for (const width of [375, 768, 1024]) {
    await page.setViewportSize({ width, height: 900 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Horizontal overflow at '+width);
  }
  await page.setViewportSize({ width:375, height:812 });
  await page.screenshot({ path:'output/playwright/print-portal/redesign-mobile.png', fullPage:true });
  await page.emulateMedia({ reducedMotion:'reduce' });
  assert(await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches), 'Reduced motion missing');
  assert(errors.length === 0, 'Browser errors: '+errors.join('; '));
  // Restore the real local application before handing the page to a user.
  await page.unroute('**/api/**');
  await page.setViewportSize({ width:1440, height:960 });
  await page.goto(origin+'/print/');
  await page.getByText('打印服务未连接', { exact:true }).waitFor();
  await page.screenshot({ path:'output/playwright/print-portal/redesign-real.png', fullPage:true });
  return { passed:true, scenarios:14, realPrintJobs:0, realSchoolLogins:0, consoleErrors:errors.length };
}
