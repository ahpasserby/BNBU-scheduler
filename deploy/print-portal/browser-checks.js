async (page) => {
  if (page.url() !== 'http://127.0.0.1:5019/print/') throw new Error('Use the local-only preview harness.');
  const origin = 'http://127.0.0.1:5019';
  await page.request.post(origin + '/__demo/state', {data:{ready:true,busy:false,logged_out:false,user_id:1,reset_jobs:true}});
  const status = await (await page.request.get(origin + '/api/print/session')).json();
  if (!status.service.demo) throw new Error('Refusing to submit to a real printer.');
  const fixture = 'tests/fixtures/print-portal.pdf';
  const posts = [];
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    if (request.url().endsWith('/api/print/jobs') && request.method() === 'POST') {
      posts.push(request.postDataJSON().idempotency_key);
    }
  });
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const prepare = async () => {
    await page.locator('#file-input').setInputFiles(fixture);
    await page.getByLabel('学校账号密码', {exact:true}).waitFor({state:'visible'});
    assert((await page.locator('#cap-pages').textContent()).includes('1'), 'Expected one inspected page');
    await page.getByLabel('学校账号密码', {exact:true}).fill('preview-only-not-a-real-password');
  };
  await page.reload();
  await page.getByText('本地演示，不会真实打印', {exact:true}).waitFor();
  await prepare();
  const firstReply = page.waitForResponse(r => r.url().endsWith('/api/print/jobs') && r.request().method() === 'POST');
  await page.locator('#submit-form').evaluate(form => { form.requestSubmit(); form.requestSubmit(); });
  const first = await (await firstReply).json();
  assert(first.job.state === 'submitted', 'Expected submitted receipt');
  await page.locator('#attempt').getByText('已交给学校队列', {exact:true}).waitFor();
  assert(posts.length === 1, 'Double submission reached the server');
  assert(await page.locator('#school-password').inputValue() === '', 'Password was not cleared');
  assert(await page.evaluate(() => localStorage.length === 0 && sessionStorage.length === 0), 'Sensitive state must not enter browser storage');

  // A gateway failure without a job receipt must retain the original intent.
  await prepare();
  let failNext = true;
  await page.route('**/api/print/jobs', async route => {
    if (failNext && route.request().method() === 'POST') {
      failNext = false;
      await route.fulfill({status:504,contentType:'text/plain',body:'Gateway timeout'});
    } else await route.continue();
  });
  await page.getByRole('button', {name:'提交打印',exact:true}).click();
  await page.locator('#attempt').getByText('提交结果待确认', {exact:true}).waitFor();
  await page.getByRole('button', {name:'查询任务状态',exact:true}).click();
  await page.getByRole('button', {name:'确认未提交，再次尝试',exact:true}).click();
  await page.getByLabel('学校账号密码', {exact:true}).fill('preview-only-not-a-real-password');
  await page.getByRole('button', {name:'提交打印',exact:true}).click();
  await page.locator('#attempt').getByText('已交给学校队列', {exact:true}).waitFor();
  assert(posts.length === 3 && posts[1] === posts[2], 'Ambiguous retry created a new idempotency key');
  await page.unroute('**/api/print/jobs');

  // Late history from the previous identity must not reappear after account switch.
  let holdNext = true;
  let captured;
  let release;
  const held = new Promise(resolve => { captured = resolve; });
  const resume = new Promise(resolve => { release = resolve; });
  await page.route('**/api/print/jobs', async route => {
    if (holdNext && route.request().method() === 'GET') {
      holdNext = false;
      const response = await route.fetch();
      captured();
      await resume;
      await route.fulfill({response});
    } else await route.continue();
  });
  await page.getByRole('button', {name:'刷新',exact:true}).click();
  await held;
  await page.request.post(origin + '/__demo/state', {data:{user_id:2}});
  await page.evaluate(() => window.dispatchEvent(new Event('online')));
  await page.getByText('第二位演示同学', {exact:true}).waitFor();
  const lateResponse = page.waitForResponse(r => r.url().endsWith('/api/print/jobs'));
  release();
  await lateResponse;
  assert(!(await page.locator('#jobs-list').textContent()).includes(first.job.id.slice(0,8)), 'Previous user history leaked into the new identity');
  await page.unroute('**/api/print/jobs');
  await page.request.post(origin + '/__demo/state', {data:{user_id:1}});

  // Offline status must stop inspection and submission.
  await page.request.post(origin + '/__demo/state', {data:{ready:false}});
  await page.reload();
  await page.getByText('打印设备暂未就绪，请稍后再试。', {exact:true}).first().waitFor();
  assert(await page.locator('#submit-btn').isDisabled(), 'Offline submit must be disabled');

  // Exercise the login dialog against the loopback demo login endpoint.
  await page.request.post(origin + '/__demo/state', {data:{ready:true,logged_out:true}});
  await page.reload();
  await page.locator('#file-input').setInputFiles(fixture);
  await page.locator('#doc-name').getByText('print-portal.pdf', {exact:true}).waitFor();
  await page.locator('#identity').getByRole('button', {name:'登录',exact:true}).click();
  await page.locator('#login-password').fill('cancel-only-not-a-real-password');
  await page.keyboard.press('Escape');
  assert(await page.locator('#login-password').inputValue() === '', 'Closing login must clear its password');
  await page.locator('#identity').getByRole('button', {name:'登录',exact:true}).click();
  await page.locator('#login-username').fill('demo');
  await page.locator('#login-password').fill('demo');
  await page.locator('#login-dialog').getByRole('button', {name:'登录',exact:true}).click();
  await page.getByText('演示同学', {exact:true}).waitFor();
  await page.getByLabel('学校账号密码', {exact:true}).waitFor({state:'visible'});
  assert(await page.locator('#doc-name').textContent() === 'print-portal.pdf', 'Initial login discarded the selected file');
  await page.getByRole('button', {name:'移除',exact:true}).click();

  // A late FileReader from the replaced file cannot overwrite the new file.
  await page.addInitScript(() => {
    if (window.printReaderRaceInstalled) return;
    window.printReaderRaceInstalled = true;
    const Original = window.FileReader;
    window.FileReader = class extends Original {
      readAsDataURL(file) {
        if (file.name === 'slow.pdf') window.releaseSlowReader = () => new Promise(resolve => {
          this.addEventListener('loadend', resolve, {once:true});
          super.readAsDataURL(file);
        });
        else super.readAsDataURL(file);
      }
    };
  });
  await page.reload();
  await page.getByText('可以提交打印', {exact:true}).waitFor();
  await page.locator('#file-input').setInputFiles('.codex/slow.pdf');
  await page.locator('#file-input').setInputFiles('.codex/fast.pdf');
  await page.getByLabel('学校账号密码', {exact:true}).waitFor({state:'visible'});
  await page.evaluate(() => window.releaseSlowReader());
  assert(await page.locator('#doc-name').textContent() === 'fast.pdf', 'Late FileReader overwrote selected document');
  await page.getByRole('button', {name:'移除',exact:true}).click();

  await page.setViewportSize({width:1440,height:1000});
  await page.screenshot({path:'output/playwright/print-portal/desktop.png',fullPage:true});
  await page.setViewportSize({width:375,height:812});
  await page.emulateMedia({reducedMotion:'reduce'});
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'Mobile horizontal overflow');
  await page.screenshot({path:'output/playwright/print-portal/mobile.png',fullPage:true});
  assert(errors.length === 0, 'Unexpected browser errors: '+errors.join('; '));
  return {passed:['inspection','submission','double-submit guard','password clearing','no browser storage','504 recovery with same key','late-response account isolation','offline gate','login and Escape clearing','FileReader replacement race','375px layout','reduced motion'],realPrintJobs:0};
}
