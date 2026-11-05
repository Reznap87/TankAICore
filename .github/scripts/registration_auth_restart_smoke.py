import argparse, hashlib, json, os, secrets, shutil, ssl, subprocess, tarfile, tempfile, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[2]
OPENSSL = shutil.which('openssl') or r'C:\Program Files\Git\usr\bin\openssl.exe'
token = secrets.token_urlsafe(32)
state = {'data': None, 'revision': 0}
class Store(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.headers.get('Authorization') != 'Bearer ' + token: self.send_error(403); return
        if state['data'] is None: self.send_error(404); return
        self.send_response(200); self.send_header('X-Auth-Revision', str(state['revision'])); self.end_headers(); self.wfile.write(state['data'])
    def do_PUT(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        if self.headers.get('Authorization') != 'Bearer ' + token: self.send_error(403); return
        if self.headers.get('If-Match') != str(state['revision']): self.send_error(409); return
        state.update(data=body, revision=state['revision'] + 1)
        self.send_response(204); self.send_header('X-Auth-Revision', str(state['revision'])); self.end_headers()
def run(args, **kwargs):
    kwargs.setdefault('timeout', 30)
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout.strip()
def call(base, path, data=None, cookie=None, csrf=None):
    headers = {'Content-Type': 'application/json'}
    if cookie: headers['Cookie'] = cookie
    if csrf: headers['X-CSRF-Token'] = csrf
    req = Request(base + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
    with urlopen(req, timeout=4) as response:
        return response.status, dict(response.headers), json.load(response)
def ready(base):
    for _ in range(120):
        try:
            if call(base, '/api/health')[2]['ok']: return
        except Exception: time.sleep(.5)
    raise RuntimeError('Container did not become ready')

def main():
    parser = argparse.ArgumentParser(description='Synthetic isolated two-container auth restore check')
    parser.add_argument('--baseline-commit', required=True)
    parser.add_argument('--expected-head')
    args = parser.parse_args()
    baseline = run(['git','rev-parse','--verify',args.baseline_commit+'^{commit}'], cwd=ROOT)
    head = run(['git','rev-parse','HEAD'], cwd=ROOT)
    if args.expected_head and args.expected_head != head:
        raise RuntimeError('Current HEAD does not match --expected-head')
    suffix = uuid.uuid4().hex[:12]; names = ['tankai-auth-old-'+suffix, 'tankai-auth-new-'+suffix]
    images = ['tankai-auth-old:'+suffix, 'tankai-auth-new:'+suffix]
    with tempfile.TemporaryDirectory(prefix='tankai-auth-restore-') as tmp:
        tmp = Path(tmp); base_src = tmp / 'base'; base_src.mkdir(); archive = tmp / 'base.tar'
        with archive.open('wb') as stream: subprocess.run(['git','archive','--format=tar',baseline],cwd=ROOT,stdout=stream,check=True,timeout=30)
        with tarfile.open(archive) as tar: tar.extractall(base_src, filter='data')
        cert, key = tmp/'cert.pem', tmp/'key.pem'
        run([OPENSSL,'req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=host.docker.internal','-addext','subjectAltName=DNS:host.docker.internal'])
        server = ThreadingHTTPServer(('0.0.0.0',0), Store)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert,key); server.socket = context.wrap_socket(server.socket,server_side=True)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        synthetic_password = 'synthetic-password-123'
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(synthetic_password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
        encoded_password = f'scrypt$16384$8$1${salt.hex()}${digest.hex()}'
        owner = json.dumps({'email':'owner@example.test','password_hash':encoded_password,'user_id':str(uuid.uuid4()),'tenant_id':str(uuid.uuid4()),'workspace_id':str(uuid.uuid4())})
        try:
            for image, source in zip(images, (base_src, ROOT)):
                run(['docker','build','-q','-t',image,str(source)], timeout=300)
            def launch(name, image):
                env = {'TANKAI_HOST':'0.0.0.0','TANKAI_AUTH_MODE':'session','TANKAI_COOKIE_SECURE':'0','TANKAI_ALLOW_REGISTRATION':'1','TANKAI_LLM':'mock','TANKAI_EMBEDDER':'hashing','TANKAI_AUTH_STATE_URL':f'https://host.docker.internal:{server.server_port}/auth','TANKAI_AUTH_STATE_TOKEN':token,'TANKAI_BOOTSTRAP_OWNER':owner,'SSL_CERT_FILE':'/tmp/tankai-cert.pem'}
                env_file = tmp / 'synthetic.env'
                fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as out:
                    out.write(''.join(f'{k}={v}\n' for k,v in env.items()))
                cmd = ['docker','run','-d','--name',name,'--add-host','host.docker.internal:host-gateway','--env-file',str(env_file),'-p','127.0.0.1::8765','-v',str(cert)+':/tmp/tankai-cert.pem:ro']
                run(cmd+[image]); address=run(['docker','port',name,'8765/tcp']); port=int(address.rsplit(':',1)[1]); url=f'http://127.0.0.1:{port}'; ready(url); return url
            first=launch(names[0],images[0]); status, headers, payload=call(first,'/api/auth/login',{'email':'owner@example.test','password':synthetic_password}); assert status==200
            cookie=headers['Set-Cookie'].split(';',1)[0]; identity=payload['user']['id']; assert call(first,'/api/auth/me',cookie=cookie)[2]['user']['id']==identity
            _, revoked_headers, revoked_payload = call(first,'/api/auth/login',{'email':'owner@example.test','password':synthetic_password})
            revoked_cookie = revoked_headers['Set-Cookie'].split(';',1)[0]
            assert call(first,'/api/auth/logout',{},cookie=revoked_cookie,csrf=revoked_payload['csrf_token'])[0] == 200
            revision_before=state['revision']; assert state['data'] is not None
            run(['docker','rm','-f',names[0]]); second=launch(names[1],images[1]); assert state['revision']>revision_before
            assert call(second,'/api/auth/me',cookie=cookie)[2]['user']['id']==identity
            try:
                call(second,'/api/auth/me',cookie=revoked_cookie)
                raise AssertionError('revoked session was accepted')
            except HTTPError as exc:
                assert exc.code == 401
            assert call(second,'/api/auth/login',{'email':'owner@example.test','password':synthetic_password})[2]['user']['id']==identity
            new_user = {'email':'new@example.test','password':'synthetic-new-password-123','display_name':'New','tenant_name':'New'}
            assert call(second,'/api/auth/register',new_user)[0] == 201
            print(f'PASS: old baseline {baseline} -> synthetic HTTPS auth state -> fresh container {head}; owner login, active and revoked sessions, registration')
        finally:
            for name in names: subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=30)
            for image in images: subprocess.run(['docker','image','rm',image],capture_output=True,timeout=30)
            server.shutdown(); server.server_close(); thread.join(timeout=5)
if __name__=='__main__': main()

