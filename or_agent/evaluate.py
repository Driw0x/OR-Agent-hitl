"""Frozen small-corpus experiments and offline, reference-based metric replay."""

import argparse
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import itertools
import json
from pathlib import Path, PureWindowsPath
import platform
import statistics
import sys
from urllib.request import urlopen

from .agent import MODEL, OPTIONS, explanation_valid, run
from .models import Gap, load_json
from .resources import WaitBudget, snapshot
from .solver import optimality_evidence
from .verify import verify


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def replay_path(path):
    """Read sealed OR-Agent data from this checkout, preserving archived bytes."""
    parts = PureWindowsPath(path).parts
    if 'OR-Agent' in parts:
        suffix = parts[parts.index('OR-Agent') + 1:]
        if suffix and suffix[0] in ('experiments', 'data') and '..' not in suffix:
            return Path(__file__).resolve().parents[1].joinpath(*suffix)
    return Path(path)


def enumerate_reference(problem):
    """Independent tiny-instance reference, not the CP-SAT converter/checker."""
    p = Gap.model_validate(problem)
    objectives = []
    if len(p.resources) ** len(p.tasks) > 1_000_000:
        raise ValueError('reference enumeration limited to1000000 assignments')
    for owners in itertools.product(range(len(p.resources)), repeat=len(p.tasks)):
        if all(sum(p.consumption[i][j] for j in range(len(p.tasks)) if owners[j] == i) <= p.capacities[i]
               for i in range(len(p.resources))):
            objectives.append(sum(p.costs[owners[j]][j] for j in range(len(p.tasks))))
    return dict(optimum=min(objectives) if objectives else None, feasible_count=len(objectives),
                optimum_count=objectives.count(min(objectives)) if objectives else 0)


def audit(data):
    cases=data['cases']
    if any(c['split'] not in ('dev','test') or c['disposition'] not in ('ready','clarify','unsupported','invalid') for c in cases):
        raise ValueError('unknown split or disposition')
    if len({c['id'] for c in cases}) != len(cases):
        raise ValueError('duplicate case IDs')
    dev={c['family'] for c in cases if c['split']=='dev'}
    test={c['family'] for c in cases if c['split']=='test'}
    if dev & test: raise ValueError('family leakage across split')
    sources={'dev':set(),'test':set()}
    formulations={'dev':set(),'test':set()}
    aligned={'dev':set(),'test':set()}
    for case in cases:
        sources[case['split']].add(case['source'].strip())
        if case['problem'] is not None:
            numerical={k:case['problem'][k] for k in ('costs','consumption','capacities')}
            formulations[case['split']].add(json.dumps(numerical,sort_keys=True))
            aligned[case['split']].add(json.dumps(canonical(case['problem']),sort_keys=True))
    if sources['dev'] & sources['test'] or formulations['dev'] & formulations['test'] or aligned['dev'] & aligned['test']:
        raise ValueError('source or formulation leakage across split')
    references={}
    for case in cases:
        if case['disposition']=='ready':
            reference=enumerate_reference(case['problem'])
            if reference['optimum']!=case['reference_optimum'] or reference['feasible_count']!=case['reference_feasible_count']:
                raise ValueError(f"incorrect reference {case['id']}")
            references[case['id']]=reference
        elif case['problem'] is not None or case['reference_optimum'] is not None or case['reference_feasible_count'] is not None:
            raise ValueError('non-ready reference cannot contain a valid problem')
    return dict(valid=True,dev_count=sum(c['split']=='dev' for c in cases),
                test_count=sum(c['split']=='test' for c in cases),family_overlap=[],references=references)


def canonical(problem):
    p=Gap.model_validate(problem)
    return dict(tasks=sorted(p.tasks),resources=sorted(p.resources),cost_unit=p.cost_unit,
                resource_unit=p.resource_unit,capacities=dict(zip(p.resources,p.capacities)),
                pairs={f'{resource}/{task}':(p.costs[i][j],p.consumption[i][j])
                       for i,resource in enumerate(p.resources) for j,task in enumerate(p.tasks)})


def assess(case, result):
    attempts=result['attempts']
    first=attempts[0] if attempts else {}
    last=attempts[-1] if attempts else {}
    extracted=result.get('extraction') or {}
    ready=case['disposition']=='ready'
    faithful=False
    if ready and extracted.get('disposition')=='ready':
        faithful=canonical(case['problem'])==canonical(extracted['problem'])
    feasible=ready and case['reference_optimum'] is not None
    reference_check=verify(Gap.model_validate(case['problem']),result['candidate']) if ready and result.get('candidate') else None
    reference_valid=bool(reference_check and reference_check['valid'])
    local_check=verify(Gap.model_validate(extracted['problem']),result['candidate']) if extracted.get('problem') and result.get('candidate') else None
    solver=result.get('solver',{})
    parameters=solver.get('parameters',{})
    proof_valid=bool(local_check and local_check['valid'] and optimality_evidence(
        solver.get('raw_status'),local_check['recomputed_objective'],solver.get('best_bound'),
        parameters.get('absolute_gap_limit'),parameters.get('relative_gap_limit'))
        and solver.get('objective')==local_check['recomputed_objective'])
    successful=bool(feasible and faithful and reference_valid)
    optimal=bool(successful and result['optimality_proven'] and proof_valid and result['candidate']['objective']==case['reference_optimum'])
    infeasible=bool(ready and not feasible and faithful and result['outcome']=='infeasible')
    disposition_correct=(extracted.get('disposition')==case['disposition'])
    correct=bool((successful or infeasible) if ready else result['outcome']==case['disposition'])
    false_validity=bool(result['outcome']=='verified' and not (local_check and local_check['valid']))
    false_optimality=bool(result['optimality_proven'] and not proof_valid)
    corrected=len(attempts)>1
    initial_valid=first.get('structural_valid',False)
    token_values=[a.get('response') or {} for a in attempts]
    def summed(key):
        values=[r.get(key) for r in token_values]
        return sum(values) if values and all(v is not None for v in values) else None
    category=None
    if not correct:
        if result['outcome'] in ('execution_error','resource_blocked'): category=result['outcome']
        elif not attempts or 'budget' in result.get('reason','') and not last.get('raw'): category='budget'
        elif not last.get('json_valid'): category='parser'
        elif not last.get('structural_valid'): category='representation_or_provenance'
        elif not faithful and ready: category='extraction_or_translation'
        elif not ready: category='disposition'
        elif not result.get('verification',{}).get('valid') and result.get('candidate'): category='checking'
        else: category='solving'
    consistent=explanation_valid(result) and not false_validity and not false_optimality
    if not consistent: category='explanation'
    return dict(json_first=bool(first.get('json_valid')),json_last=bool(last.get('json_valid')),
                structure_first=bool(initial_valid),structure_last=bool(last.get('structural_valid')),
                ready_reference=ready,feasible_reference=feasible,infeasible_reference=ready and not feasible,
                faithful=faithful,disposition_correct=disposition_correct,answer_correct=correct,
                verified_feasible=successful,proven_optimal=optimal,correct_infeasible=infeasible,
                reference_check=reference_check,reference_violation_count=len(reference_check['violations']) if reference_check else 0,
                false_validity_claim=false_validity,false_optimality_claim=false_optimality,
                reference_answer_mismatch=bool(result['outcome'] in ('verified','infeasible') and not correct),
                explanation_consistent=consistent,correction_attempted=corrected,
                correction_success=bool(corrected and correct),correction_regression=bool(corrected and initial_valid and not correct),
                initial_structure_valid=bool(initial_valid),regression_eligible=bool(corrected and initial_valid),
                input_tokens=summed('prompt_eval_count'),output_tokens=summed('eval_count'),
                load_seconds=(summed('load_duration')/1e9 if summed('load_duration') is not None else None),
                generation_seconds=(summed('eval_duration')/1e9 if summed('eval_duration') is not None else None),
                solver_seconds=result.get('solver',{}).get('solver_wall_seconds'),
                llm_calls=len(attempts),tool_calls=sum(o['tool'] in ('validate','solve','verify') for o in result['observations']),
                outcome=result['outcome'],error_category=category,elapsed_seconds=result['elapsed_seconds'])


def summarize(header, records):
    groups={method:[] for method in header['methods']}
    seen=set()
    for record in records:
        index=record['index']
        if type(index) is not int or not 0 <= index < len(header['planned']) or index in seen:
            raise ValueError('duplicate or unplanned result index')
        expected=header['planned'][index]
        if any(record.get(k)!=expected[k] for k in ('case_id','method')):
            raise ValueError('result does not match planned case/method')
        seen.add(index)
        groups[record['method']].append(record['metrics'])
    summaries={}
    for method,rows in groups.items():
        expected=sum(p['method']==method for p in header['planned'])
        def rate(key,eligible=None):
            if eligible in ('ready_reference','feasible_reference','infeasible_reference'):
                denominator=sum(p[eligible] for p in header['planned'] if p['method']==method)
            else:
                denominator=(sum(r[eligible] for r in rows) if eligible else expected)
            return dict(count=sum(bool(r[key]) for r in rows),denominator=denominator)
        summary={k:rate(k,e) for k,e in [
            ('json_first',None),('json_last',None),('structure_first',None),('structure_last',None),
            ('faithful','ready_reference'),('disposition_correct',None),('answer_correct',None),
            ('verified_feasible','feasible_reference'),('proven_optimal','feasible_reference'),
            ('correct_infeasible','infeasible_reference'),('false_validity_claim',None),
            ('false_optimality_claim',None),('reference_answer_mismatch',None),('explanation_consistent',None),
            ('correction_success','correction_attempted'),('correction_regression','regression_eligible')]}
        summary.update(planned=expected,completed=len(rows),missing=expected-len(rows),
                       reference_violations=sum(r['reference_violation_count'] for r in rows),
                       corrections=sum(r['correction_attempted'] for r in rows),
                       outcomes={o:sum(r['outcome']==o for r in rows) for o in sorted({r['outcome'] for r in rows})},
                       errors={e:sum(r['error_category']==e for r in rows) for e in sorted({r['error_category'] for r in rows if r['error_category']})})
        for key in ('elapsed_seconds','input_tokens','output_tokens','load_seconds','generation_seconds','solver_seconds','llm_calls','tool_calls'):
            values=[r[key] for r in rows if r[key] is not None]
            summary[key]=dict(sum=sum(values) if values else None,mean=statistics.mean(values) if values else None,
                              min=min(values) if values else None,max=max(values) if values else None,available=len(values))
        summaries[method]=summary
    return dict(methods=summaries,protocol='one exploratory repetition; no population uncertainty estimate',
                false_claim_scope='claims about extracted model; reference mismatches reported separately',
                missing_measurements='null values retained; missing runs never count as successes')


def freeze(data_path,output):
    data=load_json(Path(data_path).read_text(encoding='utf-8'))
    checked=audit(data)
    hardware=snapshot()
    with urlopen('http://localhost:11434/api/version',timeout=3) as response:
        server=json.load(response)
    installed=next(m for m in hardware['ollama']['models'] if m['name']==MODEL)
    manifest=dict(timestamp_utc=datetime.now(timezone.utc).isoformat(),dataset=str(Path(data_path).resolve()),
                  dataset_sha256=digest(data_path),source_hashes={str(p):digest(p) for p in sorted(Path('or_agent').glob('*.py'))},
                  model=MODEL,model_digest=installed['digest'],ollama_version=server,options=OPTIONS,
                  software={name:version(name) for name in ('ortools','pydantic','pytest')},python=sys.version,
                  platform=platform.platform(),hardware=hardware,audit=checked,
                  repetitions=1,seed=17,methods=['json','structured','structured-repair'],total_seconds=180,solver_seconds=5,
                  rerun_case=next(c['id'] for c in data['cases'] if c['split']=='test'))
    Path(output).parent.mkdir(parents=True,exist_ok=True)
    with open(output,'x',encoding='utf-8') as file: json.dump(manifest,file,indent=2)
    return manifest


def campaign(manifest_path,output,method='all',split='test',rerun=False):
    manifest=load_json(Path(manifest_path).read_text(encoding='utf-8'))
    if digest(manifest['dataset'])!=manifest['dataset_sha256']:
        raise ValueError('dataset changed after freeze')
    for path,expected in manifest['source_hashes'].items():
        if digest(path)!=expected: raise ValueError(f'source changed after freeze: {path}')
    identity=snapshot()
    installed=next((m for m in identity['ollama']['models'] if m['name']==manifest['model']),None)
    if not installed or installed['digest']!=manifest['model_digest']:
        raise ValueError('model identity differs from frozen protocol')
    data=load_json(Path(manifest['dataset']).read_text(encoding='utf-8'))
    cases=[c for c in data['cases'] if (c['id']==manifest['rerun_case'] if rerun else c['split']==split)]
    methods=['json','structured','structured-repair'] if method=='all' else [method]
    planned=[dict(case_id=c['id'],method=m,ready_reference=c['disposition']=='ready',
                  feasible_reference=c['disposition']=='ready' and c['reference_optimum'] is not None,
                  infeasible_reference=c['disposition']=='ready' and c['reference_optimum'] is None) for i,c in enumerate(cases)
             for m in (methods[i%len(methods):]+methods[:i%len(methods)])]
    header=dict(kind='campaign',manifest=str(Path(manifest_path).resolve()),manifest_sha256=digest(manifest_path),
                methods=methods,split=split,rerun=rerun,planned=planned,initial_resources=identity,
                timestamp_utc=datetime.now(timezone.utc).isoformat())
    Path(output).parent.mkdir(parents=True,exist_ok=True)
    budget=WaitBudget()
    with open(output,'x',encoding='utf-8') as file:
        def write(record):
            file.write(json.dumps(record,ensure_ascii=False)+'\n'); file.flush()
        write(header)
        for index,item in enumerate(planned):
            case=next(c for c in cases if c['id']==item['case_id'])
            write(dict(kind='start',index=index,**item))
            result=run(case['source'],item['method'],manifest['model'],
                       total_seconds=manifest['total_seconds'],solver_seconds=manifest['solver_seconds'],wait_budget=budget)
            record=dict(kind='result',index=index,**item,result=result,metrics=assess(case,result))
            write(record)
            print(f"{index+1}/{len(planned)} {case['id']} {item['method']} {result['outcome']} {result['elapsed_seconds']:.2f}s",flush=True)
        write(dict(kind='end',final_resources=snapshot(),campaign_wait_seconds=budget.used_seconds))


def replay(path):
    records=[load_json(line) for line in Path(path).read_text(encoding='utf-8').splitlines()]
    header=records[0]
    manifest_path=replay_path(header['manifest'])
    manifest=load_json(manifest_path.read_text(encoding='utf-8'))
    dataset_path=replay_path(manifest['dataset'])
    if digest(manifest_path)!=header['manifest_sha256'] or digest(dataset_path)!=manifest['dataset_sha256']:
        raise ValueError('replay provenance hash mismatch')
    data=load_json(dataset_path.read_text(encoding='utf-8'))
    results=[]
    for record in records:
        if record['kind']=='result':
            case=next(c for c in data['cases'] if c['id']==record['case_id'])
            recomputed=assess(case,record['result'])
            if recomputed!=record['metrics']: raise ValueError('saved metric differs from replay')
            results.append(record)
    return summarize(header,results)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('audit');p.add_argument('--data',default='data/benchmark.json')
    p=sub.add_parser('freeze');p.add_argument('--data',default='data/benchmark.json');p.add_argument('--output',required=True)
    p=sub.add_parser('run');p.add_argument('--manifest',required=True);p.add_argument('--output',required=True)
    p.add_argument('--strategy',choices=['json','structured','structured-repair','all'],default='all');p.add_argument('--split',choices=['dev','test'],default='test');p.add_argument('--rerun',action='store_true')
    p=sub.add_parser('metrics');p.add_argument('records');p.add_argument('--output')
    args=parser.parse_args()
    if args.command=='audit': result=audit(load_json(Path(args.data).read_text(encoding='utf-8')))
    elif args.command=='freeze': result=freeze(args.data,args.output)
    elif args.command=='run': campaign(args.manifest,args.output,args.strategy,args.split,args.rerun);return
    else:
        result=replay(args.records)
        if args.output:
            with open(args.output,'x',encoding='utf-8') as file: json.dump(result,file,indent=2)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
