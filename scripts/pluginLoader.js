"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
const PLUGIN_LOADER_VERSION = "2.3.0";
exports.PLUGIN_LOADER_VERSION = PLUGIN_LOADER_VERSION;
exports.attachWebContents = attachWebContents;
exports.translateTrayItem = translateTrayItem;
exports.translateTrayItems = translateTrayItems;
exports.formatTrayAgentCount = formatTrayAgentCount;
exports.getActiveUiPlugins = getActiveUiPlugins;
exports.resolveSafePluginFile = resolveSafePluginFile;
exports.mergeUsageHistory = mergeUsageHistory;
exports.parseKbMarkdownMeta = parseKbMarkdownMeta;
exports.resolveSafeKbPath = resolveSafeKbPath;
exports.scanKbVault = scanKbVault;
exports.searchKbVault = searchKbVault;

const crypto = require("crypto");
const electron_1 = require("electron");
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");

function stripJsonComments(text) {
    let out = "";
    let inString = false;
    let escaped = false;
    let inLineComment = false;
    let inBlockComment = false;
    for (let i = 0; i < text.length; i++) {
        const ch = text[i];
        const next = text[i + 1];
        if (inLineComment) {
            if (ch === "\n") {
                inLineComment = false;
                out += ch;
            }
            continue;
        }
        if (inBlockComment) {
            if (ch === "*" && next === "/") {
                inBlockComment = false;
                i++;
            }
            continue;
        }
        if (inString) {
            out += ch;
            if (escaped) {
                escaped = false;
            }
            else if (ch === "\\") {
                escaped = true;
            }
            else if (ch === '"') {
                inString = false;
            }
            continue;
        }
        if (ch === '"') {
            inString = true;
            out += ch;
        }
        else if (ch === "/" && next === "/") {
            inLineComment = true;
            i++;
        }
        else if (ch === "/" && next === "*") {
            inBlockComment = true;
            i++;
        }
        else {
            out += ch;
        }
    }
    return out.replace(/,\s*([}\]])/g, "$1");
}

function readJsonSafe(filePath, isJsonc = false) {
    try {
        if (!fs.existsSync(filePath)) {
            return null;
        }
        const raw = fs.readFileSync(filePath, "utf8");
        return JSON.parse(isJsonc ? stripJsonComments(raw) : raw);
    }
    catch {
        return null;
    }
}

function writePluginLog(pluginPath, message) {
    try {
        const logPath = path.join(pluginPath, "runtime", "loader.log");
        if (fs.existsSync(logPath) && fs.statSync(logPath).size > 65536) {
            fs.writeFileSync(logPath, "", "utf8");
        }
        fs.appendFileSync(logPath, `[${new Date().toISOString()}] ${message}\n`, "utf8");
    }
    catch {
        // Ignore logging errors
    }
}

function resolveSafePluginFile(pluginPath, relPath) {
    if (typeof relPath !== "string" || !relPath.trim()) {
        return null;
    }
    const base = path.resolve(pluginPath);
    const target = path.resolve(base, relPath);
    if (target !== base && !target.startsWith(base + path.sep)) {
        return null;
    }
    return target;
}

function getActiveUiPlugins() {
    const geminiConfigDir = path.join(os.homedir(), ".gemini", "config");
    const pluginsRoot = path.join(geminiConfigDir, "plugins");
    if (!fs.existsSync(pluginsRoot)) {
        return [];
    }
    const config = readJsonSafe(path.join(geminiConfigDir, "config.json")) || {};
    const configPlugins = config && typeof config.plugins === "object" && config.plugins ? config.plugins : {};
    const result = [];
    let entries;
    try {
        entries = fs.readdirSync(pluginsRoot, { withFileTypes: true });
    }
    catch {
        return [];
    }
    for (const entry of entries) {
        if (!entry.isDirectory()) {
            continue;
        }
        const dirName = entry.name;
        const pluginPath = path.join(pluginsRoot, dirName);
        const manifest = readJsonSafe(path.join(pluginPath, "plugin.json"), true) || {};
        const userSetting = configPlugins[dirName] || (typeof manifest.name === "string" ? configPlugins[manifest.name] : undefined);
        const enabled = typeof userSetting?.enabled === "boolean"
            ? userSetting.enabled
            : manifest.disabled !== true;
        if (!enabled) {
            continue;
        }

        let dict = null;
        let engineCode = null;
        const enginePath = path.join(pluginPath, "runtime", "i18n-engine.js");
        const dictPath = path.join(pluginPath, "runtime", "zh-CN.json");
        if (fs.existsSync(enginePath) && fs.existsSync(dictPath)) {
            const loadedDict = readJsonSafe(dictPath, false);
            if (loadedDict) {
                try {
                    engineCode = fs.readFileSync(enginePath, "utf8");
                    dict = loadedDict;
                }
                catch {
                    // Skip unreadable i18n engine
                }
            }
        }

        const uiScriptPaths = [];
        if (Array.isArray(manifest.uiScripts)) {
            for (const rel of manifest.uiScripts) {
                const resolved = resolveSafePluginFile(pluginPath, rel);
                if (resolved) {
                    uiScriptPaths.push({ rel, fullPath: resolved });
                }
            }
        }
        else if (typeof manifest.uiEntry === "string") {
            const resolved = resolveSafePluginFile(pluginPath, manifest.uiEntry);
            if (resolved) {
                uiScriptPaths.push({ rel: manifest.uiEntry, fullPath: resolved });
            }
        }
        else {
            const defaultWidget = path.join(pluginPath, "runtime", "ui-widget.js");
            if (fs.existsSync(defaultWidget)) {
                uiScriptPaths.push({ rel: "runtime/ui-widget.js", fullPath: defaultWidget });
            }
        }

        const uiScripts = [];
        for (const item of uiScriptPaths) {
            if (!fs.existsSync(item.fullPath)) {
                continue;
            }
            try {
                uiScripts.push({
                    rel: item.rel,
                    code: fs.readFileSync(item.fullPath, "utf8"),
                });
            }
            catch {
                // Skip unreadable UI widget script
            }
        }

        if ((dict && engineCode) || uiScripts.length > 0) {
            result.push({ dirName, pluginPath, dict, engineCode, uiScripts });
        }
    }
    return result;
}

function getMergedTrayDict() {
    const active = getActiveUiPlugins();
    const merged = {};
    for (const plugin of active) {
        if (plugin.dict && typeof plugin.dict.tray === "object" && plugin.dict.tray) {
            Object.assign(merged, plugin.dict.tray);
        }
    }
    return Object.keys(merged).length > 0 ? merged : null;
}

function translateTrayItem(item, trayDict = null) {
    if (!item || typeof item !== "object") {
        return item;
    }
    const dict = trayDict ?? getMergedTrayDict();
    if (!dict) {
        return item;
    }
    const copy = { ...item };
    if (typeof copy.label === "string" && typeof dict[copy.label] === "string") {
        copy.label = dict[copy.label];
    }
    if (Array.isArray(copy.submenu)) {
        copy.submenu = copy.submenu.map((sub) => translateTrayItem(sub, dict));
    }
    return copy;
}

function translateTrayItems(items) {
    if (!Array.isArray(items)) {
        return items;
    }
    const dict = getMergedTrayDict();
    if (!dict) {
        return items;
    }
    return items.map((item) => translateTrayItem(item, dict));
}

function formatTrayAgentCount(count) {
    const dict = getMergedTrayDict();
    if (!dict) {
        return null;
    }
    if (count > 0 && typeof dict.agents_running === "string") {
        return dict.agents_running.replace("{count}", String(count));
    }
    if (count === 0 && typeof dict["No agents running"] === "string") {
        return dict["No agents running"];
    }
    return null;
}

function isHubUiUrl(url) {
    if (!url || typeof url !== "string") {
        return false;
    }
    if (/^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?(\/|$)/i.test(url)) {
        return true;
    }
    const devUrl = process.env.DEV_URL;
    if (devUrl && url.startsWith(devUrl)) {
        return true;
    }
    return false;
}

const EARLY_RPC_TAP_CODE = `(function(){
try{
  if(window.__AGY_EARLY_RPC_TAP__) return;
  window.__AGY_EARLY_RPC_TAP__ = true;
  var H = window.__AGY_CAPTURED_LS_HEADERS__ = window.__AGY_CAPTURED_LS_HEADERS__ || {};
  var C = window.__AGY_CAPTURED_LS_CACHE__ = window.__AGY_CAPTURED_LS_CACHE__ || {};
  var PREFIX = '/exa.language_server_pb.LanguageServerService/';
  var METHODS = {
    GetUserStatus:1,
    RetrieveUserQuotaSummary:1,
    GetAvailableModels:1,
    GetCascadeModelConfigData:1,
    GetCascadeModelConfigs:1,
    GetUserAnalyticsSummary:1,
    GetAllCascadeTrajectories:1,
    GetCascadeTrajectoryGeneratorMetadata:1
  };
  var CASCADE_METHODS = {
    GetCascadeTrajectory:1,
    GetCascadeTrajectorySteps:1,
    GetCascadeTrajectoryGeneratorMetadata:1,
    GetCascadeTrajectoryExecutorMetadatas:1,
    GetTrajectoryTurns:1,
    SendUserCascadeMessage:1,
    StreamUserTrajectoryReactiveUpdates:1,
    StreamCascadeReactiveUpdates:1
  };
  var WS_CASCADE_RE = /GetCascadeTrajectorySteps|GetCascadeTrajectoryGeneratorMetadata|GetCascadeTrajectoryExecutorMetadatas|GetCascadeTrajectory|GetTrajectoryTurns|SendUserCascadeMessage|StreamCascadeReactiveUpdates|StreamUserTrajectoryReactiveUpdates/;
  function toAsciiPreview(body, limit){
    if(typeof body === 'string') return body;
    if(body instanceof ArrayBuffer){
      body = new Uint8Array(body, 0, Math.min(body.byteLength, limit || 512));
    }else if(typeof Uint8Array !== 'undefined' && (body instanceof Uint8Array || ArrayBuffer.isView(body))){
      body = new Uint8Array(body.buffer, body.byteOffset, Math.min(body.byteLength, limit || 512));
    }else{
      return '';
    }
    var txt = '';
    for(var i=0;i<body.length;i++){
      var ch = body[i];
      txt += (ch >= 32 && ch <= 126) ? String.fromCharCode(ch) : ' ';
    }
    return txt;
  }
  function scanCascadeId(method, body){
    if(!method || !CASCADE_METHODS[method] || !body) return;
    try{
      var cid = '';
      if(typeof body === 'string'){
        var trimmed = body.trim();
        if(trimmed.charAt(0) === '{'){
          var obj = JSON.parse(trimmed);
          cid = (obj && (obj.cascadeId || obj.trajectoryId || obj.conversationId)) || '';
        }
        if(!cid){
          var m = body.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
          if(m) cid = m[0];
        }
      }else{
        var txt = toAsciiPreview(body, 512);
        var mb = txt && txt.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
        if(mb) cid = mb[0];
      }
      if(cid && window.__AGY_CAPTURED_CASCADE_ID__ !== cid){
        window.__AGY_CAPTURED_CASCADE_ID__ = cid;
        if(typeof window.__AGY_ON_CASCADE_CHANGE__ === 'function'){
          try{ window.__AGY_ON_CASCADE_CHANGE__(cid, method); }catch(_){}
        }
      }
    }catch(_){}
  }
  function keepHeader(k, v){
    if(!k || v === undefined || v === null) return;
    var l = String(k).toLowerCase();
    if(l === 'x-codeium-ui-token' || l === 'x-codeium-csrf-token' || l === 'authorization' || l === 'x-requested-with' || l === 'connect-protocol-version'){
      var changed = H[l] !== String(v);
      H[l] = String(v);
      if(changed && typeof window.__AGY_ON_RPC_HEADERS__ === 'function'){
        try{ window.__AGY_ON_RPC_HEADERS__(H); }catch(_){}
      }
    }
  }
  function scanHeaders(hdrs){
    if(!hdrs) return;
    try{
      if(typeof hdrs.forEach === 'function'){
        hdrs.forEach(function(v, k){ keepHeader(k, v); });
      }else if(Array.isArray(hdrs)){
        for(var i=0;i<hdrs.length;i++){
          if(Array.isArray(hdrs[i]) && hdrs[i].length>=2) keepHeader(hdrs[i][0], hdrs[i][1]);
        }
      }else if(typeof hdrs === 'object'){
        var ks = Object.keys(hdrs);
        for(var j=0;j<ks.length;j++) keepHeader(ks[j], hdrs[ks[j]]);
      }
    }catch(_){}
  }
  if(typeof window.fetch === 'function'){
    var origFetch = window.fetch;
    window.fetch = function(input, init){
      var url = '';
      try{ url = typeof input === 'string' ? input : (input && input.url) || ''; }catch(_){}
      var idx = url.indexOf(PREFIX);
      var method = idx !== -1 ? url.slice(idx + PREFIX.length).split('?')[0] : '';
      if(idx !== -1){
        if(input && typeof input === 'object' && input.headers) scanHeaders(input.headers);
        if(init && init.headers) scanHeaders(init.headers);
        if(init && init.body) scanCascadeId(method, init.body);
      }
      var p = origFetch.apply(this, arguments);
      if(method && METHODS[method] && p && typeof p.then === 'function'){
        p.then(function(resp){
          if(!resp || !resp.ok || typeof resp.clone !== 'function') return;
          var ct = (resp.headers && resp.headers.get && resp.headers.get('content-type')) || '';
          if(ct.indexOf('json') === -1) return;
          return resp.clone().json().then(function(data){
            C[method] = data;
            if(typeof window.__AGY_ON_RPC_DATA__ === 'function'){
              try{ window.__AGY_ON_RPC_DATA__(method, data); }catch(_){}
            }
          });
        }).catch(function(){});
      }
      return p;
    };
  }
  if(typeof window.XMLHttpRequest === 'function' && window.XMLHttpRequest.prototype){
    var origOpen = window.XMLHttpRequest.prototype.open;
    var origSetHeader = window.XMLHttpRequest.prototype.setRequestHeader;
    var origSend = window.XMLHttpRequest.prototype.send;
    window.XMLHttpRequest.prototype.open = function(m, u){
      try{ this.__agyUrl = typeof u === 'string' ? u : String(u || ''); }catch(_){}
      return origOpen.apply(this, arguments);
    };
    window.XMLHttpRequest.prototype.setRequestHeader = function(k, v){
      try{
        if(this.__agyUrl && this.__agyUrl.indexOf(PREFIX) !== -1) keepHeader(k, v);
      }catch(_){}
      return origSetHeader.apply(this, arguments);
    };
    window.XMLHttpRequest.prototype.send = function(body){
      try{
        var u = this.__agyUrl || '';
        var idx = u.indexOf(PREFIX);
        if(idx !== -1){
          var method = u.slice(idx + PREFIX.length).split('?')[0];
          if(body) scanCascadeId(method, body);
          if(METHODS[method] && typeof this.addEventListener === 'function'){
            this.addEventListener('load', function(){
              try{
                if(this.status >= 200 && this.status < 300 && typeof this.responseText === 'string'){
                  var ct = this.getResponseHeader && this.getResponseHeader('content-type') || '';
                  if(ct.indexOf('json') !== -1){
                    var data = JSON.parse(this.responseText);
                    C[method] = data;
                    if(typeof window.__AGY_ON_RPC_DATA__ === 'function'){
                      window.__AGY_ON_RPC_DATA__(method, data);
                    }
                  }
                }
              }catch(_){}
            });
          }
        }
      }catch(_){}
      return origSend.apply(this, arguments);
    };
  }
  if(typeof window.WebSocket === 'function' && window.WebSocket.prototype && !window.__AGY_WS_RPC_TAP__){
    window.__AGY_WS_RPC_TAP__ = true;
    var origWsSend = window.WebSocket.prototype.send;
    if(typeof origWsSend === 'function'){
      window.WebSocket.prototype.send = function(body){
        try{
          var wsUrl = typeof this.url === 'string' ? this.url : '';
          if(!wsUrl || wsUrl.indexOf('connect-websocket') !== -1){
            var preview = toAsciiPreview(body, 768);
            var mm = preview && preview.match(WS_CASCADE_RE);
            if(mm) scanCascadeId(mm[0], preview);
          }
        }catch(_){}
        return origWsSend.apply(this, arguments);
      };
    }
  }
}catch(_){}
})();`;

function createEmptyUsageHistory() {
    return {
        version: 1,
        updatedAt: "",
        totals: {
            totalTokens: 0,
            inputTokens: 0,
            outputTokens: 0,
            thinkingTokens: 0,
            cacheReadTokens: 0,
            turns: 0,
        },
        byModel: {},
        daily: {},
        sessions: {},
        events: [],
    };
}

function readUsageHistorySafe(pluginPath) {
    const filePath = path.join(pluginPath, "runtime", "usage-history.json");
    const parsed = readJsonSafe(filePath, false);
    if (!parsed || typeof parsed !== "object") {
        return createEmptyUsageHistory();
    }
    return {
        version: 1,
        updatedAt: typeof parsed.updatedAt === "string" ? parsed.updatedAt : "",
        totals: parsed.totals && typeof parsed.totals === "object" ? parsed.totals : createEmptyUsageHistory().totals,
        byModel: parsed.byModel && typeof parsed.byModel === "object" ? parsed.byModel : {},
        daily: parsed.daily && typeof parsed.daily === "object" ? parsed.daily : {},
        sessions: parsed.sessions && typeof parsed.sessions === "object" ? parsed.sessions : {},
        events: Array.isArray(parsed.events) ? parsed.events : [],
    };
}

function mergeUsageHistory(existing, batch) {
    const base = existing && typeof existing === "object" ? existing : createEmptyUsageHistory();
    const events = Array.isArray(base.events) ? [...base.events] : [];
    const byModel = base.byModel && typeof base.byModel === "object" ? { ...base.byModel } : {};
    const daily = base.daily && typeof base.daily === "object" ? { ...base.daily } : {};
    const sessions = base.sessions && typeof base.sessions === "object" ? { ...base.sessions } : {};
    const totals = base.totals && typeof base.totals === "object"
        ? { ...createEmptyUsageHistory().totals, ...base.totals }
        : createEmptyUsageHistory().totals;

    const indexByKey = new Map();
    for (let i = 0; i < events.length; i++) {
        const ev = events[i];
        if (ev && ev.cascadeId && ev.executionId) {
            indexByKey.set(`${ev.cascadeId}::${ev.executionId}`, i);
        }
    }

    const incomingEvents = batch && Array.isArray(batch.events) ? batch.events : [];
    let changed = false;
    const applyDelta = (bucket, ev, sign) => {
        bucket.totalTokens = Math.max(0, (bucket.totalTokens || 0) + sign * (Number(ev.totalTokens) || 0));
        bucket.inputTokens = Math.max(0, (bucket.inputTokens || 0) + sign * (Number(ev.inputTokens) || 0));
        bucket.outputTokens = Math.max(0, (bucket.outputTokens || 0) + sign * (Number(ev.outputTokens) || 0));
        bucket.thinkingTokens = Math.max(0, (bucket.thinkingTokens || 0) + sign * (Number(ev.thinkingTokens) || 0));
        bucket.cacheReadTokens = Math.max(0, (bucket.cacheReadTokens || 0) + sign * (Number(ev.cacheReadTokens) || 0));
    };

    for (const rawEv of incomingEvents) {
        if (!rawEv || typeof rawEv !== "object" || !rawEv.cascadeId || !rawEv.executionId) {
            continue;
        }
        const ev = {
            ts: typeof rawEv.ts === "string" && rawEv.ts ? rawEv.ts : new Date().toISOString(),
            cascadeId: String(rawEv.cascadeId),
            executionId: String(rawEv.executionId),
            model: String(rawEv.model || "unknown"),
            modelEnum: String(rawEv.modelEnum || ""),
            inputTokens: Math.max(0, Number(rawEv.inputTokens) || 0),
            outputTokens: Math.max(0, Number(rawEv.outputTokens) || 0),
            thinkingTokens: Math.max(0, Number(rawEv.thinkingTokens) || 0),
            cacheReadTokens: Math.max(0, Number(rawEv.cacheReadTokens) || 0),
            totalTokens: Math.max(0, Number(rawEv.totalTokens) || 0),
            streamingSec: Math.max(0, Number(rawEv.streamingSec) || 0),
            tps: typeof rawEv.tps === "number" && Number.isFinite(rawEv.tps) ? Number(rawEv.tps.toFixed(2)) : null,
        };
        const key = `${ev.cascadeId}::${ev.executionId}`;
        const prevIdx = indexByKey.get(key);
        const dateKey = ev.ts.slice(0, 10);

        if (prevIdx !== undefined) {
            const prev = events[prevIdx];
            if (
                prev.totalTokens === ev.totalTokens &&
                prev.outputTokens === ev.outputTokens &&
                prev.tps === ev.tps &&
                prev.model === ev.model
            ) {
                continue;
            }
            applyDelta(totals, prev, -1);
            if (prev.model && byModel[prev.model]) {
                applyDelta(byModel[prev.model], prev, -1);
            }
            const prevDate = typeof prev.ts === "string" ? prev.ts.slice(0, 10) : dateKey;
            if (daily[prevDate]) {
                applyDelta(daily[prevDate], prev, -1);
            }
            events[prevIdx] = ev;
        }
        else {
            indexByKey.set(key, events.length);
            events.push(ev);
            totals.turns = (totals.turns || 0) + 1;
            if (!daily[dateKey]) {
                daily[dateKey] = { totalTokens: 0, inputTokens: 0, outputTokens: 0, thinkingTokens: 0, cacheReadTokens: 0, turns: 0 };
            }
            daily[dateKey].turns = (daily[dateKey].turns || 0) + 1;
        }

        applyDelta(totals, ev, 1);
        if (!byModel[ev.model]) {
            byModel[ev.model] = {
                modelEnum: ev.modelEnum,
                totalTokens: 0,
                inputTokens: 0,
                outputTokens: 0,
                thinkingTokens: 0,
                cacheReadTokens: 0,
                tpsSum: 0,
                tpsSamples: 0,
                avgTps: null,
                lastUsedAt: ev.ts,
            };
        }
        const mEntry = byModel[ev.model];
        if (ev.modelEnum && !mEntry.modelEnum) {
            mEntry.modelEnum = ev.modelEnum;
        }
        applyDelta(mEntry, ev, 1);
        mEntry.lastUsedAt = ev.ts;
        if (ev.tps !== null && ev.tps > 0 && prevIdx === undefined) {
            mEntry.tpsSum = (mEntry.tpsSum || 0) + ev.tps;
            mEntry.tpsSamples = (mEntry.tpsSamples || 0) + 1;
            mEntry.avgTps = Number((mEntry.tpsSum / mEntry.tpsSamples).toFixed(2));
        }

        if (!daily[dateKey]) {
            daily[dateKey] = { totalTokens: 0, inputTokens: 0, outputTokens: 0, thinkingTokens: 0, cacheReadTokens: 0, turns: 0 };
        }
        applyDelta(daily[dateKey], ev, 1);
        changed = true;
    }

    const incomingSessions = batch && batch.sessions && typeof batch.sessions === "object" ? batch.sessions : null;
    if (incomingSessions) {
        for (const [cid, snap] of Object.entries(incomingSessions)) {
            if (!cid || !snap || typeof snap !== "object") continue;
            const prev = sessions[cid];
            if (
                !prev ||
                prev.totalTokens !== snap.totalTokens ||
                prev.calls !== snap.calls ||
                prev.lastTps !== snap.lastTps
            ) {
                sessions[cid] = {
                    lastUpdatedAt: snap.lastUpdatedAt || new Date().toISOString(),
                    totalTokens: Number(snap.totalTokens) || 0,
                    inputTokens: Number(snap.inputTokens) || 0,
                    outputTokens: Number(snap.outputTokens) || 0,
                    calls: Number(snap.calls) || 0,
                    lastTurnTokens: Number(snap.lastTurnTokens) || 0,
                    lastTps: typeof snap.lastTps === "number" ? snap.lastTps : null,
                };
                changed = true;
            }
        }
    }

    if (!changed) {
        return null;
    }

    const MAX_EVENTS = 2000;
    const trimmedEvents = events.length > MAX_EVENTS ? events.slice(events.length - MAX_EVENTS) : events;
    return {
        version: 1,
        updatedAt: new Date().toISOString(),
        totals,
        byModel,
        daily,
        sessions,
        events: trimmedEvents,
    };
}

function writeUsageHistoryAtomic(pluginPath, data) {
    try {
        const runtimeDir = path.join(pluginPath, "runtime");
        fs.mkdirSync(runtimeDir, { recursive: true });
        const dst = path.join(runtimeDir, "usage-history.json");
        const tmp = `${dst}.${process.pid}.tmp`;
        fs.writeFileSync(tmp, JSON.stringify(data, null, 2), "utf8");
        if (fs.existsSync(dst)) {
            fs.unlinkSync(dst);
        }
        fs.renameSync(tmp, dst);
    }
    catch {
        // Ignore persistence write errors
    }
}

// ==================== 轻量化 MD 知识库本地桥接 (KB Bridge) ====================

const KB_SKIP_DIRS = new Set([
    ".git",
    "node_modules",
    ".obsidian",
    ".trash",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    "target",
    ".next",
    ".turbo",
]);
const KB_MAX_FILES = 2000;
const KB_MAX_FILE_BYTES = 2 * 1024 * 1024;
const KB_MAX_BODY_BYTES = 4 * 1024 * 1024;
const KB_MD_EXT_RE = /\.(md|markdown)$/i;

function getDefaultKbVaultPath() {
    return path.join(os.homedir(), ".gemini", "kb");
}

function ensureDefaultKbVault() {
    const vaultDir = getDefaultKbVaultPath();
    try {
        fs.mkdirSync(vaultDir, { recursive: true });
        const markerPath = path.join(vaultDir, ".kb-initialized");
        if (!fs.existsSync(markerPath)) {
            const entries = fs.readdirSync(vaultDir);
            const hasMd = entries.some((name) => KB_MD_EXT_RE.test(name));
            if (!hasMd) {
                const starter = [
                    "---",
                    'title: "欢迎使用 Antigravity 知识库"',
                    "tags: [指南, Antigravity, 知识库]",
                    `updated: ${new Date().toISOString().slice(0, 19).replace("T", " ")}`,
                    "---",
                    "",
                    "# 欢迎使用 Antigravity 轻量化 Markdown 知识库",
                    "",
                    "这是一个**直接存储在本地真实 `.md` 文件夹**中的轻量化知识库（默认路径：`~/.gemini/kb`），完全兼容 Obsidian、VS Code 与 Git。",
                    "",
                    "## 常用玩法",
                    "",
                    "- **打开面板**：在右侧边栏点击 **`+ > 插件 > 知识库`**，或按快捷键 **`Alt+K`**。",
                    "- **快速引用**：在任意对话输入框键入 **`[[`** 即可检索并插入知识库笔记。",
                    "- **双链与标签**：支持 `[[笔记标题]]` 双向链接跳转、反向链接统计，以及 `#标签` 过滤。",
                    "- **AI 自动读写**：已内置 `antigravity-kb` MCP 工具，你可以直接让 AI 检索历史笔记或将排查结论沉淀进来。",
                    "",
                    "- [ ] 试着点击这个任务复选框（会自动写回本地 `.md` 文件）",
                    "- [ ] 新建你的第一篇排查笔记或项目备忘",
                    "",
                ].join("\n");
                fs.writeFileSync(path.join(vaultDir, "欢迎使用 Antigravity 知识库.md"), starter, "utf8");
            }
            fs.writeFileSync(markerPath, new Date().toISOString(), "utf8");
        }
    }
    catch {
        // Ignore starter vault creation errors
    }
    return vaultDir;
}

function parseKbMarkdownMeta(text, relPath = "") {
    const raw = typeof text === "string" ? text : "";
    const lines = raw.split(/\r?\n/);
    let title = "";
    const fmTags = [];
    const aliases = [];
    let bodyStart = 0;

    if (lines.length > 0 && lines[0].trim() === "---") {
        let endIdx = -1;
        for (let i = 1; i < Math.min(lines.length, 80); i++) {
            const s = lines[i].trim();
            if (s === "---" || s === "...") {
                endIdx = i;
                break;
            }
        }
        if (endIdx > 0) {
            bodyStart = endIdx + 1;
            let inTags = false;
            let inAliases = false;
            for (let i = 1; i < endIdx; i++) {
                const line = lines[i];
                const stripped = line.trim();
                if (!stripped || stripped.startsWith("#")) {
                    continue;
                }
                if (/^[ \t]+-\s+/.test(line)) {
                    const item = stripped.slice(2).trim().replace(/^['"]|['"]$/g, "");
                    if (inTags && item) {
                        fmTags.push(item.replace(/^#+/, ""));
                    }
                    else if (inAliases && item) {
                        aliases.push(item);
                    }
                    continue;
                }
                inTags = false;
                inAliases = false;
                const colonIdx = stripped.indexOf(":");
                if (colonIdx > 0) {
                    const k = stripped.slice(0, colonIdx).trim().toLowerCase();
                    const v = stripped.slice(colonIdx + 1).trim();
                    if (k === "title" && v) {
                        title = v.replace(/^['"]|['"]$/g, "");
                    }
                    else if (k === "tags") {
                        if (v.startsWith("[") && v.endsWith("]")) {
                            for (const part of v.slice(1, -1).split(",")) {
                                const t = part.trim().replace(/^['"]|['"]$/g, "").replace(/^#+/, "");
                                if (t) {
                                    fmTags.push(t);
                                }
                            }
                        }
                        else if (v) {
                            for (const part of v.split(/[,，\s]+/)) {
                                const t = part.trim().replace(/^['"]|['"]$/g, "").replace(/^#+/, "");
                                if (t) {
                                    fmTags.push(t);
                                }
                            }
                        }
                        else {
                            inTags = true;
                        }
                    }
                    else if (k === "aliases") {
                        if (v.startsWith("[") && v.endsWith("]")) {
                            for (const part of v.slice(1, -1).split(",")) {
                                const a = part.trim().replace(/^['"]|['"]$/g, "");
                                if (a) {
                                    aliases.push(a);
                                }
                            }
                        }
                        else if (v) {
                            aliases.push(v.replace(/^['"]|['"]$/g, ""));
                        }
                        else {
                            inAliases = true;
                        }
                    }
                }
            }
        }
    }

    const inlineTags = [];
    const wikilinks = [];
    const excerptParts = [];
    let inFence = false;
    for (let i = bodyStart; i < lines.length; i++) {
        const line = lines[i];
        const s = line.trim();
        if (s.startsWith("```") || s.startsWith("~~~")) {
            inFence = !inFence;
            continue;
        }
        if (inFence) {
            continue;
        }
        if (!title && s.startsWith("# ")) {
            title = s.slice(2).trim();
        }
        const tagRe = /(?:^|[\s(])#([A-Za-z0-9_\-\u4e00-\u9fff/]+)/g;
        let tm;
        while ((tm = tagRe.exec(line)) !== null) {
            const tag = tm[1].replace(/\/+$/, "");
            if (tag && !/^[0-9a-fA-F]{3,8}$/.test(tag)) {
                inlineTags.push(tag);
            }
        }
        const linkRe = /\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]/g;
        let lm;
        while ((lm = linkRe.exec(line)) !== null) {
            const target = lm[1].trim();
            if (target && !wikilinks.includes(target)) {
                wikilinks.push(target);
            }
        }
        if (excerptParts.length < 3 && s && !s.startsWith("#") && s !== "---") {
            const clean = s.replace(/[`*_>\[\]]/g, "").trim();
            if (clean) {
                excerptParts.push(clean);
            }
        }
    }

    if (!title && relPath) {
        const base = path.posix.basename(relPath.replace(/\\/g, "/"));
        title = base.replace(KB_MD_EXT_RE, "");
    }

    const tags = [];
    const seenTags = new Set();
    for (const t of [...fmTags, ...inlineTags]) {
        const low = t.toLowerCase();
        if (!seenTags.has(low)) {
            seenTags.add(low);
            tags.push(t);
        }
    }

    return {
        title,
        tags,
        aliases,
        links: wikilinks,
        excerpt: excerptParts.join(" ").slice(0, 160),
    };
}

function resolveSafeKbRoot(rootPath) {
    if (typeof rootPath !== "string" || !rootPath.trim()) {
        return null;
    }
    const expanded = rootPath.startsWith("~")
        ? path.join(os.homedir(), rootPath.slice(1))
        : rootPath;
    const resolved = path.resolve(expanded);
    if (resolved === path.resolve(getDefaultKbVaultPath())) {
        ensureDefaultKbVault();
    }
    try {
        if (!fs.existsSync(resolved) || !fs.statSync(resolved).isDirectory()) {
            return null;
        }
        return resolved;
    }
    catch {
        return null;
    }
}

function resolveSafeKbPath(rootPath, relPath, options = {}) {
    const requireMarkdown = options.requireMarkdown !== false;
    if (typeof rootPath !== "string" || !rootPath.trim() || typeof relPath !== "string") {
        return null;
    }
    if (relPath.includes("\0")) {
        return null;
    }
    const cleaned = relPath.trim().replace(/\\/g, "/").replace(/^\/+/, "");
    if (!cleaned || /^[A-Za-z]:/.test(cleaned)) {
        return null;
    }
    const normalizedRel = requireMarkdown && !KB_MD_EXT_RE.test(cleaned) ? `${cleaned}.md` : cleaned;
    const base = path.resolve(rootPath);
    const target = path.resolve(base, normalizedRel);
    if (target !== base && !target.startsWith(base + path.sep)) {
        return null;
    }
    const posixRel = path.relative(base, target).replace(/\\/g, "/");
    return { fullPath: target, relPath: posixRel };
}

function scanKbVault(rootPath) {
    const base = resolveSafeKbRoot(rootPath);
    if (!base) {
        return { ok: false, exists: false, rootPath: rootPath || "", notes: [], folders: [], truncated: false };
    }
    const notes = [];
    const folders = new Set();
    let truncated = false;

    function walk(dir) {
        if (notes.length >= KB_MAX_FILES) {
            truncated = true;
            return;
        }
        let entries;
        try {
            entries = fs.readdirSync(dir, { withFileTypes: true });
        }
        catch {
            return;
        }
        entries.sort((a, b) => a.name.localeCompare(b.name));
        for (const entry of entries) {
            if (notes.length >= KB_MAX_FILES) {
                truncated = true;
                return;
            }
            if (entry.name.startsWith(".") || KB_SKIP_DIRS.has(entry.name)) {
                continue;
            }
            const full = path.join(dir, entry.name);
            const rel = path.relative(base, full).replace(/\\/g, "/");
            if (entry.isDirectory()) {
                folders.add(rel);
                walk(full);
            }
            else if (entry.isFile() && KB_MD_EXT_RE.test(entry.name)) {
                try {
                    const st = fs.statSync(full);
                    if (st.size > KB_MAX_FILE_BYTES) {
                        continue;
                    }
                    const text = fs.readFileSync(full, "utf8");
                    const meta = parseKbMarkdownMeta(text, rel);
                    notes.push({
                        relPath: rel,
                        name: entry.name,
                        title: meta.title,
                        tags: meta.tags,
                        aliases: meta.aliases,
                        links: meta.links,
                        excerpt: meta.excerpt,
                        mtimeMs: st.mtimeMs,
                        size: st.size,
                    });
                }
                catch {
                    // Ignore unreadable file
                }
            }
        }
    }

    walk(base);
    notes.sort((a, b) => b.mtimeMs - a.mtimeMs);
    return {
        ok: true,
        exists: true,
        rootPath: base,
        notes,
        folders: Array.from(folders).sort(),
        truncated,
    };
}

function searchKbVault(rootPath, options = {}) {
    const query = typeof options.query === "string" ? options.query.trim() : "";
    const tagFilter = typeof options.tag === "string" ? options.tag.trim().replace(/^#+/, "").toLowerCase() : "";
    const limit = Math.max(1, Math.min(Number(options.limit) || 50, 200));
    const scan = scanKbVault(rootPath);
    if (!scan.ok) {
        return { ok: false, matches: [] };
    }
    const qLow = query.toLowerCase();
    const terms = qLow ? qLow.split(/\s+/).filter(Boolean) : [];
    const matches = [];

    for (const note of scan.notes) {
        if (tagFilter && !note.tags.some((t) => t.toLowerCase() === tagFilter)) {
            continue;
        }
        if (terms.length === 0) {
            matches.push({ ...note, score: 10, snippets: note.excerpt ? [{ line: 1, text: note.excerpt }] : [] });
            continue;
        }
        const resolved = resolveSafeKbPath(scan.rootPath, note.relPath);
        if (!resolved) {
            continue;
        }
        let text = "";
        try {
            text = fs.readFileSync(resolved.fullPath, "utf8");
        }
        catch {
            continue;
        }
        const titleLow = (note.title || "").toLowerCase();
        const pathLow = note.relPath.toLowerCase();
        const tagsLow = note.tags.join(" ").toLowerCase();
        const textLow = text.toLowerCase();

        const allHit = terms.every(
            (term) => titleLow.includes(term) || pathLow.includes(term) || tagsLow.includes(term) || textLow.includes(term)
        );
        if (!allHit) {
            continue;
        }
        let score = 0;
        const stem = path.posix.basename(pathLow).replace(KB_MD_EXT_RE, "");
        if (titleLow === qLow || stem === qLow) score += 120;
        else if (titleLow.includes(qLow)) score += 80;
        if (tagsLow.includes(qLow)) score += 50;
        if (pathLow.includes(qLow)) score += 40;
        for (const term of terms) {
            if (textLow.includes(term)) score += 12;
        }

        const snippets = [];
        const lines = text.split(/\r?\n/);
        for (let i = 0; i < lines.length; i++) {
            const lLow = lines[i].toLowerCase();
            if (terms.some((term) => lLow.includes(term))) {
                snippets.push({ line: i + 1, text: lines[i].trim().slice(0, 200) });
                if (snippets.length >= 3) {
                    break;
                }
            }
        }
        matches.push({ ...note, score, snippets });
    }

    matches.sort((a, b) => b.score - a.score || b.mtimeMs - a.mtimeMs);
    return { ok: true, matches: matches.slice(0, limit) };
}

function readKbConfigSafe(pluginPath) {
    const defaultVault = ensureDefaultKbVault();
    const fallback = {
        version: 1,
        activeVaultId: "personal",
        vaults: [{ id: "personal", label: "个人知识库", path: defaultVault, kind: "personal" }],
        pinnedNotes: [],
        prefs: {},
    };
    if (!pluginPath) {
        return fallback;
    }
    const cfgPath = path.join(pluginPath, "runtime", "kb-config.json");
    const parsed = readJsonSafe(cfgPath, false);
    if (!parsed || typeof parsed !== "object") {
        return fallback;
    }
    const vaults = Array.isArray(parsed.vaults) && parsed.vaults.length > 0 ? parsed.vaults : fallback.vaults;
    return {
        ...fallback,
        ...parsed,
        vaults,
    };
}

function writeKbConfigAtomic(pluginPath, data) {
    if (!pluginPath || !data || typeof data !== "object") {
        return false;
    }
    try {
        const runtimeDir = path.join(pluginPath, "runtime");
        fs.mkdirSync(runtimeDir, { recursive: true });
        const dst = path.join(runtimeDir, "kb-config.json");
        const tmp = `${dst}.${process.pid}.tmp`;
        fs.writeFileSync(tmp, JSON.stringify(data, null, 2), "utf8");
        if (fs.existsSync(dst)) {
            fs.unlinkSync(dst);
        }
        fs.renameSync(tmp, dst);
        return true;
    }
    catch {
        return false;
    }
}

const kbBridgeRuntime = {
    server: null,
    port: 0,
    token: crypto.randomBytes(24).toString("hex"),
    pluginPath: null,
    startPromise: null,
};

function readHttpJsonBody(req) {
    return new Promise((resolve, reject) => {
        const chunks = [];
        let total = 0;
        req.on("data", (chunk) => {
            total += chunk.length;
            if (total > KB_MAX_BODY_BYTES) {
                reject(new Error("Payload too large"));
                req.destroy();
                return;
            }
            chunks.push(chunk);
        });
        req.on("end", () => {
            if (chunks.length === 0) {
                resolve({});
                return;
            }
            try {
                resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
            }
            catch {
                reject(new Error("Invalid JSON body"));
            }
        });
        req.on("error", reject);
    });
}

function sendKbJson(res, status, payload, origin) {
    const headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, x-agy-kb-token",
    };
    if (origin && isHubUiUrl(origin)) {
        headers["Access-Control-Allow-Origin"] = origin;
    }
    else {
        headers["Access-Control-Allow-Origin"] = "http://127.0.0.1";
    }
    res.writeHead(status, headers);
    res.end(JSON.stringify(payload));
}

async function handleKbBridgeRequest(req, res) {
    const origin = typeof req.headers.origin === "string" ? req.headers.origin : "";
    if (req.method === "OPTIONS") {
        sendKbJson(res, 204, {}, origin);
        return;
    }
    const token = req.headers["x-agy-kb-token"];
    if (token !== kbBridgeRuntime.token) {
        sendKbJson(res, 403, { ok: false, error: "Unauthorized" }, origin);
        return;
    }

    const reqUrl = (req.url || "/").split("?")[0];
    try {
        if (req.method === "GET" && reqUrl === "/health") {
            sendKbJson(
                res,
                200,
                {
                    ok: true,
                    defaultVault: ensureDefaultKbVault(),
                    pluginPath: kbBridgeRuntime.pluginPath || "",
                },
                origin
            );
            return;
        }
        if (req.method !== "POST") {
            sendKbJson(res, 405, { ok: false, error: "Method not allowed" }, origin);
            return;
        }
        const body = await readHttpJsonBody(req);

        if (reqUrl === "/config/get") {
            sendKbJson(res, 200, { ok: true, config: readKbConfigSafe(kbBridgeRuntime.pluginPath) }, origin);
            return;
        }
        if (reqUrl === "/config/save") {
            const current = readKbConfigSafe(kbBridgeRuntime.pluginPath);
            const next = { ...current, ...(body && typeof body.config === "object" ? body.config : body) };
            const ok = writeKbConfigAtomic(kbBridgeRuntime.pluginPath, next);
            sendKbJson(res, ok ? 200 : 500, { ok, config: next }, origin);
            return;
        }
        if (reqUrl === "/kb/exists") {
            const resolved = resolveSafeKbRoot(body.rootPath);
            sendKbJson(res, 200, { ok: true, exists: Boolean(resolved), rootPath: resolved || "" }, origin);
            return;
        }
        if (reqUrl === "/kb/tree") {
            const resData = scanKbVault(body.rootPath || ensureDefaultKbVault());
            sendKbJson(res, 200, resData, origin);
            return;
        }
        if (reqUrl === "/kb/search") {
            const resData = searchKbVault(body.rootPath || ensureDefaultKbVault(), body);
            sendKbJson(res, 200, resData, origin);
            return;
        }
        if (reqUrl === "/kb/read") {
            const base = resolveSafeKbRoot(body.rootPath || ensureDefaultKbVault());
            const resolved = base ? resolveSafeKbPath(base, body.relPath) : null;
            if (!resolved || !fs.existsSync(resolved.fullPath)) {
                sendKbJson(res, 404, { ok: false, error: "Note not found" }, origin);
                return;
            }
            const content = fs.readFileSync(resolved.fullPath, "utf8");
            const st = fs.statSync(resolved.fullPath);
            const meta = parseKbMarkdownMeta(content, resolved.relPath);
            sendKbJson(
                res,
                200,
                {
                    ok: true,
                    rootPath: base,
                    relPath: resolved.relPath,
                    fullPath: resolved.fullPath,
                    content,
                    meta,
                    mtimeMs: st.mtimeMs,
                    size: st.size,
                },
                origin
            );
            return;
        }
        if (reqUrl === "/kb/write") {
            const base = resolveSafeKbRoot(body.rootPath || ensureDefaultKbVault());
            const resolved = base ? resolveSafeKbPath(base, body.relPath) : null;
            if (!resolved) {
                sendKbJson(res, 400, { ok: false, error: "Invalid note path" }, origin);
                return;
            }
            if (body.overwrite === false && fs.existsSync(resolved.fullPath)) {
                sendKbJson(res, 409, { ok: false, error: "Note already exists" }, origin);
                return;
            }
            const text = typeof body.content === "string" ? body.content : "";
            fs.mkdirSync(path.dirname(resolved.fullPath), { recursive: true });
            const tmp = `${resolved.fullPath}.${process.pid}.tmp`;
            try {
                fs.writeFileSync(tmp, text, "utf8");
                try {
                    fs.renameSync(tmp, resolved.fullPath);
                }
                catch {
                    if (fs.existsSync(resolved.fullPath)) {
                        fs.unlinkSync(resolved.fullPath);
                    }
                    fs.renameSync(tmp, resolved.fullPath);
                }
            }
            finally {
                if (fs.existsSync(tmp)) {
                    try {
                        fs.unlinkSync(tmp);
                    }
                    catch {
                        // Ignore tmp cleanup error
                    }
                }
            }
            const st = fs.statSync(resolved.fullPath);
            const meta = parseKbMarkdownMeta(text, resolved.relPath);
            sendKbJson(
                res,
                200,
                {
                    ok: true,
                    rootPath: base,
                    relPath: resolved.relPath,
                    fullPath: resolved.fullPath,
                    meta,
                    mtimeMs: st.mtimeMs,
                    size: st.size,
                },
                origin
            );
            return;
        }
        if (reqUrl === "/kb/rename") {
            const base = resolveSafeKbRoot(body.rootPath || ensureDefaultKbVault());
            const oldRes = base ? resolveSafeKbPath(base, body.oldRelPath) : null;
            const newRes = base ? resolveSafeKbPath(base, body.newRelPath) : null;
            if (!oldRes || !newRes || !fs.existsSync(oldRes.fullPath)) {
                sendKbJson(res, 400, { ok: false, error: "Invalid source or target path" }, origin);
                return;
            }
            if (oldRes.fullPath !== newRes.fullPath && fs.existsSync(newRes.fullPath)) {
                sendKbJson(res, 409, { ok: false, error: "Target note already exists" }, origin);
                return;
            }
            fs.mkdirSync(path.dirname(newRes.fullPath), { recursive: true });
            fs.renameSync(oldRes.fullPath, newRes.fullPath);
            sendKbJson(res, 200, { ok: true, relPath: newRes.relPath, fullPath: newRes.fullPath }, origin);
            return;
        }
        if (reqUrl === "/kb/delete") {
            const base = resolveSafeKbRoot(body.rootPath || ensureDefaultKbVault());
            const resolved = base ? resolveSafeKbPath(base, body.relPath) : null;
            if (!resolved || !fs.existsSync(resolved.fullPath)) {
                sendKbJson(res, 404, { ok: false, error: "Note not found" }, origin);
                return;
            }
            const trashDir = path.join(base, ".trash");
            fs.mkdirSync(trashDir, { recursive: true });
            const trashName = `${Date.now()}-${path.basename(resolved.fullPath)}`;
            const trashDst = path.join(trashDir, trashName);
            fs.renameSync(resolved.fullPath, trashDst);
            sendKbJson(res, 200, { ok: true, trashedTo: trashDst }, origin);
            return;
        }
        if (reqUrl === "/kb/mkdir") {
            const base = resolveSafeKbRoot(body.rootPath || ensureDefaultKbVault());
            const resolved = base ? resolveSafeKbPath(base, body.relPath, { requireMarkdown: false }) : null;
            if (!resolved) {
                sendKbJson(res, 400, { ok: false, error: "Invalid folder path" }, origin);
                return;
            }
            fs.mkdirSync(resolved.fullPath, { recursive: true });
            sendKbJson(res, 200, { ok: true, relPath: resolved.relPath }, origin);
            return;
        }
        if (reqUrl === "/kb/pick-folder") {
            if (!electron_1 || !electron_1.dialog || typeof electron_1.dialog.showOpenDialog !== "function") {
                sendKbJson(res, 501, { ok: false, error: "Dialog unavailable" }, origin);
                return;
            }
            const win = electron_1.BrowserWindow?.getFocusedWindow?.() || undefined;
            const picked = await electron_1.dialog.showOpenDialog(win, {
                properties: ["openDirectory", "createDirectory"],
                defaultPath: body.defaultPath || os.homedir(),
            });
            const selected = !picked.canceled && Array.isArray(picked.filePaths) ? picked.filePaths[0] || "" : "";
            sendKbJson(res, 200, { ok: true, canceled: Boolean(picked.canceled), path: selected }, origin);
            return;
        }
        if (reqUrl === "/kb/open-external") {
            const base = resolveSafeKbRoot(body.rootPath || ensureDefaultKbVault());
            if (!base || !electron_1 || !electron_1.shell) {
                sendKbJson(res, 400, { ok: false, error: "Cannot open path" }, origin);
                return;
            }
            if (body.relPath) {
                const resolved = resolveSafeKbPath(base, body.relPath);
                if (resolved && fs.existsSync(resolved.fullPath)) {
                    electron_1.shell.showItemInFolder(resolved.fullPath);
                    sendKbJson(res, 200, { ok: true }, origin);
                    return;
                }
            }
            await electron_1.shell.openPath(base);
            sendKbJson(res, 200, { ok: true }, origin);
            return;
        }

        sendKbJson(res, 404, { ok: false, error: "Unknown route" }, origin);
    }
    catch (err) {
        sendKbJson(res, 500, { ok: false, error: String(err && err.message ? err.message : err) }, origin);
    }
}

function ensureKbBridgeStarted(pluginPath) {
    if (pluginPath) {
        kbBridgeRuntime.pluginPath = pluginPath;
    }
    if (kbBridgeRuntime.port > 0 && kbBridgeRuntime.server) {
        return Promise.resolve({
            url: `http://127.0.0.1:${kbBridgeRuntime.port}`,
            token: kbBridgeRuntime.token,
            defaultVault: ensureDefaultKbVault(),
            homeDir: os.homedir(),
            pluginPath: kbBridgeRuntime.pluginPath || "",
        });
    }
    if (kbBridgeRuntime.startPromise) {
        return kbBridgeRuntime.startPromise;
    }
    kbBridgeRuntime.startPromise = new Promise((resolve) => {
        try {
            const server = http.createServer((req, res) => {
                void handleKbBridgeRequest(req, res);
            });
            server.on("error", () => {
                resolve(null);
            });
            server.listen(0, "127.0.0.1", () => {
                const addr = server.address();
                kbBridgeRuntime.server = server;
                kbBridgeRuntime.port = addr && typeof addr === "object" ? addr.port : 0;
                if (typeof server.unref === "function") {
                    server.unref();
                }
                resolve({
                    url: `http://127.0.0.1:${kbBridgeRuntime.port}`,
                    token: kbBridgeRuntime.token,
                    defaultVault: ensureDefaultKbVault(),
                    homeDir: os.homedir(),
                    pluginPath: kbBridgeRuntime.pluginPath || "",
                });
            });
        }
        catch {
            resolve(null);
        }
    });
    return kbBridgeRuntime.startPromise;
}

function attachWebContents(contents) {
    if (!contents || typeof contents.on !== "function") {
        return;
    }
    if (typeof contents.getType === "function" && contents.getType() !== "window") {
        return;
    }
    let usagePluginPath = null;
    let flushInFlight = false;
    const flushPendingHistory = () => {
        if (!usagePluginPath || flushInFlight || contents.isDestroyed?.()) {
            return;
        }
        flushInFlight = true;
        void Promise.resolve(
            contents.executeJavaScript(
                "(function(){return typeof window.__AGY_DRAIN_USAGE_HISTORY__==='function'?window.__AGY_DRAIN_USAGE_HISTORY__():null;})()",
                true
            )
        )
            .then((batch) => {
                if (batch && typeof batch === "object") {
                    const existing = readUsageHistorySafe(usagePluginPath);
                    const merged = mergeUsageHistory(existing, batch);
                    if (merged) {
                        writeUsageHistoryAtomic(usagePluginPath, merged);
                    }
                }
            })
            .catch(() => {})
            .finally(() => {
                flushInFlight = false;
            });
    };
    const flushTimer = setInterval(flushPendingHistory, 5000);
    if (flushTimer && typeof flushTimer.unref === "function") {
        flushTimer.unref();
    }
    contents.on("destroyed", () => {
        clearInterval(flushTimer);
    });
    contents.on("will-navigate", () => {
        flushPendingHistory();
    });
    contents.on("did-commit-load", (_event, url, _isInPlace, isMainFrame) => {
        if (isMainFrame === false || !isHubUiUrl(url)) {
            return;
        }
        const plugins = getActiveUiPlugins();
        if (plugins.some((p) => Array.isArray(p.uiScripts) && p.uiScripts.length > 0)) {
            void Promise.resolve(contents.executeJavaScript(EARLY_RPC_TAP_CODE, true)).catch(() => {});
        }
    });
    contents.on("dom-ready", async () => {
        const url = typeof contents.getURL === "function" ? contents.getURL() : "";
        if (!isHubUiUrl(url)) {
            return;
        }
        const plugins = getActiveUiPlugins();
        if (plugins.length === 0) {
            return;
        }
        let appVersion = "unknown";
        try {
            if (electron_1 && electron_1.app && typeof electron_1.app.getVersion === "function") {
                appVersion = electron_1.app.getVersion();
            }
        }
        catch {
            // Ignore version lookup error
        }
        for (const p of plugins) {
            if (p.dict && p.engineCode) {
                const i18nCode = `(function(){\ntry{\nwindow.__AGY_PLUGIN_I18N_DICT__ = ${JSON.stringify(p.dict)};\n${p.engineCode}\n}catch(err){console.warn('[AgyPluginLoader]', err);}\n})();`;
                void Promise.resolve(contents.executeJavaScript(i18nCode, true))
                    .then(() => writePluginLog(p.pluginPath, `Injected ${p.dirName} (app v${appVersion}) into ${url}`))
                    .catch((err) => writePluginLog(p.pluginPath, `Inject error for ${p.dirName} (app v${appVersion}): ${err}`));
            }
            if (Array.isArray(p.uiScripts) && p.uiScripts.length > 0) {
                if (p.dirName === "antigravity-usage-dashboard") {
                    usagePluginPath = p.pluginPath;
                }
                const initHistory = p.dirName === "antigravity-usage-dashboard"
                    ? readUsageHistorySafe(p.pluginPath)
                    : null;
                const kbBridgeInfo = p.dirName === "antigravity-kb"
                    ? await ensureKbBridgeStarted(p.pluginPath)
                    : null;
                for (const script of p.uiScripts) {
                    const meta = JSON.stringify({ id: p.dirName, rel: script.rel, appVersion });
                    const historyInitStmt = initHistory
                        ? `window.__AGY_USAGE_HISTORY_INIT__ = ${JSON.stringify(initHistory)};\n`
                        : "";
                    const kbBridgeStmt = kbBridgeInfo
                        ? `window.__AGY_KB_BRIDGE__ = ${JSON.stringify(kbBridgeInfo)};\n`
                        : "";
                    const widgetCode = `(function(){\ntry{\nwindow.__AGY_PLUGIN_META__ = ${meta};\n${historyInitStmt}${kbBridgeStmt}${script.code}\n}catch(err){console.warn('[AgyPluginLoader]', err);}\n})();`;
                    void Promise.resolve(contents.executeJavaScript(widgetCode, true))
                        .then(() => writePluginLog(p.pluginPath, `Injected UI script ${p.dirName}:${script.rel} (app v${appVersion}) into ${url}`))
                        .catch((err) => writePluginLog(p.pluginPath, `Inject UI script error for ${p.dirName}:${script.rel} (app v${appVersion}): ${err}`));
                }
            }
        }
    });
}
