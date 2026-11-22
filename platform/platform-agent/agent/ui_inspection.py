"""Bounded interactions with the isolated application's rendered accessibility tree."""
import json

from .behavior_checks import _fields, _locator, _text


MAX_ACTIONS = 6
_KEYS = ('Enter', 'Tab', 'Escape', 'ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Space')
_OPS = ('click', 'fill', 'press', 'check', 'uncheck', 'reload')
_ROLES = ('button', 'checkbox', 'combobox', 'dialog', 'form', 'grid', 'gridcell', 'group', 'heading',
          'link', 'list', 'listbox', 'listitem', 'main', 'menu', 'menuitem', 'menuitemcheckbox',
          'menuitemradio', 'navigation', 'option', 'radio', 'radiogroup', 'region', 'row', 'rowgroup',
          'search', 'searchbox', 'slider', 'spinbutton', 'switch', 'tab', 'table', 'tablist', 'tabpanel',
          'textbox', 'toolbar', 'tree', 'treeitem')
_ROLE_NAME = dict(type='object', required=['role', 'name'], additionalProperties=False, properties={
    'role': dict(type='string', enum=list(_ROLES)), 'name': dict(type='string', minLength=1, maxLength=200)})
_TARGET = dict(_ROLE_NAME, properties=dict(_ROLE_NAME['properties'], within=_ROLE_NAME))
INSPECT_UI_SCHEMA = dict(type='function', function=dict(name='inspect_ui',
    description='Inspect the isolated current app from its homepage in a fresh browser session. '
                'Use an owned public scenario ID. Omit actions for an initial tree or supply up to six '
                'ordered actions. Use exact accessible roles and names. fill and press require value; '
                'press accepts ' + ', '.join(_KEYS) + '. reload takes no target. The report is an observation.',
    parameters=dict(type='object', required=['scenario_id', 'actions'], additionalProperties=False, properties={
        'scenario_id': dict(type='string', minLength=1, maxLength=200),
        'actions': dict(type='array', maxItems=MAX_ACTIONS, items=dict(type='object', required=['op'],
            additionalProperties=False, properties={'op':dict(type='string', enum=list(_OPS)),
            'target':_TARGET, 'value':dict(type='string', maxLength=500)}))})))


def _role_target(target, *, allow_scope):
    fields = {'role', 'name'}
    if allow_scope and isinstance(target, dict) and 'within' in target:
        fields.add('within')
        _role_target(target['within'], allow_scope=False)
    _fields(target, fields)
    if target['role'] not in _ROLES:
        raise ValueError('unsupported inspection role')
    _text(target['name'], 200)


def validate_inspection(arguments):
    _fields(arguments, {'scenario_id', 'actions'})
    _text(arguments['scenario_id'], 200)
    actions = arguments['actions']
    if not isinstance(actions, list) or len(actions) > MAX_ACTIONS:
        raise ValueError('inspect_ui accepts zero through six actions')
    for action in actions:
        op = action.get('op') if isinstance(action, dict) else None
        if op not in _OPS:
            raise ValueError('unsupported inspection action')
        if op == 'reload':
            _fields(action, {'op'})
            continue
        _fields(action, {'op', 'target', 'value'} if op in ('fill', 'press') else {'op', 'target'})
        _role_target(action['target'], allow_scope=True)
        if op == 'press' and action['value'] not in _KEYS:
            raise ValueError('unsupported inspection key')
        if op == 'fill':
            value = action['value']
            if not isinstance(value, str) or len(value) > 500 or '\x00' in value:
                raise ValueError('expected bounded text input')
    return arguments


def inspection_spec(arguments):
    validate_inspection(arguments)
    actions = []
    for action in arguments['actions']:
        op = action['op']
        if op == 'reload':
            actions.append("() => page.reload({waitUntil:'domcontentloaded', timeout:3000})")
            continue
        target = _locator(action['target'])
        value = json.dumps(action['value']) + ', ' if 'value' in action else ''
        actions.append(f'() => {target}.{op}({value}{{timeout:2500}})')
    return ("const {test}=require('@playwright/test');\nconst fs=require('node:fs');\n"
            "test.use({storageState:{cookies:[],origins:[]},serviceWorkers:'block'});\n"
            + "test('inspect current UI',async({page,baseURL},info)=>{\n"
            + "  const actions=[" + ',\n    '.join(actions) + "];\n"
            + "  const result={scenario_id:" + json.dumps(arguments['scenario_id']) + """,
    initial:'',final:'',completed_actions:0,failed_index:null,error:null};
  const origin=new URL(baseURL).origin;
  const persist=()=>fs.writeFileSync(info.outputPath('ui-inspection.json'),JSON.stringify(result),'utf8');
  const brief=error=>String(error.message || error).slice(0,600);
  function checkOrigin() {
    if(new URL(page.url()).origin!==origin) throw Error('navigation left the application');
  }
  async function snapshot() {
    checkOrigin();
    return (await page.locator('body').ariaSnapshot({timeout:1500})).slice(0,4096);
  }
  persist();
  try {
    await page.context().route('**/*',async route=>{
      if(new URL(route.request().url()).origin!==origin) return route.abort();
      const response=await route.fetch({maxRedirects:0});
      const location=response.headers()['location'];
      if(response.status()>=300 && response.status()<400 && location &&
          new URL(location,route.request().url()).origin!==origin) return route.abort();
      return route.fulfill({response});
    });
    const response=await page.goto('/',{waitUntil:'domcontentloaded',timeout:4000});
    if(!response || !response.ok()) throw Error('homepage could not be loaded');
    result.initial=await snapshot();
    persist();
    for(let i=0;i<actions.length;i++) {
      result.failed_index=i;
      await actions[i]();
      checkOrigin();
      result.completed_actions++;
      result.failed_index=null;
      persist();
    }
  } catch(error) { result.error=brief(error); }
  try { result.final=await snapshot(); }
  catch(error) { if(!result.error) result.error=brief(error); }
  persist();
});
""")
