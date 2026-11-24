'use strict';

// Installed as versions/<version>/resources/app/app_launcher/LiteLoader.js.
const path = require('path');
const fs = require('fs');
const qqRoot = path.resolve(process.resourcesPath, '../../..');
const userData = path.join(qqRoot, 'userdata');
const loaderRoot = path.join(qqRoot, 'LiteLoader');

// Set both paths before LiteLoader starts QQ or acquires its instance lock.
fs.mkdirSync(userData, { recursive: true });
console.log('[QQBot] Framework entry: setting isolated Electron paths');
const { app } = require('electron');
app.setPath('userData', userData);
app.setPath('sessionData', userData);
process.env.LITELOADERQQNT_PROFILE = loaderRoot;
console.log('[QQBot] Framework entry: loading LiteLoader');
require(loaderRoot);
console.log('[QQBot] Framework entry: LiteLoader loaded');
