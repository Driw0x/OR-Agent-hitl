"""Local-only HTTP/SSE adapter around the existing routing and HITL services."""

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from . import agent, hitl, resources, routing, visualization
from .models import load_json

ASSETS = Path(__file__).with_name('web_assets')
DIGEST = 'dae161e27b0e90dd1856c8bb3209201fd6736d8eb66298e75ed87571486f4364'
STAGES = ('received', 'extraction', 'data', 'constraints', 'certificate', 'routing',
          'generation', 'review', 'scope', 'approval', 'conversion', 'solver', 'verification', 'graphics')


class Conflict(ValueError):
    pass


def text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 8000 or '\x00' in value:
        raise ValueError('Texte requis : 1 à 8000 caractères, sans NUL.')
    value.encode('utf-8', errors='strict')
    return value


class Store:
    def __init__(self, root, *, base_url='http://127.0.0.1:11434', seconds=5.0,
                 client=None, admission=None, imports=(), simulated_reviewer=False):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.base_url, self.seconds = base_url, seconds
        self.client, self.admission = client, admission
        self.simulated_reviewer = simulated_reviewer
        self.imports = {str(i): Path(p).resolve() for i, p in enumerate(imports)}
        self.condition = threading.Condition(threading.RLock())
        self.stopping = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='or-agent-web')
        self.generation_lock = threading.Lock()
        # One application process may own this session directory at a time.
        self.lock_file = (self.root / '.server.lock').open('a+b')
        self.lock_file.write(b'0')
        self.lock_file.flush()
        self.lock_file.seek(0)
        try:
            if __import__('os').name == 'nt':
                import msvcrt
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close()
            self.executor.shutdown()
            raise Conflict('Ce dossier de sessions est déjà ouvert par un serveur.')
        for path in self.root.glob('*/web.json'):
            data = load_json(path.read_text(encoding='utf-8'))
            if data['active']:
                data['active'] = False
                data['error'] = 'Application interrompue. Aucun traitement ne sera relancé automatiquement.'
                for event in list(data['stages'].values()):
                    if event['status'] == 'running':
                        self._event(data, event['stage'], 'failed', error=data['error'])
                hitl.write(path, hitl.encoded(data))

    def path(self, sid):
        if not isinstance(sid, str) or not re.fullmatch(r'[0-9a-f]{32}', sid):
            raise ValueError('Identifiant de session invalide.')
        candidate = self.root / sid
        if candidate.is_symlink() or getattr(candidate.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('Lien de session non autorisé.')
        path = candidate.resolve()
        if path.parent != self.root or not path.is_dir():
            raise FileNotFoundError('Session introuvable.')
        if not (path / 'session').resolve().is_relative_to(path):
            raise ValueError('Chemin de session non autorisé.')
        return path

    def _load(self, sid):
        return load_json((self.path(sid) / 'web.json').read_text(encoding='utf-8'))

    def _save(self, data):
        hitl.write(self.path(data['id']) / 'web.json', hitl.encoded(data))
        self.condition.notify_all()

    def _event(self, data, stage, status, **details):
        previous = data['stages'].get(stage)
        now = time.time()
        event = dict(id=len(data['events'])+1, at=hitl.now(), epoch=now,
                     stage=stage, status=status, details=details)
        if status == 'running':
            event['started_epoch'] = previous['started_epoch'] if previous and previous['status'] == 'running' else now
        elif previous and previous['status'] == 'running':
            event['duration_seconds'] = max(0, now-previous['started_epoch'])
        data['events'].append(event)
        data['stages'][stage] = event

    def emit(self, sid, stage, status, **details):
        with self.condition:
            data = self._load(sid)
            self._event(data, stage, status, **details)
            self._save(data)

    def create(self, source, *, imported=None):
        source = text(source)
        with self.condition:
            if self.stopping.is_set():
                raise Conflict('Application en cours d’arrêt.')
            sid = uuid4().hex
            (self.root / sid).mkdir()
            data = dict(id=sid, created_at=hitl.now(), active=False, error=None,
                        events=[], stages={}, snapshot=None, imported=imported,
                        simulated_reviewer=self.simulated_reviewer)
            for stage in STAGES:
                data['stages'][stage] = dict(stage=stage, status='pending')
            self._event(data, 'received', 'completed', characters=len(source))
            self._save(data)
            self.start(sid, 'analyze', dict(source=source, imported=imported))
        return sid

    def import_session(self, key):
        if not isinstance(key, str) or key not in self.imports:
            raise ValueError('Session historique non autorisée.')
        path = self.imports[key]
        # Read an explicitly configured source; never mutate old certificates,
        # approvals, raw model responses or historical solver results.
        hitl.load(path)
        return self.create((path / 'original.txt').read_text(encoding='utf-8-sig'), imported=key)

    def start(self, sid, action, body):
        with self.condition:
            data = self._load(sid)
            if data['active'] or self.stopping.is_set():
                raise Conflict('Un traitement est déjà actif, ou le serveur s’arrête.')
            if action not in ('analyze','generate','edit','validate','review','scope','approve','reject','solve','resolve'):
                raise ValueError('Action inconnue.')
            data.update(active=True, error=None)
            self._save(data)
            future = self.executor.submit(self._run, sid, action, body)
            def completed(job):
                failure = job.exception()
                if failure:
                    print(f'Session {sid}: persistent write failure: {failure}', file=sys.stderr, flush=True)
            future.add_done_callback(completed)

    def _refresh(self, sid):
        path = self.path(sid) / 'session'
        state = routing.status(path)
        _, original = hitl.load(path)
        state['original'] = original.decode('utf-8-sig')
        for name, filename in (('proposal','proposal.txt'), ('raw','llm-raw.txt')):
            state[name] = (path / filename).read_bytes().decode('utf-8' if name == 'raw' else 'utf-8-sig') if (path / filename).exists() else ''
        state['proposal_facts'] = routing.d.extract(state['proposal']) if state['proposal'].strip() and len(state['proposal']) <= 8000 else None
        state['generation'] = load_json((path / 'generation.json').read_text(encoding='utf-8')) if (path / 'generation.json').exists() else None
        if state['generation'] and isinstance(state['generation'].get('response'), dict):
            response = state['generation']['response']
            # Expose actual output content and transport metrics, never hidden
            # reasoning fields that a model backend might also return.
            public = {k: response[k] for k in ('model','done','done_reason','created_at',
                'total_duration','load_duration','prompt_eval_count','eval_count',
                'prompt_eval_duration','eval_duration','fixture_replay') if k in response}
            public['message'] = {k: response.get('message', {}).get(k) for k in ('role','content')}
            state['generation']['response'] = public
        if 'hitl' in state:
            session, *_ = hitl.inspect(path)
            h = state['hitl']
            h['scope_confirmed'] = hitl.scope_confirmed(session, h['fingerprints'], h['review_token'])
            h['reviewed'] = session.review_token == h['review_token']
            if state.get('result_current'):
                result = state['result']
                if not any(e.get('action') == 'solved' and e.get('result') == result for e in session.events):
                    raise ValueError('saved HITL result differs from its session journal')
        with self.condition:
            data = self._load(sid)
            if data['snapshot'] != state:
                data['snapshot'] = state
                self._save(data)
        return state

    def read(self, sid):
        with self.condition:
            data = self._load(sid)
            if not data['active'] and (self.path(sid) / 'session' / 'routing.json').exists():
                self._refresh(sid)
                data = self._load(sid)
            return data

    def listing(self):
        with self.condition:
            rows = [self._load(p.name) for p in self.root.iterdir() if p.is_dir() and re.fullmatch('[0-9a-f]{32}', p.name)]
            return dict(sessions=[{k: d[k] for k in ('id','created_at','active','error')} for d in sorted(rows, key=lambda d:d['created_at'], reverse=True)],
                        imports=[dict(id=k, name=p.parent.name) for k,p in self.imports.items()])

    def delete(self, sid, confirmation):
        with self.condition:
            path = self.path(sid)
            if confirmation != 'DELETE:' + sid:
                raise ValueError('Confirmation explicite de suppression requise.')
            data = self._load(sid)
            if data['id'] != sid:
                raise ValueError('Registre de session incohérent.')
            if data['active'] or self.stopping.is_set():
                raise Conflict('Suppression interdite pendant un traitement actif ou l’arrêt du serveur.')
            # Inspect every entry before deleting anything. Never traverse links,
            # Windows junctions/reparse points, mounts or shared hard-linked files.
            pending = [path]
            while pending:
                entry = pending.pop()
                info = entry.lstat()
                if (stat.S_ISLNK(info.st_mode)
                        or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                        or os.path.ismount(entry) or not entry.resolve().is_relative_to(path)
                        or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1)):
                    raise ValueError('Chemin de suppression non autorisé : lien ou fichier partagé.')
                if stat.S_ISDIR(info.st_mode):
                    pending.extend(entry.iterdir())
            registry = path / 'web.json'
            saved = registry.read_bytes()
            try:
                # Keep the registry until all session artifacts are removed, so
                # disk failures remain visible and deletion can be retried.
                for entry in path.iterdir():
                    if entry != registry:
                        if entry.is_dir():
                            shutil.rmtree(entry)
                        else:
                            entry.unlink()
                registry.unlink()
                path.rmdir()
            except OSError as error:
                if path.exists() and not registry.exists():
                    hitl.write(registry, saved)
                raise OSError('Suppression incomplète sur disque ; certains artefacts peuvent avoir été supprimés. ' + str(error)) from error
            self.condition.notify_all()

    def _admit(self):
        observed = resources.snapshot(self.base_url, timeout=2)
        ollama = observed['ollama']
        model = next((m for m in ollama['models'] if m.get('name') == agent.MODEL or m.get('model') == agent.MODEL), {})
        if not ollama['available'] or model.get('digest', '').removeprefix('sha256:') != DIGEST:
            return dict(allowed=False, reason='Ollama unavailable or required model digest missing', snapshot=observed)
        if ollama['loaded_models'] != []:
            return dict(allowed=False, reason='Resident model or unknown residency; preserve shared activity', snapshot=observed)
        from .extended_evaluate import foreign_activity
        foreign = foreign_activity()
        if foreign['active'] is not False:
            return dict(allowed=False, reason='Foreign Ollama activity present or unknown', foreign=foreign)
        # No resource-policy sleep in the UI; a refusal remains visible, and the
        # one-call marker prevents accidental retry on reload.
        return resources.gate(agent.MODEL, self.base_url, wait_budget=resources.WaitBudget(operation_seconds=0))

    def _generate(self, sid, path):
        state = routing.status(path)
        if state['routing']['branch'] != 'HITL' or state['hitl']['state'] in ('REJECTED','APPROVED','SOLVED'):
            raise Conflict('La génération exige une proposition HITL non approuvée.')
        if state['hitl']['validation'].get('source_blockers'):
            raise ValueError('Contraintes incompatibles ou contradiction : corriger la demande dans une nouvelle session.')
        if (path / 'generation-started.json').exists() or (path / 'generation.json').exists():
            raise Conflict('Une génération a déjà été demandée. Aucun retry automatique.')
        if not self.generation_lock.acquire(blocking=False):
            raise Conflict('Une génération est déjà active dans cette application.')
        try:
            self.emit(sid, 'generation', 'running', model=agent.MODEL, connection='Vérification Ollama et ressources', streaming=False)
            def client(payload, timeout):
                self.emit(sid, 'generation', 'running', model=agent.MODEL, connection='Ollama accessible ; réponse en attente', streaming=False)
                return self.client(payload, timeout) if self.client else agent.ollama_chat(payload, self.base_url, timeout, [])
            hitl.propose(path, (path / 'original.txt').read_bytes(), existing=True,
                         base_url=self.base_url, client=client, admission=self.admission or self._admit, unload_after=True)
            record = load_json((path / 'generation.json').read_text(encoding='utf-8'))
            self.emit(sid, 'generation', 'failed' if record['error'] else 'completed',
                      model=agent.MODEL, error=record['error'], calls=record['calls'],
                      elapsed_seconds=record['elapsed_seconds'], termination_uncertain=record['termination_uncertain'])
        finally:
            self.generation_lock.release()

    def _run(self, sid, action, body):
        path = self.path(sid) / 'session'
        observe = lambda stage, status, **details: self.emit(sid, stage, status, **details)
        try:
            if action == 'analyze':
                routing.submit(path, body['source'].encode('utf-8'), human_scope=True,
                               seconds=self.seconds, observe=observe)
                if body.get('imported') is not None:
                    old = self.imports[body['imported']]
                    for filename in ('proposal.txt','llm-raw.txt','generation.json','generation-started.json'):
                        if (old / filename).exists():
                            hitl.write(path / filename, (old / filename).read_bytes())
                    hitl.validate(path)
                    observe('generation', 'not_applicable', reason='Sortie historique importée sans appel Ollama.')
            else:
                decision, _ = routing.inspect(path)
                if decision['branch'] != 'HITL' and action != 'solve':
                    raise ValueError('Cette action exige une session HITL.')
                if action == 'generate':
                    self._generate(sid, path)
                elif action == 'edit':
                    current = hitl.status(path)
                    if current['state'] == 'REJECTED' or routing.inspect(path)[0]['branch'] != 'HITL':
                        raise ValueError('Proposition non modifiable.')
                    if body.get('proposal_sha256') != current['fingerprints']['proposal_sha256']:
                        raise Conflict('La proposition a changé. Recharger avant de sauvegarder.')
                    hitl.write(path / 'proposal.txt', text(body.get('proposal')).encode('utf-8'))
                    hitl.validate(path)
                    for stage in ('review','scope','approval'):
                        observe(stage, 'pending', reason='Proposition modifiée : nouvelle confirmation requise.')
                    observe('graphics', 'blocked', reason='Ancien résultat historique, non courant.')
                elif action in ('review','validate'):
                    observe('review', 'running')
                    current = getattr(hitl, action)(path)
                    observe('review', 'completed' if current['validation']['complete'] else 'blocked', validation=current['validation'])
                elif action == 'scope':
                    observe('scope', 'running')
                    hitl.confirm_scope(path, body.get('review_token'), body.get('confirmation'), simulated=self.simulated_reviewer)
                    observe('scope', 'completed')
                elif action in ('approve', 'resolve'):
                    observe('approval', 'running')
                    approved = hitl.approve(path, body.get('review_token'), body.get('confirmation'), web=True, simulated=self.simulated_reviewer)
                    if not approved['approval_valid'] or approved['review_token'] != body.get('review_token'):
                        raise Conflict('Approbation invalide ou périmée.')
                    observe('approval', 'completed')
                    if action == 'resolve':
                        routing.solve(path, self.seconds, observe=observe)
                elif action == 'reject':
                    hitl.reject(path, text(body.get('reason')))
                    observe('approval', 'blocked', reason=body['reason'])
                elif action == 'solve':
                    routing.solve(path, self.seconds, observe=observe)
            state = self._refresh(sid)
            if action in ('analyze','generate','edit') and state['routing']['branch'] == 'HITL' and not state['hitl']['approval_valid']:
                observe('review', 'blocked', reason='Revue humaine requise ; aucune résolution autorisée.')
            if state['result'] and state.get('result_current', True):
                observe('graphics', 'running')
                visualization.html(state['result'])
                observe('graphics', 'completed')
        except Exception as error:
            message = f'{type(error).__name__}: {error}'
            with self.condition:
                data = self._load(sid)
                data['error'] = message
                running = [s for s,e in data['stages'].items() if e['status'] == 'running']
                for stage in running or [dict(analyze='routing',generate='generation',solve='solver').get(action, 'review')]:
                    self._event(data, stage, 'failed', error=message)
                self._save(data)
            try:
                if (path / 'routing.json').exists():
                    self._refresh(sid)
            except (ValueError, OSError):
                pass
        finally:
            with self.condition:
                data = self._load(sid)
                data['active'] = False
                self._save(data)

    def result(self, sid):
        data = self.read(sid)
        state = data['snapshot']
        if data['active'] or not state or not state['result'] or not state.get('result_current', True):
            raise Conflict('Aucun résultat courant autorisé. Ancien résultat conservé dans la session.')
        visualization.view(state['result'])
        return state['result']

    def close(self):
        self.stopping.set()
        with self.condition:
            self.condition.notify_all()
        self.executor.shutdown(wait=True)
        self.lock_file.close()


def make_server(store, port=8765):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, content_type='application/json; charset=utf-8'):
            if not isinstance(body, bytes):
                body = hitl.encoded(body)
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'self'; object-src 'none'; base-uri 'none'")
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(body)

        def check_host(self):
            host = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != host:
                raise ValueError('Hôte non autorisé. Utiliser 127.0.0.1.')
            return 'http://' + host

        def do_GET(self):
            try:
                self.check_host()
                url = urlsplit(self.path)
                if url.path in ('/','/app.js','/style.css'):
                    filename, mime = {'/':('index.html','text/html'), '/app.js':('app.js','text/javascript'), '/style.css':('style.css','text/css')}[url.path]
                    return self.send(200, (ASSETS / filename).read_bytes(), mime+'; charset=utf-8')
                if url.path == '/api/sessions':
                    return self.send(200, store.listing())
                if url.path == '/api/examples':
                    return self.send(200, dict(autonomous=EXAMPLE, prose=PROSE))
                match = re.fullmatch(r'/api/sessions/([0-9a-f]{32})(?:/(events|result.json|graph.svg|result.html))?', url.path)
                if not match:
                    raise FileNotFoundError('URL inconnue.')
                sid, kind = match.groups()
                if kind == 'events':
                    after = self.headers.get('Last-Event-ID') or parse_qs(url.query).get('after',['0'])[0]
                    if not re.fullmatch(r'[0-9]{1,12}', after):
                        raise ValueError('Curseur SSE invalide.')
                    store.path(sid)
                    return self.events(sid, int(after))
                if not kind:
                    return self.send(200, store.read(sid))
                result = store.result(sid)
                if kind == 'result.json':
                    return self.send(200, result)
                content = visualization.svg(result) if kind == 'graph.svg' else visualization.html(result)
                return self.send(200, content.encode('utf-8'), 'image/svg+xml' if kind == 'graph.svg' else 'text/html; charset=utf-8')
            except (ConnectionError, TimeoutError):
                pass
            except Exception as error:
                self.error(error)

        def do_POST(self):
            try:
                origin = self.check_host()
                if self.headers.get('Origin') != origin or self.headers.get('X-OR-Agent') != '1':
                    return self.send(403, dict(error='Origine ou en-tête de requête non autorisé.'))
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('Content-Type application/json requis.')
                length = self.headers.get('Content-Length', '')
                if not length.isdigit() or not 0 < int(length) <= 131072 or self.headers.get('Transfer-Encoding'):
                    raise ValueError('Taille HTTP invalide (maximum 128 KiB).')
                self.connection.settimeout(5)
                body = load_json(self.rfile.read(int(length)).decode('utf-8', errors='strict'))
                if not isinstance(body, dict):
                    raise ValueError('Objet JSON requis.')
                if self.path == '/api/sessions':
                    return self.send(202, dict(id=store.create(body.get('source'))))
                if self.path == '/api/import':
                    return self.send(202, dict(id=store.import_session(body.get('id'))))
                match = re.fullmatch(r'/api/sessions/([0-9a-f]{32})/([a-z]+)', self.path)
                if not match:
                    raise FileNotFoundError('URL inconnue.')
                sid, action = match.groups()
                if action == 'delete':
                    store.delete(sid, body.get('confirmation'))
                    return self.send(200, dict(id=sid, deleted=True))
                if action == 'analyze':
                    raise Conflict('Analyse déjà créée ; nouvelle demande exige une nouvelle session.')
                store.start(sid, action, body)
                return self.send(202, dict(id=sid, action=action))
            except (ConnectionError, TimeoutError):
                pass
            except Exception as error:
                self.error(error)

        def error(self, error):
            code = 404 if isinstance(error, FileNotFoundError) else 409 if isinstance(error, Conflict) else 400 if isinstance(error, (ValueError, UnicodeError)) else 500
            self.send(code, dict(error=f'{type(error).__name__}: {error}'))

        def events(self, sid, after):
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream; charset=utf-8')
            self.send_header('Cache-Control','no-cache')
            self.end_headers()
            self.connection.settimeout(15)
            while not store.stopping.is_set():
                with store.condition:
                    if not (store.root / sid).exists():
                        self.wfile.write(b'event: deleted\ndata: {}\n\n')
                        self.wfile.flush()
                        return
                    rows = [e for e in store._load(sid)['events'] if e['id'] > after]
                    if not rows:
                        store.condition.wait(timeout=1)
                for row in rows:
                    payload = json.dumps(row, ensure_ascii=False, allow_nan=False)
                    self.wfile.write(f'id: {row["id"]}\ndata: {payload}\n\n'.encode('utf-8'))
                    after = row['id']
                if not rows:
                    self.wfile.write(b': heartbeat\n\n')
                self.wfile.flush()

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server


EXAMPLE = '''Assign each task exactly once; minimize total cost within capacities.
Tasks: T1, T10.
Resources: R1, R10.
Cost unit: euro.
Resource unit: hour.
R1 capacity: 12.
R1 costs: T1=-1, T10=-4.
R1 consumption: T1=3, T10=0.
R10 capacity: 12.
R10 costs: T1=8, T10=22.
R10 consumption: T1=3, T10=4.'''
PROSE = '''Je dispose de deux machines, Machine_A et Machine_B. Machine_A dispose de 5 heures et Machine_B de 4 heures.
Je dois affecter Cut et Assemble, chaque tâche exactement une fois à une machine.
Sur Machine_A : Cut coûte 8 euros et consomme 2 heures ; Assemble coûte 5 euros et consomme 3 heures.
Sur Machine_B : Cut coûte 7 euros et consomme 3 heures ; Assemble coûte 6 euros et consomme 2 heures.
Minimiser le coût total, respecter les capacités cumulatives. Aucune autre contrainte.'''


def serve(root='.or-agent-web', port=8765, base_url='http://127.0.0.1:11434', imports=()):
    store = Store(root, base_url=base_url, imports=imports)
    server = None
    try:
        server = make_server(store, port)
        print(f'OR-Agent local : http://127.0.0.1:{server.server_port}', flush=True)
        print('Ctrl+C : arrêt après la fin des traitements actifs (génération bornée à 180 s).', flush=True)
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print('Arrêt demandé ; attente des traitements actifs, sans arrêter Ollama partagé.', flush=True)
    finally:
        store.close()
        if server:
            server.server_close()
