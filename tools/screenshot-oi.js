const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
  });
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  const page = await context.newPage();

  // Use cache-busting URL for hard reload
  console.log('Navigating to http://localhost:3001/?v=2 (cache-busting)');
  await page.goto('http://localhost:3001/?v=2', { waitUntil: 'domcontentloaded' });

  console.log('Waiting 4s for charts to render...');
  await page.waitForTimeout(4000);

  // Run the alignment JavaScript
  const chartAlignInfo = await page.evaluate(() => {
    const results = [];
    if (typeof Chart === 'undefined') {
      results.push('Chart.js not found on window');
      return results;
    }
    const mainChart = Chart.getChart('oiChart');
    const deltaChart = Chart.getChart('oiDeltaChart');
    if (mainChart && deltaChart) {
      const mca = mainChart.chartArea;
      const dca = deltaChart.chartArea;
      results.push('Main chartArea: ' + JSON.stringify({left: Math.round(mca.left), right: Math.round(mca.right), width: mainChart.width}));
      results.push('Delta chartArea: ' + JSON.stringify({left: Math.round(dca.left), right: Math.round(dca.right), width: deltaChart.width}));
      results.push('Left diff: ' + (mca.left - dca.left).toFixed(1) + ' px');
      results.push('Right diff: ' + ((mainChart.width - mca.right) - (deltaChart.width - dca.right)).toFixed(1) + ' px');
      results.push('window._oiChartArea: ' + JSON.stringify(window._oiChartArea));
    } else {
      results.push('mainChart found: ' + !!mainChart);
      results.push('deltaChart found: ' + !!deltaChart);
    }
    return results;
  });

  console.log('\n=== Chart Alignment Info ===');
  for (const line of chartAlignInfo) console.log(line);

  // Get tight bounding box: find a heading element + both canvases
  const box = await page.evaluate(() => {
    const oiCanvas = document.getElementById('oiChart');
    const deltaCanvas = document.getElementById('oiDeltaChart');
    if (!oiCanvas) return null;

    // Find the section heading (look for text containing 期限结构)
    let headingEl = null;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (node.textContent.includes('期限结构') || node.textContent.includes('COMEX GC')) {
        headingEl = node.parentElement;
        // Walk up to find a block element
        while (headingEl && getComputedStyle(headingEl).display === 'inline') {
          headingEl = headingEl.parentElement;
        }
        break;
      }
    }

    const oiRect = oiCanvas.getBoundingClientRect();
    const deltaRect = deltaCanvas ? deltaCanvas.getBoundingClientRect() : oiRect;

    // Include heading if found and it's above the chart
    let topY = oiRect.top + window.scrollY;
    if (headingEl) {
      const hRect = headingEl.getBoundingClientRect();
      const hTop = hRect.top + window.scrollY;
      if (hTop < topY) topY = hTop - 8; // 8px padding above heading
    } else {
      topY -= 30; // fallback padding
    }

    const bottomY = (deltaCanvas ? deltaRect.bottom : oiRect.bottom) + window.scrollY + 10;
    const leftX = Math.min(oiRect.left, deltaRect.left) + window.scrollX - 10;
    const rightX = Math.max(oiRect.right, deltaRect.right) + window.scrollX + 10;

    return {
      absTop: topY,
      absLeft: leftX,
      width: rightX - leftX,
      height: bottomY - topY
    };
  });

  console.log('\nTight bounding box:', JSON.stringify(box));

  const screenshotDir = path.join(__dirname, 'screenshots');
  if (!fs.existsSync(screenshotDir)) fs.mkdirSync(screenshotDir, { recursive: true });

  if (box) {
    // Scroll so the section top is near the viewport top
    await page.evaluate((b) => window.scrollTo(0, b.absTop - 20), box);
    await page.waitForTimeout(400);

    // After scroll, recalculate viewport-relative position
    const viewportClip = await page.evaluate((b) => {
      return {
        x: b.absLeft,
        y: b.absTop - window.scrollY,
        width: b.width,
        height: b.height
      };
    }, box);

    console.log('Viewport clip:', JSON.stringify(viewportClip));

    const sectionPath = path.join(screenshotDir, 'oi-aligned.png');
    await page.screenshot({
      path: sectionPath,
      clip: viewportClip
    });
    console.log('\nSection screenshot saved to:', sectionPath);
  }

  await browser.close();
})();
