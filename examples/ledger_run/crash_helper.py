import sys
sys.path.insert(0, '/workspace')
from migration_ledger import Ledger
import json
import os
ledger = Ledger.open('/workspace/examples/ledger_run/crash.db')
payload = json.dumps({'seq': 2, 'facts': [{'id': 'CRASH', 'old_space': 'legacy', 'old_code': 6, 'amount': 9}]}).encode()
ledger.submit_bytes(payload, precommit_hook=lambda seq: os._exit(7))
