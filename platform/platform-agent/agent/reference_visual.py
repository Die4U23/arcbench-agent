"""Independent public PNGs for exact viewport diagnostics in a frozen journey."""

from __future__ import annotations

import binascii
import hashlib
from pathlib import Path, PurePosixPath
import re
import struct

from .task_bundle import is_test_path

PIXEL_OP = 'reference_pixels'
MAX_ASSET_BYTES = 8 * 1024 * 1024
VIEWPORT_MAX = 4096
_CONTEXT = dict(mode='exact_rgba', scope='viewport', device_scale_factor=1,
                color_scheme='light', locale='en-US', timezone='UTC')
_HEADER = b'\x89PNG\r\n\x1a\n'
_ASSET_NAME = re.compile(r'arc-pixel-([0-9a-f]{64})\.png\Z')
_CAPTURE = set(_CONTEXT) | {'viewport', 'page_path', 'state_id'}


def reference_path(value):
    if not isinstance(value, str) or not value or any(c in value for c in '\\:\x00?#'):
        raise ValueError('pixel_reference_path_invalid')
    relative = PurePosixPath(value)
    if (relative.is_absolute() or '..' in relative.parts or relative.as_posix() != value
            or relative.suffix.lower() != '.png'):
        raise ValueError('pixel_reference_path_invalid')
    if is_test_path(value):
        raise ValueError('pixel_reference_path_excluded_by_input_policy')
    return relative


def png_dimensions(content):
    """Validate the static RGB/RGBA format used by the independent browser decoder."""
    if not isinstance(content, bytes) or len(content) > MAX_ASSET_BYTES or not content.startswith(_HEADER):
        raise ValueError('pixel_reference_png_required')
    position = len(_HEADER)
    dimensions = None
    pixels = False
    while position + 12 <= len(content):
        length, kind = struct.unpack_from('>I4s', content, position)
        end = position + 12 + length
        if end > len(content):
            break
        payload = content[position + 8:end - 4]
        if binascii.crc32(kind + payload) & 0xffffffff != struct.unpack_from('>I', content, end - 4)[0]:
            break
        if dimensions is None and kind != b'IHDR':
            break
        if kind == b'IHDR':
            if dimensions is not None or length != 13:
                break
            width, height, bits, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', payload)
            if (bits != 8 or color not in (2, 6) or compression or filtering or interlace
                    or not 1 <= width <= VIEWPORT_MAX or not 1 <= height <= VIEWPORT_MAX):
                raise ValueError('pixel_reference_png_format_unsupported')
            dimensions = dict(width=width, height=height)
        elif kind == b'IDAT':
            pixels = True
        elif kind == b'IEND' and length == 0 and end == len(content) and pixels:
            return dimensions
        # v30's pixel checker also rejects implicit gamma/profile conversions.
        elif kind not in (b'sRGB',) or payload != b'\0':
            raise ValueError('pixel_reference_png_chunk_unsupported')
        position = end
    raise ValueError('pixel_reference_png_invalid')


def read_reference(root, name):
    relative = reference_path(name)
    base = Path(root).resolve(strict=True)
    path = base
    for part in relative.parts:
        path /= part
        info = path.lstat()
        if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('pixel_reference_path_unsafe')
    if not path.is_file() or not path.resolve().is_relative_to(base):
        raise ValueError('pixel_reference_path_unsafe')
    with path.open('rb') as handle:
        content = handle.read(MAX_ASSET_BYTES + 1)
    return content


def inventory(root, work_items):
    """Pin only published PNGs bearing explicit pixel capture declarations."""
    images = [row for item in work_items for row in item.get('references', [])]
    pins = {}
    for row in images:
        if not isinstance(row, dict) or 'capture' not in row:
            continue
        name = row.get('reference')
        if not isinstance(name, str) or name in pins:
            continue
        try:
            content = read_reference(root, name)
            pins[name] = dict(sha256=hashlib.sha256(content).hexdigest(), viewport=png_dimensions(content))
        except (OSError, ValueError) as error:
            pins[name] = dict(reason=str(error) if isinstance(error, ValueError) else 'pixel_reference_asset_unavailable')
    return pins


def capture_gap_details(image):
    """Identify missing inputs for the existing exact viewport comparator."""
    capture = image.get('capture')
    missing = sorted(_CAPTURE.difference(capture if isinstance(capture, dict) else ()))
    return {'missing_capture_fields': missing} if missing else {}


def _capture(value):
    if not isinstance(value, dict) or set(value) != _CAPTURE:
        raise ValueError('pixel_reference_capture_not_declared')
    if any(value[key] != expected or type(value[key]) is not type(expected) for key, expected in _CONTEXT.items()):
        raise ValueError('pixel_reference_capture_protocol_unsupported')
    viewport = value['viewport']
    if (not isinstance(viewport, dict) or set(viewport) != {'width', 'height'}
            or any(type(dimension) is not int or not 1 <= dimension <= VIEWPORT_MAX
                   for dimension in viewport.values())):
        raise ValueError('pixel_reference_viewport_invalid')
    from .behavior_contracts import public_route
    if public_route(value['page_path']) != value['page_path']:
        raise ValueError('pixel_reference_page_invalid')
    if not isinstance(value['state_id'], str) or not value['state_id'].strip() or len(value['state_id']) > 500:
        raise ValueError('pixel_reference_state_invalid')
    return dict(value, viewport=dict(viewport))


def public_pixel_step(ir, image, *, page_path, state_id, target):
    """Bind a public file to one authored journey checkpoint; missing data blocks."""
    if not isinstance(image, dict) or not isinstance(image.get('page'), str) or not image['page'].strip():
        raise ValueError('pixel_reference_page_not_declared')
    capture = _capture(image.get('capture'))
    if capture['page_path'] != page_path or capture['state_id'] != state_id:
        raise ValueError('pixel_reference_state_or_page_mismatch')
    name = reference_path(image.get('reference')).as_posix()
    pin = ir.get('pixel_reference_assets', {}).get(name)
    if not isinstance(pin, dict) or not isinstance(pin.get('sha256'), str):
        raise ValueError(pin.get('reason', 'pixel_reference_asset_unavailable') if isinstance(pin, dict)
                         else 'pixel_reference_asset_unavailable')
    if pin.get('viewport') != capture['viewport']:
        raise ValueError('pixel_reference_dimensions_do_not_match_viewport')
    from .behavior_contracts import target as normalize_target
    locator = normalize_target(target)
    if locator is None:
        raise ValueError('pixel_reference_state_marker_unavailable')
    return dict(op=PIXEL_OP, target=locator,
                value=dict(reference=name, sha256=pin['sha256'], page=image['page'], capture=capture))


def normalize_value(value):
    if not isinstance(value, dict) or set(value) != {'reference', 'sha256', 'page', 'capture'}:
        raise ValueError('pixel_reference_step_invalid')
    reference_path(value['reference'])
    if not isinstance(value['page'], str) or not value['page'].strip() or len(value['page']) > 500:
        raise ValueError('pixel_reference_page_invalid')
    asset_name(value.get('sha256'))
    return dict(value, capture=_capture(value['capture']))


def asset_name(digest):
    if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
        raise ValueError('pixel_reference_digest_invalid')
    return f'arc-pixel-{digest}.png'


def is_artifact(name):
    return isinstance(name, str) and _ASSET_NAME.fullmatch(name) is not None


def suite_assets(plan, public_assets):
    """Use separate canonical inputs, never files exposed by the scratch app."""
    result = {}
    for case in plan['cases']:
        for step in case['steps']:
            if step['op'] != PIXEL_OP:
                continue
            value = normalize_value(step['value'])
            content = (public_assets or {}).get(value['reference'])
            if not isinstance(content, bytes) or hashlib.sha256(content).hexdigest() != value['sha256']:
                raise ValueError('pixel_reference_frozen_input_mismatch')
            if png_dimensions(content) != value['capture']['viewport']:
                raise ValueError('pixel_reference_dimensions_do_not_match_viewport')
            result[asset_name(value['sha256'])] = content
    return result


# Pixel decoding and comparison happen on a blank page in a separate Chromium
# context. The application cannot supply or run canvas comparison code.
PIXEL_RUNTIME_JS = r'''
const arcPixelFs = require('node:fs');
const arcPixelCrypto = require('node:crypto');
const arcPixelPath = require('node:path');
function arcPixelHash(bytes) { return arcPixelCrypto.createHash('sha256').update(bytes).digest('hex'); }
async function assertReferencePixels(page, locator, reference, file, baseURL, testInfo) {
  const capture = reference.capture;
  const expectedURL = new URL(capture.page_path, baseURL).href;
  const viewport = capture.viewport;
  expect(page.viewportSize(), 'pixel_reference_viewport_mismatch').toEqual(viewport);
  async function verifyState() {
    await expect(page).toHaveURL(expectedURL);
    await expect(locator).toHaveCount(1);
    await expect(locator).toBeVisible();
  }
  const original = arcPixelFs.readFileSync(arcPixelPath.join(__dirname, file));
  expect(arcPixelHash(original), 'pixel_reference_digest_mismatch').toBe(reference.sha256);
  await verifyState();
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all(Array.from(document.images, image => image.decode()));
  });
  const options = {type:'png', fullPage:false, scale:'css', animations:'allow'};
  const observed = await page.screenshot(options);
  await verifyState();
  await page.waitForTimeout(200);
  const stable = await page.screenshot(options);
  await verifyState();
  expect(arcPixelHash(observed), 'pixel_capture_not_stable').toBe(arcPixelHash(stable));
  const browser = page.context().browser();
  if (!browser || browser.browserType().name() !== 'chromium') throw new Error('pixel_chromium_required');
  const isolated = await browser.newContext();
  try {
    const codec = await isolated.newPage();
    const evidence = await codec.evaluate(async ({reference, actual, viewport}) => {
      const decode = async (encoded) => {
        const raw = Uint8Array.from(atob(encoded), char => char.charCodeAt(0));
        const bitmap = await createImageBitmap(new Blob([raw], {type:'image/png'}));
        try {
          if (bitmap.width !== viewport.width || bitmap.height !== viewport.height)
            throw new Error('pixel_dimensions_mismatch');
          const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
          const context = canvas.getContext('2d', {colorSpace:'srgb',willReadFrequently:true});
          context.drawImage(bitmap, 0, 0);
          return context.getImageData(0, 0, bitmap.width, bitmap.height).data;
        } finally { bitmap.close(); }
      };
      const expected = await decode(reference);
      const observed = await decode(actual);
      let different = 0;
      for (let i = 0; i < expected.length; i += 4) {
        if (expected[i+3] !== 255 || observed[i+3] !== 255) throw new Error('pixel_nonopaque_capture_unsupported');
        if (expected[i] !== observed[i] || expected[i+1] !== observed[i+1] || expected[i+2] !== observed[i+2]) different++;
      }
      return {different_pixels:different,total_pixels:expected.length / 4};
    }, {reference:original.toString('base64'),actual:observed.toString('base64'),viewport});
    const receipt = {...evidence, mode:capture.mode, viewport, page_path:capture.page_path,
      state_id:capture.state_id, reference_sha256:reference.sha256, actual_sha256:arcPixelHash(observed),
      browser_version:browser.version()};
    await testInfo.attach('arc-public-reference-pixels', {body:JSON.stringify(receipt),contentType:'application/json'});
    expect(evidence.different_pixels,'pixel_values_mismatch').toBe(0);
  } finally { await isolated.close(); }
}
'''
