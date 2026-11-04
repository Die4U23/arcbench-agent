'use strict';
// Locator-scope check. It does not execute the application or name a task.
// Page-level exact link/button queries come from the supplied tests. A shared
// layout and the route mounted inside it are one scope. Region, dialog and
// navigation queries are a different scope and do not create a page-level name.
const fs = require('node:fs');
const {babelParse} = require('./babelBundle.cjs');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const literals = {link: new Set(), button: new Set()};
const dynamic = {link: false, button: false};
const errors = [];

function ident(node) {
  if (!node) return null;
  if (node.type === 'Identifier') return node.name;
  if (node.type === 'JSXIdentifier') return node.name;
  return null;
}

function propName(node) {
  return ident(node.key) || (node.key && node.key.type === 'StringLiteral' ? node.key.value : null);
}

function isExact(options) {
  if (!options || options.type !== 'ObjectExpression') return false;
  const exact = options.properties.find((property) => property.type === 'ObjectProperty' && propName(property) === 'exact');
  return Boolean(exact && exact.value.type === 'BooleanLiteral' && exact.value.value === true);
}

function nameNode(options) {
  if (!options || options.type !== 'ObjectExpression') return null;
  const property = options.properties.find((item) => item.type === 'ObjectProperty' && propName(item) === 'name');
  return property ? property.value : null;
}

function stringArray(node) {
  if (!node || node.type !== 'ArrayExpression') return null;
  if (!node.elements.every((item) => item && item.type === 'StringLiteral')) return null;
  return node.elements.map((item) => item.value);
}

function namesFrom(node, path) {
  if (!node) return {literals: [], dynamic: false};
  if (node.type === 'StringLiteral') return {literals: [node.value], dynamic: false};
  if (node.type === 'TemplateLiteral' && node.expressions.length === 0) {
    return {literals: [node.quasis.map((item) => item.value.cooked).join('')], dynamic: false};
  }
  if (node.type !== 'Identifier') return {literals: [], dynamic: true};
  const binding = path.scope.getBinding(node.name);
  if (binding && binding.kind === 'param') return {literals: [], dynamic: false};
  if (binding && binding.path.parent.type === 'ForOfStatement') {
    const values = stringArray(binding.path.parent.right);
    if (values) return {literals: values, dynamic: false};
  }
  return {literals: [], dynamic: true};
}

function remember(role, names) {
  if (!literals[role]) return;
  for (const value of names.literals) literals[role].add(value);
  if (names.dynamic) dynamic[role] = true;
}

function roleQuery(node, receiverName) {
  if (node.type !== 'CallExpression' || node.callee.type !== 'MemberExpression') return null;
  if (ident(node.callee.property) !== 'getByRole') return null;
  if (!receiverName || node.callee.object.type !== 'Identifier' || node.callee.object.name !== receiverName) return null;
  const roleNode = node.arguments[0];
  if (!roleNode || roleNode.type !== 'StringLiteral' || !literals[roleNode.value]) return null;
  const options = node.arguments[1];
  if (!isExact(options)) return null;
  return {role: roleNode.value, name: nameNode(options)};
}

function parseFile(file) {
  return babelParse(file.content, file.path, true);
}

function collectHelpers(ast) {
  const helpers = new Map();
  function visit(node, parent) {
    if (!node || typeof node !== 'object') return;
    if (node.type === 'FunctionDeclaration' || node.type === 'FunctionExpression' || node.type === 'ArrowFunctionExpression') {
      const name = node.id && node.id.type === 'Identifier' ? node.id.name
        : parent && parent.type === 'VariableDeclarator' && parent.id.type === 'Identifier' ? parent.id.name : null;
      const params = node.params || [];
      if (name && params.length >= 2 && params[0].type === 'Identifier' && params[1].type === 'Identifier') {
        const roles = new Set();
        walk(node.body, (child) => {
          const query = roleQuery(child, params[0].name);
          if (query && query.name && query.name.type === 'Identifier' && query.name.name === params[1].name) roles.add(query.role);
        }, node);
        if (roles.size) helpers.set(name, {roles: [...roles]});
      }
    }
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach((item) => visit(item, node));
      else if (value && typeof value === 'object' && value.type) visit(value, node);
    }
  }
  visit(ast, null);
  return helpers;
}

function walk(node, visit, stopAt) {
  if (!node || typeof node !== 'object') return;
  if (node !== stopAt && (node.type === 'FunctionDeclaration' || node.type === 'FunctionExpression' || node.type === 'ArrowFunctionExpression')) return;
  visit(node);
  for (const value of Object.values(node)) {
    if (Array.isArray(value)) value.forEach((item) => walk(item, visit, stopAt));
    else if (value && typeof value === 'object' && value.type) walk(value, visit, stopAt);
  }
}

function collectQueries(ast, helpers, traverse) {
  traverse(ast, {
    CallExpression(callPath) {
      const node = callPath.node;
      const direct = roleQuery(node, 'page');
      if (direct) {
        remember(direct.role, namesFrom(direct.name, callPath));
        return;
      }
      if (node.callee.type !== 'Identifier') return;
      const helper = helpers.get(node.callee.name);
      if (!helper) return;
      const scope = node.arguments[0];
      const target = node.arguments[1];
      if (!scope || scope.type !== 'Identifier' || scope.name !== 'page') return;
      const names = namesFrom(target, callPath);
      for (const role of helper.roles) remember(role, names);
    },
  });
}

function jsxName(element) {
  const opening = element.openingElement && element.openingElement.name;
  return opening && opening.type === 'JSXIdentifier' ? opening.name : null;
}

function attribute(element, name) {
  return (element.openingElement.attributes || []).find((item) => item.type === 'JSXAttribute' && ident(item.name) === name);
}

function controlFrom(element) {
  const role = jsxName(element) === 'a' || jsxName(element) === 'Link' || jsxName(element) === 'NavLink' ? 'link'
    : jsxName(element) === 'button' ? 'button' : null;
  if (!role) return null;
  const label = attribute(element, 'aria-label');
  if (label && label.value && label.value.type === 'StringLiteral') {
    return {role, key: 'lit:' + label.value.value, literal: label.value.value};
  }
  const words = [];
  let expression = null;
  for (const child of element.children || []) {
    if (child.type === 'JSXText') {
      const text = child.value.replace(/\s+/g, ' ').trim();
      if (text) words.push(text);
    } else if (child.type === 'JSXExpressionContainer') {
      if (child.expression.type === 'StringLiteral') words.push(child.expression.value);
      else if (!expression) expression = expressionKey(child.expression);
      else expression = null;
    }
  }
  if (expression && words.length === 0) return {role, key: expression, literal: null};
  if (words.length && !expression) {
    const text = words.join(' ');
    return {role, key: 'lit:' + text, literal: text};
  }
  return null;
}

function expressionKey(node) {
  if (!node) return null;
  if (node.type === 'Identifier') return 'id:' + node.name;
  if (node.type === 'MemberExpression' && !node.computed && node.property.type === 'Identifier') {
    const object = expressionKey(node.object);
    return object ? object + '.' + node.property.name : null;
  }
  return null;
}

function walkJsx(node, visit) {
  if (!node || typeof node !== 'object') return;
  if (node.type === 'JSXElement') visit(node);
  for (const value of Object.values(node)) {
    if (Array.isArray(value)) value.forEach((item) => walkJsx(item, visit));
    else if (value && typeof value === 'object' && value.type) walkJsx(value, visit);
  }
}

function componentName(node, parent) {
  if (node.id && node.id.type === 'Identifier') return node.id.name;
  if (parent && parent.type === 'VariableDeclarator' && parent.id.type === 'Identifier') return parent.id.name;
  return null;
}

function booleanStates(fn) {
  const states = [];
  walk(fn.body, (node) => {
    if (node.type !== 'VariableDeclarator' || node.id.type !== 'ArrayPattern') return;
    const flag = node.id.elements[0];
    const setter = node.id.elements[1];
    const init = node.init;
    if (!flag || flag.type !== 'Identifier' || !setter || setter.type !== 'Identifier' || !init) return;
    if (init.type !== 'CallExpression' || ident(init.callee) !== 'useState') return;
    if (init.arguments[0] && init.arguments[0].type === 'BooleanLiteral') {
      states.push({flag: flag.name, setter: setter.name});
    }
  }, fn);
  return states;
}

function guardedReturns(fn, flag) {
  const found = [];
  walk(fn.body, (node) => {
    if (node.type !== 'IfStatement' || ident(node.test) !== flag) return;
    const block = node.consequent;
    walk(block, (child) => {
      if (child.type === 'ReturnStatement' && child.argument) found.push(child);
    }, fn);
  }, fn);
  return found;
}

function handlerOf(attributeNode) {
  if (!attributeNode || attributeNode.type !== 'JSXAttribute' || !attributeNode.value) return null;
  if (attributeNode.value.type !== 'JSXExpressionContainer') return null;
  const expression = attributeNode.value.expression;
  if (expression.type === 'ArrowFunctionExpression' || expression.type === 'FunctionExpression') return expression;
  return null;
}

function sameRoute(route, target) {
  const normalize = (value) => value.replace(/\/+$/, '') || '/';
  return normalize(route) === normalize(target);
}

function navigatesToRoute(fn, route) {
  let found = false;
  walk(fn.body || fn, (node) => {
    if (node.type !== 'CallExpression' || ident(node.callee) !== 'navigate') return;
    const target = node.arguments[0];
    if (target && target.type === 'StringLiteral' && sameRoute(route, target.value)) found = true;
  }, fn);
  return found;
}

function clearsFlag(fn, setter) {
  let found = false;
  walk(fn.body || fn, (node) => {
    if (node.type !== 'CallExpression' || ident(node.callee) !== setter) return;
    const value = node.arguments[0];
    if (value && value.type === 'BooleanLiteral' && value.value === false) found = true;
  }, fn);
  return found;
}

function collectComponents(ast, file) {
  const components = new Map();
  function visit(node, parent) {
    if (!node || typeof node !== 'object') return;
    if (node.type === 'FunctionDeclaration' || node.type === 'FunctionExpression' || node.type === 'ArrowFunctionExpression') {
      const name = componentName(node, parent);
      const returns = [];
      if (name && node.body) {
        walk(node.body, (child) => {
          if (child.type === 'ReturnStatement' && child.argument) {
            const controls = [];
            walkJsx(child.argument, (element) => {
              const control = controlFrom(element);
              if (control) controls.push(control);
            });
            returns.push({node: child, controls});
          }
        }, node);
        if (returns.length) components.set(name, {file, node, returns, states: booleanStates(node)});
      }
    }
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach((item) => visit(item, node));
      else if (value && typeof value === 'object' && value.type) visit(value, node);
    }
  }
  visit(ast, null);
  return components;
}

function elementComponent(element) {
  const attr = attribute(element, 'element');
  if (!attr || !attr.value || attr.value.type !== 'JSXExpressionContainer') return null;
  const expression = attr.value.expression;
  return expression.type === 'JSXElement' ? jsxName(expression) : null;
}

function routePath(element) {
  const attr = attribute(element, 'path');
  if (!attr || !attr.value || attr.value.type !== 'StringLiteral') return null;
  return attr.value.value;
}

function collectRoutes(ast) {
  const routes = [];
  walkJsx(ast, (element) => {
    if (jsxName(element) !== 'Route') return;
    const children = (element.children || []).filter((child) => child.type === 'JSXElement' && jsxName(child) === 'Route');
    const layout = elementComponent(element);
    if (children.length && layout) {
      for (const child of children) {
        const page = elementComponent(child);
        const path = routePath(child);
        if (page && path) routes.push({layout, page, path});
      }
      return;
    }
    const page = elementComponent(element);
    const path = routePath(element);
    if (page && path && !children.length) routes.push({layout: null, page, path});
  });
  return routes;
}

function reportDuplicates(controls, where, file) {
  const buckets = new Map();
  for (const control of controls) {
    const id = control.role + '\n' + control.key;
    const bucket = buckets.get(id) || [];
    bucket.push(control);
    buckets.set(id, bucket);
  }
  for (const bucket of buckets.values()) {
    if (bucket.length < 2) continue;
    const sample = bucket[0];
    if (sample.literal !== null) {
      if (!literals[sample.role].has(sample.literal)) continue;
      errors.push({path: file, error: `Page-level exact ${sample.role} "${sample.literal}" occurs ${bucket.length} times in ${where}. The shared layout and current route are one locator scope; keep one control with that accessible name.`});
    } else if (dynamic[sample.role]) {
      errors.push({path: file, error: `Page-level exact ${sample.role} matches ${bucket.length} controls with accessible name ${sample.key} in ${where}. A strict locator requires one target in that scope.`});
    }
  }
}

function reportCancels(component, route) {
  for (const state of component.states) {
    for (const item of guardedReturns(component.node, state.flag)) {
      walkJsx(item.argument, (element) => {
        for (const name of ['onCancel', 'onClick']) {
          const handler = handlerOf(attribute(element, name));
          if (!handler || !navigatesToRoute(handler, route)) continue;
          if (name === 'onClick') {
            const control = controlFrom(element);
            if (!control || control.role !== 'button' || !control.literal || !literals.button.has(control.literal)) continue;
          }
          if (clearsFlag(handler, state.setter)) continue;
          errors.push({path: component.file, error: `Cancel handler on route ${route} navigates to the current route without clearing local state "${state.flag}", so the previous view stays unmounted.`});
        }
      });
    }
  }
}

function analyze() {
  const {traverse} = require('./babelBundle.cjs');
  const helperMaps = [];
  const testAsts = [];
  for (const file of input.testFiles || []) {
    const ast = parseFile(file);
    testAsts.push(ast);
    helperMaps.push(collectHelpers(ast));
  }
  const helpers = new Map();
  for (const map of helperMaps) for (const [name, value] of map) helpers.set(name, value);
  for (const ast of testAsts) collectQueries(ast, helpers, traverse);

  const components = new Map();
  const routes = [];
  for (const file of input.appFiles || []) {
    const ast = parseFile(file);
    for (const [name, component] of collectComponents(ast, file.path)) components.set(name, component);
    routes.push(...collectRoutes(ast));
  }
  const seen = new Set();
  for (const route of routes) {
    const page = components.get(route.page);
    if (!page) continue;
    const layout = route.layout ? components.get(route.layout) : null;
    const layoutViews = layout ? layout.returns : [{controls: []}];
    for (const outer of layoutViews) {
      for (const inner of page.returns) {
        reportDuplicates(outer.controls.concat(inner.controls), `route ${route.path} (${route.page})`, page.file);
      }
    }
    reportCancels(page, route.path);
  }
  const unique = [];
  for (const error of errors) {
    const key = error.path + '\n' + error.error;
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(error);
  }
  process.stdout.write(JSON.stringify({errors: unique}));
}

try {
  analyze();
} catch (error) {
  process.stderr.write(String(error && error.stack || error));
  process.exit(1);
}
