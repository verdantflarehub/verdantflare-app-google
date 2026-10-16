"""Studio-only management listener for Google AI Studio channel accounts."""
import asyncio
import hmac
import json
import os
import re
import uuid
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
import uvicorn

from . import __version__
from .accounts import Accounts
from .profile import origin
from .store import Store, Conflict
from .calls import Calls
from .dashboard import DashboardAssets
from .execution import Execution
from .ownership import execution_owner

IDENTITY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')


class APIError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


def response(data, status=200):
    if status >= 400:
        code = str(data.get('error', '')).upper()
        data = {**data, 'code': code if re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', code) else 'REQUEST_FAILED'}
    return JSONResponse(data, status_code=status, headers={'Cache-Control': 'no-store'})


async def body(request, limit=32768):
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise APIError('request_too_large', 413)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_key')
            result[key] = value
        return result
    value = json.loads(chunks, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError('invalid_request')
    return value


class Google:
    def __init__(self, store, client=None):
        self.accounts = Accounts(store)
        self.calls = Calls(store)
        self.client = client or httpx.Client(follow_redirects=False, timeout=15, trust_env=False)
        self.execution = Execution(self.calls, self.client)

    def check(self, account, key):
        if account['proxy'] != {'mode': 'direct'}:
            raise APIError('proxy_unavailable', 409)
        # Revalidate the saved origin against current controlled egress configuration.
        base = origin(account['api_url'])
        try:
            result = self.client.get(base + '/v1beta/models', params={'pageSize': 1},
                                     headers={'x-goog-api-key': key}, timeout=15)
        except httpx.HTTPError:
            return 'unavailable'
        if result.status_code in {401, 403}:
            return 'invalid'
        if result.status_code != 200:
            return 'unavailable'
        try:
            data = result.json()
            return 'active' if isinstance(data, dict) and isinstance(data.get('models'), list) else 'unavailable'
        except ValueError:
            return 'unavailable'


def create_app(service=None, token=None):
    service = service or Google(Store(os.environ.get('GOOGLE_STATE_DB', '/data/google/state.sqlite3'),
                                     os.environ['GOOGLE_ENCRYPTION_KEY']))
    accounts = service.accounts
    token = token if token is not None else os.environ.get('GOOGLE_MANAGEMENT_TOKEN', '')
    refreshes = {}
    dashboard = Path(os.environ.get('GOOGLE_DASHBOARD_DIR', '/app/dashboard'))
    assets = DashboardAssets(dashboard) if dashboard.is_dir() else None

    async def refresh(org, actor, account_id):
        account = accounts.get(org, account_id)
        revision, key = accounts.credential(org, account_id)
        if account['revision'] != revision:
            raise Conflict('revision_conflict')
        state = await run_in_threadpool(service.check, account, key)
        return {'schema_version': 1, 'account': accounts.checked(org, actor, account_id, revision, state),
                'authorization_state': state, 'balance_status': 'manual_only'}

    def finished(identity, task):
        if refreshes.get(identity) is task:
            refreshes.pop(identity)
        if not task.cancelled():
            task.exception()

    async def manage(request):
        try:
            authorization = request.headers.getlist('authorization')
            if len(token) < 32 or len(authorization) != 1 or not hmac.compare_digest(authorization[0], 'Bearer ' + token):
                raise APIError('unauthorized', 401)
            path = request.url.path
            if path.startswith('/internal/'):
                if assets is None:
                    raise APIError('dashboard_unavailable', 503)
                if path == '/internal/dashboard-declaration':
                    data, mime = assets.declaration, 'application/json'
                elif path == '/internal/dashboard-index':
                    data, mime = assets.index, 'application/json'
                else:
                    try:
                        data, mime = assets.asset(request.path_params['asset'])
                    except (KeyError, ValueError, OSError):
                        raise APIError('dashboard_asset_unavailable', 404) from None
                return Response(data, headers={'Content-Type': mime, 'Cache-Control': 'no-store',
                                               'X-Content-Type-Options': 'nosniff'})
            org, actor = request.headers.get('x-organization-id', ''), request.headers.get('x-user-id', '')
            if not IDENTITY.fullmatch(org) or not IDENTITY.fullmatch(actor):
                raise APIError('forbidden', 403)
            if path in {'/v1/calls', '/v1/calls/search'}:
                query = await body(request) if request.method == 'POST' else dict(request.query_params)
                if set(query) - {'account_id', 'state', 'search', 'limit', 'offset', 'media', 'created_from', 'created_before'}:
                    raise ValueError('invalid_filter')
                for key, value in query.items():
                    if key in {'limit', 'offset'}:
                        if request.method == 'GET' and isinstance(value, str) and value.isascii() and value.isdigit():
                            query[key] = int(value)
                        if type(query[key]) is not int:
                            raise ValueError('invalid_pagination')
                    elif not isinstance(value, str) or len(value) > 200:
                        raise ValueError('invalid_filter')
                if 'account_id' in query:
                    query['account'] = query.pop('account_id')
                return response(service.calls.list(org, **query))
            if path == '/v1/accounts/search' or (path == '/v1/accounts' and request.method == 'GET'):
                query = await body(request) if request.method == 'POST' else dict(request.query_params)
                if set(query) - {'search', 'status', 'limit', 'offset'}:
                    raise ValueError('invalid_filter')
                for key, value in query.items():
                    if key in {'limit', 'offset'}:
                        if request.method == 'GET' and isinstance(value, str) and value.isascii() and value.isdigit():
                            query[key] = int(value)
                        if type(query[key]) is not int:
                            raise ValueError('invalid_pagination')
                    elif not isinstance(value, str) or len(value) > 200:
                        raise ValueError('invalid_filter')
                return response(accounts.list(org, **query))
            if path == '/v1/accounts':
                value = await body(request)
                operation = value.pop('operation_id', request.headers.get('idempotency-key', ''))
                if request.headers.get('idempotency-key', operation) != operation:
                    raise ValueError('idempotency_key_mismatch')
                return response(accounts.create(org, actor, operation, value), 201)
            account_id = str(request.path_params['account_id'])
            if path.endswith('/refresh'):
                identity = (org, account_id)
                task = refreshes.get(identity)
                if task is None:
                    task = asyncio.create_task(refresh(org, actor, account_id))
                    refreshes[identity] = task
                    task.add_done_callback(lambda task, identity=identity: finished(identity, task))
                return response(await asyncio.shield(task))
            if path.endswith('/credential'):
                value = await body(request)
                if set(value) - {'api_key', 'expected_revision', 'api_url', 'proxy'} or 'expected_revision' not in value:
                    raise ValueError('invalid_credential_request')
                key, revision = value.get('api_key'), value['expected_revision']
                if key is not None and (not isinstance(key, str) or not 16 <= len(key) <= 4096 or any(c.isspace() for c in key)):
                    raise ValueError('invalid_key')
                if type(revision) is not int or revision < 1:
                    raise ValueError('invalid_revision')
                account = accounts.get(org, account_id)
                if account['revision'] != revision:
                    raise Conflict('revision_conflict')
                if account['status'] == 'archived':
                    raise Conflict('account_archived')
                if key is None:
                    current_revision, key = accounts.credential(org, account_id)
                    if current_revision != revision:
                        raise Conflict('revision_conflict')
                connection = {'api_url': origin(value.get('api_url', account['api_url'])),
                              'proxy': value.get('proxy', account['proxy'])}
                candidate = {**account, **connection}
                state = await run_in_threadpool(service.check, candidate, key)
                updated = accounts.checked(org, actor, account_id, revision, state, key, connection)
                return response({'schema_version': 1, 'account': updated, 'authorization_state': state},
                                200 if state == 'active' else 422 if state == 'invalid' else 503)
            if request.method == 'GET':
                return response(accounts.get(org, account_id))
            value = await body(request)
            revision = value.pop('expected_revision', None)
            return response(accounts.patch(org, actor, account_id, revision, value, archive=request.method == 'DELETE'))
        except APIError as error:
            return response({'error': error.code}, error.status)
        except Conflict as error:
            return response({'error': str(error)}, 409)
        except KeyError:
            return response({'error': 'account_not_found'}, 404)
        except (ValueError, TypeError):
            return response({'error': 'invalid_account_request'}, 400)

    async def health(request):
        return response({'service': 'google-channel', 'version': __version__, 'status': 'ok',
                         'management': True, 'execution_capabilities': ['image']})

    return Starlette(routes=[Route('/health', health),
        Route('/internal/dashboard-declaration', manage), Route('/internal/dashboard-index', manage),
        Route('/internal/dashboard/{asset:path}', manage),
        Route('/v1/calls', manage), Route('/v1/calls/search', manage, methods=['POST']),
        Route('/v1/accounts', manage, methods=['GET', 'POST']),
        Route('/v1/accounts/search', manage, methods=['POST']),
        Route('/v1/accounts/{account_id:uuid}', manage, methods=['GET', 'PATCH', 'DELETE']),
        Route('/v1/accounts/{account_id:uuid}/refresh', manage, methods=['POST']),
        Route('/v1/accounts/{account_id:uuid}/credential', manage, methods=['PUT'])])


def create_execution(service, token):
    async def execute(request):
        try:
            authorization = request.headers.getlist('authorization')
            if len(token) < 32 or len(authorization) != 1 or not hmac.compare_digest(authorization[0], 'Bearer ' + token):
                raise APIError('unauthorized', 401)
            org = request.headers.get('x-organization-id', '')
            caller = request.headers.get('x-caller-id', '')
            if not IDENTITY.fullmatch(org) or caller != 'image-mcp':
                raise APIError('forbidden', 403)
            if request.method == 'POST':
                value = await body(request, 64 * 1024 * 1024)
                if set(value) - {'idempotency_key', 'task_id', 'model_id', 'capability', 'payload', 'account_id', 'budget_limit'}:
                    raise ValueError('invalid_request')
                if value.get('capability') != 'image':
                    raise APIError('capability_not_supported')
                for field in ('idempotency_key', 'task_id'):
                    if not isinstance(value.get(field), str) or not IDENTITY.fullmatch(value[field]):
                        raise ValueError('invalid_identity')
                account = value.get('account_id')
                if account is not None:
                    account = str(uuid.UUID(account))
                return response(await run_in_threadpool(service.execution.submit, org, caller,
                    value['idempotency_key'], value['task_id'], value.get('model_id'), value.get('payload'),
                    account, value.get('budget_limit')))
            if 'operation_id' in request.path_params:
                return response(service.calls.by_operation(org, caller, request.path_params['operation_id']))
            call_id = str(request.path_params['call_id'])
            if request.url.path.endswith('/result'):
                return response(service.execution.result(org, caller, call_id))
            return response(service.calls.project(service.calls.get(org, call_id, caller)))
        except APIError as error:
            return response({'error': error.code}, error.status)
        except Conflict as error:
            return response({'error': str(error)}, 409)
        except KeyError:
            return response({'error': 'call_not_found'}, 404)
        except (ValueError, TypeError, AttributeError):
            return response({'error': 'invalid_request'}, 400)
    return Starlette(routes=[Route('/v1/calls', execute, methods=['POST']),
        Route('/v1/calls/by-idempotency/{operation_id}', execute),
        Route('/v1/calls/{call_id:uuid}', execute), Route('/v1/calls/{call_id:uuid}/result', execute)])


async def main():
    path = os.environ.get('GOOGLE_STATE_DB', '/data/google/state.sqlite3')
    with execution_owner(path):
        service = Google(Store(path, os.environ['GOOGLE_ENCRYPTION_KEY']))
        apps = [(create_app(service), 8000), (create_execution(service, os.environ.get('GOOGLE_EXECUTION_TOKEN', '')), 8001)]
        servers = [uvicorn.Server(uvicorn.Config(app, host='0.0.0.0', port=port, access_log=False)) for app, port in apps]
        try:
            await asyncio.gather(*(server.serve() for server in servers))
        finally:
            service.client.close()


if __name__ == '__main__':
    asyncio.run(main())
