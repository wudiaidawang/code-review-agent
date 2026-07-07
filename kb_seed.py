"""知识库种子数据 — 代码规范 & 漏洞模式"""

CODE_STANDARDS = [
    # --- Google Python Style Guide 要点 ---
    {
        "id": "cs_001",
        "text": "函数和变量名使用 snake_case，类名使用 PascalCase。避免使用单字符变量名（计数器除外）。",
        "meta": {"source": "Google Python Style Guide", "category": "naming"},
    },
    {
        "id": "cs_002",
        "text": "每行代码不超过 80 字符。长字符串、URL、导入语句可例外。",
        "meta": {"source": "Google Python Style Guide", "category": "formatting"},
    },
    {
        "id": "cs_003",
        "text": "所有函数和类方法必须包含 docstring（单行或多行）。使用 Args/Returns/Raises 格式描述参数和返回值。",
        "meta": {"source": "Google Python Style Guide", "category": "documentation"},
    },
    {
        "id": "cs_004",
        "text": "import 语句按标准库、第三方库、本地模块分组，每组之间空一行。禁止使用 from module import *。",
        "meta": {"source": "Google Python Style Guide", "category": "imports"},
    },
    {
        "id": "cs_005",
        "text": "使用 try-except 时，捕获具体异常类型而非裸 except:。不在 finally 中使用 return/continue/break。",
        "meta": {"source": "Google Python Style Guide", "category": "exceptions"},
    },
    # --- 通用编码规范 ---
    {
        "id": "cs_006",
        "text": "避免使用可变默认参数（如 def f(x=[])）。应使用 None 作为默认值，在函数体内初始化。",
        "meta": {"source": "Common Python Best Practice", "category": "pitfalls"},
    },
    {
        "id": "cs_007",
        "text": "使用列表推导式代替 map/filter + lambda。但嵌套超过2层时应改用 for 循环。",
        "meta": {"source": "Common Python Best Practice", "category": "style"},
    },
    {
        "id": "cs_008",
        "text": "使用 with 语句管理文件、锁、连接等资源，确保正确释放。",
        "meta": {"source": "Common Python Best Practice", "category": "resource_management"},
    },
    {
        "id": "cs_009",
        "text": "类型注解：在新代码中为公开 API 添加类型注解。使用 typing 模块的 List, Dict, Optional 等（Python 3.9+ 可用内置泛型）。",
        "meta": {"source": "Google Python Style Guide", "category": "typing"},
    },
    {
        "id": "cs_010",
        "text": "避免使用全局变量。模块级常量使用 UPPER_SNAKE_CASE 命名。",
        "meta": {"source": "Google Python Style Guide", "category": "globals"},
    },
]

VULN_PATTERNS = [
    # --- OWASP Top 10 / CWE ---
    {
        "id": "vuln_001",
        "text": "[SQL注入] 使用字符串拼接或 f-string 构造 SQL 查询属于高危漏洞。应始终使用参数化查询（cursor.execute(sql, params)）或 ORM。",
        "meta": {"source": "OWASP Top 10 A03:2021", "category": "injection", "severity": "critical"},
    },
    {
        "id": "vuln_002",
        "text": "[命令注入] os.system()、subprocess.call() 使用 shell=True 且拼接用户输入属于严重漏洞。应使用 subprocess.run() 传参列表，或使用 shlex.quote() 转义。",
        "meta": {"source": "OWASP Top 10 A03:2021", "category": "injection", "severity": "critical"},
    },
    {
        "id": "vuln_003",
        "text": "[硬编码密钥] API Key、密码、Token 等敏感信息不得硬编码在源码中。应使用环境变量、密钥管理服务或 .env 文件（不提交到版本控制）。",
        "meta": {"source": "CWE-798", "category": "credentials", "severity": "high"},
    },
    {
        "id": "vuln_004",
        "text": "[路径遍历] 使用用户输入拼接文件路径可能导致任意文件读取。应使用 os.path.realpath() 验证路径在预期目录内。",
        "meta": {"source": "CWE-22", "category": "path_traversal", "severity": "high"},
    },
    {
        "id": "vuln_005",
        "text": "[不安全的反序列化] pickle.load() 加载不可信数据可导致远程代码执行。应使用 JSON、protobuf 等安全格式替代 pickle。",
        "meta": {"source": "CWE-502", "category": "deserialization", "severity": "high"},
    },
    {
        "id": "vuln_006",
        "text": "[XSS] 在 Web 应用中，未转义的用户输入直接渲染到 HTML 可导致跨站脚本攻击。应使用模板引擎的自动转义功能（如 Jinja2 的 autoescape）。",
        "meta": {"source": "OWASP Top 10 A07:2017", "category": "xss", "severity": "high"},
    },
    {
        "id": "vuln_007",
        "text": "[不安全的随机数] random.random() 不适合生成安全令牌、密码重置链接等。应使用 secrets.token_hex() 或 os.urandom()。",
        "meta": {"source": "CWE-338", "category": "crypto", "severity": "medium"},
    },
    {
        "id": "vuln_008",
        "text": "[资源泄露] 未使用 with 语句或 try-finally 关闭文件/连接/锁，可能导致资源泄露和拒绝服务。",
        "meta": {"source": "CWE-404", "category": "resource_leak", "severity": "medium"},
    },
    {
        "id": "vuln_009",
        "text": "[弱加密] MD5、SHA1 不再安全，不应用于密码哈希或签名验证。密码哈希应使用 bcrypt/scrypt/argon2，签名应使用 SHA-256 或更高。",
        "meta": {"source": "CWE-327", "category": "crypto", "severity": "high"},
    },
    {
        "id": "vuln_010",
        "text": "[调试代码残留] print()、console.log()、debug 模式开启等调试代码不应出现在生产代码中。可能泄露敏感信息。",
        "meta": {"source": "CWE-489", "category": "debug", "severity": "low"},
    },
]


def seed_kb(kb) -> None:
    """向知识库灌入种子数据（幂等 — 通过 collection.count() 判断）"""
    if kb.code_standards.count() == 0:
        kb.add_code_standards(CODE_STANDARDS)
        print(f"  [code_standards] seeded {len(CODE_STANDARDS)} entries")

    if kb.vuln_patterns.count() == 0:
        kb.add_vuln_patterns(VULN_PATTERNS)
        print(f"  [vuln_patterns]  seeded {len(VULN_PATTERNS)} entries")
