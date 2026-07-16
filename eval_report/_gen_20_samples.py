"""生成 20 个手工构造的测试样本，覆盖全部 5 个 Tool 的评测。

样本分布:
  bandit-focused:  s01-s07  安全漏洞检测
  ruff-focused:    s08-s12  代码风格/质量
  dependency:      s13-s14  依赖文件变更
  python_ast:      s15      符号提取验证
  mixed:           s16-s18  多工具综合
  edge:            s19-s20  边界/极端情况
"""

import json
import os
import subprocess
import shutil
from pathlib import Path

SAMPLES_DIR = Path(os.environ.get("TEMP", "/tmp")) / "eval_report_samples"
SAMPLES_DIR.mkdir(parents=True, exist_ok=True)


def init_repo(name: str, safe_files: dict[str, str], vuln_files: dict[str, str],
              expected_issues: list[dict]) -> Path:
    """创建一个两 commit 的 git repo：safe_version → vuln_version."""
    repo = SAMPLES_DIR / name
    if repo.exists():
        shutil.rmtree(repo, ignore_errors=True)
    repo.mkdir(parents=True)

    git = lambda *args: subprocess.run(["git", "-C", str(repo)] + list(args),
                                       check=True, capture_output=True)
    git("init")
    git("config", "user.email", "eval@test.local")
    git("config", "user.name", f"Eval-{name}")

    # Commit 1: safe version
    if safe_files:
        for path, content in safe_files.items():
            fpath = repo / path
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content, encoding="utf-8")
    else:
        # 占位文件确保首 commit 非空
        (repo / "README.md").write_text("# Safe baseline\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "initial: safe baseline")

    # Commit 2: vuln version (skip if no vuln files)
    if vuln_files:
        for path, content in vuln_files.items():
            fpath = repo / path
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content, encoding="utf-8")
        git("add", "-A")
        git("commit", "-m", "feat: add feature with issues")

    # Save meta
    meta = {
        "sample_id": name,
        "expected_issues": expected_issues,
        "files": list(vuln_files.keys()),
    }
    (repo / "_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return repo


# ============================================================
# s01: SQL 注入 — bandit B608
# ============================================================
init_repo(
    "s01_sql_injection", {},
    {"db.py": '''"""Database query module."""
import sqlite3

def get_user(user_id: str):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    query = "SELECT * FROM users WHERE id = '" + user_id + "'"
    cursor.execute(query)
    return cursor.fetchone()

def search_products(keyword: str):
    conn = sqlite3.connect("shop.db")
    cursor = conn.cursor()
    sql = f"SELECT * FROM products WHERE name LIKE '%{keyword}%'"
    cursor.execute(sql)
    return cursor.fetchall()
'''},
    [{"tool": "bandit", "rule_id": "B608", "severity": "medium",
      "file": "db.py", "desc": "SQL 注入: 字符串拼接构造查询"},
     {"tool": "bandit", "rule_id": "B608", "severity": "medium",
      "file": "db.py", "desc": "SQL 注入: f-string 拼接查询"}]
)

# ============================================================
# s02: 命令注入 — bandit B602
# ============================================================
init_repo(
    "s02_command_injection", {},
    {"worker.py": '''"""Background job worker."""
import subprocess
import os

def ping_host(host: str):
    subprocess.run(f"ping -c 1 {host}", shell=True)
    return True

def backup_db(db_name: str):
    os.system(f"pg_dump {db_name} > backup.sql")

def run_script(path: str):
    subprocess.Popen(f"bash {path}", shell=True)
'''},
    [{"tool": "bandit", "rule_id": "B602", "severity": "high",
      "file": "worker.py", "desc": "命令注入: shell=True + 用户输入"},
     {"tool": "bandit", "rule_id": "B605", "severity": "medium",
      "file": "worker.py", "desc": "os.system 调用"},
     {"tool": "bandit", "rule_id": "B606", "severity": "low",
      "file": "worker.py", "desc": "Popen 与 shell=True"}]
)

# ============================================================
# s03: 反序列化 — bandit B301
# ============================================================
init_repo(
    "s03_deserialization", {},
    {"loader.py": '''"""Data loader with deserialization issues."""
import pickle
import yaml
import marshal

def load_user_data(raw: bytes):
    return pickle.loads(raw)

def load_config(path: str):
    with open(path) as f:
        return yaml.load(f, Loader=yaml.Loader)

def load_cache(data: bytes):
    return marshal.loads(data)
'''},
    [{"tool": "bandit", "rule_id": "B301", "severity": "medium",
      "file": "loader.py", "desc": "pickle.loads 不安全反序列化"},
     {"tool": "bandit", "rule_id": "B506", "severity": "medium",
      "file": "loader.py", "desc": "yaml.load 使用不安全 Loader"},
     {"tool": "bandit", "rule_id": "B302", "severity": "low",
      "file": "loader.py", "desc": "marshal.loads 不安全反序列化"}]
)

# ============================================================
# s04: 硬编码凭据 — bandit B105/B106/B107
# ============================================================
init_repo(
    "s04_hardcoded_secrets", {},
    {"config.py": '''"""Application configuration."""
DB_PASSWORD = "admin123"
API_KEY = "sk-abc123def456ghi789jkl"
SECRET_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.abc123def456"

DATABASE_URL = "postgresql://admin:password123@localhost:5432/mydb"
AWS_ACCESS_KEY = "AKIA1234567890ABCDEF"
'''},
    [{"tool": "bandit", "rule_id": "B105", "severity": "low",
      "file": "config.py", "desc": "硬编码密码"},
     {"tool": "bandit", "rule_id": "B106", "severity": "low",
      "file": "config.py", "desc": "硬编码密钥"},
     {"tool": "bandit", "rule_id": "B107", "severity": "low",
      "file": "config.py", "desc": "硬编码令牌"}]
)

# ============================================================
# s05: 认证缺陷 — bandit + auth_change 触发
# ============================================================
init_repo(
    "s05_auth_flaws", {},
    {"auth/login.py": '''"""User authentication module."""
import hashlib

def verify_password(plain: str, stored_hash: str) -> bool:
    if plain == "super_secret_master_key":
        return True
    return hashlib.md5(plain.encode()).hexdigest() == stored_hash

def create_token(user_id: int) -> str:
    import base64
    return base64.b64encode(f"{user_id}:admin".encode()).decode()

def check_admin(user):
    if user.name == "root":
        return True
    return user.role == "admin"
'''},
    [{"tool": "bandit", "rule_id": "B105", "severity": "low",
      "file": "auth/login.py", "desc": "硬编码后门密码"},
     {"tool": "bandit", "rule_id": "B303", "severity": "medium",
      "file": "auth/login.py", "desc": "MD5 哈希不安全"},
     {"tool": "bandit", "rule_id": "B324", "severity": "low",
      "file": "auth/login.py", "desc": "base64 编码非加密"}]
)

# ============================================================
# s06: 多种安全风险叠加 — bandit 多项命中
# ============================================================
init_repo(
    "s06_multi_risk", {},
    {"api.py": '''"""API endpoint handlers with multiple issues."""
import subprocess
import pickle
import sqlite3

API_SECRET = "prod-secret-12345"

def export_data(fmt: str, user_input: str):
    if fmt == "sql":
        conn = sqlite3.connect("data.db")
        q = f"SELECT * FROM data WHERE tag='{user_input}'"
        conn.execute(q)
    elif fmt == "cmd":
        subprocess.run(f"zip -r data.zip {user_input}", shell=True)
    elif fmt == "pkl":
        with open("cache.pkl", "wb") as f:
            pickle.dump({"data": user_input}, f)
'''},
    [{"tool": "bandit", "rule_id": "B105", "severity": "low",
      "file": "api.py", "desc": "硬编码密钥"},
     {"tool": "bandit", "rule_id": "B608", "severity": "medium",
      "file": "api.py", "desc": "f-string SQL 注入"},
     {"tool": "bandit", "rule_id": "B602", "severity": "high",
      "file": "api.py", "desc": "subprocess shell=True"},
     {"tool": "bandit", "rule_id": "B301", "severity": "medium",
      "file": "api.py", "desc": "pickle.dump 不安全序列化"}]
)

# ============================================================
# s07: 大文件变更触发 bandit — >100 行变更
# ============================================================
_long_code = ""
for i in range(1, 61):
    _long_code += f"def helper_{i}(x):\n    return x * {i}\n\n"
_long_code += '''
import subprocess
def admin_task(cmd: str):
    subprocess.run(cmd, shell=True)
'''
init_repo(
    "s07_large_change_bandit", {},
    {"utils.py": _long_code},
    [{"tool": "bandit", "rule_id": "B602", "severity": "high",
      "file": "utils.py", "desc": "大变更 (>100 行) 触发 bandit + shell=True 命令注入"}]
)

# ============================================================
# s08: Ruff 未使用导入 — F401
# ============================================================
init_repo(
    "s08_ruff_unused_import", {},
    {"cleanup.py": '''"""Data cleaning utilities."""
import os
import json
import csv
import math  # unused
import random  # unused
from datetime import datetime, timedelta
from collections import defaultdict, OrderedDict, namedtuple  # OrderedDict unused

def clean_data(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)

def format_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")
'''},
    [{"tool": "ruff", "rule_id": "F401", "severity": "low",
      "file": "cleanup.py", "desc": "未使用的导入 math, random, OrderedDict"}]
)

# ============================================================
# s09: Ruff 行过长 + 多个风格问题 — E501/E302/E225
# ============================================================
init_repo(
    "s09_ruff_style_issues", {},
    {"formatter.py": '''"""Report formatter with multiple style issues — this line is intentionally way too long and should trigger E501 line length check by ruff linter """

import  os,  sys,   json


def report():
    x=1
    y    =2
    z    =x    +y;print(    z)
    return {"result":z}

def  extra_spaces  (  a,b  ):
    return a    +    b
'''},
    [{"tool": "ruff", "rule_id": "E501", "severity": "low",
      "file": "formatter.py", "desc": "行过长 (第1行)"},
     {"tool": "ruff", "rule_id": "E302", "severity": "low",
      "file": "formatter.py", "desc": "模块级函数前空行不足"},
     {"tool": "ruff", "rule_id": "E225", "severity": "low",
      "file": "formatter.py", "desc": "操作符周围缺少空格"}]
)

# ============================================================
# s10: Ruff 裸 except + 未定义变量 — E722/F821
# ============================================================
init_repo(
    "s10_ruff_bare_except", {},
    {"parser.py": '''"""Simple config parser."""

def parse_config(text: str) -> dict:
    result = {}
    for line in text.split("\\n"):
        try:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
        except:
            pass
    return result

def load_config(path):
    try:
        with open(path) as f:
            return parse_config(f.read())
    except:
        print(f"Failed to load {unknown_var}")
        return {}
'''},
    [{"tool": "ruff", "rule_id": "E722", "severity": "medium",
      "file": "parser.py", "desc": "裸 except (2处)"},
     {"tool": "ruff", "rule_id": "F821", "severity": "medium",
      "file": "parser.py", "desc": "未定义变量 unknown_var"}]
)

# ============================================================
# s11: Ruff 复杂代码 — C901 (圈复杂度)
# ============================================================
init_repo(
    "s11_ruff_complexity", {},
    {"analyzer.py": '''"""Code complexity analyzer."""

def classify_issue(severity: str, category: str, source: str, has_fix: bool,
                   is_security: bool, line_count: int, file_ext: str) -> str:
    if severity == "critical":
        if is_security:
            return "P0-security"
        elif source == "bandit":
            return "P0-bandit"
        else:
            return "P0-critical"
    elif severity == "high":
        if is_security:
            if source == "bandit":
                return "P1-bandit-security"
            else:
                return "P1-other-security"
        elif category == "bug":
            if has_fix:
                return "P1-bug-fixable"
            else:
                return "P1-bug-nofix"
        else:
            if line_count > 100:
                return "P1-large"
            else:
                return "P1-high"
    elif severity == "medium":
        if category == "style":
            return "P2-style"
        elif file_ext == ".py":
            return "P2-python"
        else:
            return "P2-medium"
    else:
        if has_fix:
            return "P3-fixable"
        return "P3-low"
'''},
    [{"tool": "ruff", "rule_id": "C901", "severity": "low",
      "file": "analyzer.py", "desc": "圈复杂度过高"}]
)

# ============================================================
# s12: Ruff 安全检查 (S 系列) — S101 assert
# ============================================================
init_repo(
    "s12_ruff_assert", {},
    {"validate.py": '''"""Input validation with assert misuse."""

def validate_age(age):
    assert age > 0
    return True

def validate_email(email):
    assert "@" in email
    return True

def process_payment(amount):
    assert amount > 0, "Amount must be positive"
    assert amount < 1000000
    return f"Payment: ${amount}"
'''},
    [{"tool": "ruff", "rule_id": "S101", "severity": "low",
      "file": "validate.py", "desc": "使用 assert 做输入校验 (3处)"}]
)

# ============================================================
# s13: 依赖变更 (requirements.txt) — DependencyTool
# ============================================================
init_repo(
    "s13_dependency_change", {},
    {"app/main.py": '''"""Minimal Flask app."""
from flask import Flask
app = Flask(__name__)

@app.route("/")
def index():
    return "Hello World"
''',
     "requirements.txt": '''flask==2.0.1
requests==2.25.1
sqlalchemy==1.3.23
cryptography==3.3.2
pyyaml==5.3.1
'''},
    [{"tool": "dependency", "rule_id": "DEP_FILE_CHANGED", "severity": "low",
      "file": "requirements.txt", "desc": "requirements.txt 新增依赖"},
     {"tool": "dependency", "rule_id": "EXTERNAL_IMPORT", "severity": "low",
      "file": "app/main.py", "desc": "新增 flask 外部依赖导入"}]
)

# ============================================================
# s14: 依赖变更 (pyproject.toml) — DependencyTool
# ============================================================
init_repo(
    "s14_pyproject_dep", {},
    {"pyproject.toml": '''[project]
name = "my-app"
version = "0.1.0"
dependencies = [
    "fastapi>=0.100.0",
    "uvicorn[standard]",
    "pydantic>=2.0",
    "httpx",
    "python-jose[cryptography]",
]

[tool.ruff]
line-length = 100
''',
     "src/app.py": '''"""FastAPI app entry."""
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

class Item(BaseModel):
    name: str
    price: float

@app.get("/")
def root():
    return {"status": "ok"}
'''},
    [{"tool": "dependency", "rule_id": "DEP_FILE_CHANGED", "severity": "low",
      "file": "pyproject.toml", "desc": "pyproject.toml 新增依赖配置"},
     {"tool": "dependency", "rule_id": "EXTERNAL_IMPORT", "severity": "low",
      "file": "src/app.py", "desc": "新增 fastapi/pydantic 外部依赖导入"}]
)

# ============================================================
# s15: AST 符号提取验证 — python_ast 产出符号索引
# ============================================================
init_repo(
    "s15_ast_symbols", {},
    {"models.py": '''"""Data models with classes and functions."""
from dataclasses import dataclass
from typing import Optional, List
import json

@dataclass
class User:
    id: int
    name: str
    email: Optional[str] = None

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name}

class UserService:
    def __init__(self, db_url: str):
        self.db_url = db_url

    def create_user(self, name: str, email: str) -> User:
        user = User(id=1, name=name, email=email)
        return user

    def find_by_name(self, name: str) -> Optional[User]:
        return None

    def delete_all(self) -> int:
        return 0

def initialize_database(url: str, options: dict) -> bool:
    return True

def export_users(users: List[User], fmt: str = "json") -> str:
    data = [u.to_dict() for u in users]
    return json.dumps(data)
'''},
    [{"tool": "python_ast", "rule_id": "SYMBOL_INDEX", "severity": "info",
      "file": "models.py", "desc": "应提取 2 类 + 4 函数/方法的符号索引"}]
)

# ============================================================
# s16: 综合：ruff + bandit + dependency 三合一
# ============================================================
init_repo(
    "s16_combined_all_tools", {},
    {"auth.py": '''"""Authentication with issues."""
import os, json, hashlib
import pickle

SECRET_KEY = "hardcoded-key-abc123"

def login(username:str, password:str)->bool:
    if username=="admin" and password=="admin123":
        return True
    h = hashlib.md5(password.encode()).hexdigest()
    return h == "stored_hash"

def save_session(data):
    with open("/tmp/session.pkl", "wb") as f:
        pickle.dump(data, f)

def load_session():
    with open("/tmp/session.pkl", "rb") as f:
        return pickle.load(f)
''',
     "requirements.txt": '''flask==2.0.1
pyjwt==2.0.0
'''},
    [{"tool": "ruff", "rule_id": "F401", "severity": "low",
      "file": "auth.py", "desc": "os/json 未使用"},
     {"tool": "ruff", "rule_id": "E231", "severity": "low",
      "file": "auth.py", "desc": "参数列表中空格缺失"},
     {"tool": "bandit", "rule_id": "B105", "severity": "low",
      "file": "auth.py", "desc": "硬编码密钥/密码"},
     {"tool": "bandit", "rule_id": "B303", "severity": "medium",
      "file": "auth.py", "desc": "MD5 哈希"},
     {"tool": "bandit", "rule_id": "B301", "severity": "medium",
      "file": "auth.py", "desc": "pickle.load 反序列化"},
     {"tool": "dependency", "rule_id": "DEP_FILE_CHANGED", "severity": "low",
      "file": "requirements.txt", "desc": "依赖文件变更"}]
)

# ============================================================
# s17: 非代码 + 代码混合
# ============================================================
init_repo(
    "s17_mixed_code_config", {},
    {"README.md": """# Project Title

## Installation
pip install -r requirements.txt
## Usage
python main.py
""",
     "settings.json": """{
  "debug": false,
  "secret_key": "prod-secret-in-config",
  "db_url": "postgres://localhost/mydb"
}""",
     "main.py": '''"""Main entry point."""
import subprocess
import json

def load_settings():
    with open("settings.json") as f:
        cfg = json.load(f)
    return cfg

def run_backup():
    subprocess.run("tar -czf backup.tar.gz data/", shell=True)

if __name__ == "__main__":
    settings = load_settings()
    print("Starting with debug=", settings.get("debug"))
    run_backup()
'''},
    [{"tool": "bandit", "rule_id": "B602", "severity": "high",
      "file": "main.py", "desc": "subprocess shell=True"},
     {"tool": "python_ast", "rule_id": "SYMBOL_INDEX", "severity": "info",
      "file": "main.py", "desc": "应提取 3 个函数/入口的符号"}]
)

# ============================================================
# s18: 纯 Python 无风险 — 仅 git + python_ast + ruff
# ============================================================
init_repo(
    "s18_clean_python", {},
    {"utils.py": '''"""Utility functions — clean code."""
from pathlib import Path
from typing import Optional
import json


def read_json(path: str) -> Optional[dict]:
    """Read and parse a JSON file safely."""
    file_path = Path(path)
    if not file_path.exists():
        return None
    with open(file_path, encoding="utf-8") as f:
        return json.load(f)


def safe_divide(a: float, b: float) -> float:
    """Divide with zero check."""
    if b == 0:
        return 0.0
    return a / b


def chunk_list(items: list, size: int) -> list[list]:
    """Split list into chunks."""
    if size <= 0:
        raise ValueError("chunk size must be positive")
    return [items[i:i + size] for i in range(0, len(items), size)]
'''},
    [{"tool": "python_ast", "rule_id": "SYMBOL_INDEX", "severity": "info",
      "file": "utils.py", "desc": "无风险代码，仅验证 git+ast+ruff 正常运行"},
     {"note": "此样本预期 0 个 bandit Issue，ruff 0 个 Issue（代码风格符合规范）"}]
)

# ============================================================
# s19: 仅删除文件 — 边界情况
# ============================================================
init_repo(
    "s19_delete_only", {},
    {},
    [{"note": "纯删除操作，预期 git 只有一个 deleted 文件记录，无其他工具触发"}]
)
# 手动处理：先创建文件，commit，再删除
_repo19 = SAMPLES_DIR / "s19_delete_only"
(_repo19 / "old_module.py").write_text("# this file will be removed\nprint('hello')\n", encoding="utf-8")
subprocess.run(["git", "-C", str(_repo19), "add", "-A"], check=True, capture_output=True)
subprocess.run(["git", "-C", str(_repo19), "commit", "-m", "initial: add module"], check=True, capture_output=True)
(_repo19 / "old_module.py").unlink()
subprocess.run(["git", "-C", str(_repo19), "add", "-A"], check=True, capture_output=True)
subprocess.run(["git", "-C", str(_repo19), "commit", "-m", "chore: remove old module"], check=True, capture_output=True)
meta19 = {
    "sample_id": "s19_delete_only",
    "expected_issues": [],
    "files": ["old_module.py"],
    "note": "纯删除，预期无 Issues，仅 git 记录变更",
}
(_repo19 / "_meta.json").write_text(json.dumps(meta19, ensure_ascii=False, indent=2), encoding="utf-8")

# ============================================================
# s20: 非 Python 文件 (JS + JSON) — 仅 git
# ============================================================
init_repo(
    "s20_non_python", {},
    {"package.json": '''{
  "name": "test-project",
  "version": "1.0.0",
  "dependencies": {
    "express": "4.17.1",
    "lodash": "4.17.20"
  }
}''',
     "index.js": '''const express = require("express");
const app = express();

app.get("/user/:id", (req, res) => {
  const query = "SELECT * FROM users WHERE id = " + req.params.id;
  db.query(query, (err, result) => {
    res.json(result);
  });
});

app.listen(3000);
'''},
    [{"note": "非 Python 项目，预期仅 git 触发，ruff/bandit/ast/dependency 均不适用"}]
)

# ---- 保存索引 ----
_index = []
for d in sorted(SAMPLES_DIR.iterdir()):
    if (d / "_meta.json").exists():
        meta = json.loads((d / "_meta.json").read_text(encoding="utf-8"))
        _index.append({"sample_id": d.name, "dir": str(d),
                       "expected_issues": meta.get("expected_issues", [])})

index_path = SAMPLES_DIR / "_index.json"
index_path.write_text(json.dumps(_index, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"生成完成: {len(_index)} 个样本")
print(f"目录: {SAMPLES_DIR}")
for entry in _index:
    n = len(entry["expected_issues"])
    print(f"  {entry['sample_id']} — {n} expected issues")
