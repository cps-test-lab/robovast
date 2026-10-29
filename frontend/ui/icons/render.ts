// Draws the installable app's icons from the RoboVAST mark (public/favicon.svg) into this directory,
// with the fingerprint of what they were drawn from. `npm run icons`; commit what it writes.
//
// Rasterising needs a renderer, and the build carries none: this uses Playwright's Chromium, as the
// phone rig does, so it runs where that is installed and is not a dependency of the build. The
// build checks the icons against the fingerprint (pwa.ts, readIcons) and refuses stale ones.

import { writeFileSync } from 'node:fs'
import { basename, join } from 'node:path'
import { ALL_ICONS, FINGERPRINT_FILE, iconSvg, iconsFingerprint, robovastPwaOptions } from '../pwa'

async function playwright() {
  try {
    return await import('playwright')
  } catch {
    throw new Error(
      'icons: Playwright is not installed. Install it for this run, without adding it to the '
      + 'project: `npm install --no-save playwright && npx playwright install chromium`.',
    )
  }
}

const opts = robovastPwaOptions()
const { chromium } = await playwright()
const browser = await chromium.launch()
try {
  for (const icon of ALL_ICONS) {
    const page = await browser.newPage({ viewport: { width: icon.size, height: icon.size }, deviceScaleFactor: 1 })
    await page.setContent(
      `<body style="margin:0;background:transparent">${iconSvg(opts.markSvg, icon, opts.background)}</body>`,
    )
    // Transparent where the drawing is: a tile icon's rounded corners stay see-through.
    const png = await page.screenshot({ omitBackground: true, clip: { x: 0, y: 0, width: icon.size, height: icon.size } })
    writeFileSync(join(opts.iconDir, basename(icon.file)), png)
    await page.close()
  }
} finally {
  await browser.close()
}
writeFileSync(join(opts.iconDir, FINGERPRINT_FILE), `${iconsFingerprint(opts.markSvg, opts.background)}\n`)
console.log(`icons: ${ALL_ICONS.length} drawn into ${opts.iconDir}`)
