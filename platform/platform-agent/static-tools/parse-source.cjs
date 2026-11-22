'use strict';
// Parse strings only. Babel project configs and custom plugins are disabled by
// the pinned Playwright bundle. Never import or execute an application module.
const fs = require('node:fs');
const {babelParse, traverse} = require('./babelBundle.cjs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const results = [];

function observeParameters(rows) {
  return function(path) {
    const owner = path.node.id || (path.isMethod() && !path.node.computed ? path.node.key :
      path.parentPath.isVariableDeclarator() ? path.parentPath.node.id : null);
    if (owner?.type !== 'Identifier') return;
    path.node.params.forEach((param, index) => {
      const pattern = param.type === 'AssignmentPattern' ? param.left : param;
      if (pattern.type !== 'ObjectPattern') return;
      // A named field can be deliberately excluded from the remaining object.
      if (pattern.properties.some(property => property.type === 'RestElement')) return;
      for (const {type, computed, key, value} of pattern.properties) {
        if (type !== 'ObjectProperty' || computed || key.type !== 'Identifier' || value.type !== 'Identifier') continue;
        const binding = path.scope.getOwnBinding(value.name);
        if (binding?.identifier !== value || binding.referenced || !binding.constant) continue;
        const identity = JSON.stringify([owner.name, index, key.name, value.name]);
        rows.push({identity, kind:'unused_destructured_parameter', function:owner.name,
          property:key.name, binding:value.name, line:value.loc.start.line, column:value.loc.start.column + 1});
      }
    });
  };
}

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
    const row = {path:file.path,valid:true,imports:[...new Set(imports)],unbound:[...unbound].sort()};
    if (Object.hasOwn(file, 'previous')) {
      try {
        const observations = [];
        traverse(ast, {Function:observeParameters(observations)});
        if (observations.length) {
          const previous = [];
          if (file.previous !== null)
            traverse(babelParse(file.previous, file.path, true), {Function:observeParameters(previous)});
          const known = new Set(previous.map(item => item.identity));
          row.observations = observations.filter(item => !known.has(item.identity)).slice(0, 8)
            .map(({identity, ...observation}) => observation);
        }
      } catch {
        // An unavailable edit observation has no bearing on source validity.
      }
    }
    results.push(row);
  } catch (error) {
    results.push({path:file.path,valid:false,error:String(error.message).slice(0,1600)});
  }
}
process.stdout.write(JSON.stringify({files:results}));
