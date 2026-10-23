// Capture Domoticz device cards one by one (Machinon theme) for the hero and the docs.
//
//   node capture_cards.js <out-dir> <page> [--no-footer] [--name From=To ...] <idx> [<idx> ...]
//
// <page> is the Domoticz page the cards are on (Utility, LightSwitches, ...). Set DZ_URL for an
// instance other than http://127.0.0.1:8080. Needs Playwright (NODE_PATH pointing at a
// node_modules that has it).
//
// Each card is clipped 10 px wide of its border box so its drop shadow survives, on a transparent
// page background so its rounded corners do too. --no-footer hides the last-update time, star,
// menu and graph link (hidden, not removed, so every card keeps its height). --name replaces an
// appliance name in the captured text, so English images can show "Washer" where the instance
// has the owner's own name; screenshot only, nothing changes in Domoticz.
const { chromium } = require('playwright');

const BASE = process.env.DZ_URL || 'http://127.0.0.1:8080';

const args = process.argv.slice(2);
const outDir = args.shift();
const page = args.shift();
let noFooter = false;
const names = [];
const idxs = [];
while (args.length) {
  const a = args.shift();
  if (a === '--no-footer') noFooter = true;
  else if (a === '--name') names.push(args.shift().split('='));
  else idxs.push(a);
}
const wait = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch();
  const p = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2 });
  await p.goto(`${BASE}/#/${page}`, { waitUntil: 'networkidle' });
  // Long enough for the device-timeout toasts to expire; they would sit on the cards.
  await wait(9000);
  // Removed, not closed: a leftover backdrop tints every pixel.
  await p.evaluate(() => {
    document.querySelectorAll('.modal-backdrop, .modal').forEach(e => e.remove());
    document.body.classList.remove('modal-open');
  });
  await p.evaluate((names) => {
    const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = walk.nextNode(); n; n = walk.nextNode())
      for (const [from, to] of names) n.nodeValue = n.nodeValue.replace(from, to);
  }, names);
  let css = `html, body, #holder, .container-fluid, .bannercontent, #main-view, .dd-page,
    .grid-stack, .dd-grid, #itemtable, #itemtable tbody { background: transparent !important; }`;
  if (noFooter) {
    css += `.ion-ios-pulse, .ion-ios-pulse + span, td.options-cell, td.timers_log, td.favorite,
      i.ion-md-more { visibility: hidden !important; }`;
  }
  await p.addStyleTag({ content: css });

  for (const idx of idxs) {
    // A card can carry data-idx on a wrapper and an inner element: the largest is the card.
    let best = null, area = 0;
    for (const h of await p.$$(`[data-idx="${idx}"]`)) {
      const box = await h.boundingBox();
      if (box && box.width * box.height > area) { best = h; area = box.width * box.height; }
    }
    if (!best) { console.log('missing', idx); process.exitCode = 1; continue; }
    // The clip is in viewport coordinates: an off-screen card would clip to nothing.
    await best.evaluate(e => e.scrollIntoView({ block: 'center' }));
    await wait(350);
    const box = await best.boundingBox();
    const m = 10;
    await p.screenshot({
      path: `${outDir}/${idx}.png`, omitBackground: true,
      clip: { x: box.x - m, y: box.y - m, width: box.width + 2 * m, height: box.height + 2 * m },
    });
    console.log('ok', idx);
  }
  await browser.close();
})();
