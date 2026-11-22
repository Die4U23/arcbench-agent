"""Browser probes for shared public interaction requirements."""
import re

from .group_exclusions import _clauses, _return_controls
from .platform_task_ir import _ANNOTATION, _annotations


INVENTORY_OP = 'hover_action_inventory'
MAX_INVENTORY_WITNESSES = 8


def inventory_value(value):
    if (not isinstance(value, dict) or set(value) != {'action_kind', 'witnesses'}
            or value['action_kind'] != 'button' or not isinstance(value['witnesses'], list)
            or not 1 <= len(value['witnesses']) <= MAX_INVENTORY_WITNESSES):
        raise ValueError('invalid hover action inventory')
    names = set()
    for witness in value['witnesses']:
        if (not isinstance(witness, dict) or set(witness) != {'role', 'name'} or witness['role'] != value['action_kind']
                or not isinstance(witness['name'], str) or not witness['name'].strip() or len(witness['name']) > 500
                or witness['name'] in names):
            raise ValueError('invalid hover action inventory witness')
        names.add(witness['name'])
    return value


def _unqualified_words(text):
    words = set(re.findall(r"[\w'-]+", _ANNOTATION.sub('', text).casefold()))
    if (words & {'not', 'no', 'never', 'without', 'except', 'unless', 'if', 'may', 'might', 'only'}
            or any(word.endswith("n't") for word in words)):
        return set()
    return words


def _workspace_entry(clauses, group):
    workspace, separator, _ = group['source_clauses']['given'].partition('. ')
    if not separator or not workspace.endswith(' is open'):
        return None
    workspace = workspace.removesuffix(' is open')
    landmarks = {name for source in clauses for name, role in _annotations(source['text'])
                 if role == 'navigation'}
    choices = {(landmark, control) for landmark in landmarks
               for evidence, control in _return_controls(clauses, workspace, landmark)
               if _unqualified_words(evidence['text'])}
    if len(choices) == 1:
        (landmark, (name, role)), = choices
        return dict(name=name, role=role, within=dict(name=landmark, role='navigation'))
    return None


def _lineage(ir, item):
    rows = {row['id']: row for row in ir['work_items']}
    ancestors = set()
    while item:
        if item['id'] in ancestors:
            raise ValueError('shared_public_ancestor_unbound')
        ancestors.add(item['id'])
        yield item
        item = rows.get(item.get('parent'))


def _declared_locale(lineage):
    locales = set()
    for item in lineage:
        environment = item.get('environment')
        if isinstance(environment, dict) and 'locale' in environment:
            locale = environment['locale']
            if not isinstance(locale, str) or not re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', locale):
                raise ValueError('shared_public_locale_invalid')
            locales.add(locale.casefold())
    if len(locales) > 1:
        raise ValueError('shared_public_locale_conflicting')
    return next(iter(locales), None)


def bind_group_shared(ir, item, group):
    """Bind workspace selection and an explicitly declared interface locale."""
    clauses = list(_clauses(ir))
    lineage = list(_lineage(ir, item))
    entry = _workspace_entry(clauses, group)
    result = dict(entry_steps=[], workspace_bound=entry is not None)
    if entry:
        result['entry_steps'].append(dict(op='current', target=entry))
        if locale := _declared_locale(lineage):
            result['entry_steps'].append(dict(op='interface_locale', target=entry, value=locale))
    return result


KEYBOARD_RUNTIME_JS = r"""async function repeatFocusedNode(page, visited) {
  return await page.evaluate(seen => {
    if (!document.hasFocus()) return false;
    let node = document.activeElement;
    while (node?.shadowRoot?.activeElement) node = node.shadowRoot.activeElement;
    if (!node || ([document.body, document.documentElement].includes(node) && node.tabIndex < 0)) return false;
    if (seen.has(node)) return true;
    seen.add(node);
    return false;
  }, visited);
}

async function assertKeyboardReach(page, item, requireInitiallyVisible) {
  if (requireInitiallyVisible) {
    await expect(item).toBeVisible();
    await expect(item).toBeEnabled();
  } else {
    // The keyboard path must stand on its own, including when focus reveals the control.
    await page.mouse.move(-1, -1);
  }
  const visited = await page.evaluateHandle(() => new Set());
  try {
    for (let i = 0; i < 512; i++) {
      await page.keyboard.press('Tab');
      if (await item.evaluateAll(elements => elements.length === 1 &&
          elements[0].ownerDocument.hasFocus() && elements[0].matches(':focus'))) {
        if (!requireInitiallyVisible) {
          await expect.poll(() => item.evaluateAll(elements => {
            if (elements.length !== 1 || !elements[0].ownerDocument.hasFocus() || !elements[0].matches(':focus')) return false;
            const element = elements[0];
            const {width, height} = element.getBoundingClientRect();
            return width > 0 && height > 0 && element.checkVisibility({visibilityProperty:true, opacityProperty:true});
          }), {message:'Public entry was not reached by keyboard Tab'}).toBe(true);
        }
        await expect(item).toBeEnabled();
        return;
      }
      if (await repeatFocusedNode(page, visited)) break;
    }
    throw new Error('Public entry was not reached by keyboard Tab');
  } finally { await visited.dispose(); }
}"""


HOVER_INVENTORY_RUNTIME_JS = r"""const inventoryActionLimit = 16;
const inventoryTabLimit = 128;

async function inventoryName(owner, candidate, role) {
  // Read Playwright's rendered accessible role and name, then round-trip the identity
  // through the same API. A snapshot that cannot be bound fails the probe.
  let line = (await candidate.ariaSnapshot()).split('\n', 1)[0].slice(2);
  if (line.startsWith("'") && line.endsWith("'")) line = line.slice(1, -1).replace(/''/g, "'");
  const parsed = /^(\w+)(?: ("(?:[^"\\]|\\.)*"))?(?=$| \[|:)/.exec(line);
  if (parsed?.[1] !== role) throw new Error('Hover inventory inconclusive: could not read the accessible action identity');
  const name = parsed[2] === undefined ? '' : JSON.parse(parsed[2]);
  if (await candidate.and(owner.getByRole(role, {name, exact:true})).count() !== 1) {
    throw new Error('Hover inventory inconclusive: the accessible action identity changed during observation');
  }
  return name;
}

async function recordAction(actions, owner, candidate, role, identities) {
  const id = await candidate.evaluate((el, seen) => {
    if (!seen.nodes.has(el)) seen.nodes.set(el, ++seen.last);
    return seen.nodes.get(el);
  }, identities);
  actions.set(id, await inventoryName(owner, candidate, role));
  if (actions.size > inventoryActionLimit) {
    throw new Error(`Hover inventory inconclusive: more than ${inventoryActionLimit} actions in the record`);
  }
}

async function renderedActions(owner, role, identities) {
  const visible = owner.getByRole(role).and(owner.locator(':visible'));
  const actions = new Map();
  // Layout bounds alone are insufficient for CSS hover strips that start transparent.
  const indexed = await visible.evaluateAll(elements => elements.flatMap((el, index) => {
    const {width, height} = el.getBoundingClientRect();
    return width > 0 && height > 0 && el.checkVisibility({visibilityProperty:true, opacityProperty:true}) ? [index] : [];
  }));
  if (indexed.length > inventoryActionLimit) {
    throw new Error(`Hover inventory inconclusive: more than ${inventoryActionLimit} rendered controls in the record`);
  }
  for (const index of indexed) {
    const candidate = visible.nth(index);
    if (await candidate.isEnabled()) await recordAction(actions, owner, candidate, role, identities);
  }
  return actions;
}

async function inventoryTab(page, budget) {
  if (budget.remaining === 0) throw new Error(`Hover inventory inconclusive: Tab traversal exceeded ${inventoryTabLimit} steps`);
  budget.remaining--;
  await page.keyboard.press('Tab');
}

async function leaveInventoryFocus(page, owner, budget) {
  while (await owner.evaluate(el => el.matches(':focus-within'))) await inventoryTab(page, budget);
}

async function keyboardActions(page, owner, role, identities, budget) {
  const focused = owner.getByRole(role).and(owner.locator(':focus'));
  const reached = new Map();
  const visited = await page.evaluateHandle(() => new Set());
  try {
    await repeatFocusedNode(page, visited);
    while (true) {
      await inventoryTab(page, budget);
      if (await repeatFocusedNode(page, visited)) return reached;
      if (await focused.count() !== 1 || !(await focused.isEnabled())) continue;
      // Allow a focused control's rendered state to settle through a short CSS transition.
      const exposed = await focused.evaluate(async el => {
        const deadline = performance.now() + 500;
        do {
          if (!el.ownerDocument.hasFocus() || !el.matches(':focus')) return false;
          const {width, height} = el.getBoundingClientRect();
          if (width > 0 && height > 0 && el.checkVisibility({visibilityProperty:true, opacityProperty:true})) return true;
          await new Promise(requestAnimationFrame);
        } while (performance.now() < deadline);
        return false;
      });
      if (exposed) await recordAction(reached, owner, focused, role, identities);
    }
  } finally { await visited.dispose(); }
}

async function hoverInventoryCenter(page, owner) {
  await owner.scrollIntoViewIfNeeded();
  const center = await owner.evaluate(el => {
    const box = el.getBoundingClientRect();
    const x = box.x + box.width / 2;
    const y = box.y + box.height / 2;
    const hit = el.getRootNode().elementFromPoint(x, y);
    return el.contains(hit) ? {x, y} : null;
  });
  if (!center) throw new Error('Hover inventory inconclusive: record center is clipped or intercepted');
  const receipt = await owner.evaluateHandle((el, point) => {
    const document = el.ownerDocument;
    const state = {seen:false, detach:() => document.removeEventListener('pointermove', capture, true)};
    function capture(event) {
      if (event.isTrusted && event.pointerType === 'mouse' && event.composedPath().includes(el)
          && Math.abs(event.clientX - point.x) < 1 && Math.abs(event.clientY - point.y) < 1) state.seen = true;
    }
    document.addEventListener('pointermove', capture, {capture:true, passive:true});
    return state;
  }, center);
  try {
    await page.mouse.move(center.x, center.y);
    await expect.poll(() => receipt.evaluate(state => state.seen),
                      {message:'Hover inventory inconclusive: pointer did not reach the scoped record center'}).toBe(true);
  } finally {
    await receipt.evaluate(state => state.detach());
    await receipt.dispose();
  }
}

async function settleInventory(owner) {
  const settled = await owner.evaluate(async el => {
    // Read settled exposure after an opacity/visibility transition instead of
    // assigning residual pointer exposure to the idle baseline.
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const transitions = el.getAnimations({subtree:true}).filter(animation =>
      animation instanceof CSSTransition && ['opacity', 'visibility'].includes(animation.transitionProperty));
    if (!transitions.length) return true;
    return await Promise.race([
      Promise.allSettled(transitions.map(animation => animation.finished)).then(() => true),
      new Promise(resolve => setTimeout(() => resolve(false), 500))
    ]);
  });
  if (!settled) throw new Error('Hover inventory inconclusive: visibility did not settle');
}

async function assertHoverActionInventory(page, owner, {action_kind:role, witnesses}) {
  await expect(owner).toBeVisible();
  if (typeof owner.ariaSnapshot !== 'function') {
    throw new Error('Hover inventory inconclusive: Playwright cannot read accessible action identities');
  }
  const tabBudget = {remaining:inventoryTabLimit};
  const identities = await page.evaluateHandle(() => ({nodes:new WeakMap(), last:0}));
  try {
    await page.mouse.move(-1, -1);
    await owner.scrollIntoViewIfNeeded();
    await leaveInventoryFocus(page, owner, tabBudget);
    await settleInventory(owner);
    const idle = await renderedActions(owner, role, identities);
    const keyboard = await keyboardActions(page, owner, role, identities, tabBudget);
    await leaveInventoryFocus(page, owner, tabBudget);
    await hoverInventoryCenter(page, owner);
    await settleInventory(owner);
    const pointer = await renderedActions(owner, role, identities);
    const pointerNames = [...pointer.values()];
    const keyboardNames = [...keyboard.values()];
    for (const {name} of witnesses) {
      expect(pointerNames, `Missing rendered hover inventory witness ${JSON.stringify(name)}`).toContain(name);
    }
    for (const [id, name] of pointer) {
      if (idle.get(id) === name || keyboard.get(id) === name) continue;
      if (keyboardNames.includes(name)) {
        throw new Error(`Hover inventory inconclusive: ${JSON.stringify(name)} is a different control after pointer and keyboard exposure`);
      }
      throw new Error(`Hover action not reached by keyboard Tab: ${JSON.stringify(name)}`);
    }
    await page.mouse.move(-1, -1);
  } finally {
    await identities.dispose();
  }
}"""
