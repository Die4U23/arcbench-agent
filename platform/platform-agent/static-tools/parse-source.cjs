'use strict';
// Parse strings only. Babel project configs and custom plugins are disabled by
// the pinned Playwright bundle. Never import or execute an application module.
const fs = require('node:fs');
const {babelParse, traverse} = require('./babelBundle.cjs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const results = [];
for (const file of input.files) {
  try {
    const ast = babelParse(file.content, file.path, true);
    const imports = [];
    function visit(node) {
      if (!node || typeof node !== 'object') return;
      if (['ImportDeclaration','ExportNamedDeclaration','ExportAllDeclaration'].includes(node.type) && node.source)
        imports.push(node.source.value);
      if (node.type === 'CallExpression' && node.callee?.name === 'require' && node.arguments?.length === 1 && node.arguments[0].type === 'StringLiteral')
        imports.push(node.arguments[0].value);
      for (const [key, value] of Object.entries(node)) {
        if (['loc','tokens','comments','leadingComments','trailingComments'].includes(key)) continue;
        if (Array.isArray(value)) value.forEach(visit); else if (value && typeof value === 'object') visit(value);
      }
    }
    visit(ast);
    // Scope analysis catches runtime ReferenceErrors without evaluating code.
    // TypeScript type names are excluded; only executable references count.
    const globals = new Set(('undefined NaN Infinity globalThis console window document navigator location history localStorage sessionStorage fetch XMLHttpRequest FormData Headers Request Response URL URLSearchParams Blob File FileReader Image Audio HTMLElement Element Node Event CustomEvent MouseEvent KeyboardEvent MutationObserver ResizeObserver IntersectionObserver DOMParser AbortController AbortSignal TextEncoder TextDecoder Intl Date Math JSON Object Array String Number Boolean BigInt Symbol RegExp Error TypeError RangeError SyntaxError ReferenceError Promise Map Set WeakMap WeakSet WeakRef Proxy Reflect Function ArrayBuffer DataView Uint8Array Uint16Array Uint32Array Int8Array Int16Array Int32Array Float32Array Float64Array Buffer process require module exports __dirname __filename global setTimeout clearTimeout setInterval clearInterval setImmediate clearImmediate queueMicrotask requestAnimationFrame cancelAnimationFrame performance crypto atob btoa encodeURIComponent decodeURIComponent encodeURI decodeURI parseInt parseFloat isNaN isFinite structuredClone alert confirm prompt arguments React JSX').split(' '));
    const unbound = new Set();
    traverse(ast, {ReferencedIdentifier(path) {
      const name=path.node.name;
      if (globals.has(name) || path.scope.hasBinding(name)) return;
      // Types are not emitted into JavaScript. TS expressions still contain
      // executable references and must be checked.
      let parent=path.parentPath;
      while(parent) {
        const type=parent.node.type;
        if(type.startsWith('TS') && !['TSAsExpression','TSNonNullExpression','TSSatisfiesExpression','TSInstantiationExpression'].includes(type)) return;
        parent=parent.parentPath;
      }
      unbound.add(name);
    }});
    results.push({path:file.path,valid:true,imports:[...new Set(imports)],unbound:[...unbound].sort()});
  } catch (error) {
    results.push({path:file.path,valid:false,error:String(error.message).slice(0,1600)});
  }
}
process.stdout.write(JSON.stringify({files:results}));
