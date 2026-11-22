"""Exercise reviewed spatial obligations through both public plan entry points."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'platform/platform-agent'))
from agent.behavior_checks import compile_plan
from agent.behavior_contracts import plan_coverage, requirement_contracts
from agent.collection_layout import bind_reviewed_layout, compile_layout_diagnostic
from agent.platform_task_ir import build_task_ir


REF, SID = 'paint', 'arrange-samples'
SWITCH = {'op': 'click', 'target': {'role': 'button', 'name': 'Change arrangement'}}


def fixture(relations=('single_column','multiple_columns'), second_scene=False):
    fields = ['Pigments', 'Violet', 'Blue', 'Sample A', 'Sample B']
    states = [dict(action=SWITCH, relation=r) for r in relations]
    whens = [{'When': 'Click "Change arrangement"(role=button).'} for _ in states]
    thens = [{'Then': f'"{name}"(role=region) is visible.'} for name in fields]
    declarations = {'single_column':'Arrange the samples vertically in a single column without overlap.',
                    'multiple_columns':'The samples occupy distinct columns side by side in the same row without overlap.'}
    given = 'The Pigments gallery contains the Violet sample with its content Sample A and the Blue sample with its content Sample B.'
    steps = [{'Given':given}]
    for action,state in zip(whens,states):
        steps.extend([action,{'Then':declarations[state['relation']]}])
    scenario = dict(id=SID, name='Arrange the samples', steps=[*steps,*thens])
    scenarios = [scenario]
    if second_scene:
        scenarios.append(dict(id='inventory',steps=[{'Then':'"Sample B"(role=region) is visible.'},
                                                    {'Then':'"Stock"(role=region) is visible.'}]))
    with TemporaryDirectory() as root:
        path = Path(root) / 'public.json'
        path.write_text(json.dumps(dict(id=REF, name='Paint inventory', scenarios=scenarios)), encoding='utf-8')
        ir = build_task_ir(path,root)
    raw = ir['scenarios'][0]
    sources = []
    for phase, rows in [('given',[raw['given']]), ('when',raw['when']), ('then',raw['then'])]:
        for i, row in enumerate(rows):
            quote = row['text']
            sources.append(dict(owner=REF,scenario_id=SID,phase=phase,index=i,span=[0,len(quote)],quote=quote))
    members = [dict(role='region', name='Violet', contents=['Sample A']),dict(role='region',name='Blue',contents=['Sample B'])]
    for state in states:
        state['source'] = next(s for s in sources if s['quote'] == declarations[state['relation']])
    member_sources = {}
    for member in members:
        quote = member['name'] + ' sample with its content ' + member['contents'][0]
        start = given.index(quote)
        member_sources[member['name']] = dict(sources[0], span=[start,start+len(quote)],quote=quote)
    review = dict(sources=sources, target=dict(role='region',name='Pigments'), members=members, states=states,
                  member_sources=member_sources)
    bound = bind_reviewed_layout(ir,REF,SID,review)
    obligations = requirement_contracts(ir)[REF]['scenarios'][0]['obligations']
    steps = [dict(op='goto',value='/')]
    state_iter = iter(states)
    for obligation in obligations:
        step = obligation['step']
        steps.append(step)
        if step == SWITCH:
            state = next(state_iter)
            steps.append(dict(op='collection_layout',target=review['target'],value=dict(relation=state['relation'],members=members)))
    case = dict(name='A display alias',requirement_id=REF,requirement_quote=thens[0]['Then'],steps=steps)
    return ir,bound,review,{'cases':[case]}


class LayoutProof(unittest.TestCase):
    def test_single_state_and_repeated_control_compile_when_declared(self):
        for states in [('multiple_columns',),('single_column','multiple_columns')]:
            with self.subTest(states=states):
                ir,bound,review,plan = fixture(states)
                diagnostic = compile_layout_diagnostic(ir,REF,SID,review,plan)
                self.assertIn('await assertCollectionLayout(',diagnostic['source'])
                self.assertTrue(diagnostic['diagnostic_only'])
                plan['cases'][0]['name'] = 'Completely different display name'
                self.assertIn('await assertCollectionLayout(',compile_layout_diagnostic(ir,REF,SID,review,plan)['source'])
                coverage = plan_coverage(plan,bound)
                self.assertNotIn('reviewed_layout_obligations_missing_or_wrong_state',[g['reason'] for g in coverage['missing']])
                proof, = coverage['reviewed_layouts']
                self.assertEqual((proof['status'],proof['execution'],proof['matched_cases']),
                                 ('planned','not_run',['Completely different display name']))
                for state in proof['states']:
                    witness, = state['witnesses']
                    steps = plan['cases'][0]['steps']
                    self.assertEqual(steps[witness['action_index']], state['action'])
                    self.assertEqual(steps[witness['assertion_index']], state['assertion'])
                self.assertTrue(coverage['unresolved'])
                with self.assertRaisesRegex(ValueError,'requirement_constraints_incomplete'): compile_plan(plan,bound)

    def test_dropping_or_weakening_any_state_never_covers_the_review(self):
        ir,bound,review,original = fixture()
        for mutation in ('counts','one_state','wrong_state','lose_member','swap_bodies','other_container','separate_case'):
            with self.subTest(mutation=mutation):
                plan = deepcopy(original)
                case = plan['cases'][0]
                pos = next(i for i,s in enumerate(case['steps']) if s['op']=='collection_layout')
                value = case['steps'][pos]['value']
                if mutation == 'counts': case['steps'][pos] = dict(op='count',target=review['target'],value=1)
                if mutation == 'one_state': case['steps'].pop(pos)
                if mutation == 'wrong_state': value['relation'] = 'multiple_columns'
                if mutation == 'lose_member': value['members'].pop()
                if mutation == 'swap_bodies': value['members'][0]['contents']=['Sample B']
                if mutation == 'other_container': case['steps'][pos]['target'] = dict(role='region',name='Blue')
                if mutation == 'separate_case':
                    second = deepcopy(case)
                    case['steps'].pop(pos)
                    second['name'] += ' 2'
                    second['steps'].pop(pos+2)
                    plan['cases'].append(second)
                coverage = plan_coverage(plan,bound)
                self.assertFalse(coverage['complete'])
                self.assertEqual(coverage['reviewed_layouts'][0]['status'],'missing')
                with self.assertRaises(ValueError): compile_plan(plan,bound)
                with self.assertRaises(ValueError): compile_layout_diagnostic(ir,REF,SID,review,plan)

    def test_wrong_or_repeated_quote_never_proves_the_named_scenario(self):
        ir,bound,review,plan = fixture(second_scene=True)
        for quote in ['"Stock"(role=region) is visible.', '"Sample B"(role=region) is visible.', 'sample with its content',
                      'Click "Change arrangement"(role=button).']:
            with self.subTest(quote=quote):
                plan['cases'][0]['requirement_quote'] = quote
                with self.assertRaises(ValueError): compile_layout_diagnostic(ir,REF,SID,review,plan)
                gaps = plan_coverage(plan,bound)['missing']
                self.assertIn('reviewed_layout_obligations_missing_or_wrong_state',[g['reason'] for g in gaps])
                with self.assertRaises(ValueError): compile_plan(plan,bound)

    def test_source_provenance_and_authority(self):
        ir,bound,review,plan = fixture()
        original = requirement_contracts(ir)[REF]
        self.assertEqual(dict(requirement_contracts(bound)[REF]), original)
        with self.assertRaisesRegex(ValueError,'requires_reviewed_public_assertion'): compile_plan(plan,ir)
        for field,value in [('owner','other'),('span',[1,4]),('quote','fabricated text')]:
            altered = deepcopy(review)
            altered['sources'][0][field] = value
            with self.assertRaises(ValueError): bind_reviewed_layout(ir,REF,SID,altered)

    def test_each_semantic_assertion_has_its_own_source(self):
        ir,_,review,_ = fixture()
        for mutation in ('missing_state_source','swapped_public_record_source','same_result_span','action_as_spatial_result'):
            altered = deepcopy(review)
            if mutation == 'missing_state_source': altered['states'][1].pop('source')
            if mutation == 'swapped_public_record_source': altered['member_sources']['Blue'] = altered['states'][0]['source']
            if mutation == 'same_result_span': altered['states'][1]['source'] = altered['states'][0]['source']
            if mutation == 'action_as_spatial_result': altered['states'][0]['source'] = altered['sources'][1]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): bind_reviewed_layout(ir,REF,SID,altered)

    def test_read_only_observation_can_intervene_but_mutation_cannot(self):
        _,bound,_,plan = fixture()
        case = plan['cases'][0]
        pos = next(i for i,s in enumerate(case['steps']) if s['op']=='collection_layout')
        case['steps'].insert(pos, dict(op='visible',target=dict(role='region',name='Pigments')))
        self.assertEqual(plan_coverage(plan,bound)['reviewed_layouts'][0]['status'],'planned')
        case['steps'][pos] = dict(op='reload')
        self.assertEqual(plan_coverage(plan,bound)['reviewed_layouts'][0]['status'],'missing')


if __name__ == '__main__':
    unittest.main()
