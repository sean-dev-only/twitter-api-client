import asyncio
import logging.config
import math
import platform
import random
from concurrent.futures import ThreadPoolExecutor, as_completed

import aiofiles
import httpx
import websockets
from httpx import AsyncClient, Limits, ReadTimeout, URL
from tqdm import tqdm
from tqdm.asyncio import tqdm_asyncio

from .constants import *
from .login import login
from .util import *

try:
    if get_ipython().__class__.__name__ == 'ZMQInteractiveShell':
        import nest_asyncio

        nest_asyncio.apply()
except:
    ...

if platform.system() != 'Windows':
    try:
        import uvloop

        uvloop.install()
    except ImportError as e:
        ...


class Scraper:
    def __init__(self, email: str = None, username: str = None, password: str = None, session: Client = None, **kwargs):
        self.guest = False
        self.logger = self.init_logger(kwargs.get('log_config', False))
        self.session = self.validate_session(email, username, password, session, **kwargs)
        self.save = kwargs.get('save', True)
        self.debug = kwargs.get('debug', 0)
        self.out_path = Path('data')
        self.api = 'https://twitter.com/i/api/graphql'

    @staticmethod
    def init_logger(cfg: dict) -> Logger:
        if cfg:
            logging.config.dictConfig(cfg)
            return logging.getLogger(__name__)
        return logger

    def validate_session(self, *args, **kwargs):
        email, username, password, session = args
        if session and all(session.cookies.get(c) for c in {'ct0', 'auth_token'}):
            # authenticated session provided
            return session
        if not session:
            # no session provided, login to authenticate
            return login(email, username, password, **kwargs)
        self.logger.warning(f'\n{RED}WARNING: This is a guest session, '
                            f'some endpoints cannot be accessed.{RESET}\n')
        self.guest = True
        return session

    def users(self, screen_names: list[str], **kwargs) -> list[dict]:
        return self._run(Operation.UserByScreenName, screen_names, **kwargs)

    def tweets_by_id(self, tweet_ids: list[int], **kwargs) -> list[dict]:
        return self._run(Operation.TweetResultByRestId, tweet_ids, **kwargs)

    def tweets_details(self, tweet_ids: list[int], **kwargs) -> list[dict]:
        return self._run(Operation.TweetDetail, tweet_ids, **kwargs)

    def tweets(self, user_ids: list[int], **kwargs) -> list[dict]:
        return self._run(Operation.UserTweets, user_ids, **kwargs)

    def tweets_and_replies(self, user_ids: list[int], **kwargs) -> list[dict]:
        return self._run(Operation.UserTweetsAndReplies, user_ids, **kwargs)

    def media(self, user_ids: list[int], **kwargs) -> list[dict]:
        return self._run(Operation.UserMedia, user_ids, **kwargs)

    def likes(self, user_ids: list[int], **kwargs) -> list[dict]:
        return self._run(Operation.Likes, user_ids, **kwargs)

    def followers(self, user_ids: list[int], **kwargs) -> list[dict]:
        return self._run(Operation.Followers, user_ids, **kwargs)

    def following(self, user_ids: list[int], **kwargs) -> list[dict]:
        return self._run(Operation.Following, user_ids, **kwargs)

    def favoriters(self, tweet_ids: list[int], **kwargs) -> list[dict]:
        return self._run(Operation.Favoriters, tweet_ids, **kwargs)

    def retweeters(self, tweet_ids: list[int], **kwargs) -> list[dict]:
        return self._run(Operation.Retweeters, tweet_ids, **kwargs)

    def profile_spotlights(self, screen_names: list[str], **kwargs) -> list[dict]:
        """
        This endpoint is included for completeness only. It returns very few data points.
        Use the batched query `users_by_ids` instead if you wish to pull user profile data.
        """
        return self._run(Operation.ProfileSpotlightsQuery, screen_names, **kwargs)

    def users_by_id(self, user_ids: list[int], **kwargs) -> list[dict]:
        """
        This endpoint is included for completeness only.
        Use the batched query `users_by_ids` instead if you wish to pull user profile data.
        """
        return self._run(Operation.UserByRestId, user_ids, **kwargs)

    def tweet_stats(self, user_ids: list[int], **kwargs) -> list[dict]:
        return self._run(Operation.TweetStats, user_ids, **kwargs)

    def users_by_rest_ids(self, user_ids: list[int], **kwargs) -> list[dict]:
        return self._run(Operation.UsersByRestIds, batch_ids(user_ids), **kwargs)

    def recommended_users(self, user_ids: list[int] = None, **kwargs) -> dict:
        if user_ids:
            contexts = [{"context": orjson.dumps({"contextualUserId": x}).decode()} for x in user_ids]
        else:
            contexts = [{'context': None}]
        return self._run(Operation.ConnectTabTimeline, contexts, **kwargs)

    def download_media(self, ids: list[int], photos: bool = True, videos: bool = True) -> None:
        tweets = self.tweets_by_id(ids)
        urls = []
        for tweet in tweets:
            tweet_ids = find_key(tweet, 'id_str')
            tweet_id = tweet_ids[0]
            url = f'https://twitter.com/i/status/{tweet_id}'  # `i` evaluates to screen_name
            media = [y for x in find_key(tweet, 'media') for y in x]
            if photos:
                photo_urls = list({u for m in media if 'ext_tw_video_thumb' not in (u := m['media_url_https'])})
                [urls.append([url, photo]) for photo in photo_urls]
            if videos:
                video_urls = [x['variants'] for m in media if (x := m.get('video_info'))]
                hq_videos = {sorted(v, key=lambda d: d.get('bitrate', 0))[-1]['url'] for v in video_urls}
                [urls.append([url, video]) for video in hq_videos]

        with tqdm(total=len(urls), desc='downloading media') as pbar:
            with ThreadPoolExecutor(max_workers=32) as e:
                for future in as_completed(e.submit(self._download, x, y) for x, y in urls):
                    future.result()
                    pbar.update()

    def _download(self, post_url: str, cdn_url: str, path: str = 'media', chunk_size: int = 4096) -> None:
        (self.out_path / 'media').mkdir(parents=True, exist_ok=True)
        name = urlsplit(post_url).path.replace('/', '_')[1:]
        ext = urlsplit(cdn_url).path.split('/')[-1]
        try:
            with httpx.stream('GET', cdn_url, headers=self.session.headers, cookies=self.session.cookies) as r:
                with open(f'{path}/{name}_{ext}', 'wb') as f:
                    for chunk in r.iter_bytes(chunk_size=chunk_size):
                        f.write(chunk)
        except Exception as e:
            self.logger.error(f'[{RED}error{RESET}] Failed to download media: {post_url} {e}')

    def trends(self) -> dict:
        """Get trends for all UTC offsets"""

        def get_trends(offset: str, url: str, headers: dict):
            try:
                headers['x-twitter-utcoffset'] = offset
                r = self.session.get(url, headers=headers)
                trends = find_key(r.json(), 'item')
                return {t['content']['trend']['name']: t for t in trends}
            except Exception as e:
                self.logger.error('Failed to get trends', e)

        headers = get_headers(self.session)
        url = set_qs('https://twitter.com/i/api/2/guide.json', trending_params)
        offsets = [f"{str(i).zfill(3)}00" if i < 0 else f"+{str(i).zfill(2)}00" for i in range(-12, 15)]
        trends = {}
        with tqdm(total=len(offsets), desc='downloading trends') as pbar:
            with ThreadPoolExecutor(max_workers=32) as e:
                for future in as_completed(e.submit(get_trends, o, url, headers) for o in offsets):
                    trends |= future.result()
                    pbar.update()

        path = self.out_path / 'raw/trends'
        path.mkdir(parents=True, exist_ok=True)
        (path / f'{time.time_ns()}.json').write_text(
            orjson.dumps(trends, option=orjson.OPT_INDENT_2).decode(),
            encoding='utf-8'
        )
        return trends

    def spaces(self, *, rooms: list[str] = None, search: list[dict] = None, audio: bool = False, chat: bool = False,
               **kwargs) -> list[dict]:
        if rooms:
            spaces = self._run(Operation.AudioSpaceById, rooms, **kwargs)
        else:
            res = self._run(Operation.AudioSpaceSearch, search, **kwargs)
            search_results = set(find_key(res, 'rest_id'))
            spaces = self._run(Operation.AudioSpaceById, search_results, **kwargs)
        if audio or chat:
            return self._get_space_data(spaces, audio, chat)
        return spaces

    def _get_space_data(self, spaces: list[dict], audio=True, chat=True):
        streams = self._check_streams(spaces)
        chat_data = None
        if chat:
            temp = []  # get necessary keys instead of passing large dicts
            for stream in filter(lambda x: x['stream'], streams):
                meta = stream['space']['data']['audioSpace']['metadata']
                if meta['state'] not in {SpaceState.Running, SpaceState.NotStarted}:
                    temp.append({
                        'rest_id': meta['rest_id'],
                        'chat_token': stream['stream']['chatToken'],
                        'media_key': meta['media_key'],
                        'state': meta['state'],
                    })
            chat_data = self._get_chat_data(temp)
        if audio:
            temp = []
            for stream in streams:
                if stream.get('stream'):
                    chunks = self._get_chunks(stream['stream']['source']['location'])
                    temp.append({
                        'rest_id': stream['space']['data']['audioSpace']['metadata']['rest_id'],
                        'chunks': chunks,
                    })
            self._download_audio(temp)
        return chat_data

    async def _get_stream(self, client: AsyncClient, media_key: str) -> dict | None:
        params = {
            'client': 'web',
            'use_syndication_guest_id': 'false',
            'cookie_set_host': 'twitter.com',
        }
        url = f'https://twitter.com/i/api/1.1/live_video_stream/status/{media_key}'
        try:
            r = await client.get(url, params=params)
            return r.json()
        except Exception as e:
            self.logger.error(f'stream not available for playback\n{e}')

    async def _init_chat(self, client: AsyncClient, chat_token: str) -> dict:
        payload = {'chat_token': chat_token}  # stream['chatToken']
        url = 'https://proxsee.pscp.tv/api/v2/accessChatPublic'
        r = await client.post(url, json=payload)
        return r.json()

    async def _get_chat(self, client: AsyncClient, endpoint: str, access_token: str, cursor: str = '') -> list[dict]:
        payload = {
            'access_token': access_token,
            'cursor': cursor,
            'limit': 1000,  # or 0
            'since': None,
            'quick_get': True,
        }
        url = f"{endpoint}/chatapi/v1/history"
        r = await client.post(url, json=payload)
        data = r.json()
        res = [data]
        while cursor := data.get('cursor'):
            try:
                r = await client.post(url, json=payload | {'cursor': cursor})
                if r.status_code == 503:
                    # not our fault, service error, something went wrong with the stream
                    break
                data = r.json()
                res.append(data)
            except ReadTimeout as e:
                self.logger.debug(f'End of chat data\n{e}')
                break

        parsed = []
        for r in res:
            messages = r.get('messages', [])
            for msg in messages:
                try:
                    msg['payload'] = orjson.loads(msg.get('payload', '{}'))
                    msg['payload']['body'] = orjson.loads(msg['payload'].get('body'))
                except Exception as e:
                    self.logger.error(f'Failed to parse chat message\n{e}')
            parsed.extend(messages)
        return parsed

    def _get_chunks(self, location: str) -> list[str]:
        try:
            url = URL(location)
            stream_type = url.params.get('type')
            r = self.session.get(
                url=location,
                params={'type': stream_type},
                headers={'authority': url.host}
            )
            # don't need an m3u8 parser
            chunks = re.findall('\n(chunk_.*)\n', r.text, flags=re.I)
            url = '/'.join(location.split('/')[:-1])
            return [f'{url}/{chunk}' for chunk in chunks]
        except Exception as e:
            self.logger.error(f'Failed to get chunks\n{e}')

    def _get_chat_data(self, keys: list[dict]) -> list[dict]:
        async def get(c: AsyncClient, key: dict) -> dict:
            info = await self._init_chat(c, key['chat_token'])
            chat = await self._get_chat(c, info['endpoint'], info['access_token'])
            if self.save:
                (self.out_path / 'raw' / f"chat_{key['rest_id']}.json").write_bytes(orjson.dumps(chat))
            return {
                'space': key['rest_id'],
                'chat': chat,
                'info': info,
            }

        async def process():
            limits = Limits(max_connections=100, max_keepalive_connections=10)
            headers = self.session.headers if self.guest else get_headers(self.session)
            cookies = self.session.cookies
            async with AsyncClient(limits=limits, headers=headers, cookies=cookies, timeout=20) as c:
                return await tqdm_asyncio.gather(*(get(c, key) for key in keys), desc='downloading chat')

        return asyncio.run(process())

    def _download_audio(self, data: list[dict]) -> None:
        async def get(s: AsyncClient, chunk: str, rest_id: str) -> tuple:
            r = await s.get(chunk)
            return rest_id, r

        async def process(data: list[dict]) -> list:
            limits = Limits(max_connections=100, max_keepalive_connections=10)
            headers = self.session.headers if self.guest else get_headers(self.session)
            cookies = self.session.cookies
            async with AsyncClient(limits=limits, headers=headers, cookies=cookies, timeout=20) as c:
                tasks = []
                for d in data:
                    tasks.extend([get(c, chunk, d['rest_id']) for chunk in d['chunks']])
                return await tqdm_asyncio.gather(*tasks, desc='downloading audio')

        chunks = asyncio.run(process(data))
        streams = {}
        [streams.setdefault(_id, []).append(chunk) for _id, chunk in chunks]
        # ensure chunks are in correct order
        for k, v in streams.items():
            streams[k] = sorted(v, key=lambda x: int(re.findall('_(\d+)_\w\.aac$', x.url.path)[0]))
        out = self.out_path / 'audio'
        out.mkdir(parents=True, exist_ok=True)
        for space_id, chunks in streams.items():
            # 1hr ~= 50mb
            with open(out / f'{space_id}.aac', 'wb') as fp:
                [fp.write(c.content) for c in chunks]

    def _check_streams(self, keys: list[dict]) -> list[dict]:
        async def get(c: AsyncClient, space: dict) -> dict:
            media_key = space['data']['audioSpace']['metadata']['media_key']
            stream = await self._get_stream(c, media_key)
            return {'space': space, 'stream': stream}

        async def process():
            limits = Limits(max_connections=100, max_keepalive_connections=10)
            headers = self.session.headers if self.guest else get_headers(self.session)
            cookies = self.session.cookies
            async with AsyncClient(limits=limits, headers=headers, cookies=cookies, timeout=20) as c:
                return await asyncio.gather(*(get(c, key) for key in keys))

        return asyncio.run(process())

    def _run(self, operation: tuple[dict, str, str], queries: set | list[int | str | dict], **kwargs):
        keys, qid, name = operation
        save = kwargs.pop('save', True)
        op = kwargs.pop('res', 'json')

        ops = {
            None: lambda x: x,  # return raw response
            'json': lambda x: list(filter(None, (get_json(r, name, save, **kwargs) for r in x))),
            'text': lambda x: [r.text for r in x],
        }

        # stay within rate-limits
        if (l := len(queries)) > 500:
            self.logger.warning(f'Got {l} queries, truncating to first 500.')
            queries = list(queries)[:500]

        if all(isinstance(q, dict) for q in queries):
            return ops[op](asyncio.run(self._process(operation, list(queries), **kwargs)))

        # queries are of type set | list[int|str], need to convert to list[dict]
        _queries = [{k: q} for q in queries for k, v in keys.items()]
        res = ops[op](asyncio.run(self._process(operation, _queries, **kwargs)))
        return res.pop() if kwargs.get('cursor') else flatten(res)

    async def _query(self, c: AsyncClient, operation: tuple, **kwargs) -> Response:
        keys, qid, name = operation
        params = {
            'variables': Operation.default_variables | keys | kwargs,
            'features': Operation.default_features,
        }
        url = f'https://twitter.com/i/api/graphql/{qid}/{name}'
        r = await c.get(url, params=build_params(params))
        if self.debug:
            log(self.logger, self.debug, r)
        if self.save:
            save_json(r, self.out_path, name, **kwargs)
        return r

    async def _process(self, operation: tuple, queries: list[dict], **kwargs):
        limits = Limits(max_connections=100, max_keepalive_connections=10)
        headers = self.session.headers if self.guest else get_headers(self.session)
        cookies = self.session.cookies
        async with AsyncClient(limits=limits, headers=headers, cookies=cookies, timeout=20) as c:
            return await tqdm_asyncio.gather(
                *(self._paginate(c, operation, **q, **kwargs) for q in queries),
                desc=operation[-1],
            )

    async def _paginate(self, client: AsyncClient, operation: tuple, **kwargs):
        limit = kwargs.pop('limit', math.inf)
        cursor = kwargs.pop('cursor', None)
        is_resuming = False
        dups = 0
        DUP_LIMIT = 3
        if cursor:
            is_resuming = True
            res = []
            ids = set()
        else:
            try:
                r = await self._query(client, operation, **kwargs)
                initial_data = r.json()
                res = [r]
                ids = set(find_key(initial_data, 'rest_id'))
                cursor = get_cursor(initial_data)
            except Exception as e:
                self.logger.error('Failed to get initial pagination data', e)
                return
        while (dups < DUP_LIMIT) and cursor:
            prev_len = len(ids)
            if prev_len >= limit:
                break
            try:
                r = await self._query(client, operation, cursor=cursor, **kwargs)
                data = r.json()
            except Exception as e:
                self.logger.error('Failed to get pagination data', e)
                return
            cursor = get_cursor(data)
            ids |= set(find_key(data, 'rest_id'))
            if self.debug:
                self.logger.debug(f'cursor: {cursor}\tunique results: {len(ids)}')
            if prev_len == len(ids):
                dups += 1
            res.append(r)
        if is_resuming:
            return res, cursor
        return res

    async def _space_listener(self, chat: dict, frequency: int):
        rand_color = lambda: random.choice([RED, GREEN, RESET, BLUE, CYAN, MAGENTA, YELLOW])
        uri = f"wss://{URL(chat['endpoint']).host}/chatapi/v1/chatnow"
        with open('chatlog.jsonl', 'ab') as fp:
            async with websockets.connect(uri) as ws:
                await ws.send(orjson.dumps({
                    "payload": orjson.dumps({"access_token": chat['access_token']}).decode(),
                    "kind": 3
                }).decode())
                await ws.send(orjson.dumps({
                    "payload": orjson.dumps({
                        "body": orjson.dumps({
                            "room": chat['room_id']
                        }).decode(),
                        "kind": 1
                    }).decode(),
                    "kind": 2
                }).decode())

                prev_message = ''
                prev_user = ''
                while True:
                    msg = await ws.recv()
                    temp = orjson.loads(msg)
                    kind = temp.get('kind')
                    if kind == 1:
                        signature = temp.get('signature')
                        payload = orjson.loads(temp.get('payload'))
                        payload['body'] = orjson.loads(payload.get('body'))
                        res = {
                            'kind': kind,
                            'payload': payload,
                            'signature': signature,
                        }
                        fp.write(orjson.dumps(res) + b'\n')
                        body = payload['body']
                        message = body.get('body')
                        user = body.get('username')
                        # user_id = body.get('user_id')
                        final = body.get('final')

                        if frequency == 1:
                            if final:
                                if user != prev_user:
                                    print()
                                    print(f"({rand_color()}{user}{RESET})")
                                    prev_user = user
                                # print(message, end=' ')
                                print(message)

                        # dirty
                        if frequency == 2:
                            if user and (not final):
                                if user != prev_user:
                                    print()
                                    print(f"({rand_color()}{user}{RESET})")
                                    prev_user = user
                                new_message = re.sub(f'^({prev_message})', '', message, flags=re.I).strip()
                                if len(new_message) < 100:
                                    print(new_message, end=' ')
                                    prev_message = message

    async def _get_live_chats(self, client: Client, spaces: list[dict]):
        async def get(c: AsyncClient, space: dict) -> list[dict]:
            media_key = space['data']['audioSpace']['metadata']['media_key']
            r = await c.get(
                url=f'https://twitter.com/i/api/1.1/live_video_stream/status/{media_key}',
                params={
                    'client': 'web',
                    'use_syndication_guest_id': 'false',
                    'cookie_set_host': 'twitter.com',
                })
            r = await c.post(
                url='https://proxsee.pscp.tv/api/v2/accessChatPublic',
                json={'chat_token': r.json()['chatToken']}
            )
            return r.json()

        limits = Limits(max_connections=100)
        async with AsyncClient(headers=client.headers, limits=limits, timeout=30) as c:
            return await tqdm_asyncio.gather(*(get(c, _id) for _id in spaces), desc='getting live transcripts')

    def space_live_transcript(self, room: str, frequency: int = 1):
        async def get(spaces: list[dict]):
            client = init_session()
            chats = await self._get_live_chats(client, spaces)
            await asyncio.gather(*(self._space_listener(c, frequency) for c in chats))

        spaces = self.spaces(rooms=[room])
        asyncio.run(get(spaces))

    def spaces_live(self, rooms: list[str]):
        chunk_idx = lambda chunk: re.findall('_(\d+)_\w\.aac', chunk)[0]
        sort_chunks = lambda chunks: sorted(chunks, key=lambda x: int(chunk_idx(x)))
        parse_chunks = lambda txt: re.findall('\n(chunk_.*)\n', txt, flags=re.I)

        async def get_m3u8(client: AsyncClient, space: dict) -> dict:
            try:
                media_key = space['data']['audioSpace']['metadata']['media_key']
                r = await client.get(
                    url=f'https://twitter.com/i/api/1.1/live_video_stream/status/{media_key}',
                    params={'client': 'web', 'use_syndication_guest_id': 'false', 'cookie_set_host': 'twitter.com'}
                )
                data = r.json()
                room = data['shareUrl'].split('/')[-1]
                return {"url": data['source']['location'], "room": room}
            except Exception as e:
                room = space['data']['audioSpace']['metadata']['rest_id']
                self.logger.error(f'Failed to get stream info for https://twitter.com/i/spaces/{room}\n{e}')

        async def get_chunks(client: AsyncClient, url: str) -> list[str]:
            try:
                url = URL(url)
                r = await client.get(
                    url=url,
                    params={'type': url.params.get('type')},
                    headers={'authority': url.host}
                )
                base = '/'.join(str(url).split('/')[:-1])
                return [f'{base}/{c}' for c in parse_chunks(r.text)]
            except Exception as e:
                self.logger.error(f'Failed to get chunks\n{e}')

        async def poll_space(client: AsyncClient, space: dict) -> dict | None:
            curr = 0
            lim = 10
            all_chunks = set()
            playlist = await get_m3u8(client, space)
            if not playlist: return
            chunks = await get_chunks(client, playlist['url'])
            if not chunks: return
            out = self.out_path / 'live'
            out.mkdir(parents=True, exist_ok=True)
            async with aiofiles.open(out / f'{playlist["room"]}.aac', 'wb') as fp:
                while curr < lim:
                    chunks = await get_chunks(client, playlist['url'])
                    if not chunks:
                        return {'space': space, 'chunks': sort_chunks(all_chunks)}
                    new_chunks = set(chunks) - all_chunks
                    all_chunks |= new_chunks
                    for c in sort_chunks(new_chunks):
                        try:
                            self.logger.debug(f"write: chunk [{chunk_idx(c)}]\t{c}")
                            r = await client.get(c)
                            await fp.write(r.content)
                        except Exception as e:
                            self.logger.error(f'Failed to write chunk {c}\n{e}')
                    curr = 0 if new_chunks else curr + 1
                    # wait for new chunks. dynamic playlist is updated every 2-3 seconds
                    await asyncio.sleep(random.random() + 1.5)
            return {'space': space, 'chunks': sort_chunks(all_chunks)}

        async def process(spaces: list[dict]):
            limits = Limits(max_connections=100)
            headers, cookies = self.session.headers, self.session.cookies
            async with AsyncClient(limits=limits, headers=headers, cookies=cookies, timeout=20) as c:
                return await asyncio.gather(*(poll_space(c, space) for space in spaces))

        spaces = self.spaces(rooms=rooms)
        return asyncio.run(process(spaces))
