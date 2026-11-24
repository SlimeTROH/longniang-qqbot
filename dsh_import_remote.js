#!/usr/bin/env node
/**
 * 把 QQ 远程操控（dsh-acp-home）里产生的会话导入本地 DSH 桌面端。
 *
 * 做的事：
 *   1. 读 <ACP_HOME>/sessions/<projectKey>/<sessionId>/session.v*.jsonl.zstd 的头部
 *   2. 复制到 <本地 ~/.dsh>/sessions/<projectKey>/<sessionId>/
 *   3. 复制投影缓存 <home>/storages/session_projcache/sessions/<id>.json（有就复制）
 *   4. 把会话挂到 workspace.json 里对应路径的工作区；没有工作区就新建
 *
 * 用法：
 *   node dsh_import_remote.js --dry-run     只看会做什么
 *   node dsh_import_remote.js --apply       真正写入（会先备份 workspace.json）
 *
 * 注意：DSH 桌面端运行时只认内存里的注册表，退出时可能覆盖文件。
 *       建议：先退出 DSH 桌面端 → 运行本脚本 → 再打开桌面端。
 */
const fs = require("fs");
const path = require("path");
const os = require("os");
const zlib = require("zlib");
const crypto = require("crypto");

const ACP_HOME = process.env.DSH_ACP_HOME || "D:\\dshex\\dsh-acp-home";
const LOCAL_HOME = process.env.DSH_HOME || path.join(os.homedir(), ".dsh");
const args = process.argv.slice(2);
const DRY = !args.includes("--apply");
const CREATE_MISSING_DIRS = args.includes("--create-dirs");

const srcSessions = path.join(ACP_HOME, "sessions");
const dstSessions = path.join(LOCAL_HOME, "sessions");
const srcCache = path.join(ACP_HOME, "storages", "session_projcache", "sessions");
const dstCache = path.join(LOCAL_HOME, "storages", "session_projcache", "sessions");
const regPath = path.join(LOCAL_HOME, "storages", "workspace.json");

if (!fs.existsSync(srcSessions)) {
  console.error("找不到远程会话目录:", srcSessions);
  process.exit(1);
}
if (!fs.existsSync(regPath)) {
  console.error("找不到本地 workspace.json:", regPath);
  process.exit(1);
}

function readHeader(file) {
  const buf = fs.readFileSync(file);
  let text;
  try {
    text = zlib.zstdDecompressSync(buf).toString("utf8");
  } catch (e) {
    return null;
  }
  const line = text.split("\n").find((x) => x.trim());
  if (!line) return null;
  try {
    return JSON.parse(line);
  } catch (e) {
    return null;
  }
}

function pickSessionFile(dir) {
  const files = fs
    .readdirSync(dir)
    .filter((f) => /^session\.v\d+\.jsonl\.zstd$/.test(f))
    .sort();
  return files.length ? path.join(dir, files[files.length - 1]) : null;
}

// ---------------- 扫描远程会话 ----------------
const sessions = [];
for (const key of fs.readdirSync(srcSessions)) {
  const keyDir = path.join(srcSessions, key);
  if (!fs.statSync(keyDir).isDirectory()) continue;
  for (const sid of fs.readdirSync(keyDir)) {
    const sdir = path.join(keyDir, sid);
    if (!fs.statSync(sdir).isDirectory()) continue;
    const sfile = pickSessionFile(sdir);
    if (!sfile) continue;
    const header = readHeader(sfile);
    if (!header || !header.cwd) continue;
    sessions.push({
      key,
      sid,
      cwd: header.cwd,
      createdAt: header.createdAt || 0,
      title: header.title || "",
      file: sfile,
      dir: sdir,
    });
  }
}
sessions.sort((a, b) => a.createdAt - b.createdAt);

// ---------------- 更新注册表 ----------------
const reg = JSON.parse(fs.readFileSync(regPath, "utf8"));
if (!reg.tables || !reg.tables.workspaces || !reg.global) {
  console.error("workspace.json 结构不认识，先备份再手动看");
  process.exit(1);
}
const workspaces = reg.tables.workspaces;
const byPath = new Map();
for (const [wid, w] of Object.entries(workspaces)) {
  if (w && w.path) byPath.set(String(w.path).toLowerCase(), wid);
}

const report = { copied: 0, attached: 0, createdWorkspaces: [], skipped: [] };

for (const s of sessions) {
  // 本地已存在同名会话就不再覆盖（避免把本地更新的进度盖掉）
  const dstDir = path.join(dstSessions, s.key, s.sid);
  const exists = fs.existsSync(dstDir);

  let cwdOk = fs.existsSync(s.cwd);
  if (!cwdOk && CREATE_MISSING_DIRS) {
    if (!DRY) fs.mkdirSync(s.cwd, { recursive: true });
    cwdOk = true;
  }
  if (!cwdOk) {
    report.skipped.push(`${s.sid} (cwd 不存在: ${s.cwd})`);
    continue;
  }

  if (!DRY) {
    fs.mkdirSync(dstDir, { recursive: true });
    // 复制会话文件（本地已有则取更大的那个，避免覆盖新进度）
    const dstFile = path.join(dstDir, path.basename(s.file));
    const dstSize = fs.existsSync(dstFile) ? fs.statSync(dstFile).size : -1;
    const srcSize = fs.statSync(s.file).size;
    if (!exists || srcSize > dstSize) {
      fs.copyFileSync(s.file, dstFile);
      report.copied++;
    }
    // 投影缓存
    const cacheSrc = path.join(srcCache, `${s.sid}.json`);
    if (fs.existsSync(cacheSrc)) {
      fs.mkdirSync(dstCache, { recursive: true });
      fs.copyFileSync(cacheSrc, path.join(dstCache, `${s.sid}.json`));
    }
  } else {
    report.copied++;
  }

  // 挂到工作区
  const wid = byPath.get(s.cwd.toLowerCase());
  if (wid) {
    const w = workspaces[wid];
    if (!w) {
      // 预演时刚“虚拟创建”的工作区，后续会话不再重复统计
    } else {
      if (!Array.isArray(w.sessionIds)) w.sessionIds = [];
      if (!w.sessionIds.includes(s.sid)) {
        if (!DRY) {
          w.sessionIds.unshift(s.sid);
          w.updatedAt = new Date().toISOString();
        }
        report.attached++;
      }
    }
  } else {
    const newId = crypto.randomUUID();
    if (!DRY) {
      workspaces[newId] = {
        path: s.cwd,
        title: path.basename(s.cwd) || s.cwd,
        sessionIds: [s.sid],
        createdAt: new Date(s.createdAt || Date.now()).toISOString(),
        updatedAt: new Date().toISOString(),
      };
      reg.global.workspaceIds = [newId, ...(reg.global.workspaceIds || [])];
    }
    byPath.set(s.cwd.toLowerCase(), newId);
    report.createdWorkspaces.push(`${s.cwd} (${path.basename(s.cwd)})`);
  }
}

if (!DRY) {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  fs.copyFileSync(regPath, `${regPath}.bak-${stamp}`);
  fs.writeFileSync(regPath, JSON.stringify(reg, null, 2), "utf8");
}

console.log(DRY ? "=== 预演（没有写任何文件） ===" : "=== 已写入 ===");
console.log(`远程会话总数: ${sessions.length}`);
console.log(`复制/更新: ${report.copied}`);
console.log(`新挂到已有工作区: ${report.attached}`);
console.log(`新建工作区: ${report.createdWorkspaces.length}`);
for (const w of report.createdWorkspaces) console.log("  +", w);
if (report.skipped.length) {
  console.log(`跳过: ${report.skipped.length}`);
  for (const x of report.skipped.slice(0, 10)) console.log("  -", x);
}
console.log(`\n本地 home: ${LOCAL_HOME}`);
console.log(DRY ? "确认无误后加 --apply 正式导入。" : "重启 DSH 桌面端即可在侧栏看到这些会话。");
