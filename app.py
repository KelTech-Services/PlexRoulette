from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response
from functools import wraps
import random
import requests
import json
import os
import sys
from datetime import datetime, timedelta
from xml.etree import ElementTree
from werkzeug.security import generate_password_hash, check_password_hash

# Force unbuffered output for Docker logs
sys.stdout.reconfigure(line_buffering=True)

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
CONFIG_PATH = os.path.join(DATA_DIR, 'config.json')
WATCHLIST_FILE = os.path.join(DATA_DIR, 'watchlist.json')
USERS_FILE = os.path.join(DATA_DIR, 'users.json')
HISTORY_FILE = os.path.join(DATA_DIR, 'pick_history.json')

PLEX_PRODUCT = "MediaRouletteApp"
PLEX_CLIENT_IDENTIFIER = "mediaroulette-client-001"
PLEX_PLATFORM = "Web"
PLEX_DEVICE_NAME = "MediaRoulette"
PLEX_DEVICE = "PythonApp"
PLEX_VERSION = "1.0"

DEFAULT_SESSION_LIMIT = 20

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'mediaroulette-dev-key-change-in-prod')

# Ensure data directory and files exist
os.makedirs(DATA_DIR, exist_ok=True)
if not os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, 'w') as f:
        json.dump({}, f)
if not os.path.exists(WATCHLIST_FILE):
    with open(WATCHLIST_FILE, 'w') as f:
        json.dump([], f)

def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, 'r') as f:
        return json.load(f)

def save_users(users):
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)

def is_setup_complete():
    config = load_config()
    # Setup complete if local auth disabled + Plex connected
    if config.get('local_auth_disabled') and config.get('plex_token'):
        return True
    # Or if users exist (traditional setup)
    users = load_users()
    return len(users) > 0

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        config = load_config()
        
        # If local auth disabled and Plex connected, auto-login
        if config.get('local_auth_disabled') and config.get('plex_token'):
            if not session.get('logged_in'):
                session['logged_in'] = True
                session['username'] = 'plex_user'
            return f(*args, **kwargs)
        
        if not is_setup_complete():
            return redirect(url_for('setup'))
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

RATING_OPTIONS = [
    'G', 'PG', 'PG-13', 'R', 'NC-17', 'Not Rated', 'Unrated',
    'TV-Y', 'TV-Y7', 'TV-G', 'TV-PG', 'TV-14', 'TV-MA'
]

def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, 'r') as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)

def load_watchlist():
    if not os.path.exists(WATCHLIST_FILE):
        return []
    with open(WATCHLIST_FILE, 'r') as f:
        return json.load(f)

def load_pick_history(username):
    """Load pick history for a user from file"""
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, 'r') as f:
            all_history = json.load(f)
        return all_history.get(username, [])
    except:
        return []

def save_pick_history(username, history):
    """Save pick history for a user to file"""
    all_history = {}
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                all_history = json.load(f)
        except:
            pass
    all_history[username] = history[-DEFAULT_SESSION_LIMIT:]
    with open(HISTORY_FILE, 'w') as f:
        json.dump(all_history, f, indent=2)

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    config = load_config()
    
    # If local auth disabled and Plex connected, go to index
    if config.get('local_auth_disabled') and config.get('plex_token'):
        return redirect(url_for('index'))
    
    # If local auth disabled but Plex not connected yet, continue to Plex login
    if config.get('local_auth_disabled') and not config.get('plex_token'):
        session['logged_in'] = True
        session['username'] = 'plex_user'
        return redirect(url_for('plex_login'))
    
    # If users exist (traditional setup), go to login
    users = load_users()
    if len(users) > 0:
        return redirect(url_for('login'))
    
    error = None
    if request.method == 'POST':
        # Handle skip local auth option
        if 'skip_local_auth' in request.form:
            config['local_auth_disabled'] = True
            save_config(config)
            session['logged_in'] = True
            session['username'] = 'plex_user'
            print("[MediaRoulette] Local auth disabled, proceeding to Plex login", flush=True)
            return redirect(url_for('plex_login'))
        
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        if not username or not password:
            error = 'Username and password are required.'
        elif len(password) < 6:
            error = 'Password must be at least 6 characters.'
        elif password != confirm_password:
            error = 'Passwords do not match.'
        else:
            users = {}
            users[username] = {
                'password_hash': generate_password_hash(password),
                'is_admin': True,
                'created_at': datetime.now().isoformat()
            }
            save_users(users)
            session['logged_in'] = True
            session['username'] = username
            print(f"[MediaRoulette] Admin account created: {username}", flush=True)
            # If Plex already connected, go to index; otherwise to Plex login
            if config.get('plex_token'):
                return redirect(url_for('index'))
            return redirect(url_for('plex_login'))
    
    # Check if Plex is already connected (for users enabling local auth later)
    plex_connected = bool(config.get('plex_token'))
    return render_template('setup.html', error=error, plex_connected=plex_connected)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not is_setup_complete():
        return redirect(url_for('setup'))
    
    if session.get('logged_in'):
        return redirect(url_for('index'))
    
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        users = load_users()
        user = users.get(username)
        
        if user and check_password_hash(user['password_hash'], password):
            session['logged_in'] = True
            session['username'] = username
            print(f"User logged in: {username}", flush=True)
            return redirect(url_for('index'))
        else:
            error = 'Invalid username or password.'
            print(f"Failed login attempt for: {username}", flush=True)
    
    return render_template('login.html', error=error)

@app.route('/app_logout')
def app_logout():
    username = session.get('username', 'unknown')
    session.pop('logged_in', None)
    session.pop('username', None)
    print(f"User logged out: {username}", flush=True)
    return redirect(url_for('login'))

@app.route('/reset_password', methods=['POST'])
@login_required
def reset_password():
    """Delete users.json to allow creating a new admin account"""
    username = session.get('username', 'unknown')
    if os.path.exists(USERS_FILE):
        os.remove(USERS_FILE)
        print(f"[MediaRoulette] Admin account reset by: {username}", flush=True)
    session.clear()
    return redirect(url_for('setup'))

@app.route('/enable_local_auth', methods=['POST'])
@login_required
def enable_local_auth():
    """Enable local authentication (for users who skipped it initially)"""
    config = load_config()
    config['local_auth_disabled'] = False
    save_config(config)
    print("[MediaRoulette] Local auth enabled, redirecting to create admin account", flush=True)
    session.clear()
    return redirect(url_for('setup'))

@app.route('/plex_login')
@login_required
def plex_login():
    headers = {
        'X-Plex-Client-Identifier': PLEX_CLIENT_IDENTIFIER,
        'X-Plex-Product': PLEX_PRODUCT,
        'X-Plex-Version': PLEX_VERSION,
        'X-Plex-Device': PLEX_DEVICE,
        'X-Plex-Platform': PLEX_PLATFORM,
        'X-Plex-Device-Name': PLEX_DEVICE_NAME,
        'Accept': 'application/json'
    }
    try:
        response = requests.post("https://plex.tv/api/v2/pins", headers=headers, timeout=10)
        if response.status_code != 201:
            return "Failed to initiate Plex login", 500
        data = response.json()
    except Exception as e:
        print(f"Plex login error: {e}")
        return "Failed to connect to Plex servers", 500

    session['plex_pin_id'] = data.get("id")
    session['plex_code'] = data.get("code")
    session['plex_expires'] = int(data.get("expires_in", 900))
    return render_template('plex_login.html', code=session['plex_code'], expires_in=session['plex_expires'])

@app.route('/plex_poll')
@login_required
def plex_poll():
    pin_id = session.get('plex_pin_id')
    if not pin_id:
        return jsonify({'status': 'error', 'message': 'No PIN ID in session'})

    headers = {
        'X-Plex-Client-Identifier': PLEX_CLIENT_IDENTIFIER,
        'X-Plex-Product': PLEX_PRODUCT,
        'X-Plex-Version': PLEX_VERSION,
        'X-Plex-Device': PLEX_DEVICE,
        'X-Plex-Platform': PLEX_PLATFORM,
        'X-Plex-Device-Name': PLEX_DEVICE_NAME,
        'Accept': 'application/json'
    }
    try:
        response = requests.get(f"https://plex.tv/api/v2/pins/{pin_id}", headers=headers, timeout=10)
        data = response.json()
    except Exception as e:
        print(f"Plex poll error: {e}")
        return jsonify({'status': 'error', 'message': 'Failed to connect to Plex'})

    if "errors" in data:
        return jsonify({'status': 'expired'}), 400

    auth_token = data.get("authToken")
    if auth_token:
        session['plex_token'] = auth_token
        config = load_config()
        config['plex_token'] = auth_token

        servers = []
        try:
            server_response = requests.get("https://plex.tv/api/resources?includeHttps=1", headers={
                'X-Plex-Token': auth_token,
                'Accept': 'application/xml'
            }, timeout=10)
            root = ElementTree.fromstring(server_response.text)
            for device in root.findall('Device'):
                if device.attrib.get('provides') != 'server':
                    continue
                name = device.attrib.get('name')
                accessToken = device.attrib.get('accessToken')
                connections = device.findall('Connection')
                
                # Collect all connections, preferring local
                local_conns = [c for c in connections if c.attrib.get('local') == '1']
                remote_conns = [c for c in connections if c.attrib.get('local') == '0']
                
                # Try local first, then remote
                selected_uri = None
                for conn in local_conns + remote_conns:
                    uri = conn.attrib.get('uri')
                    if uri:
                        selected_uri = uri
                        print(f"[MediaRoulette] Selected connection for {name}: {uri} (local={conn.attrib.get('local')})")
                        break
                
                if selected_uri:
                    servers.append({
                        'name': name,
                        'uri': selected_uri,
                        'accessToken': accessToken
                    })
        except Exception as e:
            print(f"[MediaRoulette] Failed to fetch servers: {e}")
        config['plex_servers'] = servers
        if servers:
            config['plex_server_url'] = servers[0]['uri']
            print(f"[MediaRoulette] Fetching libraries from: {servers[0]['uri']}")
            # Fetch libraries from the first server
            try:
                lib_response = requests.get(
                    f"{servers[0]['uri']}/library/sections",
                    headers={'Accept': 'application/json'},
                    params={'X-Plex-Token': servers[0]['accessToken']},
                    timeout=15
                )
                if lib_response.ok:
                    config['plex_libraries'] = lib_response.json().get('MediaContainer', {}).get('Directory', [])
                    print(f"[MediaRoulette] Found {len(config['plex_libraries'])} libraries")
                else:
                    print(f"[MediaRoulette] Library fetch failed with status: {lib_response.status_code}")
            except requests.exceptions.Timeout:
                print(f"[MediaRoulette] Timeout connecting to Plex server at {servers[0]['uri']}")
            except Exception as e:
                print(f"[MediaRoulette] Failed to fetch libraries: {e}")
        save_config(config)
        return jsonify({'status': 'success'})
    return jsonify({'status': 'pending'})

@app.route('/signout')
@login_required
def signout():
    session.clear()
    config = load_config()
    for key in ['plex_token', 'plex_server_url', 'movies_library', 'tvshows_library', 'plex_servers']:
        config.pop(key, None)
    save_config(config)
    return redirect(url_for('plex_login'))

def get_machine_identifier():
    url = f"{session.get('plex_server_url')}?X-Plex-Token={session.get('plex_token')}"
    try:
        r = requests.get(url)
        r.raise_for_status()
        if 'xml' in r.headers.get('Content-Type', ''):
            root = ElementTree.fromstring(r.text)
            return root.attrib.get('machineIdentifier', 'unknown')
        elif r.headers.get('Content-Type', '').startswith('application/json'):
            return r.json().get('MediaContainer', {}).get('machineIdentifier', 'unknown')
        else:
            print("Unexpected response type in get_machine_identifier():", r.text[:200])
            return 'unknown'
    except Exception as e:
        print(f"Failed to get machine identifier: {e}")
        return 'unknown'

def get_library_key(name):
    if not name:
        return None
    config = load_config()
    libs = config.get('plex_libraries', [])
    return next((lib['key'] for lib in libs if lib['title'] == name), None)

def get_library_keys(names):
    """Get library keys for multiple library names"""
    if not names:
        return []
    config = load_config()
    libs = config.get('plex_libraries', [])
    keys = []
    for name in names:
        key = next((lib['key'] for lib in libs if lib['title'] == name), None)
        if key:
            keys.append(key)
    return keys

def get_items_from_library(key, unwatched=False):
    if not key:
        return []
    url = f"{session.get('plex_server_url')}/library/sections/{key}/all"
    headers = {'Accept': 'application/json'}
    params = {
        'X-Plex-Token': session.get('plex_token'),
        'X-Plex-Container-Start': 0,
        'X-Plex-Container-Size': 10000  # Request up to 10k items
    }
    if unwatched:
        params['unwatched'] = 1
    try:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        items = r.json().get('MediaContainer', {}).get('Metadata', [])
        print(f"Library {key}: fetched {len(items)} items (unwatched={unwatched})")
        return items
    except Exception as e:
        print(f"Failed to fetch library {key}: {e}")
        return []

def extract_genres(items):
    genres = set()
    for item in items:
        for g in item.get('Genre', []):
            genres.add(g['tag'])
    return sorted(genres)

def build_item_data(item, machine_id):
    rating_key = item.get('ratingKey')
    duration = item.get('duration')
    runtime = int(duration / 60000) if duration else None
    item_type = item.get('type', 'movie')  # 'movie' or 'show'
    return {
        'title': item.get('title'),
        'year': item.get('year'),
        'summary': item.get('summary', 'No summary available.'),
        'genres': ', '.join([g['tag'] for g in item.get('Genre', [])]) if 'Genre' in item else '',
        'poster': f"{session.get('plex_server_url')}{item.get('thumb')}?X-Plex-Token={session.get('plex_token')}" if item.get('thumb') else '',
        'link': f"{session.get('plex_server_url')}/web/index.html#!/server/{machine_id}/details?key=%2Flibrary%2Fmetadata%2F{rating_key}",
        'rating': item.get('contentRating', 'Unrated'),
        'runtime': str(runtime) if runtime else 'N/A',
        'audience_rating': f"{item.get('audienceRating', 0):.1f}" if item.get('audienceRating') else None,
        'audience_rating_image': item.get('audienceRatingImage'),
        'media_type': 'TV Show' if item_type == 'show' else 'Movie'
    }

@app.route('/export_watchlist')
@login_required
def export_watchlist():
    watchlist = load_watchlist()
    export_format = request.args.get('format', 'json')
    if export_format == 'csv':
        headers = ['title', 'year', 'summary', 'genres', 'poster', 'link', 'rating', 'runtime', 'audience_rating']
        output = [headers] + [[item.get(h, '') for h in headers] for item in watchlist]
        lines = []
        for row in output:
            cells = ['"{}"'.format(str(cell).replace('"', '""')) for cell in row]
            lines.append(','.join(cells))
        csv_text = '\n'.join(lines)
        return Response(csv_text, mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=watchlist.csv'})
    return Response(json.dumps(watchlist, indent=2), mimetype='application/json', headers={'Content-Disposition': 'attachment; filename=watchlist.json'})

@app.route('/', methods=['GET', 'POST'])
@login_required
def index():
    config = load_config()

    if not session.get('plex_token') and config.get('plex_token'):
        session['plex_token'] = config['plex_token']
    if not session.get('plex_server_url') and config.get('plex_server_url'):
        session['plex_server_url'] = config['plex_server_url']

    if 'theme' not in session:
        session['theme'] = config.get('default_theme', 'dark')
    if 'enable_history' not in config:
        config['enable_history'] = True

    if not session.get("plex_token") or not session.get("plex_server_url"):
        return redirect(url_for('plex_login'))

    # Support both old single-value and new multi-value config
    movies_libraries = config.get('movies_libraries', [])
    tvshows_libraries = config.get('tvshows_libraries', [])
    
    # Backward compatibility: if no arrays but old single values exist, use those
    if not movies_libraries and config.get('movies_library'):
        movies_libraries = [config.get('movies_library')]
    if not tvshows_libraries and config.get('tvshows_library'):
        tvshows_libraries = [config.get('tvshows_library')]
    
    if not movies_libraries and not tvshows_libraries:
        return redirect(url_for('settings'))

    movie_keys = get_library_keys(movies_libraries)
    show_keys = get_library_keys(tvshows_libraries)
    machine_id = get_machine_identifier()
    items = []

    for key in movie_keys:
        items += get_items_from_library(key)
    for key in show_keys:
        items += get_items_from_library(key)

    genres = extract_genres(items)
    form = request.form
    results = session.get('saved_results', [])  # Restore results from session
    filters = session.get('filters', {})  # Get saved filters

    if request.method == 'POST':
        print(f"POST received. Form keys: {list(form.keys())}", flush=True)
        if 'toggle_history' in form:
            session['show_history'] = not session.get('show_history', False)
        elif 'clear_history' in form:
            save_pick_history(session.get('username', 'default'), [])
        elif 'reset_filters' in form:
            # Clear saved filters and results
            session.pop('filters', None)
            session.pop('saved_results', None)
            session.pop('seen_items', None)  # Also reset seen items when filters change
        elif 'reset_seen' in form:
            # Clear seen items and results
            session.pop('seen_items', None)
            session.pop('saved_results', None)
            session.pop('seen_count', None)
            session.pop('all_seen', None)
            session.pop('total_matching', None)
        elif 'add_to_watchlist' in form:
            item = {
                'title': form.get('saved_title'),
                'year': form.get('saved_year'),
                'summary': form.get('saved_summary'),
                'genres': form.get('saved_genres'),
                'poster': form.get('saved_poster'),
                'link': form.get('saved_link'),
                'rating': form.get('saved_rating'),
                'runtime': form.get('saved_runtime'),
                'audience_rating': form.get('saved_audience_rating'),
                'media_type': form.get('saved_media_type', 'Movie')
            }
            if item['title'] and item['year']:
                watchlist = load_watchlist()
                if not any(w['title'] == item['title'] and str(w['year']) == str(item['year']) for w in watchlist):
                    watchlist.append(item)
                    with open(WATCHLIST_FILE, 'w') as f:
                        json.dump(watchlist, f, indent=2)
            return redirect(url_for('watchlist'))
        else:
            print("Entering spin logic...", flush=True)
            media_type = form.get('media_type', 'both')
            unwatched = 'unwatched' in form
            filtered = []
            print(f"media_type={media_type}, unwatched={unwatched}, genre={form.get('genre')}", flush=True)

            # Save filter settings to session
            session['filters'] = {
                'media_type': media_type,
                'genre': form.get('genre', ''),
                'rating': form.get('rating', ''),
                'keyword': form.get('keyword', ''),
                'min_score': form.get('min_score', ''),
                'unwatched': unwatched,
                'recent_releases': 'recent_releases' in form,
                'show_three': 'show_three' in form
            }

            if media_type == 'movie' and movie_keys:
                for key in movie_keys:
                    filtered += get_items_from_library(key, unwatched=unwatched)
            elif media_type == 'show' and show_keys:
                for key in show_keys:
                    filtered += get_items_from_library(key, unwatched=unwatched)
            else:
                for key in movie_keys:
                    filtered += get_items_from_library(key, unwatched=unwatched)
                for key in show_keys:
                    filtered += get_items_from_library(key, unwatched=unwatched)
            
            print(f"After fetch: {len(filtered)} items total", flush=True)

            # Additional client-side unwatched filter (Plex API param unreliable for shows)
            if unwatched:
                def is_unwatched(item):
                    # For movies: viewCount = 0 or missing means unwatched
                    # For shows: viewedLeafCount = 0 or missing means fully unwatched
                    if item.get('type') == 'show':
                        return item.get('viewedLeafCount', 0) == 0
                    else:
                        return item.get('viewCount', 0) == 0
                before_filter = len(filtered)
                filtered = [i for i in filtered if is_unwatched(i)]
                print(f"After unwatched client filter: {len(filtered)} items (removed {before_filter - len(filtered)})", flush=True)

            if form.get('genre'):
                selected_genre = form.get('genre').lower()
                # Handle combined genres like "Action/Adventure" - match if any part matches
                genre_parts = [g.strip().lower() for g in selected_genre.split('/')]
                filtered = [i for i in filtered if any(
                    any(part in g['tag'].lower() for part in genre_parts)
                    for g in i.get('Genre', [])
                )]
                print(f"After genre filter ({form.get('genre')}): {len(filtered)} items", flush=True)
            if form.get('rating'):
                filtered = [i for i in filtered if i.get('contentRating') == form.get('rating')]
            if form.get('keyword'):
                filtered = [i for i in filtered if form.get('keyword').lower() in i.get('summary', '').lower()]
            if form.get('recent_releases'):
                cutoff = datetime.now() - timedelta(days=5 * 365)
                filtered = [i for i in filtered if i.get('originallyAvailableAt') and datetime.strptime(i.get('originallyAvailableAt'), "%Y-%m-%d") > cutoff]
            if form.get('min_score'):
                min_score = float(form.get('min_score'))
                filtered = [i for i in filtered if i.get('audienceRating') and float(i.get('audienceRating', 0)) >= min_score]
                print(f"After min_score filter ({min_score}+): {len(filtered)} items", flush=True)

            print(f"Final filtered count: {len(filtered)}", flush=True)
            
            # Track seen items to avoid repeats
            seen_items = set(session.get('seen_items', []))
            total_matching = len(filtered)
            
            # Filter out already-seen items
            unseen = [i for i in filtered if i.get('ratingKey') not in seen_items]
            print(f"Unseen items: {len(unseen)} of {total_matching}", flush=True)
            
            # Check if all unique items exhausted
            all_seen = len(unseen) == 0 and total_matching > 0
            
            # If all seen, use full filtered list (allow repeats)
            pool = unseen if unseen else filtered
            
            picks = 3 if 'show_three' in form else 1
            selected = random.sample(pool, min(picks, len(pool)))
            results = [build_item_data(i, machine_id) for i in selected]
            print(f"Picked {len(results)} result(s): {[r['title'] for r in results]}", flush=True)
            
            # Add picked items to seen list
            for item in selected:
                seen_items.add(item.get('ratingKey'))
            session['seen_items'] = list(seen_items)
            session['all_seen'] = all_seen
            session['seen_count'] = len(seen_items)
            session['total_matching'] = total_matching

            # Save results to session
            session['saved_results'] = results

            if config.get('enable_history', True):
                username = session.get('username', 'default')
                history = load_pick_history(username)
                history.extend(results)
                save_pick_history(username, history)

    # Load history from file for display
    pick_history = load_pick_history(session.get('username', 'default')) if config.get('enable_history', True) else []
    
    # Get current filters (may have been updated during POST)
    filters = session.get('filters', {})

    return render_template('index.html',
                           results=results,
                           genres=genres,
                           rating_options=RATING_OPTIONS,
                           pick_history=pick_history,
                           show_history=session.get('show_history', False),
                           filters=filters,
                           has_movies=bool(movie_keys),
                           has_tvshows=bool(show_keys),
                           default_theme=config.get("default_theme", "dark"),
                           config=config,
                           all_seen=session.get('all_seen', False),
                           seen_count=session.get('seen_count', 0),
                           total_matching=session.get('total_matching', 0))

@app.route('/api/server_libraries/<path:server_uri>')
@login_required
def get_server_libraries(server_uri):
    """Fetch libraries for a specific server"""
    config = load_config()
    servers = config.get('plex_servers', [])
    
    # Find the server by URI
    selected_server = next((s for s in servers if s['uri'] == server_uri), None)
    if not selected_server:
        return jsonify({'error': 'Server not found'}), 404
    
    try:
        print(f"[MediaRoulette] Fetching libraries from server: {server_uri}")
        lib_response = requests.get(
            f"{server_uri}/library/sections",
            headers={'Accept': 'application/json'},
            params={'X-Plex-Token': selected_server['accessToken']},
            timeout=15
        )
        if lib_response.ok:
            libraries = lib_response.json().get('MediaContainer', {}).get('Directory', [])
            print(f"[MediaRoulette] Found {len(libraries)} libraries on {selected_server['name']}")
            return jsonify({'libraries': libraries})
        else:
            print(f"[MediaRoulette] Failed to fetch libraries: {lib_response.status_code}")
            return jsonify({'error': f'Failed to fetch libraries: {lib_response.status_code}'}), 500
    except requests.exceptions.Timeout:
        print(f"[MediaRoulette] Timeout connecting to {server_uri}")
        return jsonify({'error': 'Connection timed out'}), 504
    except Exception as e:
        print(f"[MediaRoulette] Error fetching libraries: {e}")
        return jsonify({'error': 'An unexpected error occurred'}), 500

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    config = load_config()
    if request.method == 'POST':
        server_uri = request.form.get('plex_server_url')
        selected_server = next((s for s in config.get('plex_servers', []) if s['uri'] == server_uri), None)
        config['plex_server_url'] = selected_server['uri'] if selected_server else server_uri
        
        # Handle multi-select libraries (getlist returns empty list if none selected)
        movies_libraries = request.form.getlist('movies_library')
        tvshows_libraries = request.form.getlist('tvshows_library')
        config['movies_libraries'] = [lib for lib in movies_libraries if lib]  # Filter out empty strings
        config['tvshows_libraries'] = [lib for lib in tvshows_libraries if lib]
        
        # Keep old single-value keys for backward compatibility
        config['movies_library'] = config['movies_libraries'][0] if config['movies_libraries'] else None
        config['tvshows_library'] = config['tvshows_libraries'][0] if config['tvshows_libraries'] else None
        
        config['default_theme'] = request.form.get('default_theme', 'dark')
        config['enable_history'] = 'enable_history' in request.form
        
        # Fetch and save libraries for the selected server
        if selected_server:
            try:
                lib_response = requests.get(
                    f"{selected_server['uri']}/library/sections",
                    headers={'Accept': 'application/json'},
                    params={'X-Plex-Token': selected_server['accessToken']},
                    timeout=15
                )
                if lib_response.ok:
                    config['plex_libraries'] = lib_response.json().get('MediaContainer', {}).get('Directory', [])
                    print(f"[MediaRoulette] Saved {len(config['plex_libraries'])} libraries for {selected_server['name']}")
            except Exception as e:
                print(f"[MediaRoulette] Failed to fetch libraries on save: {e}")
        
        save_config(config)

        session['plex_token'] = config.get('plex_token')
        session['plex_server_url'] = config.get('plex_server_url')
        session['theme'] = config.get('default_theme', 'dark')

        return redirect(url_for('index'))

    return render_template('settings.html',
                           config=config,
                           libraries=config.get('plex_libraries', []),
                           servers=config.get('plex_servers', []),
                           default_theme=config.get("default_theme", "dark"))

@app.route('/watchlist', methods=['GET', 'POST'])
@login_required
def watchlist():
    if request.method == 'POST':
        title = request.form.get('title')
        year = request.form.get('year')
        if title and year:
            watchlist = load_watchlist()
            updated = [item for item in watchlist if not (item['title'] == title and str(item['year']) == str(year))]
            with open(WATCHLIST_FILE, 'w') as f:
                json.dump(updated, f, indent=2)
        return redirect(url_for('watchlist'))

    return render_template('watchlist.html',
                           watchlist=load_watchlist(),
                           default_theme=load_config().get("default_theme", "dark"))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug, host='0.0.0.0', port=port)
