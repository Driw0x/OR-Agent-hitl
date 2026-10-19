"""Small JSON-first command-line interface."""

import argparse
import json
from pathlib import Path

from .models import Candidate, Extraction, load_json, read_gap


def main():
    parser=argparse.ArgumentParser(description='OR-Agent: exact-integer GAP only')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('schema')
    commands.add_parser('resources')
    commands.add_parser('ollama')
    command=commands.add_parser('web',help='local-only browser interface; existing deterministic/HITL services')
    command.add_argument('--port',type=int,default=8765)
    command.add_argument('--sessions',default='.or-agent-web')
    command.add_argument('--base-url',default='http://127.0.0.1:11434')
    command.add_argument('--import-session',action='append',default=[],help='allow read-only import of this existing HITL session')
    for name in ('validate','solve','verify'):
        command=commands.add_parser(name)
        command.add_argument('problem')
        if name=='verify': command.add_argument('candidate')
        if name=='solve': command.add_argument('--seconds',type=float,default=5)
    command=commands.add_parser('agent')
    command.add_argument('source',help='UTF-8 text file; use - for interactive input')
    command.add_argument('--model',default='qwen2.5-coder:7b')
    command.add_argument('--max-calls',type=int)
    command.add_argument('--total-seconds',type=float,default=180)
    command.add_argument('--solver-seconds',type=float,default=5)
    command.add_argument('--output')
    command=commands.add_parser('route',help='deterministic admission / explicit HITL routing')
    operations=command.add_subparsers(dest='route_operation',required=True)
    operation=operations.add_parser('analyze')
    operation.add_argument('source',help='UTF-8 request file')
    operation=operations.add_parser('submit')
    operation.add_argument('session',help='new session directory')
    operation.add_argument('--source',required=True)
    operation.add_argument('--llm',action='store_true',help='one optional HITL draft call; never used by certified inputs')
    operation.add_argument('--base-url',default='http://localhost:11434')
    operation.add_argument('--seconds',type=float,default=5)
    operation.add_argument('--human-scope',action='store_true',help='allow unknown prose only with explicit GAP scope confirmation')
    for name in ('status','solve','review','validate','scope','approve','reject'):
        operation=operations.add_parser(name)
        operation.add_argument('session')
        if name=='solve': operation.add_argument('--seconds',type=float,default=5)
        if name in ('approve','scope'):
            operation.add_argument('--review-token',required=True)
            operation.add_argument('--confirm')
        if name=='reject': operation.add_argument('--reason',required=True)
    command=commands.add_parser('hitl',help='optional human-confirmed GAP workflow')
    operations=command.add_subparsers(dest='operation',required=True)
    for name in ('propose','review','validate','approve','reject','solve','status'):
        operation=operations.add_parser(name)
        operation.add_argument('session',help='session directory; new directory for propose')
        if name=='propose':
            operation.add_argument('--source',required=True,help='unchanged UTF-8 request file')
            operation.add_argument('--base-url',default='http://localhost:11434')
        if name=='approve':
            operation.add_argument('--review-token',required=True)
            operation.add_argument('--confirm',help='exact APPROVE:<review-token>; otherwise prompted')
        if name=='reject': operation.add_argument('--reason',required=True)
        if name=='solve': operation.add_argument('--seconds',type=float,default=5)
    args=parser.parse_args()
    try:
        if args.command=='web':
            from .web import serve
            serve(args.sessions,args.port,args.base_url,args.import_session)
            return
        if args.command=='route':
            from . import deterministic, hitl, routing
            action=args.route_operation
            if action=='analyze':
                source=Path(args.source).read_text(encoding='utf-8-sig')
                report=deterministic.extract(source)
                output=dict(deterministic=report,certificate=routing.certify(source,report))
            elif action=='submit':
                output=routing.submit(args.session,Path(args.source).read_bytes(),llm=args.llm,
                    base_url=args.base_url,seconds=args.seconds,human_scope=args.human_scope)
            elif action in ('status','solve'):
                output=routing.solve(args.session,args.seconds) if action=='solve' else routing.status(args.session)
            else:
                routing.inspect(args.session)
                if action in ('approve','scope'):
                    prefix='APPROVE' if action=='approve' else 'SCOPE'
                    confirmation=args.confirm if args.confirm is not None else input('Type '+prefix+':<review-token>: ')
                    function=hitl.approve if action=='approve' else hitl.confirm_scope
                    output=function(args.session,args.review_token,confirmation)
                elif action=='reject': output=hitl.reject(args.session,args.reason)
                else: output=getattr(hitl,action)(args.session)
        elif args.command=='hitl':
            from . import hitl
            if args.operation=='propose':
                output=hitl.propose(args.session,Path(args.source).read_bytes(),base_url=args.base_url)
            elif args.operation=='approve':
                confirmation=args.confirm if args.confirm is not None else input('Type APPROVE:<review-token>: ')
                output=hitl.approve(args.session,args.review_token,confirmation)
            elif args.operation=='reject': output=hitl.reject(args.session,args.reason)
            elif args.operation=='solve': output=hitl.solve(args.session,args.seconds)
            else: output=getattr(hitl,args.operation)(args.session)
            if args.operation=='review':
                print('ORIGINAL REQUEST\n'+output.pop('original')+'\n\nPROPOSED FORMULATION\n'+output.pop('proposal'))
                print('\nGAP RULES\n'+'\n'.join(output['constraints'])+'\n\nDIAGNOSTICS AND RECONSTRUCTED MATRICES')
        elif args.command=='schema': output=Extraction.model_json_schema()
        elif args.command in ('resources','ollama'):
            from .resources import snapshot
            output=snapshot()
            if args.command=='ollama': output=output['ollama']
        elif args.command=='agent':
            from .agent import run
            source=input('GAP request: ') if args.source=='-' else Path(args.source).read_text(encoding='utf-8-sig')
            output=run(source,model=args.model,max_calls=args.max_calls,
                       total_seconds=args.total_seconds,solver_seconds=args.solver_seconds)
            if args.output:
                path=Path(args.output)
                path.parent.mkdir(parents=True,exist_ok=True)
                with path.open('x',encoding='utf-8') as file:
                    json.dump(output,file,ensure_ascii=False,indent=2)
        else:
            problem=read_gap(Path(args.problem).read_text(encoding='utf-8-sig'))
            if args.command=='validate': output=dict(valid=True,problem=problem.model_dump(),fidelity='unassessed')
            elif args.command=='verify':
                from .verify import verify
                output=verify(problem,load_json(Path(args.candidate).read_text(encoding='utf-8-sig')))
            else:
                from .solver import solve
                from .verify import verify
                output=solve(problem,args.seconds)
                output['verification']=verify(problem,output['candidate']) if output['candidate'] else None
                output['optimality_proven']=bool(output['solver_proof'] and output['verification'] and output['verification']['valid'])
        print(json.dumps(output,ensure_ascii=False,indent=2))
    except (ValueError,OSError) as error:
        print(json.dumps(dict(outcome='input_or_execution_error',error=str(error)),ensure_ascii=False,indent=2))
        raise SystemExit(2)
