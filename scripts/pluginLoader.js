"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.attachWebContents = attachWebContents;
exports.translateTrayItem = translateTrayItem;
exports.translateTrayItems = translateTrayItems;
exports.formatTrayAgentCount = formatTrayAgentCount;

const electron_1 = require("electron");
const fs = require("fs");
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
        const userSetting = configPlugins[dirName];
        const enabled = typeof userSetting?.enabled === "boolean"
            ? userSetting.enabled
            : manifest.disabled !== true;
        if (!enabled) {
            continue;
        }
        const enginePath = path.join(pluginPath, "runtime", "i18n-engine.js");
        const dictPath = path.join(pluginPath, "runtime", "zh-CN.json");
        if (!fs.existsSync(enginePath) || !fs.existsSync(dictPath)) {
            continue;
        }
        const dict = readJsonSafe(dictPath, false);
        if (!dict) {
            continue;
        }
        try {
            const engineCode = fs.readFileSync(enginePath, "utf8");
            result.push({ dirName, pluginPath, dict, engineCode });
        }
        catch {
            // Skip unreadable plugin runtime
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

function attachWebContents(contents) {
    if (!contents || typeof contents.on !== "function") {
        return;
    }
    if (typeof contents.getType === "function" && contents.getType() !== "window") {
        return;
    }
    contents.on("dom-ready", () => {
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
            const code = `(function(){\ntry{\nwindow.__AGY_PLUGIN_I18N_DICT__ = ${JSON.stringify(p.dict)};\n${p.engineCode}\n}catch(err){console.warn('[AgyPluginLoader]', err);}\n})();`;
            void Promise.resolve(contents.executeJavaScript(code, true))
                .then(() => writePluginLog(p.pluginPath, `Injected ${p.dirName} (app v${appVersion}) into ${url}`))
                .catch((err) => writePluginLog(p.pluginPath, `Inject error for ${p.dirName} (app v${appVersion}): ${err}`));
        }
    });
}
