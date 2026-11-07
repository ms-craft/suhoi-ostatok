const { app, BrowserWindow } = require('electron');
const path = require('path');
const fs = require('fs');

app.disableHardwareAcceleration();
app.commandLine.appendSwitch('no-sandbox');

const OUT = process.argv[2] || path.join(__dirname, '_shots');
const sleep = ms => new Promise(r => setTimeout(r, ms));

app.whenReady().then(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const win = new BrowserWindow({
    width: 390, height: 844, x: 0, y: 0, show: true, frame: false,
    webPreferences: { contextIsolation: true },
  });
  await win.loadFile(path.join(__dirname, 'index.html'));
  await sleep(1100);
  win.setAlwaysOnTop(true); win.focus(); win.show();

  const shot = async (name) => {
    await win.webContents.executeJavaScript('window.getSelection().removeAllRanges()');
    await win.webContents.capturePage();
    await sleep(120);
    const img = await win.webContents.capturePage();
    fs.writeFileSync(path.join(OUT, name + '.png'), img.toPNG());
    console.log('shot', name);
  };

  await win.webContents.executeJavaScript(`window.__SK.setScreen('scan')`);
  await sleep(400); await shot('m-scan');

  await win.webContents.executeJavaScript(`window.__SK.setScreen('sheets')`);
  await sleep(400); await shot('m-sheets');

  await win.webContents.executeJavaScript(`window.__SK.setScreen('check')`);
  await sleep(900); await shot('m-check');

  await win.webContents.executeJavaScript(`window.__SK.setScreen('result')`);
  await sleep(500); await shot('m-result');

  await win.webContents.executeJavaScript(`window.__SK.openModal(1,1)`);
  await sleep(600); await shot('m-modal');

  app.quit();
}).catch(e => { console.log('FATAL', e); app.quit(); });
