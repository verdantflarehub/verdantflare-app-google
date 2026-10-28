"""Studio-only management listener for Google AI Studio channel accounts."""
import asyncio
import hmac
import json
import os
import re
import uuid
from pathlib import Path
from contextlib import contextmanager

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
from .execution import Execution, MODEL, IMAGE_MODELS, VIDEO_MODELS, image
from .omni import payload as omni_payload
from .video_pricing import video_preflight, RETIRED_VIDEO_MODELS, OMNI_MODEL
from .analysis import MIMES as ANALYSIS_MIMES, MAX_BYTES as ANALYSIS_MAX_BYTES
from .analysis_tls import analysis_tls_options
from .metadata import DEFAULT_IMAGE_CAPABILITIES, analysis_capabilities, catalog_version, image_capabilities, video_capabilities
from .ownership import execution_owner
from .discovery_lifecycle import configured_publisher, PublicationLifetime, serve_owned_servers
from .proxy import NetworkProxies, ProxyUnavailable, proxy_reference

IDENTITY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')


class APIError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


def response(data, status=200):
    if status >= 400:
        code = str(data.get('error', '')).upper()
        data = {**data, 'code': code if re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', code) else 'REQUEST_FAILED'}
    return JSONResponse(data, status_code=status, headers={'Cache-Control': 'no-store'})


def requested_proxy(value):
    """Malformed management input is 400; unavailable Network references are 503."""
    try:
        return proxy_reference(value)
    except ProxyUnavailable:
        raise ValueError('invalid_proxy_reference') from None


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
    def __init__(self, store, client=None, *, network=None):
        self.accepting = True
        self.accounts = Accounts(store)
        self.calls = Calls(store)
        self.client = client or httpx.Client(follow_redirects=False, timeout=15, trust_env=False)
        self.network = network
        self.execution = Execution(self.calls, self.client, transport=self.transport)

    def validate_proxy(self, organization, value):
        value = proxy_reference(value)
        if value['mode'] == 'proxy':
            if self.network is None:
                raise ProxyUnavailable()
            return self.network.resolve_url(organization, value)
        return None

    @contextmanager
    def transport(self, organization, value):
        url = self.validate_proxy(organization, value)
        if url is None:
            yield self.client
            return
        try:
            client = httpx.Client(proxy=url, trust_env=False, follow_redirects=False, timeout=15)
        except (ValueError, ImportError):
            raise ProxyUnavailable() from None
        with client:
            yield client

    def check(self, account, key, organization=None):
        # Revalidate the saved origin against current controlled egress configuration.
        base = origin(account['api_url'])
        try:
            with self.transport(organization, account['proxy']) as client:
                result = client.get(base + '/v1beta/models', params={'pageSize': 1},
                                    headers={'x-goog-api-key': key}, timeout=15)
        except httpx.HTTPError:
            return 'unavailable'
        if result.status_code in {401, 403}:
            return 'invalid'
        if result.status_code == 400:
            try:
                data = result.json()
                error = data.get('error') if isinstance(data, dict) else None
                details = error.get('details') if isinstance(error, dict) else None
                if isinstance(details, list) and any(isinstance(item, dict) and
                        item.get('reason') == 'API_KEY_INVALID' for item in details):
                    return 'invalid'
            except ValueError:
                pass
            return 'unavailable'
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
        state = await run_in_threadpool(service.check, account, key, org)
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
            if path == '/v1/activity':
                with accounts.store.connect() as db:
                    rows = db.execute('SELECT state,COUNT(*) FROM channel_calls GROUP BY state').fetchall()
                terminal = {'completed', 'failed', 'rejected'}
                active = {'acceptance_unknown', 'queued', 'in_queue', 'in_progress'}
                if any(state not in terminal | active for state, _ in rows):
                    raise APIError('activity_unavailable', 503)
                return response({'schema_version': 1, 'scope': 'installation',
                    'submitted_count': sum(count for _, count in rows),
                    'active_count': sum(count for state, count in rows if state in active)})
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
            if path == '/v1/proxies':
                return response(await run_in_threadpool(service.network.list, org) if service.network else
                                {'schema_version': 1, 'items': []})
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
                value['proxy'] = requested_proxy(value.get('proxy'))
                operation = value.pop('operation_id', request.headers.get('idempotency-key', ''))
                if request.headers.get('idempotency-key', operation) != operation:
                    raise ValueError('idempotency_key_mismatch')
                return response(await run_in_threadpool(accounts.create, org, actor, operation, value,
                    validate_proxy=lambda proxy: service.validate_proxy(org, proxy)), 201)
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
                              'proxy': requested_proxy(value.get('proxy', account['proxy']))}
                candidate = {**account, **connection}
                state = await run_in_threadpool(service.check, candidate, key, org)
                updated = accounts.checked(org, actor, account_id, revision, state, key, connection)
                return response({'schema_version': 1, 'account': updated, 'authorization_state': state},
                                200 if state == 'active' else 422 if state == 'invalid' else 503)
            if request.method == 'GET':
                return response(accounts.get(org, account_id))
            value = await body(request)
            revision = value.pop('expected_revision', None)
            return response(accounts.patch(org, actor, account_id, revision, value, archive=request.method == 'DELETE'))
        except ProxyUnavailable:
            return response({'error': 'proxy_unavailable'}, 503)
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
                         'management': True, 'execution_capabilities': ['image', 'video'],
                         'recovery_capabilities': ['video']})

    return Starlette(routes=[Route('/health', health),
        Route('/v1/activity', manage),
        Route('/v1/proxies', manage),
        Route('/internal/dashboard-declaration', manage), Route('/internal/dashboard-index', manage),
        Route('/internal/dashboard/{asset:path}', manage),
        Route('/v1/calls', manage), Route('/v1/calls/search', manage, methods=['POST']),
        Route('/v1/accounts', manage, methods=['GET', 'POST']),
        Route('/v1/accounts/search', manage, methods=['POST']),
        Route('/v1/accounts/{account_id:uuid}', manage, methods=['GET', 'PATCH', 'DELETE']),
        Route('/v1/accounts/{account_id:uuid}/refresh', manage, methods=['POST']),
        Route('/v1/accounts/{account_id:uuid}/credential', manage, methods=['PUT'])])


def create_execution(service, token, *, analysis_token='', analysis_enabled=False, analysis_catalog=(), image_catalog=None,
                     video_catalog=()):
    if (analysis_enabled and not analysis_token) or (analysis_token and
            (len(analysis_token) < 32 or hmac.compare_digest(analysis_token, token))):
        raise ValueError('separate_analysis_token_required')
    available = {item['model_id'] for item in analysis_catalog if item['execution_state'] == 'enabled'}
    resolved_image_catalog = [dict(item) for item in (DEFAULT_IMAGE_CAPABILITIES if image_catalog is None else image_catalog)]
    resolved_video_catalog = [dict(item) for item in video_catalog]
    available_video = {item['model_id'] for item in resolved_video_catalog
                       if item.get('execution_state') == 'enabled' and item.get('model_id') in VIDEO_MODELS - RETIRED_VIDEO_MODELS}
    async def execute(request):
        try:
            caller = request.headers.get('x-caller-id', '')
            expected_token = analysis_token if caller == 'world-analysis' else token
            authorization = request.headers.getlist('authorization')
            if len(expected_token) < 32 or len(authorization) != 1 or not hmac.compare_digest(authorization[0], 'Bearer ' + expected_token):
                raise APIError('unauthorized', 401)
            if caller not in {'image-mcp', 'video-mcp', 'world-analysis'}:
                raise APIError('forbidden', 403)
            if request.url.path == '/v1/capabilities':
                if caller not in {'image-mcp', 'video-mcp'}:
                    raise APIError('forbidden', 403)
                published = []
                selected_catalog = resolved_video_catalog if caller == 'video-mcp' else resolved_image_catalog
                implemented = VIDEO_MODELS - RETIRED_VIDEO_MODELS if caller == 'video-mcp' else IMAGE_MODELS
                for item in selected_catalog:
                    active = (item.get('model_id') in implemented and item.get('execution_state') == 'enabled' and service.accepting)
                    published.append(dict(item, execution_state='enabled' if active else (
                        'disabled' if not service.accepting or item.get('model_id') not in implemented
                        else item.get('execution_state', 'disabled'))))
                models = []
                for item in published:
                    mid = item.get('model_id')
                    if item.get('execution_state') == 'enabled' and mid not in models:
                        models.append(mid)
                return response({
                    'schema_version': 1,
                    'channel_id': 'google',
                    'version': catalog_version(selected_catalog),
                    'default_model_id': MODEL if caller == 'image-mcp' else None,
                    'execution_state': 'enabled' if models and service.accepting else 'disabled',
                    'models': models,
                    'capabilities': published,
                })
            org = request.headers.get('x-organization-id', '')
            if not IDENTITY.fullmatch(org):
                raise APIError('forbidden', 403)
            if request.url.path == '/v1/video-preflight':
                if caller != 'video-mcp':
                    raise APIError('forbidden', 403)
                value = await body(request, 64 * 1024 * 1024)
                if (not isinstance(value, dict) or set(value) - {'model_id', 'payload', 'account_id', 'budget_limit'}
                        or not isinstance(value.get('model_id'), str) or not 1 <= len(value['model_id']) <= 200
                        or not isinstance(value.get('payload'), dict)):
                    raise ValueError('invalid_request')
                if value['model_id'] == OMNI_MODEL and OMNI_MODEL in available_video:
                    account = value.get('account_id')
                    if account is not None:
                        account = str(uuid.UUID(account))
                    result = await run_in_threadpool(service.execution.omni.preflight, org,
                        omni_payload(value['payload'], image), account, value.get('budget_limit'))
                else:
                    result = video_preflight(value['model_id'])
                if not service.accepting:
                    result['blocked_reason'] = 'channel_unavailable'
                    result['can_submit'] = False
                return response(result)
            if request.url.path == '/v1/analysis/capabilities':
                if caller != 'world-analysis':
                    raise APIError('forbidden', 403)
                return response({'schema_version': 1, 'channel_id': 'google', 'capability': 'analysis',
                                 'execution_state': 'enabled' if available and analysis_enabled and service.accepting else 'disabled',
                                 'models': sorted(available), 'capabilities': [dict(item, execution_state=item['execution_state']
                                     if analysis_enabled and service.accepting else 'disabled') for item in analysis_catalog],
                                 'input_mime_types': sorted(ANALYSIS_MIMES),
                                 'max_media_bytes': ANALYSIS_MAX_BYTES, 'supports_source_text': True,
                                 'supports_video_range': True, 'max_video_range_ms': 300000,
                                 'supports_response_schema': True, 'max_response_schema_bytes': 32768})
            if request.method == 'POST':
                if not service.accepting or caller == 'world-analysis' and not analysis_enabled:
                    return response({'error': 'channel_unavailable', 'schema_version': 1,
                                     'accepted': False, 'state': 'rejected'}, 503)
                value = await body(request, (68 if caller == 'world-analysis' else 64) * 1024 * 1024)
                if set(value) - {'idempotency_key', 'task_id', 'model_id', 'capability', 'payload', 'account_id', 'budget_limit'}:
                    raise ValueError('invalid_request')
                capability = value.get('capability')
                if capability != {'image-mcp': 'image', 'video-mcp': 'video', 'world-analysis': 'analysis'}[caller]:
                    raise APIError('capability_not_supported')
                if caller == 'world-analysis' and value.get('model_id') not in available:
                    raise APIError('capability_not_supported')
                for field in ('idempotency_key', 'task_id'):
                    if not isinstance(value.get(field), str) or not IDENTITY.fullmatch(value[field]):
                        raise ValueError('invalid_identity')
                if caller == 'video-mcp' and value.get('model_id') == OMNI_MODEL and OMNI_MODEL not in available_video:
                    try:
                        service.calls.by_operation(org, caller, value['idempotency_key'])
                    except KeyError:
                        raise APIError('capability_not_supported') from None
                account = value.get('account_id')
                if account is not None:
                    account = str(uuid.UUID(account))
                return response(await run_in_threadpool(service.execution.submit, org, caller,
                    value['idempotency_key'], value['task_id'], value.get('model_id'), value.get('payload'),
                    account, value.get('budget_limit'), capability=capability))
            if 'operation_id' in request.path_params:
                return response(service.calls.by_operation(org, caller, request.path_params['operation_id']))
            call_id = str(request.path_params['call_id'])
            if request.url.path.endswith('/result'):
                row = service.calls.get(org, call_id, caller)
                if row.get('media') == 'video':
                    data = await run_in_threadpool(service.execution.video_result, org, caller, call_id)
                    return Response(data, media_type='video/mp4', headers={'Cache-Control': 'no-store'})
                return response(await run_in_threadpool(service.execution.result, org, caller, call_id))
            return response(await run_in_threadpool(service.execution.refresh, org, caller, call_id))
        except APIError as error:
            return response({'error': error.code}, error.status)
        except PermissionError:
            return response({'error': 'forbidden'}, 403)
        except Conflict as error:
            return response({'error': str(error)}, 409)
        except KeyError:
            return response({'error': 'call_not_found'}, 404)
        except (ValueError, TypeError, AttributeError):
            return response({'error': 'invalid_request'}, 400)
    return Starlette(routes=[Route('/v1/capabilities', execute), Route('/v1/analysis/capabilities', execute),
        Route('/v1/video-preflight', execute, methods=['POST']),
        Route('/v1/calls', execute, methods=['POST']),
        Route('/v1/calls/by-idempotency/{operation_id}', execute),
        Route('/v1/calls/{call_id:uuid}', execute), Route('/v1/calls/{call_id:uuid}/result', execute)])


async def main():
    tls_options = analysis_tls_options(os.environ)
    path = os.environ.get('GOOGLE_STATE_DB', '/data/google/state.sqlite3')
    with execution_owner(path):
        network = NetworkProxies(os.environ['GOOGLE_NETWORK_URL'], os.environ.get('GOOGLE_NETWORK_TOKEN', '')) if os.environ.get('GOOGLE_NETWORK_URL') else None
        service = Google(Store(path, os.environ['GOOGLE_ENCRYPTION_KEY']), network=network)
        analysis_enabled = os.environ.get('GOOGLE_ANALYSIS_ENABLED', 'false')
        if analysis_enabled not in {'true', 'false'}:
            raise ValueError('invalid_analysis_admission')
        metadata_file = os.environ.get('CHANNEL_METADATA_FILE')
        catalog = analysis_capabilities(metadata_file)
        img_catalog = image_capabilities(metadata_file)
        vid_catalog = video_capabilities(metadata_file)
        apps = [(create_app(service), 8000, {}), (create_execution(service, os.environ.get('GOOGLE_EXECUTION_TOKEN', ''),
                 analysis_token=os.environ.get('GOOGLE_ANALYSIS_TOKEN', ''), analysis_enabled=analysis_enabled == 'true',
                 analysis_catalog=catalog, image_catalog=img_catalog, video_catalog=vid_catalog), 8001, {})]
        if tls_options is not None:
            # No image/video credential is accepted on this listener. Both
            # execution listeners share the same durable analysis operations.
            apps.append((create_execution(service, '',
                analysis_token=os.environ['GOOGLE_ANALYSIS_TOKEN'],
                analysis_enabled=analysis_enabled == 'true', analysis_catalog=catalog), 8002, tls_options))
        publisher = configured_publisher(os.environ, service.accounts.store, __version__, analysis_catalog=catalog,
                                         video_catalog=vid_catalog)
        lifetime = None

        class ChannelServer(uvicorn.Server):
            def handle_exit(self, sig, frame):
                service.accepting = False
                if lifetime:
                    lifetime.begin_draining()
                super().handle_exit(sig, frame)
                for server in servers:
                    server.should_exit = True
                    server.force_exit = self.force_exit

        servers = [ChannelServer(uvicorn.Config(app, host='0.0.0.0', port=port,
                   access_log=False, **options)) for app, port, options in apps]
        publication_task = None
        if publisher:
            lifetime = PublicationLifetime(publisher, servers, lambda value: setattr(service, 'accepting', value))
            publication_task = asyncio.create_task(lifetime.run())
        try:
            def begin_draining():
                service.accepting = False
                if lifetime:
                    lifetime.begin_draining()
            await serve_owned_servers(servers, begin_draining)
        finally:
            try:
                if lifetime:
                    await lifetime.finish(publication_task)
            finally:
                service.client.close()
                if network:
                    network.close()


if __name__ == '__main__':
    asyncio.run(main())
