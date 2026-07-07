"""Sample file with intentional issues for code review demo."""

import os
import sqlite3
import pickle

# BUG: mutable default argument
def add_item(item, items=[]):
    items.append(item)
    return items

# VULN: SQL injection via string formatting
def get_user(username):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    query = f"SELECT * FROM users WHERE name = '{username}'"
    cursor.execute(query)
    return cursor.fetchall()

# VULN: hardcoded credentials
API_KEY = "sk-1234567890abcdef"
DB_PASSWORD = "admin123"

# CODE SMELL: bare except
def parse_data(data):
    try:
        return pickle.loads(data)
    except:
        return None

# VULN: command injection
def ping_host(host):
    os.system("ping " + host)

# CODE SMELL: too broad exception + unused variable
def process_file(path):
    try:
        f = open(path)
        content = f.read()
        f.close()
        return content
    except Exception as e:
        print("something went wrong")
        return ""

# BUG: division by zero potential
def average(numbers):
    total = 0
    for n in numbers:
        total += n
    return total / len(numbers)

# CODE SMELL: global variable
counter = 0

def increment():
    global counter
    counter += 1
    return counter
