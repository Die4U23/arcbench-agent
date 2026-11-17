"""Exact public CSS/geometry diagnostics, not a screenshot or pixel-similarity oracle.

Only finite explicit input is accepted. Dimensions are rendered border-box CSS px.
The default observation environment is desktop 1280x720, DPR 1, light scheme.
Unsupported compositing fails closed rather than pretending computed CSS is pixels.
"""
from __future__ import annotations

import json
import math
import re
from decimal import Decimal

DEFAULT_VIEWPORT = {"width": 1280, "height": 720}
VISUAL_PROPERTIES = {"background-color", "color", "border-color", "width", "height", "border-radius", "shape"}
# The basic CSS palette plus unambiguous standard CSS names. Other color spaces
# and named colors remain unsupported until independently implemented and tested.
_COLORS = dict(zip(
    "black silver gray white maroon red purple fuchsia green lime olive yellow navy blue teal aqua orange rebeccapurple".split(),
    "000000 c0c0c0 808080 ffffff 800000 ff0000 800080 ff00ff 008000 00ff00 808000 ffff00 000080 0000ff 008080 00ffff ffa500 663399".split()))
_COLORS.update(grey="808080", magenta="ff00ff", cyan="00ffff")
_NUMBER = r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"


def _number(value):
    number = Decimal(value)
    numeric = float(number)
    if not math.isfinite(numeric) or Decimal.from_float(numeric) != number:
        raise ValueError("visual_precision_not_exactly_representable")
    # Never truncate a requirement to make a nearby number appear exact.
    return format(number, "f").rstrip("0").rstrip(".") if "." in format(number, "f") else format(number, "f")


def normalize_color(value):
    if not isinstance(value, str):
        raise ValueError("visual_color_must_be_explicit_css_string")
    value = value.strip().lower()
    if value in _COLORS:
        value = "#" + _COLORS[value]
    if re.fullmatch(r"#[0-9a-f]{3,4}|#[0-9a-f]{6}(?:[0-9a-f]{2})?", value):
        code = value[1:]
        if len(code) in (3, 4):
            code = "".join(c * 2 for c in code)
        if len(code) == 8 and code[6:] != "ff":
            raise ValueError("visual_translucent_color_not_supported")
        channels = [int(code[i:i+2], 16) for i in (0, 2, 4)]
    else:
        match = re.fullmatch(r"rgba?\((.*)\)", value)
        if not match:
            raise ValueError("visual_color_requires_supported_exact_css_value")
        raw = match.group(1)
        if "," in raw:
            pieces = [part.strip() for part in raw.split(",")]
        else:
            pieces = raw.replace("/", " / ").split()
            if "/" in pieces:
                if len(pieces) != 5 or pieces[3] != "/":
                    raise ValueError("visual_invalid_color")
                pieces.pop(3)
        if len(pieces) not in (3, 4):
            raise ValueError("visual_invalid_color")
        if len(pieces) == 4 and pieces[3] not in ("1", "1.0", "100%"):
            raise ValueError("visual_translucent_color_not_supported")
        channels = []
        for part in pieces[:3]:
            if not re.fullmatch(r"[0-9]{1,3}", part) or not 0 <= int(part) <= 255:
                raise ValueError("visual_color_requires_integer_srgb_channels")
            channels.append(int(part))
    return "rgb(" + ", ".join(map(str, channels)) + ")"


def normalize_visual(value):
    if not isinstance(value, dict) or set(value) != {"properties", "viewport"}:
        raise ValueError("visual_requires_properties_and_viewport_no_tolerance")
    viewport = value["viewport"]
    if (not isinstance(viewport, dict) or set(viewport) != {"width", "height"}
            or any(type(viewport[k]) is not int or not 1 <= viewport[k] <= 4096 for k in viewport)):
        raise ValueError("visual_invalid_viewport")
    properties = value["properties"]
    if not isinstance(properties, dict) or not properties or set(properties) - VISUAL_PROPERTIES:
        raise ValueError("visual_missing_or_unsupported_properties")
    result = {}
    for prop, raw in properties.items():
        if not isinstance(raw, str) or len(raw) > 120:
            raise ValueError("visual_property_requires_bounded_string")
        raw = raw.strip().lower()
        if prop.endswith("color"):
            result[prop] = normalize_color(raw)
        elif prop in {"width", "height", "border-radius"}:
            match = re.fullmatch("(" + _NUMBER + r")(px|%)", raw)
            if not match or (match.group(2) == "%" and prop != "border-radius"):
                raise ValueError("visual_length_requires_exact_px_or_radius_percent")
            number = float(match.group(1))
            if number > (2**53 if prop == "border-radius" else 4096) or (prop != "border-radius" and number <= 0):
                raise ValueError("visual_length_out_of_bounds")
            if prop in {"width", "height"} and Decimal(match.group(1)) * 64 != (Decimal(match.group(1)) * 64).to_integral_value():
                raise ValueError("visual_geometry_not_exact_css_pixel_grid")
            result[prop] = _number(match.group(1)) + match.group(2)
        elif prop == "shape":
            if raw not in {"circle", "square", "rectangle", "pill"}:
                raise ValueError("visual_shape_requires_circle_square_rectangle_or_pill")
            result[prop] = raw
    return {"properties": result, "viewport": dict(viewport)}


def parse_visual_tail(tail):
    """Return a canonical visual value, or raise for unbound/ambiguous input."""
    viewport = dict(DEFAULT_VIEWPORT)
    match = re.search(r"\s+at viewport ([0-9]+)x([0-9]+)$", tail, re.I)
    if match:
        viewport = dict(width=int(match.group(1)), height=int(match.group(2)))
        tail = tail[:match.start()]
    match = re.fullmatch(r"visual is (\{.*\})", tail, re.I)
    if match:
        def unique_pairs(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("visual_duplicate_property")
                result[key] = item
            return result
        properties = json.loads(match.group(1), object_pairs_hook=unique_pairs)
    else:
        match = re.fullmatch(r"CSS ([a-z-]+) is (\"(?:\\.|[^\"\\])*\")", tail, re.I)
        if match:
            properties = {match.group(1).lower(): json.loads(match.group(2))}
        else:
            match = re.fullmatch(r"(width|height|border-radius) is (" + _NUMBER + r"(?:px|%))", tail, re.I)
            if match:
                properties = {match.group(1).lower(): match.group(2)}
            else:
                match = re.fullmatch(r"shape is (circle|square|rectangle|pill)", tail, re.I)
                if not match:
                    raise ValueError("visual_statement_not_exact_or_supported")
                properties = {"shape": match.group(1).lower()}
    return normalize_visual(dict(properties=properties, viewport=viewport))


# Controller-owned source only. User/model strings are JSON data, never code.
VISUAL_RUNTIME_JS = r'''
async function assertPublicVisual(page, locator, required) {
  await expect(locator).toHaveCount(1);
  await expect(locator).toBeVisible();
  await locator.scrollIntoViewIfNeeded();
  // JavaScript shadowRoot intentionally hides closed roots, including roots on
  // ordinary div/span hosts. Inspect Chromium's actual DOM rather than patching
  // attachShadow, which misses preexisting roots and other execution realms.
  let shadowInspection = 'unavailable';
  let session;
  try {
    session = await page.context().newCDPSession(page);
    const tree = await session.send('DOM.getDocument', {depth:-1, pierce:true});
    if (!tree || !tree.root || tree.root.nodeType !== 9 || !Array.isArray(tree.root.children))
      throw new Error('invalid DOM inspection result');
    let closed = false;
    const inspect = node => {
      if (!node || typeof node !== 'object') throw new Error('invalid DOM node');
      if (node.shadowRootType === 'closed') closed = true;
      if (node.shadowRootType && !['open','closed','user-agent'].includes(node.shadowRootType))
        throw new Error('unsupported shadow tree type');
      for (const field of ['children','shadowRoots','pseudoElements']) {
        if (field in node) {
          if (!Array.isArray(node[field])) throw new Error('invalid DOM children');
          node[field].forEach(inspect);
        }
      }
      for (const field of ['contentDocument','templateContent','importedDocument'])
        if (node[field]) inspect(node[field]);
    };
    inspect(tree.root);
    shadowInspection = closed ? 'closed' : 'clear';
  } catch (_) {
    shadowInspection = 'unavailable';
  } finally {
    if (session) {
      try { await session.detach(); } catch (_) { shadowInspection = 'unavailable'; }
    }
  }
  const observed = await locator.evaluate((el, input) => {
    const req = input.required;
    const issues = [];
    const fail = reason => { if (!issues.includes(reason)) issues.push(reason); };
    if (input.shadowInspection === 'closed') fail('visual_unverifiable:closed_shadow_tree');
    else if (input.shadowInspection !== 'clear') fail('visual_unverifiable:shadow_inspection_unavailable');
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    const intersects = (a,b) => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
    const opaque = color => !color || color === 'transparent' || /rgba\([^)]*,\s*0\s*\)$/.test(color);
    const painted = s => !opaque(s.backgroundColor) || s.backgroundImage !== 'none' ||
      ['Top','Right','Bottom','Left'].some(side => parseFloat(s['border'+side+'Width']) > 0 && !['none','hidden'].includes(s['border'+side+'Style']) && !opaque(s['border'+side+'Color']));
    // Namespace, custom elements and replaced CSS content may paint through
    // trees that ordinary descendant/computed-style inspection cannot prove.
    // Closed shadow roots deliberately have no inspectable shadowRoot handle.
    const opaqueTree = node => {
      if (node.namespaceURI !== 'http://www.w3.org/1999/xhtml') fail('visual_unverifiable:foreign_namespace');
      if (node.localName.includes('-') || node.hasAttribute('is')) fail('visual_unverifiable:custom_element_paint');
    };
    const replacedContent = s => {
      if (s.content && !['normal','none'].includes(s.content)) fail('visual_unverifiable:replaced_content');
    };
    const pseudo = node => {
      for (const which of ['::before','::after']) {
        const p = getComputedStyle(node,which);
        if (!['none','normal'].includes(p.content) && p.display !== 'none' && p.visibility === 'visible')
          fail('visual_unverifiable:pseudo_element');
      }
    };
    if (innerWidth !== req.viewport.width || innerHeight !== req.viewport.height || devicePixelRatio !== 1)
      fail('visual_environment_mismatch');
    if (rect.width <= 0 || rect.height <= 0 || el.getClientRects().length !== 1)
      fail('visual_unverifiable:empty_or_fragmented_geometry');
    if (rect.left < 0 || rect.top < 0 || rect.right > innerWidth || rect.bottom > innerHeight)
      fail('visual_unverifiable:outside_viewport');
    if (document.fonts && document.fonts.status !== 'loaded') fail('visual_unverifiable:fonts_not_ready');
    const nodes = [el, ...el.querySelectorAll('*')];
    for (let a=el.parentElement; a; a=a.parentElement) nodes.push(a);
    for (const node of nodes) {
      const s=getComputedStyle(node);
      opaqueTree(node);
      replacedContent(s);
      if (s.visibility !== 'visible' || s.display === 'none' || s.contentVisibility === 'hidden') fail('visual_unverifiable:hidden');
      if (/\b(paint|content|strict)\b/.test(s.contain) || (s.contentVisibility && s.contentVisibility !== 'visible'))
        fail('visual_unverifiable:paint_containment');
      for (const corner of ['corner-shape','corner-top-left-shape','corner-top-right-shape','corner-bottom-right-shape','corner-bottom-left-shape']) {
        const curve=s.getPropertyValue(corner).trim();
        if (curve && !['round','superellipse(1)'].includes(curve)) fail('visual_unverifiable:non_round_corner_shape');
      }

      if (Number(s.opacity) !== 1) fail('visual_unverifiable:opacity');
      if (s.transform !== 'none' || !['none','1'].includes(s.scale || 'none') ||
          !['none','0deg'].includes(s.rotate || 'none') || !['none','0px'].includes(s.translate || 'none') ||
          !['normal','1'].includes(s.zoom || '1') || s.perspective !== 'none') fail('visual_unverifiable:transform');
      if (s.filter !== 'none' || (s.backdropFilter && s.backdropFilter !== 'none') || s.mixBlendMode !== 'normal')
        fail('visual_unverifiable:filter_or_blend');
      if (s.clipPath !== 'none' || (s.maskImage && s.maskImage !== 'none') ||
          (s.webkitMaskImage && s.webkitMaskImage !== 'none') || s.clip !== 'auto') fail('visual_unverifiable:clip_or_mask');
      if (node.shadowRoot) fail('visual_unverifiable:shadow_tree');
      // Progress/meter keep anonymous internal paint even with appearance:none.
      // Their track/value pseudo trees are not proven by the host's CSS values.
      if (['PROGRESS','METER','SELECT','TEXTAREA'].includes(node.tagName) ||
          (node.tagName === 'INPUT' && !(s.appearance === 'none' && ['radio','checkbox'].includes(node.type))))
        fail('visual_unverifiable:native_control_paint');

      if (node.getAnimations().some(a => a.playState === 'running' || a.pending)) fail('visual_unverifiable:animation');
      pseudo(node);
      if (node !== el && node.contains(el)) {
        const a=node.getBoundingClientRect();
        if ((s.overflowX !== 'visible' || s.overflowY !== 'visible') &&
            ['TopLeft','TopRight','BottomLeft','BottomRight'].some(c => parseFloat(s['border'+c+'Radius']) !== 0))
          fail('visual_unverifiable:rounded_ancestor_clipping');
        if ((s.overflowX !== 'visible' && (rect.left < a.left+node.clientLeft || rect.right > a.left+node.clientLeft+node.clientWidth)) ||
            (s.overflowY !== 'visible' && (rect.top < a.top+node.clientTop || rect.bottom > a.top+node.clientTop+node.clientHeight)))
          fail('visual_unverifiable:ancestor_clipping');
      }
      if (node !== el && el.contains(node) && node.getClientRects().length &&
          (painted(s) || ['IMG','SVG','CANVAS','AUDIO','VIDEO','IFRAME','OBJECT','EMBED'].includes(node.tagName)))
        fail('visual_unverifiable:painted_descendant');
      if (s.textShadow !== 'none' || s.boxShadow !== 'none') fail('visual_unverifiable:shadow_effect');
      if (parseFloat(s.outlineWidth) > 0 && s.outlineStyle !== 'none') fail('visual_unverifiable:outline_effect');
      if ('color' in req.properties && (node === el || el.contains(node))) {
        if (parseFloat(s.fontSize) <= 0 || s.lineHeight === '0px' || parseFloat(s.textIndent) !== 0)
          fail('visual_unverifiable:text_not_reliably_painted');
        if (s.color !== req.properties.color || (s.webkitTextFillColor && s.webkitTextFillColor !== req.properties.color) ||
            parseFloat(s.webkitTextStrokeWidth || '0') !== 0) fail('visual_mismatch:foreground_color');
        for (const which of ['::first-letter','::first-line']) {
          const p=getComputedStyle(node,which);
          if (p.color !== req.properties.color || (p.webkitTextFillColor && p.webkitTextFillColor !== req.properties.color) ||
              parseFloat(p.webkitTextStrokeWidth || '0') !== 0 || p.textShadow !== 'none')
            fail('visual_unverifiable:text_pseudo_paint');
        }
      }
    }
    if (['IMG','SVG','CANVAS','AUDIO','VIDEO','IFRAME','OBJECT','EMBED'].includes(el.tagName) || style.backgroundImage !== 'none' ||
        (style.borderImageSource && style.borderImageSource !== 'none')) fail('visual_unverifiable:non_flat_paint');
    if ('border-color' in req.properties && !['Top','Right','Bottom','Left'].every(side =>
        parseFloat(style['border'+side+'Width']) > 0 && style['border'+side+'Style'] === 'solid'))
      fail('visual_unverifiable:non_solid_or_absent_border');
    if ('color' in req.properties && !(el.innerText || '').trim()) fail('visual_unverifiable:no_rendered_text');
    if (('background-color' in req.properties || 'shape' in req.properties) && style.backgroundClip !== 'border-box')
      fail('visual_unverifiable:background_clip');
    if ('color' in req.properties) {
      const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);
      let text, count=0;
      while ((text=walker.nextNode())) {
        if (!text.textContent.trim()) continue;
        const range=document.createRange(); range.selectNodeContents(text);
        const box=range.getBoundingClientRect(); count++;
        if (box.width <= 0 || box.height <= 0 || !intersects(box,rect)) fail('visual_unverifiable:text_not_reliably_painted');
        for (let a=text.parentElement; a && (a === el || el.contains(a)); a=a.parentElement) {
          const s=getComputedStyle(a), b=a.getBoundingClientRect();
          if ((s.overflowX !== 'visible' && (box.left < b.left+a.clientLeft || box.right > b.left+a.clientLeft+a.clientWidth)) ||
              (s.overflowY !== 'visible' && (box.top < b.top+a.clientTop || box.bottom > b.top+a.clientTop+a.clientHeight)))
            fail('visual_unverifiable:text_clipping');
        }
      }
      if (!count) fail('visual_unverifiable:no_rendered_text');
    }
    if ('shape' in req.properties && !painted(style)) fail('visual_unverifiable:no_shape_paint');
    // Hit tests plus a conservative overlap guard catch pointer-events:none overlays.
    // Any intersecting unrelated paint is unverifiable, even if it may be behind us.
    // This deliberately prefers a refusal over a compositing-based false pass.
    for (const other of document.querySelectorAll('*')) {
      if (other === el || el.contains(other) || other.contains(el)) continue;
      const s=getComputedStyle(other), box=other.getBoundingClientRect();
      // A positioned pseudo may paint outside a zero-sized host; host bounds cannot clear it.
      pseudo(other);
      if (s.display === 'none' || s.visibility !== 'visible' || Number(s.opacity) === 0) continue;
      // Shadows/outlines/filter effects and opaque trees can paint beyond their
      // host border box. Intersection of host boxes cannot disprove occlusion.
      // Conservative rejection is intentional until paint bounds are proven.
      if (s.boxShadow !== 'none' || s.textShadow !== 'none' ||
          (parseFloat(s.outlineWidth) > 0 && !['none','hidden'].includes(s.outlineStyle)) ||
          s.filter !== 'none' || (s.backdropFilter && s.backdropFilter !== 'none'))
        fail('visual_unverifiable:unbounded_external_paint');
      opaqueTree(other);
      if (!intersects(rect,box)) continue;
      replacedContent(s);
      if (painted(s) || (other.textContent || '').trim() || ['IMG','SVG','CANVAS','AUDIO','VIDEO','IFRAME','INPUT','SELECT','TEXTAREA','PROGRESS','METER','BUTTON'].includes(other.tagName))
        fail('visual_unverifiable:overlapping_paint');
      pseudo(other);
    }
    for (const [x,y] of [[.5,.5],[.5,.1],[.5,.9],[.1,.5],[.9,.5]]) {
      const front=document.elementFromPoint(rect.left+rect.width*x,rect.top+rect.height*y);
      if (!front || (front !== el && !el.contains(front))) fail('visual_unverifiable:occluded');
    }
    const radius = raw => {
      const parts=raw.trim().split(/\s+/);
      const px=(part,total) => part.endsWith('%') ? parseFloat(part)*total/100 : part.endsWith('px') ? parseFloat(part) : NaN;
      return [px(parts[0],rect.width),px(parts[1] || parts[0],rect.height)];
    };
    const radii=['TopLeft','TopRight','BottomRight','BottomLeft'].map(c => radius(style['border'+c+'Radius']));
    const scale=Math.min(1,rect.width/(radii[0][0]+radii[1][0]),rect.width/(radii[3][0]+radii[2][0]),
      rect.height/(radii[0][1]+radii[3][1]),rect.height/(radii[1][1]+radii[2][1]));
    const effective=radii.map(pair => pair.map(n => n*scale));
    for (const [prop,want] of Object.entries(req.properties)) {
      if (prop === 'width' || prop === 'height') {
        if (rect[prop] !== parseFloat(want)) fail('visual_mismatch:'+prop);
      } else if (prop === 'border-radius') {
        const r=radius(want), k=Math.min(1,rect.width/(2*r[0]),rect.height/(2*r[1]));
        if (!effective.every(pair => pair[0] === r[0]*k && pair[1] === r[1]*k)) fail('visual_mismatch:border-radius');
      } else if (prop === 'shape') {
        const zero=effective.every(pair => pair[0] === 0 && pair[1] === 0);
        const circle=rect.width === rect.height && effective.every(pair => pair[0] === rect.width/2 && pair[1] === rect.height/2);
        const short=Math.min(rect.width,rect.height);
        const pill=rect.width !== rect.height && effective.every(pair => pair[0] === short/2 && pair[1] === short/2);
        if ((want === 'circle' && !circle) || (want === 'square' && !(zero && rect.width === rect.height)) ||
            (want === 'rectangle' && !zero) || (want === 'pill' && !pill)) fail('visual_mismatch:shape');
      } else if (prop === 'border-color') {
        if (!['Top','Right','Bottom','Left'].every(side => style['border'+side+'Color'] === want)) fail('visual_mismatch:border-color');
      } else if (style.getPropertyValue(prop).trim() !== want) fail('visual_mismatch:'+prop);
    }
    return {issues, observed:{width:rect.width,height:rect.height,backgroundColor:style.backgroundColor,
      color:style.color,radii:effective,viewport:{width:innerWidth,height:innerHeight},deviceScaleFactor:devicePixelRatio},
      scope:'explicit_css_and_border_box_geometry_only_not_screenshot_equivalence'};
  }, {required, shadowInspection});
  expect(observed.issues, JSON.stringify(observed)).toEqual([]);
}
'''
