"""Build the public App Store snapshot and GitHub Pages artifact."""
import argparse
import gzip
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import jwt
import pycountry

DEVELOPER_ID = 1541281376
def territory_country(code):
    if code == 'XKS':
        return 'xk'
    country = pycountry.countries.get(alpha_3=code)
    if not country:
        raise RuntimeError('Unknown Apple territory: ' + str(code))
    return country.alpha_2.lower()
API_ROOT = 'https://api.appstoreconnect.apple.com/v1'
PUBLISHED_SNAPSHOT = 'https://mehmedbayram.github.io/data/appstore.json'
PRODUCT_LOCALES = {'tr': 'tr', 'us': 'en-US'}
PUBLIC_APP_FIELDS = (
    'trackId', 'artistId', 'trackName', 'description', 'releaseDate',
    'artworkUrl100', 'artworkUrl512', 'primaryGenreName', 'minimumOsVersion',
    'screenshotUrls', 'ipadScreenshotUrls', 'appletvScreenshotUrls',
    'price', 'formattedPrice', 'userRatingCount', 'averageUserRating',
)


class AppleClient:
    def __init__(self):
        names = ('ASC_ISSUER_ID', 'ASC_KEY_ID', 'ASC_PRIVATE_KEY')
        missing = [name for name in names if not os.environ.get(name)]
        if missing:
            raise RuntimeError('Missing GitHub repository secrets: ' + ', '.join(missing))
        self.issuer, self.key_id, self.private_key = [os.environ[name].strip() for name in names]
        self.token = ''
        self.expires_at = 0
        self.scope = None

    def authorization(self, scope):
        now = int(time.time())
        if now >= self.expires_at - 60 or self.scope != scope:
            self.scope = scope
            self.expires_at = now + 900
            self.token = jwt.encode(
                {'iss': self.issuer, 'iat': now, 'exp': self.expires_at, 'aud': 'appstoreconnect-v1', 'scope': [scope]},
                self.private_key,
                algorithm='ES256',
                headers={'kid': self.key_id, 'typ': 'JWT'},
            )
        return 'Bearer ' + self.token

    def countries(self):
        url = API_ROOT + '/territories?limit=200'
        countries = set()
        while url:
            if urlparse(url).netloc != 'api.appstoreconnect.apple.com' or urlparse(url).scheme != 'https':
                raise RuntimeError('Unexpected territory pagination destination')
            data = request_json(url, {'Authorization': self.authorization('GET /v1/territories')})
            countries.update(territory_country(item['id']) for item in data['data'])
            url = data.get('links', {}).get('next')
        if len(countries) < 170 or not {'tr', 'us'} <= countries:
            raise RuntimeError('Incomplete worldwide territory list')
        # US metadata wins; Turkish metadata is preferred for Turkey-only apps.
        return sorted(countries - {'tr', 'us'}) + ['tr', 'us']

    def reviews(self, app_id):
        query = urlencode({'sort': '-createdDate', 'limit': 200})
        url = f'{API_ROOT}/apps/{app_id}/customerReviews?{query}'
        scope = f'GET /v1/apps/{app_id}/customerReviews'
        reviews = {}
        visited = set()
        while url:
            if url in visited:
                raise RuntimeError(f'Repeated review page for app {app_id}')
            parsed = urlparse(url)
            if parsed.scheme != 'https' or parsed.netloc != 'api.appstoreconnect.apple.com':
                raise RuntimeError('Unexpected Apple pagination destination')
            visited.add(url)
            data = request_json(url, {'Authorization': self.authorization(scope)})
            if not isinstance(data.get('data'), list):
                raise RuntimeError(f'Invalid review response for app {app_id}')
            for item in data['data']:
                attrs = item['attributes']
                country = territory_country(attrs.get('territory'))
                rating = int(attrs['rating'])
                if rating not in range(1, 6):
                    raise RuntimeError(f'Invalid review rating for app {app_id}')
                reviews[item['id']] = {
                    'id': item['id'], 'rating': rating,
                    'title': attrs.get('title', ''), 'body': attrs.get('body', ''),
                    'author': attrs.get('reviewerNickname', 'App Store user'),
                    'date': attrs.get('createdDate', ''), 'storefront': country,
                }
            url = data.get('links', {}).get('next')
        return list(reviews.values())


def request_json(url, headers=None, *, attempts=4, timeout=25):
    for attempt in range(attempts):
        try:
            request = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'AppPortfolioSync/1.0', **(headers or {})})
            with urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                # Credentials and response bodies must stay out of workflow logs.
                raise RuntimeError(f'Apple request failed with HTTP {error.code}') from None
        except (URLError, TimeoutError):
            if attempt == attempts - 1:
                raise RuntimeError('Apple request timed out after retries') from None
        time.sleep(2 ** (attempt + 1))


def lookup(country):
    query = urlencode({'id': DEVELOPER_ID, 'entity': 'software', 'limit': 200, 'country': country})
    data = request_json('https://itunes.apple.com/lookup?' + query)
    apps = [app for app in data.get('results', []) if app.get('wrapperType') == 'software' and app.get('artistId') == DEVELOPER_ID]
    if not isinstance(data.get('results'), list):
        raise RuntimeError(f'Invalid public app response for {country}')
    return apps


def combine_apps(countries, snapshots):
    merged = {}
    for country, snapshot in zip(countries, snapshots):
        for app in snapshot:
            app_id = str(app['trackId'])
            previous = merged.get(app_id, {})
            descriptions = dict(previous.get('storeDescriptions', {}))
            names = dict(previous.get('storeNames', {}))
            description = app.get('description')
            if country in ('tr', 'us') and isinstance(description, str) and description.strip():
                descriptions[country] = description.strip()
            if country in ('tr', 'us') and app.get('trackName'):
                names[country] = app['trackName']
            ratings = {**previous.get('storeRatings', {}), country: {
                'count': max(0, int(app.get('userRatingCount', 0))),
                'average': float(app.get('averageUserRating', 0)),
            }}
            count = sum(value['count'] for value in ratings.values())
            points = sum(value['count'] * value['average'] for value in ratings.values())
            merged[app_id] = {
                **{key: value for key, value in app.items() if key in PUBLIC_APP_FIELDS},
                'storeDescriptions': descriptions,
                'storeNames': names,
                'storefront': country, 'availableStores': list(ratings), 'storeRatings': ratings,
                'userRatingCount': count, 'averageUserRating': points / count if count else 0,
            }
    return sorted(merged.values(), key=lambda app: (app.get('releaseDate', '9999'), app['trackId']))


def product_rank(html, app_id, country):
    canonical = re.search(r'<link rel="canonical" href="([^"]+)"', html)
    path = urlparse(canonical[1]).path if canonical else ''
    if not path.startswith(f'/{country}/app/') or not path.endswith(f'/id{app_id}'):
        return None
    data = re.search(r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', html, re.S)
    if data is None:
        return None
    if '"chartPosition"' not in data[1]:
        return {}

    pending = [json.loads(data[1])]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(reversed(node))
        elif isinstance(node, dict):
            if node.get('$kind') == 'Badge' and node.get('type') == 'chartPosition':
                position = str(node.get('content', {}).get('position', ''))
                url = node.get('clickAction', {}).get('pageUrl', '')
                if position.isascii() and position.isdecimal() and 'chart=top-free' in url:
                    return {'country': country, 'chart': 'free', 'rank': int(position),
                            'category': node.get('caption', '')}
            pending.extend(reversed(list(node.values())))
    return {}


def read_product_metadata(html, language):
    lang = re.search(r'<html\b[^>]*\blang=["\']([^"\']+)', html, re.I)
    if lang is None or lang[1].lower().split('-')[0] != language:
        return {}
    structured = re.search(
        r'<script\b[^>]*\bid=(?:"software-application"|\'software-application\'|software-application(?=\s|>))[^>]*>(.*?)</script>',
        html, re.S,
    )
    if structured is None:
        return {}
    try:
        data = json.loads(structured[1])
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {field: data[field].strip() for field in ('name', 'description')
            if isinstance(data.get(field), str) and data[field].strip()}


def refresh_product_data(snapshot):
    apps = snapshot['apps']
    previous = snapshot.get('localizedMetadata', {}) if snapshot.get('metadataSource') == 'product-pages' else {}
    records = {str(app['trackId']): dict(previous.get(str(app['trackId']), {}).get('locales', {})) for app in apps}
    now = datetime.now(timezone.utc)

    jobs = [(str(app['trackId']), country)
            for country in PRODUCT_LOCALES
            for app in apps if country in app['availableStores']]
    deadline = time.monotonic() + 5 * 60
    request_lock = Lock()
    next_request = 0.0

    def read_product(job):
        nonlocal next_request
        app_id, country = job
        locale = PRODUCT_LOCALES[country]
        language = locale.split('-')[0]
        url = f'https://apps.apple.com/{country}/app/id{app_id}?l={language}'
        for attempt in range(3):
            while True:
                with request_lock:
                    now = time.monotonic()
                    if now >= deadline:
                        return 'deferred', None
                    delay = next_request - now
                    if delay <= 0:
                        next_request = now + 0.7
                        break
                time.sleep(min(delay, 5))
            try:
                request = Request(url, headers={'User-Agent': 'AppPortfolioSync/1.0',
                                                'Accept': 'text/html', 'Accept-Encoding': 'gzip'})
                with urlopen(request, timeout=8) as response:
                    content = response.read()
                    if response.headers.get('Content-Encoding') == 'gzip':
                        content = gzip.decompress(content)
                html = content.decode('utf-8')
                rank = product_rank(html, app_id, country)
                if rank is None:
                    return 'unverified', None
                metadata = read_product_metadata(html, language)
                return 'checked', (rank, metadata)
            except HTTPError as error:
                if error.code == 404:
                    return 'unavailable', None
                if error.code == 429:
                    retry_after = error.headers.get('Retry-After', '')
                    cooldown = max(30, int(retry_after)) if retry_after.isdecimal() else 60
                    with request_lock:
                        next_request = max(next_request, time.monotonic() + cooldown)
                elif error.code < 500:
                    return 'unverified', None
            except (URLError, TimeoutError, OSError, EOFError, ValueError):
                pass
        return 'unverified', None

    matches = {}
    checked = unavailable = updated = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for (app_id, country), (status, result) in zip(jobs, pool.map(read_product, jobs)):
            checked += status == 'checked'
            unavailable += status == 'unavailable'
            if result is None:
                continue
            rank, metadata = result
            if rank:
                matches.setdefault(app_id, []).append(rank)
            if metadata:
                fields = records[app_id].setdefault(PRODUCT_LOCALES[country], {})
                fields.update({key: {'value': value, 'fetchedAt': now.isoformat(timespec='seconds')}
                               for key, value in metadata.items()})
                updated += 1
    print(f'Checked {checked}/{len(jobs)} App Store product pages, {unavailable} unavailable. '
          f'Updated metadata from {updated} pages.', flush=True)
    snapshot['charts'] = {'source': 'product-pages', 'scanVersion': 3, 'stores': list(PRODUCT_LOCALES),
                          'fetchedAt': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                          'checked': checked, 'unavailable': unavailable, 'total': len(jobs), 'apps': matches}
    snapshot['metadataSource'] = 'product-pages'
    snapshot['localizedMetadata'] = {app_id: {'locales': locales} for app_id, locales in records.items() if locales}


def build_snapshot(client, cache=None, previous=None):
    countries = client.countries()
    snapshots = []
    # Stay below the public Search API's approximate 20 requests/minute limit.
    for index, country in enumerate(countries):
        saved = cache / (country + '.json') if cache else None
        if saved and saved.exists():
            snapshot = json.loads(saved.read_text())
        else:
            if snapshots:
                time.sleep(3.2)
            snapshot = lookup(country)
            if saved:
                saved.parent.mkdir(parents=True, exist_ok=True)
                saved.write_text(json.dumps(snapshot))
        snapshots.append(snapshot)
        if (index + 1) % 25 == 0:
            print(f'Collected {index + 1}/{len(countries)} storefronts.', flush=True)
    apps = combine_apps(countries, snapshots)
    if not apps:
        raise RuntimeError('No published apps returned worldwide')
    records = {}
    for app in apps:
        app_id = str(app['trackId'])
        reviews = client.reviews(app_id)
        records[app_id] = {
            'reviews': reviews,
            'totalCount': len(reviews),
            'totalPoints': sum(review['rating'] for review in reviews),
            'storeCounts': {country: sum(review['storefront'] == country for review in reviews) for country in countries},
        }
    fetched_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    previous = previous or {}
    snapshot = {
        'schemaVersion': 2, 'scope': 'worldwide', 'fetchedAt': fetched_at, 'countries': countries, 'apps': apps,
        'reviews': {'scope': 'worldwide', 'countries': countries, 'fetchedAt': fetched_at, 'apps': records},
        'metadataSource': previous.get('metadataSource'),
        'localizedMetadata': previous.get('localizedMetadata', {}),
    }
    refresh_product_data(snapshot)
    return snapshot


def replace_payload(html, name, data):
    pattern = rf'(<script id="{name}"[^>]*>).*?(</script>)'
    encoded = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
    result, count = re.subn(pattern, lambda match: match[1] + encoded + match[2], html, flags=re.S)
    if count != 1:
        raise RuntimeError(f'Expected one {name} payload')
    return result


def write_site(source, output, snapshot):
    if not snapshot.get('apps') or not snapshot.get('reviews', {}).get('apps'):
        raise RuntimeError('Refusing to publish an empty snapshot')
    html = (source / 'index.html').read_text()
    html = replace_payload(html, 'appstore-seed', snapshot['apps'])
    html = replace_payload(html, 'appstore-reviews', snapshot['reviews'])
    if 'BEGIN PRIVATE KEY' in html or 'api.appstoreconnect.apple.com' in html:
        raise RuntimeError('Unexpected credential or private API reference in public HTML')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'data').mkdir(exist_ok=True)
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))
    (output / 'data/appstore.json').write_text(encoded)
    (output / 'index.html').write_text(html)
    (output / '.nojekyll').touch()
    ads = source / 'app-ads.txt'
    if ads.is_file():
        shutil.copy2(ads, output / ads.name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('_site'))
    parser.add_argument('--lookup-cache', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    source = Path(__file__).resolve().parent.parent
    try:
        try:
            previous = request_json(PUBLISHED_SNAPSHOT, attempts=1, timeout=12)
        except (RuntimeError, ValueError):
            previous = {}
        snapshot = build_snapshot(AppleClient(), args.lookup_cache, previous)
        write_site(source, args.output, snapshot)
    except (RuntimeError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1
    ratings = sum(app['userRatingCount'] for app in snapshot['apps'])
    reviews = sum(record['totalCount'] for record in snapshot['reviews']['apps'].values())
    print(f"Updated {len(snapshot['apps'])} apps, {ratings} ratings and {reviews} written reviews.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
