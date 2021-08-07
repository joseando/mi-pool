import asyncio
import itertools
import logging
import time

from .store.abstract import AbstractPoolStore

from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint64
from decimal import Decimal
from typing import Optional

logger = logging.getLogger('partials')


class PartialsInterval(object):

    def __init__(self, keep_interval):
        self.partials = []
        self.points = 0
        self.additions = itertools.count()
        self.keep_interval = keep_interval

    def __repr__(self):
        return f'<PartialsInterval[{self.points}]>'

    def add(self, timestamp, difficulty, remove=True):
        self.partials.append((timestamp, difficulty))
        self.points += difficulty

        if remove:
            drop_time = int(time.time()) - self.keep_interval
            while self.partials:
                timestamp, difficulty = self.partials[0]
                if timestamp < drop_time:
                    del self.partials[0]
                    self.points -= difficulty
                else:
                    # We assume `partials` is a list in chronological order
                    break
        return next(self.additions)


class PartialsCache(dict):

    def __init__(
            self, *args,
            store=None, config=None, pool_config=None, keep_interval: int = 86400,
            **kwargs):
        self.store = store
        self.config = config
        self.pool_config = pool_config
        self.keep_interval = keep_interval
        self.all = PartialsInterval(keep_interval)
        self._lock = asyncio.Lock()
        super().__init__(*args, **kwargs)

    def __missing__(self, launcher_id):
        pi = PartialsInterval(self.keep_interval)
        self[launcher_id] = pi
        return pi

    async def __aenter__(self, *args, **kwargs):
        await self._lock.acquire()

    async def __aexit__(self, *args, **kwargs):
        self._lock.release()

    async def add(self, launcher_id, timestamp, difficulty):
        if launcher_id not in self:
            self[launcher_id] = PartialsInterval(self.keep_interval)

        async with self._lock:
            additions = self[launcher_id].add(timestamp, difficulty)
            self.all.add(timestamp, difficulty)
        # Update estimated size and PPLNS every 5 partials
        if additions % 5 == 0:
            if self[launcher_id].keep_interval == self.pool_config['time_target']:
                points = self[launcher_id].points
            else:
                last_time_target = timestamp - self.pool_config['time_target']
                points = sum(map(
                    lambda x: x[1],
                    filter(lambda x: x[0] >= last_time_target, self[launcher_id].partials),
                ))

            estimated_size = int(points / (self.pool_config['time_target'] * 1.088e-15))
            if self.config['full_node']['selected_network'] == 'testnet7':
                estimated_size = int(estimated_size / 14680000)

            share_pplns = Decimal(points) / Decimal(self.all.points)
            logger.info(
                'Updating %r with points of %d (%.3f GiB), PPLNS %.5f',
                launcher_id,
                points,
                estimated_size / 1073741824,  # 1024 ^ 3
                share_pplns,
            )
            await self.store.update_estimated_size_and_pplns(
                launcher_id, estimated_size, points, share_pplns
            )


class Partials(object):

    def __init__(self, store: AbstractPoolStore, config, pool_config):
        self.store = store
        self.config = config
        self.pool_config = pool_config
        # By default keep partials for the last day
        self.keep_interval = pool_config.get('pplns_interval', 86400)

        self.cache = PartialsCache(
            store=store,
            config=config,
            pool_config=pool_config,
            keep_interval=self.keep_interval,
        )

    async def load_from_store(self):
        """
        Fill in the cache from database when initializing.
        """
        start_time = int(time.time()) - self.keep_interval
        for lid, t, d in await self.store.get_recent_partials(start_time):
            self.cache[lid].add(t, d, remove=False)
            self.cache.all.add(t, d, remove=False)

    async def add_partial(self, launcher_id: bytes32, timestamp: uint64, difficulty: uint64, error: Optional[str] = None):

        # Add to database
        await self.store.add_partial(launcher_id, timestamp, difficulty, error)

        # Add to the cache and compute the estimated farm size if a successful partial
        if error is None:
            await self.cache.add(launcher_id.hex(), timestamp, difficulty)

    async def get_recent_partials(self, launcher_id: bytes32, number_of_partials: int):
        """
        Difficulty function expects descendent order.
        """
        return [
            (uint64(x[0]), uint64(x[1]))
            for x in reversed(self.cache[launcher_id.hex()].partials[-number_of_partials:])
        ]

    async def get_farmer_points_and_payout_instructions(self):
        launcher_id_and_ph = await self.store.get_launcher_id_and_payout_instructions()
        points_and_ph = []
        async with self.cache:
            for launcher_id, points_interval in self.cache.items():
                if points_interval.points == 0:
                    continue
                ph = launcher_id_and_ph.get(launcher_id)
                if ph is None:
                    logger.error(
                        'Did not find payout instructions for %r, points %d.',
                        launcher_id, points_interval.points,
                    )
                    continue
                points_and_ph.append((uint64(points_interval.points), ph))
        return points_and_ph, self.cache.all.points