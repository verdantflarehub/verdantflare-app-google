import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet
from google_channel.accounts import Accounts
from google_channel.calls import Calls
from google_channel.store import Store


class LogAccountOptionsTest(unittest.TestCase):
    def test_archived_options_require_history_and_stay_organization_scoped(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'GOOGLE_ALLOWED_API_ORIGINS': 'https://gateway.example.invalid'}):
            store = Store(str(Path(directory) / 'state.db'), Fernet.generate_key().decode())
            accounts, calls = Accounts(store), Calls(store)
            ids = []
            for org, name in [('org', 'Unused'), ('org', 'Historical'), ('foreign', 'Foreign')]:
                account = accounts.create(org, 'user', name, {'api_url': 'https://generativelanguage.googleapis.com',
                    'api_key': 'synthetic-key-for-log-tests', 'proxy': {'mode': 'direct'}, 'name': name})
                ids.append(account['account_id'])
                if name != 'Unused':
                    accounts.checked(org, 'user', account['account_id'], 1, 'active')
                    calls.reserve(org, 'image-mcp', name, name, 'model', {'prompt': 'test'}, account['account_id'], '1')
            self.assertEqual(len(calls.list('org')['accounts']), 2)
            with store.connect() as db:
                db.execute("UPDATE channel_accounts SET status='archived'")
            result = calls.list('org')
            self.assertEqual([a['account_id'] for a in result['accounts']], [ids[1]])
            self.assertEqual(result['total'], 1)
            self.assertEqual(result['items'][0]['account_status'], 'archived')
            self.assertEqual(calls.list('org', account=ids[1])['total'], 1)
