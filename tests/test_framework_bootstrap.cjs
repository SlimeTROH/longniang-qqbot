const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../scripts/qq_framework_entry.js'), 'utf8');

test('isolates the bot before QQ startup, including paths containing spaces', () => {
    const qqRoot = 'D:\\bot installation\\qqnt_bot';
    const userData = path.win32.join(qqRoot, 'userdata');
    const loaderRoot = path.win32.join(qqRoot, 'LiteLoader');
    const events = [];
    const env = { LITELOADERQQNT_PROFILE: 'D:\\personal profile', HTTPS_PROXY: 'existing-proxy' };
    vm.runInNewContext(source, {
        process: { resourcesPath: path.win32.join(qqRoot, 'versions/v1/resources'), env },
        require(name) {
            if (name === 'path') return path.win32;
            if (name === 'fs') return { mkdirSync: (...args) => events.push(['mkdir', ...args]) };
            if (name === 'electron') return { app: { setPath: (...args) => events.push(['setPath', ...args]) } };
            assert.equal(name, loaderRoot);
            assert.equal(env.LITELOADERQQNT_PROFILE, loaderRoot);
            events.push(['startQQ']);
        },
    });
    assert.deepEqual(JSON.parse(JSON.stringify(events)), [
        ['mkdir', userData, { recursive: true }],
        ['setPath', 'userData', userData],
        ['setPath', 'sessionData', userData],
        ['startQQ'],
    ]);
    assert.equal(env.HTTPS_PROXY, 'existing-proxy');
});

test('does not start QQ if its isolated directory cannot be created', () => {
    let started = false;
    assert.throws(() => vm.runInNewContext(source, {
        process: { resourcesPath: 'D:\\bot\\versions/v1/resources', env: {} },
        require(name) {
            if (name === 'path') return path.win32;
            if (name === 'fs') return { mkdirSync() { throw new Error('access denied'); } };
            if (name === 'electron') return { app: { setPath() {} } };
            started = true;
        },
    }), /access denied/);
    assert.equal(started, false);
});
