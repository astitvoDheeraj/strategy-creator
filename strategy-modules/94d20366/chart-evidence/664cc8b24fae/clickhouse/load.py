"""Load this acceptance snapshot into the local ClickHouse started by .github/workflows/copilot-setup-steps.yml.

Usage: python load.py   (then run pytest with CLICKHOUSE_HOST=localhost CLICKHOUSE_PASSWORD=acceptance)
"""
import base64
import glob
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
URL = f"http://{os.getenv('CLICKHOUSE_HOST', 'localhost')}:{os.getenv('CLICKHOUSE_PORT', '8123')}/"
AUTH = base64.b64encode(f"{os.getenv('CLICKHOUSE_USERNAME', 'default')}:{os.getenv('CLICKHOUSE_PASSWORD', 'acceptance')}".encode()).decode()


def post(body, query=None, gz=False):
    params = {"input_format_skip_unknown_fields": 1, "date_time_input_format": "best_effort"}
    if query:
        params["query"] = query
    headers = {"Authorization": "Basic " + AUTH}
    if gz:
        headers["Content-Encoding"] = "gzip"
    req = urllib.request.Request(URL + "?" + urllib.parse.urlencode(params), data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read().decode()


for _ in range(60):
    try:
        urllib.request.urlopen(URL + "ping", timeout=5)
        break
    except Exception:
        time.sleep(2)
else:
    sys.exit("Local ClickHouse is not running on " + URL + " - is copilot-setup-steps.yml installed?")

with open(os.path.join(HERE, "schema.sql"), encoding="utf-8") as f:
    for statement in f.read().split(";\n\n"):
        if statement.strip():
            post(statement.strip().encode())
for path in sorted(glob.glob(os.path.join(HERE, "data", "*.jsonl.gz"))):
    table = os.path.basename(path)[:-len(".jsonl.gz")]
    post(f"TRUNCATE TABLE IF EXISTS {table}".encode())
    with open(path, "rb") as f:
        post(f.read(), query=f"INSERT INTO {table} FORMAT JSONEachRow", gz=True)
    print(table, post(f"SELECT count() FROM {table}".encode()).strip(), "rows")
