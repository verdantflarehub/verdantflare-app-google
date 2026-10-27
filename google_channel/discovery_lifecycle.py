"""Bind publication to the existing execution owner's server lifetime."""
import asyncio
import logging

from .discovery import Config, DiscoveryError, Publisher
from .video_pricing import RETIRED_VIDEO_MODELS
from .execution import VIDEO_MODELS

logger = logging.getLogger(__name__)


def configured_publisher(env, store, version, *, analysis_catalog=(), video_catalog=()):
    endpoint = env.get('CHANNEL_DISCOVERY_ETCD_URL')
    scope = env.get('CHANNEL_DISCOVERY_SCOPE_ID')
    if not endpoint and not scope:
        return None
    if not endpoint or not scope:
        raise ValueError('incomplete_discovery_configuration')
    config = Config(endpoint, scope, env.get('ETCD_USERNAME', ''), env.get('ETCD_PASSWORD', ''))
    capabilities = [{
        'capability_id': capability, 'adapter_version': '1',
        'execution_state': 'enabled'}
        for capability in ('image.generate', 'image.edit')]
    video_enabled = any(item.get('execution_state') == 'enabled'
                        and item.get('model_id') in VIDEO_MODELS - RETIRED_VIDEO_MODELS for item in video_catalog)
    capabilities.append({'capability_id': 'video.generate', 'adapter_version': '1',
                         'execution_state': 'enabled' if video_enabled else 'disabled'})
    if env.get('GOOGLE_ANALYSIS_ENABLED') == 'true' and any(item['execution_state'] == 'enabled' for item in analysis_catalog):
        capabilities.append({'capability_id': 'analysis.analyze', 'adapter_version': '1', 'execution_state': 'enabled'})
    return Publisher(config, store, version, capabilities)


class PublicationLifetime:
    def __init__(self, publisher, servers, set_accepting):
        self.publisher, self.servers, self.set_accepting = publisher, servers, set_accepting
        self.draining = asyncio.Event()
        self.finished = asyncio.Event()
        self.set_accepting(False)

    def begin_draining(self):
        # Called synchronously by the existing server signal handler: reject new
        # submissions immediately; already executing calls keep their identity.
        self.set_accepting(False)
        self.draining.set()

    async def _wait(self, seconds):
        try:
            await asyncio.wait_for(self.draining.wait(), seconds)
        except TimeoutError:
            pass

    async def run(self, interval=60):
        try:
            while not all(server.started for server in self.servers):
                if self.finished.is_set() or self.draining.is_set():
                    return
                await self._wait(.1)
            await asyncio.to_thread(self.publisher.resume)
            while True:
                if self.draining.is_set():
                    try:
                        await asyncio.to_thread(self.publisher.drain)
                        await asyncio.to_thread(self.publisher.publish)
                    except DiscoveryError:
                        logger.warning('channel_discovery_drain_unpublished')
                    await self.finished.wait()
                    return
                try:
                    await asyncio.to_thread(self.publisher.publish)
                    state = await asyncio.to_thread(self.publisher.state)
                    self.set_accepting(not self.draining.is_set() and state['state'] in ('enabled', 'degraded'))
                except DiscoveryError:
                    self.set_accepting(False)
                    logger.warning('channel_discovery_unavailable')
                    if self.publisher.fenced:
                        return
                await self._wait(interval)
        finally:
            self.set_accepting(False)
            await asyncio.to_thread(self.publisher.close)

    async def finish(self, task):
        self.begin_draining()
        self.finished.set()
        # Repeated cancellation must not cancel a running to_thread registry
        # request or release the execution owner before its lease cleanup ends.
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError


async def serve_owned_servers(servers, begin_draining):
    """Keep the execution owner until every listener and in-flight worker drains."""
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    cancelled = False
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        cancelled = True
    finally:
        begin_draining()
        for server in servers:
            server.should_exit = True
        drain = asyncio.gather(*tasks, return_exceptions=True)
        while not drain.done():
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError:
                cancelled = True
        results = drain.result()
    if cancelled:
        raise asyncio.CancelledError
    if any(isinstance(result, BaseException) for result in results) or any(not server.started for server in servers):
        raise RuntimeError('channel_listener_failed') from None
