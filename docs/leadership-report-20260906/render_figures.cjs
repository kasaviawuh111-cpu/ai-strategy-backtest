const {chromium}=require('/Users/mima0000/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const path=require('path');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:'/Users/mima0000/Library/Caches/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-mac-arm64/chrome-headless-shell'});
 const page=await browser.newPage({viewport:{width:1240,height:950},deviceScaleFactor:2});
 await page.goto('file://'+path.join(__dirname,'figures.html'));
 for(const name of ['journey','architecture','cache']) await page.locator('#fig-'+name).screenshot({path:path.join(__dirname,'assets',name+'.png')});
 await page.goto('file://'+path.join(__dirname,'index.html'));
 await page.screenshot({path:path.join(__dirname,'assets','desktop.png')});
 await page.setViewportSize({width:390,height:844});
 await page.screenshot({path:path.join(__dirname,'assets','mobile.png')});
 const issues=await page.evaluate(()=>({scrollWidth:document.documentElement.scrollWidth,width:innerWidth,brokenLinks:[...document.querySelectorAll('a[href^="#"]')].filter(a=>!document.getElementById(a.hash.slice(1))).map(a=>a.hash)}));
 console.log(JSON.stringify(issues));await browser.close();
})();
