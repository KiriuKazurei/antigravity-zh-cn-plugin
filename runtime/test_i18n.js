const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

console.log('Testing plugin zh-CN.json and i18n-engine.js...');
const dictPath = path.join(__dirname, 'zh-CN.json');
const enginePath = path.join(__dirname, 'i18n-engine.js');
assert(fs.existsSync(dictPath), 'zh-CN.json must exist');
assert(fs.existsSync(enginePath), 'i18n-engine.js must exist');

const dict = JSON.parse(fs.readFileSync(dictPath, 'utf8'));
assert(dict.exact && typeof dict.exact === 'object', 'exact must be an object');
assert(Array.isArray(dict.patterns), 'patterns must be an array');
assert(dict.tray && typeof dict.tray === 'object', 'tray must be an object');
assert(Array.isArray(dict.preserved_proper_nouns), 'preserved_proper_nouns must be an array');

// Validate engine syntax and verify zero Node require() calls
const engineCode = fs.readFileSync(enginePath, 'utf8');
assert(!/\brequire\s*\(/.test(engineCode), 'i18n-engine.js must not call require()');
new vm.Script(engineCode, { filename: 'i18n-engine.js' });

// Validate preserved proper nouns
for (const noun of dict.preserved_proper_nouns) {
  if (dict.exact[noun]) {
    assert(
      dict.exact[noun].toLowerCase().includes(noun.toLowerCase()),
      `Proper noun ${noun} must remain preserved in "${dict.exact[noun]}"`
    );
  }
}

// Validate patterns
for (const p of dict.patterns) {
  const re = new RegExp(p.regex);
  if (p.regex.includes('Thought for')) {
    assert.strictEqual('Thought for 3s'.replace(re, p.replacement), '思考了 3 秒');
    assert.strictEqual('Thought for 3.2s'.replace(re, p.replacement), '思考了 3.2 秒');
  }
  if (p.regex.includes('files? changed')) {
    assert.strictEqual('5 files changed'.replace(re, p.replacement), '共更改了 5 个文件');
  }
  if (p.regex.includes('Plugin:')) {
    assert.strictEqual('Plugin: antigravity-zh-cn'.replace(re, p.replacement), '插件: antigravity-zh-cn');
  }
  if (p.regex.includes('MCP servers? failed')) {
    assert.strictEqual('2 MCP servers failed to start'.replace(re, p.replacement), '2 个 MCP 服务器启动失败');
  }
}

console.log(`Passed! exact=${Object.keys(dict.exact).length}, patterns=${dict.patterns.length}, tray=${Object.keys(dict.tray).length}`);
