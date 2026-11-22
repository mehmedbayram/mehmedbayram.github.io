"""Build the public App Store snapshot and GitHub Pages artifact."""
import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
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


def request_json(url, headers=None):
    for attempt in range(4):
        try:
            request = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'AppPortfolioSync/1.0', **(headers or {})})
            with urlopen(request, timeout=25) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 3:
                # Credentials and response bodies must stay out of workflow logs.
                raise RuntimeError(f'Apple request failed with HTTP {error.code}') from None
        except (URLError, TimeoutError):
            if attempt == 3:
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
            ratings = {**previous.get('storeRatings', {}), country: {
                'count': max(0, int(app.get('userRatingCount', 0))),
                'average': float(app.get('averageUserRating', 0)),
            }}
            count = sum(value['count'] for value in ratings.values())
            points = sum(value['count'] * value['average'] for value in ratings.values())
            merged[app_id] = {
                **{key: value for key, value in app.items() if key in PUBLIC_APP_FIELDS},
                'storefront': country, 'availableStores': list(ratings), 'storeRatings': ratings,
                'userRatingCount': count, 'averageUserRating': points / count if count else 0,
            }
    return sorted(merged.values(), key=lambda app: (app.get('releaseDate', '9999'), app['trackId']))


def build_snapshot(client, cache=None):
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
    return {
        'schemaVersion': 2, 'scope': 'worldwide', 'fetchedAt': fetched_at, 'countries': countries, 'apps': apps,
        'reviews': {'scope': 'worldwide', 'countries': countries, 'fetchedAt': fetched_at, 'apps': records},
    }


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
        snapshot = build_snapshot(AppleClient(), args.lookup_cache)
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
