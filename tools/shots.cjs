// node tools/shots.cjs <outdir> <url1> [url2 ...] — desktop (1280) and phone (390) screenshots, JPEG, full page capped.
// Run from the consult root, so `playwright` resolves from its node_modules. consult starts it with a clean
// environment (no API keys). Every viewport gets a fresh browser context: no cookies, no stored logins, no persistent
// profile; service workers and downloads are off, only http(s) requests are allowed, and a redirect to another host
// (apart from www.) fails the shot instead of capturing a page nobody asked for.
const { chromium } = require('playwright');
const bare = h => h.replace(/^www\./, '');
(async () => {
  const [out, ...urls] = process.argv.slice(2);
  const b = await chromium.launch();
  for (const [w, h, tag] of [[1280, 900, 'desktop'], [390, 844, 'mobile']]) {
    const ctx = await b.newContext({ viewport: { width: w, height: h }, serviceWorkers: 'block', acceptDownloads: false });
    await ctx.route('**/*', r => (/^https?:$/.test(new URL(r.request().url()).protocol) ? r.continue() : r.abort()));
    const p = await ctx.newPage();
    for (const u of urls) {
      const name = (new URL(u).pathname.replace(/\/$/, '') || '/home').split('/').filter(Boolean).join('-') || 'home';
      await p.goto(u, { waitUntil: 'networkidle' });
      if (bare(new URL(p.url()).host) !== bare(new URL(u).host)) throw new Error(`${u} redirected to ${p.url()}`);
      const height = Math.min(await p.evaluate(() => document.documentElement.scrollHeight), tag === 'mobile' ? 4200 : 3200);
      await p.setViewportSize({ width: w, height });
      await p.screenshot({ path: `${out}/${name}-${tag}.jpg`, type: 'jpeg', quality: 70 });
      console.log(`${name}-${tag}.jpg`);
    }
    await ctx.close();
  }
  await b.close();
})().catch(e => { console.error(e.message); process.exit(1); });
