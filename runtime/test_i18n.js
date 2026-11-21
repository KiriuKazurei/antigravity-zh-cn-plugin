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

// Validate permission preset & runtime authorization translations
const requiredExact = [
  'Every command requires approval.',
  'Ask before sensitive operations.',
  'Automatically decide if commands run.',
  'Run all commands without restrictions.',
  'Runtime permission',
  'Confirm the command is safe to run outside of the sandbox with full network and disk access.',
  'Yes, allow this time',
  'Yes, and always allow in this conversation',
  'Yes, and always allow',
  'Allow once',
  'Allow for conversation',
  'Always allow',
  'Deny with note',
  'Tell the agent what to do instead...',
  '(tell the agent what to do instead)',
  'Other (write your answer)',
  'Skip',
  'Submit',
  'Continue',
  'No',
];
for (const key of requiredExact) {
  assert(typeof dict.exact[key] === 'string' && dict.exact[key].length > 0, `Missing exact translation for: ${key}`);
}

// Helper mirroring i18n-engine.js translateString()
const compiledPatterns = dict.patterns.map((p) => ({
  regex: new RegExp(p.regex),
  replacement: p.replacement,
}));
function translate(str) {
  const trimmed = str.trim();
  if (Object.prototype.hasOwnProperty.call(dict.exact, trimmed)) {
    return str.replace(trimmed, dict.exact[trimmed]);
  }
  for (const p of compiledPatterns) {
    if (p.regex.test(trimmed)) {
      return str.replace(trimmed, trimmed.replace(p.regex, p.replacement));
    }
  }
  return str;
}

assert.strictEqual(translate('Allow running npm test outside the sandbox?'), '允许running npm test outside the sandbox？');
assert.strictEqual(translate('Save rule to always allow git push?'), '保存规则以始终允许git push？');
assert.strictEqual(translate("Yes, and always allow 'rm' in this conversation"), '是，并在当前对话中始终允许“rm”');
assert.strictEqual(translate("Yes, and always allow 'rm'"), '是，并始终允许“rm”');
assert.strictEqual(translate("Yes, save rule for 'curl' globally"), '是，全局保存针对“curl”的规则');
assert.strictEqual(translate('Yes, allow downloads from example.com for this run'), '是，在本次运行中允许从 example.com 下载');
assert.strictEqual(translate('Conflicts with your configured Ask permission: command(rm)'), '与您配置的询问权限规则冲突: command(rm)');
assert.strictEqual(translate('Requires manual confirmation: hook-name'), '需要人工确认: hook-name');
assert.strictEqual(translate('Submit (↵)'), '提交 (↵)');
assert.strictEqual(translate('Skip (Esc), Skip All (Ctrl+Esc)'), '跳过 (Esc)，全部跳过 (Ctrl+Esc)');
assert.strictEqual(
  translate('Default and Auto need the terminal sandbox, which isn\'t available on this host.'),
  'Default and Auto 需要终端沙箱，但当前主机不支持该功能。'
);

// Verify i18n-engine.js translates TEXTAREA attributes (e.g. placeholder) while ignoring TEXTAREA text content
const fakeDoc = {
  readyState: 'complete',
  body: null,
};
const textareaTextNode = { nodeType: 3, nodeValue: 'Yes, allow this time', parentElement: null };
const attrs = {
  placeholder: '(tell the agent what to do instead)',
  'aria-label': 'Edit permission target',
};
const textareaEl = {
  nodeType: 1,
  tagName: 'TEXTAREA',
  isContentEditable: false,
  classList: { contains: () => false },
  closest: () => null,
  getAttribute: (k) => attrs[k] ?? null,
  setAttribute: (k, v) => { attrs[k] = v; },
  firstChild: textareaTextNode,
  nextSibling: null,
  isConnected: true,
};
textareaTextNode.parentElement = textareaEl;
const buttonTextNode = { nodeType: 3, nodeValue: 'Submit', parentElement: null };
const buttonEl = {
  nodeType: 1,
  tagName: 'BUTTON',
  isContentEditable: false,
  classList: { contains: () => false },
  closest: () => null,
  getAttribute: () => null,
  setAttribute: () => {},
  firstChild: buttonTextNode,
  nextSibling: textareaEl,
  isConnected: true,
};
buttonTextNode.parentElement = buttonEl;
fakeDoc.body = {
  nodeType: 1,
  tagName: 'BODY',
  isContentEditable: false,
  classList: { contains: () => false },
  closest: () => null,
  getAttribute: () => null,
  setAttribute: () => {},
  firstChild: buttonEl,
  nextSibling: null,
  isConnected: true,
};
const sandboxWindow = {
  __AGY_PLUGIN_I18N_DICT__: dict,
};
const ctx = vm.createContext({
  window: sandboxWindow,
  document: fakeDoc,
  Node: { ELEMENT_NODE: 1, TEXT_NODE: 3 },
  MutationObserver: class {
    observe() {}
    takeRecords() { return []; }
  },
  requestAnimationFrame: (fn) => fn(),
  setTimeout,
});
vm.runInContext(engineCode, ctx);
assert.strictEqual(buttonTextNode.nodeValue, '提交');
assert.strictEqual(attrs.placeholder, '（告诉 Agent 改为执行什么操作）');
assert.strictEqual(attrs['aria-label'], '编辑权限目标');
assert.strictEqual(textareaTextNode.nodeValue, 'Yes, allow this time', 'TEXTAREA text content must not be translated');

console.log(`Passed! exact=${Object.keys(dict.exact).length}, patterns=${dict.patterns.length}, tray=${Object.keys(dict.tray).length}`);
