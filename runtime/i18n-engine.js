/**
 * Antigravity GUI 简体中文汉化引擎 (插件运行时)
 * 纯浏览器 DOM 环境执行，无任何 Node.js 内置模块依赖。
 */
(function () {
  'use strict';

  const dictionary = window.__AGY_PLUGIN_I18N_DICT__;
  try {
    delete window.__AGY_PLUGIN_I18N_DICT__;
  } catch (_) {}

  if (!dictionary || typeof dictionary !== 'object') {
    return;
  }

  if (window.__AGY_I18N_ACTIVE__) {
    return;
  }
  window.__AGY_I18N_ACTIVE__ = true;

  const exactMap = new Map();
  const compiledPatterns = [];

  if (dictionary.exact && typeof dictionary.exact === 'object') {
    for (const [k, v] of Object.entries(dictionary.exact)) {
      if (typeof k === 'string' && typeof v === 'string') {
        exactMap.set(k, v);
      }
    }
  }

  if (Array.isArray(dictionary.patterns)) {
    for (const p of dictionary.patterns) {
      if (p && typeof p.regex === 'string' && typeof p.replacement === 'string') {
        try {
          compiledPatterns.push({
            regex: new RegExp(p.regex),
            replacement: p.replacement,
          });
        } catch (_) {}
      }
    }
  }

  const stats = {
    plugin: 'antigravity-zh-cn',
    exactCount: exactMap.size,
    patternCount: compiledPatterns.length,
    translatedCount: 0,
  };
  window.__AGY_I18N_STATS__ = stats;

  const IGNORE_TAGS = new Set([
    'SCRIPT',
    'STYLE',
    'CODE',
    'PRE',
    'NOSCRIPT',
    'TEXTAREA',
  ]);

  const IGNORE_CLASSES = [
    'monaco-editor',
    'xterm',
    'terminal',
    'code-block',
    'hljs',
  ];

  function shouldIgnoreElement(el) {
    if (!el || el.nodeType !== Node.ELEMENT_NODE) return false;
    if (IGNORE_TAGS.has(el.tagName)) return true;
    if (el.isContentEditable) return true;
    if (el.classList) {
      for (const cls of IGNORE_CLASSES) {
        if (el.classList.contains(cls)) return true;
      }
    }
    if (el.closest && el.closest('pre, code, .monaco-editor, .xterm, textarea, [contenteditable="true"]')) {
      return true;
    }
    return false;
  }

  function translateString(str) {
    if (!str || typeof str !== 'string') return null;
    const trimmed = str.trim();
    if (!trimmed) return null;

    if (exactMap.has(trimmed)) {
      const translated = exactMap.get(trimmed);
      return str.replace(trimmed, translated);
    }

    for (const p of compiledPatterns) {
      if (p.regex.test(trimmed)) {
        const translated = trimmed.replace(p.regex, p.replacement);
        return str.replace(trimmed, translated);
      }
    }

    return null;
  }

  let isModifyingDOM = false;

  function translateTextNode(node) {
    if (!node || node.nodeType !== Node.TEXT_NODE) return;
    const parent = node.parentElement;
    if (!parent || shouldIgnoreElement(parent)) return;

    const original = node.nodeValue;
    const translated = translateString(original);
    if (translated && translated !== original) {
      node.nodeValue = translated;
      stats.translatedCount++;
    }
  }

  const TRANSLATED_ATTRS = ['placeholder', 'title', 'aria-label'];

  function translateElementAttributes(el) {
    if (!el || el.nodeType !== Node.ELEMENT_NODE) return;
    if (shouldIgnoreElement(el)) return;

    for (const attr of TRANSLATED_ATTRS) {
      const val = el.getAttribute(attr);
      if (val) {
        const translated = translateString(val);
        if (translated && translated !== val) {
          el.setAttribute(attr, translated);
          stats.translatedCount++;
        }
      }
    }
  }

  function walkAndTranslate(root) {
    if (!root) return;
    if (root.nodeType === Node.ELEMENT_NODE && shouldIgnoreElement(root)) {
      return;
    }

    if (root.nodeType === Node.TEXT_NODE) {
      translateTextNode(root);
      return;
    }

    if (root.nodeType === Node.ELEMENT_NODE) {
      translateElementAttributes(root);
    }

    let child = root.firstChild;
    while (child) {
      walkAndTranslate(child);
      child = child.nextSibling;
    }
  }

  let scheduled = false;
  const dirtyRoots = new Set();

  const observer = new MutationObserver((mutations) => {
    if (isModifyingDOM) return;

    for (const m of mutations) {
      if (m.type === 'childList') {
        for (let i = 0; i < m.addedNodes.length; i++) {
          scheduleTranslation(m.addedNodes[i]);
        }
      } else if (m.type === 'characterData' || m.type === 'attributes') {
        scheduleTranslation(m.target);
      }
    }
  });

  function processDirtyNodes() {
    isModifyingDOM = true;
    try {
      for (const root of dirtyRoots) {
        if (root && root.isConnected !== false) {
          walkAndTranslate(root);
        }
      }
      dirtyRoots.clear();
      // Discard mutations triggered by our own DOM updates before unlocking.
      observer.takeRecords();
    } finally {
      isModifyingDOM = false;
      scheduled = false;
    }
  }

  function scheduleTranslation(node) {
    if (!node) return;
    dirtyRoots.add(node);
    if (!scheduled) {
      scheduled = true;
      requestAnimationFrame(processDirtyNodes);
    }
  }

  function startObserving() {
    if (document.body) {
      isModifyingDOM = true;
      try {
        walkAndTranslate(document.body);
      } finally {
        observer.takeRecords();
        isModifyingDOM = false;
      }
      observer.observe(document.body, {
        childList: true,
        subtree: true,
        characterData: true,
        attributes: true,
        attributeFilter: TRANSLATED_ATTRS,
      });
    } else {
      setTimeout(startObserving, 30);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startObserving);
  } else {
    startObserving();
  }
})();
