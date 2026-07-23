"""Transactional local identity and per-user conversation persistence."""
from __future__ import annotations
import hashlib, hmac, json, secrets, sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "runs" / "users.sqlite3"
class ConflictError(ValueError): pass

class UserStore:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path=db_path; db_path.parent.mkdir(parents=True,exist_ok=True); self._init_db()
    def _connect(self):
        con=sqlite3.connect(self.db_path, timeout=10, isolation_level=None); con.row_factory=sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL"); con.execute("PRAGMA foreign_keys=ON"); con.execute("PRAGMA busy_timeout=10000")
        return con
    def _init_db(self):
        with self._connect() as c:
            c.executescript("""CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,salt TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires_at TEXT NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id));
            CREATE TABLE IF NOT EXISTS conversations(id TEXT NOT NULL,user_id TEXT NOT NULL,title TEXT NOT NULL,repo_json TEXT,messages_json TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL,PRIMARY KEY(user_id,id),FOREIGN KEY(user_id) REFERENCES users(id));
            CREATE INDEX IF NOT EXISTS idx_conversations_user_updated ON conversations(user_id,updated_at DESC); CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);""")
            cols={r['name'] for r in c.execute("PRAGMA table_info(conversations)")}
            if 'version' not in cols: # migrate the pre-version local schema atomically
                c.execute("BEGIN IMMEDIATE"); c.execute("ALTER TABLE conversations RENAME TO conversations_legacy")
                c.execute("CREATE TABLE conversations(id TEXT NOT NULL,user_id TEXT NOT NULL,title TEXT NOT NULL,repo_json TEXT,messages_json TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL,PRIMARY KEY(user_id,id),FOREIGN KEY(user_id) REFERENCES users(id))")
                c.execute("INSERT INTO conversations SELECT id,user_id,title,repo_json,messages_json,1,updated_at FROM conversations_legacy")
                c.execute("DROP TABLE conversations_legacy"); c.execute("COMMIT")
    @staticmethod
    def _hash(password,salt): return hashlib.scrypt(password.encode(),salt=salt,n=2**14,r=8,p=1).hex()
    @staticmethod
    def _now(): return datetime.now(timezone.utc).isoformat()
    def register(self, username,password):
        username=username.strip()
        if not 3<=len(username)<=40 or not username.replace('_','').replace('-','').isalnum(): raise ValueError('用户名格式无效')
        if not 8<=len(password)<=256: raise ValueError('密码长度必须为 8-256 位')
        uid=secrets.token_hex(12); salt=secrets.token_bytes(16)
        try:
            with self._connect() as c: c.execute('INSERT INTO users VALUES(?,?,?,?,?)',(uid,username,self._hash(password,salt),salt.hex(),self._now()))
        except sqlite3.IntegrityError as e: raise ValueError('用户名已存在') from e
        return self._session(uid,username)
    def login(self,username,password):
        with self._connect() as c: row=c.execute('SELECT * FROM users WHERE username=?',(username.strip(),)).fetchone()
        if not row or not hmac.compare_digest(row['password_hash'],self._hash(password,bytes.fromhex(row['salt']))): raise ValueError('用户名或密码错误')
        return self._session(row['id'],row['username'])
    def _session(self,uid,username):
        token=secrets.token_urlsafe(32); exp=(datetime.now(timezone.utc)+timedelta(days=14)).isoformat(); digest=hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as c: c.execute('INSERT INTO sessions VALUES(?,?,?)',(digest,uid,exp)); c.execute('DELETE FROM sessions WHERE expires_at<=?',(self._now(),))
        return {'token':token,'user':{'id':uid,'username':username},'expires_at':exp}
    def user_for_token(self,token):
        with self._connect() as c: row=c.execute('SELECT users.id,users.username FROM sessions JOIN users ON users.id=sessions.user_id WHERE token_hash=? AND expires_at>?',(hashlib.sha256(token.encode()).hexdigest(),self._now())).fetchone()
        return dict(row) if row else None
    def logout(self,token):
        with self._connect() as c: c.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
    def list_conversations(self,uid):
        with self._connect() as c: rows=c.execute('SELECT * FROM conversations WHERE user_id=? ORDER BY updated_at DESC',(uid,)).fetchall()
        return [self._row(r) for r in rows]
    def save_conversation(self,uid,data):
        cid=str(data.get('id',''))[:80]; title=str(data.get('title','新建调查'))[:120]; messages=data.get('messages',[]); version=int(data.get('version',0)); repo=data.get('repo') if isinstance(data.get('repo'),dict) else None
        if not cid or not isinstance(messages,list) or len(messages)>200: raise ValueError('对话数据无效')
        for m in messages:
            if not isinstance(m,dict) or m.get('role') not in {'user','agent'} or len(str(m.get('text','')))>16000: raise ValueError('消息数据无效')
        now=self._now(); payload=(title,json.dumps(repo,ensure_ascii=False),json.dumps(messages,ensure_ascii=False),now)
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if version==0:
                try: c.execute('INSERT INTO conversations(id,user_id,title,repo_json,messages_json,version,updated_at) VALUES(?,?,?,?,?,?,?)',(cid,uid,*payload[:3],1,now)); new=1
                except sqlite3.IntegrityError as e: c.execute('ROLLBACK'); raise ConflictError('对话已存在，请刷新历史后重试') from e
            else:
                cur=c.execute('UPDATE conversations SET title=?,repo_json=?,messages_json=?,version=version+1,updated_at=? WHERE user_id=? AND id=? AND version=?',(*payload,uid,cid,version))
                if cur.rowcount!=1: c.execute('ROLLBACK'); raise ConflictError('历史已在其他页面更新，请刷新后重试')
                new=version+1
            c.execute('COMMIT')
        return {'id':cid,'title':title,'repo':repo,'messages':messages,'version':new,'updated_at':now}
    def delete_conversation(self,uid,cid,version):
        with self._connect() as c:
            cur=c.execute('DELETE FROM conversations WHERE user_id=? AND id=? AND version=?',(uid,cid,version))
        if not cur.rowcount: raise ConflictError('历史已变更或不存在，请刷新后重试')
    @staticmethod
    def _row(r): return {'id':r['id'],'title':r['title'],'repo':json.loads(r['repo_json'] or 'null'),'messages':json.loads(r['messages_json']),'version':r['version'],'updated_at':r['updated_at']}
