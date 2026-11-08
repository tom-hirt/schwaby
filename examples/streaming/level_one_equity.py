"""A complete streaming consumer, with backpressure.

The README shows the four lines that start a stream. This shows the shape a
long-running one wants: a queue between the socket and your processing, so a
slow consumer drops old data rather than stalling the reader, and an error
handler so a feed that quietly stops looks different from a quiet market.

Run it with a token file you already have. `schwaby.auth.easy_client` creates
one.
"""

import asyncio
import pprint

import schwaby
from schwaby.streaming import StreamClient

API_KEY = 'XXXXXX'
APP_SECRET = 'XXXXXX'
TOKEN_PATH = './token.json'

SYMBOLS = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'SPY']


class StreamConsumer:
    def __init__(self, api_key, app_secret, token_path, queue_size=100):
        self.api_key = api_key
        self.app_secret = app_secret
        self.token_path = token_path

        self.client = None
        self.stream_client = None

        # Bounded on purpose. An unbounded queue in front of a slow consumer
        # does not fix anything -- it converts falling behind into growing
        # memory, and the data at the front is the stalest.
        self.queue = asyncio.Queue(queue_size)

    def initialize(self):
        self.client = schwaby.auth.client_from_token_file(
                self.token_path, api_key=self.api_key,
                app_secret=self.app_secret)

        self.stream_client = StreamClient(self.client)

        self.stream_client.add_level_one_equity_handler(
                self.handle_level_one_equity)

        # Failures this client absorbs rather than raising: a handler that
        # raised, a late rejection of a request nobody is waiting for, a frame
        # that will not parse. Without this they are logged and nothing else,
        # so a broken feed is indistinguishable from a quiet one.
        self.stream_client.add_error_handler(self.handle_error)

    async def stream(self):
        await self.stream_client.login()
        await self.stream_client.level_one_equity_subs(SYMBOLS)

        asyncio.ensure_future(self.handle_queue())

        while True:
            await self.stream_client.handle_message()

    async def handle_level_one_equity(self, msg):
        # Called from the reader, so it must not block. Anything slow belongs
        # on the other side of the queue.
        if self.queue.full():
            await self.queue.get()   # drop the oldest
        await self.queue.put(msg)

    async def handle_error(self, service, exception, message):
        # Note the order: service first. And do not treat service and message
        # both being None as a signature meaning anything. It is not a
        # contract, and a kind of failure added later with nothing to say
        # would arrive the same way, so tell them apart by the exception's
        # type.
        print('stream error on %r: %r' % (service, exception))

    async def handle_queue(self):
        while True:
            msg = await self.queue.get()
            pprint.pprint(msg)


async def main():
    consumer = StreamConsumer(API_KEY, APP_SECRET, TOKEN_PATH)
    consumer.initialize()
    await consumer.stream()


if __name__ == '__main__':
    asyncio.run(main())
