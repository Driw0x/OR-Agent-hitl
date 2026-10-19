"""Frozen development model selection and a separate, resumable GAP experiment."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from .agent import INSTRUCTION, OPTIONS, run
from .extended_corpus import audit
from .extended_metrics import assess, summarize
from .evaluate import replay_path
from .models import Extraction, load_json
from .resources import WaitBudget, gate, snapshot


ROOT = Path(__file__).resolve().parent.parent
RANKING = ['final_answer_correct', 'semantic_fidelity', 'strict_fidelity_raw',
           'numerical_fidelity', 'provenance_last', 'json_last']
REPETITION_RULE = ('Run repetitions 2 and 3 only after all 300 primary observations complete, '
                   'with zero execution errors, timeouts or resource refusals, and '
                   '3 * primary summed run elapsed seconds <= 10800. Scores are not consulted.')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return load_json(Path(path).read_text(encoding='utf-8'))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as file:
        json.dump(value, file, ensure_ascii=False, indent=2, allow_nan=False)
    return value


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def source_hashes():
    return {str(path): digest(path) for path in sorted((ROOT / 'or_agent').glob('*.py'))}


def planned(case, model, method, repetition=1):
    ready = case['disposition'] == 'ready'
    return dict(case_id=case['id'], method=method, model=model['name'], model_digest=model['digest'],
                repetition=repetition, ready_reference=ready,
                feasible_reference=ready and case['reference_optimum'] is not None,
                infeasible_reference=ready and case['reference_optimum'] is None)


def freeze_selection(data_path, output, base_url='http://localhost:11435'):
    if Path(output).exists():
        raise FileExistsError(output)
    data = read(data_path)
    checked = audit(data, historical=read(ROOT / 'data' / 'benchmark.json'))
    hardware = snapshot(base_url)
    if not hardware.get('ollama', {}).get('available'):
        raise ValueError('Ollama inventory unavailable; cannot freeze candidates')
    models = [m for m in hardware['ollama']['models']
              if 'completion' in m.get('capabilities', ['completion'])]
    if not models or any(not m.get('digest') or type(m.get('size')) is not int for m in models):
        raise ValueError('completion candidates need exact digest and size')
    models = sorted(models, key=lambda m: (m['size'], m['name']))
    cases = [c for c in data['cases'] if c['split'] == 'dev']
    plan = [planned(c, m, 'structured') for m in models for c in cases]
    with urlopen(base_url.rstrip('/') + '/api/version', timeout=3) as response:
        server = json.load(response)
    manifest = dict(kind='extended_selection', timestamp_utc=timestamp(),
                    dataset=str(Path(data_path).resolve()), dataset_sha256=digest(data_path), audit=checked,
                    source_hashes=source_hashes(), base_url=base_url.rstrip('/'),
                    owned_server=base_url.rstrip('/') in ('http://localhost:11435', 'http://127.0.0.1:11435'),
                    models=models, planned=plan, methods=['structured'], split='dev', options=OPTIONS.copy(),
                    instruction_sha256=hashlib.sha256(INSTRUCTION.encode()).hexdigest(),
                    schema_sha256=hashlib.sha256(json.dumps(Extraction.model_json_schema(), sort_keys=True).encode()).hexdigest(),
                    total_seconds=180, solver_seconds=5, campaign_seconds=21600,
                    waiting=dict(interval_seconds=60, operation_seconds=600, total_seconds=1800),
                    ranking=RANKING, tie_break=['median_generation_seconds', 'model_size_bytes', 'name'],
                    selection_rule='Lexicographic descending counts on all 30 development cases; '
                                   'only complete candidates without execution/resource failures are eligible. '
                                   'Then ascending median summed generation seconds, weight size, name. '
                                   'One observation per candidate/case. No held-out score is used.',
                    repetition_rule=REPETITION_RULE, hardware=hardware, ollama_version=server,
                    software={name: version(name) for name in ('ortools', 'pydantic', 'pytest')},
                    python=sys.version, platform=platform.platform())
    return save(output, manifest)


def verify_manifest(path):
    manifest = read(path)
    if digest(manifest['dataset']) != manifest['dataset_sha256']:
        raise ValueError('dataset changed after freeze')
    for source, expected in manifest['source_hashes'].items():
        if digest(source) != expected:
            raise ValueError(f'source changed after freeze: {source}')
    if manifest['options'] != OPTIONS or manifest['instruction_sha256'] != hashlib.sha256(INSTRUCTION.encode()).hexdigest():
        raise ValueError('production prompt/options differ from frozen protocol')
    if manifest['schema_sha256'] != hashlib.sha256(json.dumps(Extraction.model_json_schema(), sort_keys=True).encode()).hexdigest():
        raise ValueError('production schema differs from frozen protocol')
    for name in ('selection_manifest', 'selection_records'):
        if name in manifest and digest(manifest[name]) != manifest[name + '_sha256']:
            raise ValueError(f'{name} changed after test freeze')
    return dict(valid=True, manifest_sha256=digest(path), dataset_sha256=manifest['dataset_sha256'],
                source_files=len(manifest['source_hashes']), kind=manifest['kind'])


def records(path):
    rows = [load_json(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]
    if not rows or rows[0].get('kind') != 'campaign':
        raise ValueError('missing campaign header')
    header = rows[0]
    manifest_path = replay_path(header['manifest'])
    if digest(manifest_path) != header['manifest_sha256']:
        raise ValueError('campaign manifest hash mismatch')
    manifest = read(manifest_path)
    dataset_path = replay_path(manifest['dataset'])
    if digest(dataset_path) != manifest['dataset_sha256']:
        raise ValueError('campaign dataset hash mismatch')
    cases = {c['id']: c for c in read(dataset_path)['cases']}
    expected_phase = 'selection' if manifest['kind'] == 'extended_selection' else 'test'
    frozen_plan = manifest['planned']
    if header['phase'] == 'repetitions' and manifest['kind'] == 'extended_test':
        expected_phase, frozen_plan = 'repetitions', manifest['repetition_plan']
    if header['phase'] != expected_phase or header['split'] != manifest['split']:
        raise ValueError('campaign phase/split differs from frozen protocol')
    methods = header['methods']
    if not methods or any(method not in manifest['methods'] for method in methods):
        raise ValueError('campaign methods differ from frozen protocol')
    if header['planned'] != [item for item in frozen_plan if item['method'] in methods]:
        raise ValueError('campaign plan differs from frozen protocol')
    if expected_phase == 'repetitions':
        condition = header.get('repetition_condition') or {}
        primary_path = condition.get('primary_records')
        if not primary_path or digest(replay_path(primary_path)) != condition.get('primary_records_sha256'):
            raise ValueError('primary records changed after repetition admission')
    results = {}
    for row in rows[1:]:
        if row.get('kind') != 'result':
            continue
        index = row['index']
        if type(index) is not int or not 0 <= index < len(header['planned']) or index in results:
            raise ValueError('duplicate or unplanned result index')
        if any(row.get(k) != v for k, v in header['planned'][index].items()):
            raise ValueError('result identity differs from frozen plan')
        result = row['result']
        for key, expected in [('source', cases[row['case_id']]['source']), ('model', row['model']),
                              ('method', row['method']), ('options', manifest['options'])]:
            if key in result and result[key] != expected:
                raise ValueError(f'result {key} differs from frozen protocol')
        if assess(cases[row['case_id']], row['result']) != row['metrics']:
            raise ValueError('saved metrics differ from offline replay')
        results[index] = row
    return header, rows, results, list(cases.values())


def replay(path):
    header, rows, results, cases = records(path)
    if header['phase'] == 'selection':
        candidates = sorted({item['model'] for item in header['planned']})
        grouped = {}
        for candidate in candidates:
            indices = [i for i, item in enumerate(header['planned']) if item['model'] == candidate]
            plan = [header['planned'][i] for i in indices]
            observed = [dict(results[i], index=indices.index(i)) for i in indices if i in results]
            grouped[candidate] = summarize(dict(header, planned=plan), observed, cases)
        output = dict(candidates=grouped)
    else:
        output = summarize(header, list(results.values()), cases)
    output.update(phase=header['phase'], records_sha256=digest(path),
                  end_record_present=any(r.get('kind') == 'end' for r in rows),
                  planned_observations=len(header['planned']), completed_observations=len(results))
    return output


def select(manifest_path, selection_records, output=None):
    verify_manifest(manifest_path)
    manifest = read(manifest_path)
    header, _, results, _ = records(selection_records)
    if manifest['kind'] != 'extended_selection' or header['phase'] != 'selection' or header['manifest_sha256'] != digest(manifest_path):
        raise ValueError('selection requires the frozen development campaign')
    ranking = []
    for model in manifest['models']:
        rows = [row for row in results.values() if row['model'] == model['name']]
        expected = sum(item['model'] == model['name'] for item in manifest['planned'])
        failures = [r['result']['outcome'] for r in rows
                    if r['result']['outcome'] in ('execution_error', 'resource_blocked')]
        eligible = len(rows) == expected and not failures
        values = [r['metrics'].get('generation_seconds') for r in rows]
        median = statistics.median(values) if values and all(v is not None for v in values) else None
        ranking.append(dict(model=model['name'], digest=model['digest'], size_bytes=model['size'],
                            planned=expected, completed=len(rows), eligible=eligible,
                            status='complete' if eligible else (failures[0] if failures else 'incomplete'),
                            counts={k: sum(r['metrics'].get(k) is True for r in rows) for k in manifest['ranking']},
                            generation_median_seconds=median))
    def key(row):
        return tuple(-row['counts'][k] for k in manifest['ranking']) + (
            row['generation_median_seconds'] if row['generation_median_seconds'] is not None else float('inf'),
            row['size_bytes'], row['model'])
    ranking.sort(key=lambda row: (not row['eligible'], key(row)))
    if any(row['status'] == 'incomplete' for row in ranking):
        raise ValueError('model selection is unfinished; complete every candidate or record its resource/execution refusal')
    eligible = [row for row in ranking if row['eligible']]
    if not eligible:
        raise ValueError('no model has completed the 30-case development evaluation')
    result = dict(selected=eligible[0], ranking=ranking, comparable_candidates=len(eligible),
                  comparison='comparative development benchmark' if len(eligible) > 1 else 'one eligible candidate; no comparative superiority established',
                  uncertainty='One development observation per case/model; lexicographic tie-break is not statistical evidence.',
                  rule=manifest['selection_rule'], selection_manifest_sha256=digest(manifest_path),
                  selection_records_sha256=digest(selection_records))
    return save(output, result) if output else result


def freeze_test(selection_manifest, selection_records, output):
    choice = select(selection_manifest, selection_records)
    original = read(selection_manifest)
    model = next(m for m in original['models'] if m['name'] == choice['selected']['model'])
    cases = [c for c in read(original['dataset'])['cases'] if c['split'] == 'test']
    plan = [planned(case, model, method) for index, case in enumerate(cases)
            for method in (['json', 'structured', 'structured-repair'][index % 3:] + ['json', 'structured', 'structured-repair'][:index % 3])]
    repeats = [dict(item, repetition=repetition) for repetition in (2, 3) for item in plan]
    manifest = dict(original, kind='extended_test', timestamp_utc=timestamp(), split='test',
                    models=[model], methods=['json', 'structured', 'structured-repair'], planned=plan, repetition_plan=repeats,
                    selection=choice, selection_manifest=str(Path(selection_manifest).resolve()),
                    selection_manifest_sha256=digest(selection_manifest),
                    selection_records=str(Path(selection_records).resolve()),
                    selection_records_sha256=digest(selection_records))
    return save(output, manifest)


@contextmanager
def campaign_lock():
    """OS releases the lock if this process exits, including an interrupted campaign."""
    path = ROOT / 'experiments' / 'extended' / '.campaign.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as file:
        if file.tell() == 0:
            file.write(b'0')
            file.flush()
        file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError('another extended campaign owns the experiment lock') from error
        try:
            yield
        finally:
            file.seek(0)
            if os.name == 'nt':
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def repeat_eligibility(primary_records, manifest_path):
    header, _, completed, _ = records(primary_records)
    if header['phase'] != 'test' or header['manifest_sha256'] != digest(manifest_path):
        raise ValueError('primary records do not match this test protocol')
    if len(header['planned']) != 300 or len(completed) != 300:
        raise ValueError('repetitions require all 300 primary observations')
    failed = any(row['result']['outcome'] in ('execution_error', 'resource_blocked')
                 or 'timeout' in row['result'].get('reason', '').lower()
                 or 'elapsed_budget' in row['result'].get('reason', '') for row in completed.values())
    projected = 3 * sum(row['result']['elapsed_seconds'] for row in completed.values())
    if failed or projected > 10800:
        raise ValueError(f'predefined repetition condition fails: failure={failed}, projected_seconds={projected}')
    return dict(eligible=True, rule=REPETITION_RULE, primary_records=str(Path(primary_records).resolve()),
                primary_records_sha256=digest(primary_records), projected_900_run_seconds=projected)


def host_ollama():
    """Read server/runner PIDs and listening ports; no process or service is changed."""
    if os.name != 'nt':
        return dict(valid=False, error='native process ownership unavailable', processes=[], listeners=[])
    script = "$ErrorActionPreference='Stop'; $p=@(Get-CimInstance Win32_Process -Filter \"Name LIKE 'ollama%'\"); " \
             "$l=@(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | " \
             "Where-Object { $_.LocalPort -eq 11434 -or $_.OwningProcess -in $p.ProcessId }); " \
             "@{processes=@($p | Select-Object ProcessId,ParentProcessId,CommandLine); " \
             "listeners=@($l | Select-Object LocalPort,OwningProcess)} | ConvertTo-Json -Compress -Depth 4"
    try:
        completed = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                   capture_output=True, text=True, timeout=10, creationflags=0x08000000)
        if completed.returncode:
            raise ValueError(completed.stderr.strip())
        return dict(load_json(completed.stdout), valid=True)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return dict(valid=False, error=str(error), processes=[], listeners=[])


def foreign_activity():
    """Residence outside our dedicated server means concurrent-load uncertainty."""
    host = host_ollama()
    pid_file = ROOT / 'experiments' / 'extended' / 'ollama-server.pid'
    owned = int(pid_file.read_text().strip()) if pid_file.exists() else None
    foreign = [p for p in host['processes'] if p['ProcessId'] != owned
               and p['ParentProcessId'] != owned and any(word in (p.get('CommandLine') or '').lower()
                                                        for word in (' serve', ' runner', 'llama_server'))]
    ports = {11434}
    ports.update(item['LocalPort'] for item in host['listeners']
                 if any(p['ProcessId'] == item['OwningProcess'] for p in foreign))
    endpoints = []
    unknown = not host['valid'] or any(not p.get('CommandLine') and p['ProcessId'] != owned
                                      for p in host['processes'])
    active = any('serve' not in (p.get('CommandLine') or '').lower() for p in foreign)
    for port in sorted(ports):
        if host['valid'] and not any(item['LocalPort'] == port for item in host['listeners']):
            continue
        endpoint = f'http://127.0.0.1:{port}/api/ps'
        try:
            with urlopen(endpoint, timeout=2) as response:
                payload = json.load(response)
            if not isinstance(payload.get('models'), list):
                raise ValueError('shared Ollama ps has no models array')
            endpoints.append(dict(endpoint=endpoint, models=payload['models']))
            active |= bool(payload['models'])
        except (OSError, ValueError) as error:
            reason = error.reason if isinstance(error, URLError) else error
            refused = isinstance(reason, ConnectionRefusedError) or getattr(reason, 'winerror', None) == 10061
            unknown |= not refused
            endpoints.append(dict(endpoint=endpoint, error=f'{type(error).__name__}: {error}'))
    if any(not any(l['OwningProcess'] == p['ProcessId'] for l in host['listeners']) for p in foreign):
        unknown = True
    return dict(active=True if active else (None if unknown else False), host=host,
                foreign_processes=foreign, endpoints=endpoints)


def admission(model, base_url, budget, deadline):
    checks = []
    waited = 0
    while True:
        current = foreign_activity()
        checks.append(current)
        if current['active'] is False:
            result = gate(model, base_url, wait_budget=budget, deadline=deadline)
            result['foreign_server_checks'] = checks
            result['foreign_wait_seconds'] = waited
            return result
        allowance = min(budget.interval_seconds, budget.operation_seconds - waited,
                        budget.total_seconds - budget.used_seconds, deadline - time.monotonic())
        if allowance <= 0:
            return dict(allowed=False, reason='foreign_ollama_busy' if current['active'] else 'foreign_ollama_activity_unknown',
                        foreign_server_checks=checks, wait_seconds=waited, campaign_wait_seconds=budget.used_seconds)
        started = time.monotonic()
        time.sleep(allowance)
        charged = max(allowance, time.monotonic() - started)
        waited += charged
        budget.used_seconds = min(budget.total_seconds, budget.used_seconds + charged)


def _execute(manifest_path, output, *, resume=False, pilot=False, limit=None, method='all',
             repetitions=1, primary_records=None, idle_verified=False):
    verify_manifest(manifest_path)
    manifest = read(manifest_path)
    selection_phase = manifest['kind'] == 'extended_selection'
    if not selection_phase and manifest['kind'] != 'extended_test':
        raise ValueError('unknown experiment protocol')
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError('limit must be a nonnegative integer')
    if repetitions not in (1, 3):
        raise ValueError('repetitions must be 1 or 3')
    repeat_decision = None
    if repetitions == 3:
        if selection_phase or not primary_records or method != 'all':
            raise ValueError('repetitions need --primary-records and all three methods')
        repeat_decision = repeat_eligibility(primary_records, manifest_path)
    plan = manifest['repetition_plan'] if repetitions == 3 else manifest['planned']
    plan = [p for p in plan if method == 'all' or p['method'] == method]
    phase = 'selection' if selection_phase else ('repetitions' if repetitions == 3 else 'test')
    cases = {c['id']: c for c in read(manifest['dataset'])['cases']}
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with campaign_lock():
        if resume:
            header, rows, completed, _ = records(path)
            if header['manifest_sha256'] != digest(manifest_path) or header['planned'] != plan or header['phase'] != phase:
                raise ValueError('resume protocol or plan differs')
            if len(completed) == len(plan):
                return dict(phase=phase, completed=len(completed), planned=len(plan),
                            reason='already_complete', output=str(path.resolve()))
            started = {row['index'] for row in rows if row.get('kind') == 'start'}
            transport_failure = any(row['result']['outcome'] == 'execution_error'
                                    or 'elapsed_budget' in row['result'].get('reason', '') for row in completed.values())
            transport_failure |= any(row.get('reason') == 'unload_execution_error' for row in rows)
            if (started - completed.keys() or transport_failure) and not idle_verified:
                raise ValueError('unfinished/failed request has unknown server activity; verify the dedicated server idle, then use --idle-verified')
        else:
            if path.exists():
                raise FileExistsError(path)
            identity = snapshot(manifest['base_url'])
            header = dict(kind='campaign', timestamp_utc=timestamp(), phase=phase,
                          manifest=str(Path(manifest_path).resolve()), manifest_sha256=digest(manifest_path),
                          dataset_sha256=manifest['dataset_sha256'], methods=sorted({p['method'] for p in plan}),
                          split=manifest['split'], planned=plan, total_planned=len(plan),
                          primary_repetition=2 if repetitions == 3 else 1,
                          repetition_condition=repeat_decision, initial_resources=identity)
            rows, completed = [], {}
        used_wait = max([r.get('campaign_wait_seconds', 0) for r in rows] + [0])
        used_time = sum(r['result']['elapsed_seconds'] for r in completed.values())
        budget = WaitBudget(used_seconds=used_wait)
        blocked = {r['model'] for r in completed.values() if r['result']['outcome'] == 'resource_blocked'}
        prior_rows = rows
        if not rows and 'selection_records' in manifest:
            _, prior_rows, _, _ = records(manifest['selection_records'])
        old_calls = [r for r in prior_rows if r.get('kind') == 'result' and r['result'].get('attempts')]
        owned = old_calls[-1]['model'] if old_calls else None
        launched = 0
        session_started = time.monotonic()
        with path.open('a' if resume else 'x', encoding='utf-8') as file:
            def write(row):
                file.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
                file.flush()
            if not resume:
                write(header)
            write(dict(kind='session', timestamp_utc=timestamp(), pid=os.getpid(), idle_verified=idle_verified))
            reason = 'complete'
            for index, item in enumerate(plan):
                if index in completed:
                    continue
                if limit is not None and launched >= limit:
                    reason = 'invocation_limit'; break
                if pilot and any(r['model'] == item['model'] for r in completed.values()):
                    continue
                if selection_phase and item['model'] in blocked:
                    continue
                if used_time + time.monotonic() - session_started + manifest['total_seconds'] > manifest['campaign_seconds']:
                    reason = 'campaign_time_budget'; break
                current = snapshot(manifest['base_url'])
                installed = next((m for m in current.get('ollama', {}).get('models', []) if m['name'] == item['model']), None)
                if not installed or installed.get('digest') != item['model_digest']:
                    reason = 'frozen_model_unavailable'
                    write(dict(kind='stop', reason=reason, index=index, resources=current)); break
                resident = {m.get('name', m.get('model')) for m in current.get('ollama', {}).get('loaded_models') or []}
                if owned and owned not in resident:
                    owned = None
                if owned and owned != item['model'] and manifest['owned_server']:
                    before = current
                    payload = dict(model=owned, keep_alive=0)
                    try:
                        request = Request(manifest['base_url'] + '/api/generate', data=json.dumps(payload).encode(),
                                          headers={'Content-Type': 'application/json'})
                        with urlopen(request, timeout=180) as response:
                            reply = json.load(response)
                        current = snapshot(manifest['base_url'])
                        write(dict(kind='unload_owned_model', model=owned, request=payload, response=reply,
                                   before=before, after=current))
                        owned = None
                    except (OSError, ValueError) as error:
                        reason = 'unload_execution_error'
                        write(dict(kind='stop', reason=reason, error=str(error), before=before)); break
                if used_time + time.monotonic() - session_started + manifest['total_seconds'] > manifest['campaign_seconds']:
                    reason = 'campaign_time_budget'; break
                write(dict(kind='start', index=index, timestamp_utc=timestamp(), **item))
                deadline = time.monotonic() + manifest['total_seconds']
                result = run(cases[item['case_id']]['source'], item['method'], item['model'],
                             manifest['base_url'], total_seconds=manifest['total_seconds'],
                             solver_seconds=manifest['solver_seconds'], wait_budget=budget,
                             admission=lambda: admission(item['model'], manifest['base_url'], budget, deadline))
                record = dict(kind='result', index=index, **item, result=result,
                              metrics=assess(cases[item['case_id']], result), campaign_wait_seconds=budget.used_seconds)
                write(record)
                completed[index] = record
                launched += 1
                if result.get('attempts'):
                    owned = item['model']
                print(f'{len(completed)}/{len(plan)} {item["case_id"]} {item["method"]} '
                      f'{item["model"]} {result["outcome"]} {result["elapsed_seconds"]:.2f}s', flush=True)
                if result['outcome'] == 'resource_blocked':
                    blocked.add(item['model'])
                    if not selection_phase:
                        reason = 'resource_blocked'; break
                if result['outcome'] == 'execution_error' or 'elapsed_budget' in result.get('reason', ''):
                    reason = 'execution_error_circuit_breaker'; break
            if reason == 'complete' and len(completed) != len(plan):
                reason = 'candidates_resource_blocked' if blocked else ('pilot_complete' if pilot else 'incomplete')
            write(dict(kind='end' if len(completed) == len(plan) else 'pause', timestamp_utc=timestamp(),
                       reason=reason, completed=len(completed), planned=len(plan),
                       invocation_seconds=time.monotonic() - session_started,
                       campaign_wait_seconds=budget.used_seconds))
    return dict(phase=phase, completed=len(completed), planned=len(plan), reason=reason, output=str(path.resolve()))


def selection(manifest_path, output, resume=False, pilot=False, limit=None, idle_verified=False):
    if read(manifest_path)['kind'] != 'extended_selection':
        raise ValueError('selection requires a development protocol')
    return _execute(manifest_path, output, resume=resume, pilot=pilot, limit=limit, idle_verified=idle_verified)


def campaign(manifest_path, output, resume=False, method='all', repetitions=1, limit=None,
             primary_records=None, idle_verified=False):
    if read(manifest_path)['kind'] != 'extended_test':
        raise ValueError('run requires the frozen held-out protocol')
    return _execute(manifest_path, output, resume=resume, method=method, repetitions=repetitions,
                    limit=limit, primary_records=primary_records, idle_verified=idle_verified)


def history(baseline):
    files = read(baseline)['files']
    changed = [p for p, expected in files.items() if not (ROOT / p).exists() or digest(ROOT / p) != expected]
    docs = {'README.md', 'docs/memory.md', 'docs/roadmap.md'}
    protected = [p for p in changed if p.replace('\\', '/') not in docs]
    return dict(valid=not protected, checked=len(files), protected_changes=protected,
                authorized_document_changes=[p for p in changed if p not in protected], baseline_sha256=digest(baseline))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    command = sub.add_parser('freeze-selection')
    command.add_argument('--data', required=True); command.add_argument('--output', required=True)
    command.add_argument('--base-url', default='http://localhost:11435')
    command = sub.add_parser('selection')
    command.add_argument('--manifest', required=True); command.add_argument('--output', required=True)
    command.add_argument('--resume', action='store_true'); command.add_argument('--pilot', action='store_true')
    command.add_argument('--limit', type=int); command.add_argument('--idle-verified', action='store_true')
    command = sub.add_parser('select')
    command.add_argument('--manifest', required=True); command.add_argument('--records', required=True)
    command.add_argument('--output')
    command = sub.add_parser('freeze-test')
    command.add_argument('--selection-manifest', required=True); command.add_argument('--selection-records', required=True)
    command.add_argument('--output', required=True)
    command = sub.add_parser('run')
    command.add_argument('--manifest', required=True); command.add_argument('--output', required=True)
    command.add_argument('--resume', action='store_true'); command.add_argument('--strategy', choices=['all', 'json', 'structured', 'structured-repair'], default='all')
    command.add_argument('--repetitions', type=int, choices=[1, 3], default=1); command.add_argument('--limit', type=int)
    command.add_argument('--primary-records'); command.add_argument('--idle-verified', action='store_true')
    command = sub.add_parser('verify'); command.add_argument('--manifest', required=True)
    command = sub.add_parser('metrics'); command.add_argument('records'); command.add_argument('--output')
    command = sub.add_parser('history'); command.add_argument('--baseline', required=True)
    args = parser.parse_args()
    if args.command == 'freeze-selection': result = freeze_selection(args.data, args.output, args.base_url)
    elif args.command == 'selection': result = selection(args.manifest, args.output, args.resume, args.pilot, args.limit, args.idle_verified)
    elif args.command == 'select': result = select(args.manifest, args.records, args.output)
    elif args.command == 'freeze-test': result = freeze_test(args.selection_manifest, args.selection_records, args.output)
    elif args.command == 'run': result = campaign(args.manifest, args.output, args.resume, args.strategy, args.repetitions,
                                               args.limit, args.primary_records, args.idle_verified)
    elif args.command == 'verify': result = verify_manifest(args.manifest)
    elif args.command == 'history': result = history(args.baseline)
    else:
        result = replay(args.records)
        if args.output: save(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
