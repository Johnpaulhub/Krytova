import os
import io
import re
import uuid
import secrets
import sqlite3
from collections import defaultdict
from datetime import datetime, date, timedelta
from time import time
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, session,
    flash, g, send_file, jsonify, abort
)
from flask_socketio import SocketIO, join_room, leave_room, emit
from jinja2 import DictLoader
from markupsafe import Markup, escape
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from bs4 import BeautifulSoup
import requests
from PIL import Image

UPLOAD_FOLDER = "static/uploads"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
DATABASE = "unique_social.db"
SYSTEM_USERNAME = "krptova"

DAILY_PROMPTS = [
    "Is being liked a trap?",
    "Should schools teach disagreement as a skill?",
    "Is optimism a form of laziness?",
    "What should people stop pretending about?",
    "Is popularity proof of anything?",
    "When is silence actually a lie?",
]

app = Flask(__name__)
app.secret_key = os.environ.get("KRPTOVA_SECRET", "dev-only-change-me-before-public")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
app.config["DEBUG"] = False
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')
_rate = defaultdict(list)

def rate_ok(key, n=40, window=30):
    now = time()
    _rate[key] = [t for t in _rate[key] if now - t < window]
    if len(_rate[key]) >= n:
        return False
    _rate[key].append(now)
    return True

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def optimize_image(file_storage, output_path, max_size=(1200, 1200), quality=80):
    try:
        img = Image.open(file_storage)
        img.verify()
        file_storage.seek(0)
        img = Image.open(file_storage)
        img.thumbnail(max_size)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.save(output_path, "WEBP", quality=quality)
        return True
    except Exception:
        return False

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("Sign in first.")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

def timeago(value):
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", ""))
    except ValueError:
        return str(value)
    sec = int((datetime.utcnow() - dt).total_seconds())
    if sec < 0:
        sec = 0
    steps = ((86400, "d"), (3600, "h"), (60, "m"))
    for size, label in steps:
        if sec >= size:
            return f"{sec // size}{label}"
    return "now"

def avatar_color(name):
    h = 0
    for ch in name or "?":
        h = (h * 31 + ord(ch)) & 0xFFFFFF
    palette = ["#3b82f6", "#10b981", "#f59e0b", "#8b5cf6", "#ef4444", "#06b6d4", "#f97316"]
    return palette[h % len(palette)]

def format_pulse(content):
    text = str(content or "")
    out = str(escape(text))
    out = re.sub(r"(#\w+)", r'<a href="/tag/\1" class="tag">\1</a>', out)
    out = re.sub(r"(?<!\w)@([A-Za-z0-9_]{1,32})", r'<a href="/profile/\1" class="tag">@\1</a>', out)
    return Markup(out)

def pulse_score(resonates, replies, breaks):
    return int(resonates or 0) + 2 * int(replies or 0) + 3 * int(breaks or 0)

# --- DATABASE & CORE SETUP ---

def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                bio TEXT,
                avatar TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            db.execute("ALTER TABLE users ADD COLUMN avatar TEXT;")
        except sqlite3.OperationalError:
            pass

        db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                content TEXT,
                image_filenames TEXT,
                og_url TEXT,
                og_title TEXT,
                og_description TEXT,
                og_image TEXT,
                is_stance BOOLEAN DEFAULT 0,
                is_prompt BOOLEAN DEFAULT 0,
                retracted BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER,
                user_id INTEGER,
                content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(post_id) REFERENCES posts(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS resonates (
                user_id INTEGER,
                post_id INTEGER,
                PRIMARY KEY(user_id, post_id),
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(post_id) REFERENCES posts(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS breaks (
                user_id INTEGER,
                post_id INTEGER,
                PRIMARY KEY(user_id, post_id),
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(post_id) REFERENCES posts(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS follows (
                follower_id INTEGER,
                followed_id INTEGER,
                PRIMARY KEY(follower_id, followed_id),
                FOREIGN KEY(follower_id) REFERENCES users(id),
                FOREIGN KEY(followed_id) REFERENCES users(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS bookmarks (
                user_id INTEGER,
                post_id INTEGER,
                PRIMARY KEY(user_id, post_id),
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(post_id) REFERENCES posts(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER,
                FOREIGN KEY(post_id) REFERENCES posts(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS poll_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                poll_id INTEGER,
                text TEXT,
                FOREIGN KEY(poll_id) REFERENCES polls(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS poll_votes (
                user_id INTEGER,
                poll_id INTEGER,
                option_id INTEGER,
                PRIMARY KEY(user_id, poll_id),
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(poll_id) REFERENCES polls(id),
                FOREIGN KEY(option_id) REFERENCES poll_options(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS stories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                image_filename TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                sender_id INTEGER,
                type TEXT,
                post_id INTEGER,
                is_read BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(sender_id) REFERENCES users(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER,
                recipient_id INTEGER,
                content TEXT,
                is_read BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(sender_id) REFERENCES users(id),
                FOREIGN KEY(recipient_id) REFERENCES users(id)
            )
        """)
        db.execute("INSERT OR IGNORE INTO users (id, username, password_hash, bio) VALUES (?, ?, ?, ?)",
                   (1, SYSTEM_USERNAME, generate_password_hash("system-secure-krptova"), "System core broadcast account."))
        db.execute("INSERT OR IGNORE INTO users (username, password_hash, bio) VALUES (?, ?, ?)",
                   ("johnpaul", generate_password_hash("defaultpass"), "Undergraduate student pursuing Bachelor of Education (Arts) at Karatina University."))
        db.commit()

# --- TEMPLATES ---

BASE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Krptova</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
    <style>
        :root {
            --bg: #0b0e14;
            --card-bg: #151a21;
            --text: #f3f4f6;
            --text-muted: #9ca3af;
            --border: #2d3748;
            --primary: #3b82f6;
            --primary-hover: #2563eb;
            --danger: #ef4444;
            --success: #10b981;
            --warn: #f59e0b;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
        body { background: var(--bg); color: var(--text); padding-top: 65px; padding-bottom: 75px; overflow-x: hidden; }
        nav { background: rgba(11,14,20,.9); border-bottom: 1px solid var(--border); padding: 0 16px; display: flex; justify-content: space-between; align-items: center; position: fixed; top: 0; left: 0; right: 0; z-index: 100; height: 60px; backdrop-filter: blur(12px); }
        .logo { font-size: 1.35rem; font-weight: 800; color: var(--text); text-decoration: none; letter-spacing: -.5px; }
        .nav-links { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
        .nav-links a { color: var(--text-muted); text-decoration: none; font-weight: 500; font-size: .85rem; position: relative; }
        .nav-links a:hover { color: var(--primary); }
        .container { max-width: 600px; margin: 0 auto; padding: 0 12px; }
        .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 14px; padding: 18px; margin-bottom: 16px; position: relative; }
        .btn { background: var(--primary); color: #fff; border: none; padding: 8px 18px; border-radius: 20px; font-weight: 600; cursor: pointer; text-decoration: none; font-size: .85rem; display: inline-block; }
        .btn:hover { background: var(--primary-hover); }
        .btn-outline { background: transparent; border: 1px solid var(--border); color: var(--text); }
        .form-group { margin-bottom: 1rem; }
        label { display: block; margin-bottom: .4rem; font-size: .85rem; color: var(--text-muted); font-weight: 500; }
        input, textarea { width: 100%; padding: 12px; border-radius: 10px; border: 1px solid var(--border); background: #090d16; color: var(--text); font-size: .95rem; outline: none; }
        input:focus, textarea:focus { border-color: var(--primary); }
        .alert { padding: 12px; border-radius: 10px; margin-bottom: 1rem; background: rgba(59,130,246,.1); border: 1px solid rgba(59,130,246,.3); color: #60a5fa; text-align: center; font-size: .85rem; }
        .post-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; gap: 8px; }
        .who { display: flex; align-items: center; gap: 8px; min-width: 0; }
        .av { width: 34px; height: 34px; border-radius: 50%; display: grid; place-items: center; font-size: .75rem; font-weight: 800; color: #fff; flex-shrink: 0; object-fit: cover; border: 1px solid var(--border); }
        .username { font-weight: 700; font-size: .9rem; color: var(--text); text-decoration: none; }
        .timestamp { font-size: .75rem; color: var(--text-muted); white-space: nowrap; }
        .post-content { font-size: .95rem; line-height: 1.5; margin-bottom: 12px; word-break: break-word; }
        .image-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 8px; margin-bottom: 12px; }
        .post-image { width: 100%; height: 130px; border-radius: 8px; object-fit: cover; border: 1px solid var(--border); cursor: pointer; transition: opacity .2s; }
        .post-image:hover { opacity: .9; }
        .post-image.single { height: auto; max-height: 350px; grid-column: 1 / -1; }
        .quote-box { background: rgba(0,0,0,.25); border-left: 3px solid var(--primary); padding: 10px 12px; border-radius: 0 8px 8px 0; margin-bottom: 12px; font-size: .9rem; }
        .poll-container { background: #090d16; border: 1px solid var(--border); border-radius: 10px; padding: 12px; margin-bottom: 12px; }
        .poll-option { display: block; width: 100%; text-align: left; background: var(--card-bg); border: 1px solid var(--border); padding: 8px 12px; border-radius: 8px; margin-top: 6px; color: var(--text); cursor: pointer; position: relative; overflow: hidden; font-size: .85rem; }
        .poll-option:hover { border-color: var(--primary); }
        .poll-bar { position: absolute; top: 0; left: 0; bottom: 0; background: rgba(59, 130, 246, 0.2); z-index: 1; pointer-events: none; }
        .poll-text { position: relative; z-index: 2; display: flex; justify-content: space-between; }
        .post-actions { display: flex; gap: 10px; margin-top: 12px; border-top: 1px solid var(--border); padding-top: 12px; font-size: .8rem; color: var(--text-muted); align-items: center; flex-wrap: wrap; }
        .linkish { background: none; border: none; color: var(--text-muted); cursor: pointer; font-size: .8rem; padding: 0; }
        .linkish:hover, .post-actions a:hover { color: var(--primary); }
        .post-actions a { color: var(--text-muted); text-decoration: none; }
        .tag { color: var(--primary); text-decoration: none; font-weight: 600; }
        .badge { display: inline-block; font-size: .65rem; font-weight: 800; letter-spacing: .06em; padding: 2px 7px; border-radius: 999px; border: 1px solid var(--warn); color: var(--warn); }
        .badge-sys { border-color: var(--primary); color: var(--primary); }
        .badge-dead { border-color: var(--danger); color: var(--danger); }
        .badge-counter { background: var(--danger); color: white; border-radius: 50%; padding: 1px 5px; font-size: .6rem; position: absolute; top: -6px; right: -10px; font-weight: bold; display: none; }
        .feeds { display: flex; gap: 10px; flex-wrap: wrap; font-size: .85rem; }
        .feeds a { color: var(--text-muted); text-decoration: none; font-weight: 600; }
        .feeds a.on { color: var(--primary); }
        .mobile-nav { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(11,14,20,.9); border-top: 1px solid var(--border); display: flex; justify-content: space-around; z-index: 100; height: 60px; align-items: center; backdrop-filter: blur(12px); }
        .mobile-nav a { color: var(--text-muted); text-decoration: none; font-size: 1.25rem; }
        .row { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
        .stories-bar { display: flex; gap: 12px; overflow-x: auto; padding-bottom: 12px; margin-bottom: 16px; scrollbar-width: none; }
        .stories-bar::-webkit-scrollbar { display: none; }
        .story-ring { width: 56px; height: 56px; border-radius: 50%; background: linear-gradient(45deg, var(--primary), var(--warn)); padding: 2px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; cursor: pointer; }
        .story-inner { width: 100%; height: 100%; border-radius: 50%; background: var(--card-bg); display: flex; align-items: center; justify-content: center; overflow: hidden; }
        .story-inner img { width: 100%; height: 100%; object-fit: cover; }
        #lightbox { display:none; position:fixed; z-index:1000; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,.9); justify-content:center; align-items:center; }
        #lightbox img { max-width:90%; max-height:90%; border-radius:8px; object-fit:contain; }
        #lightbox span { position:absolute; top:20px; right:30px; font-size:2rem; color:#fff; cursor:pointer; }
    </style>
</head>
<body>
    <nav>
        <a href="{{ url_for('index') }}" class="logo">⚡ Krptova</a>
        <div class="nav-links">
            <a href="{{ url_for('index') }}">Home</a>
            <a href="{{ url_for('explore') }}">Explore</a>
            <a href="{{ url_for('trending_page') }}">🔥 Trending</a>
            {% if session.get('user_id') %}
                <a href="{{ url_for('bookmarks_page') }}">🔖 Bookmarks</a>
                <a href="{{ url_for('messages') }}">Inbox</a>
                <a href="{{ url_for('notifications') }}">Notifications<span id="notif-badge" class="badge-counter">0</span></a>
                <a href="{{ url_for('profile', username=session.get('username')) }}">Profile</a>
                <a href="{{ url_for('logout') }}" style="color:var(--danger)">Logout</a>
            {% else %}
                <a href="{{ url_for('login') }}" class="btn" style="padding:6px 14px;">Log In</a>
            {% endif %}
        </div>
    </nav>
    <div class="container" style="margin-top:16px;">
        {% with messages = get_flashed_messages() %}
            {% if messages %}
                {% for message in messages %}<div class="alert">{{ message }}</div>{% endfor %}
            {% endif %}
        {% endwith %}
        {% block content %}{% endblock %}
    </div>
    <div id="lightbox" onclick="this.style.display='none'">
        <span onclick="document.getElementById('lightbox').style.display='none'">&times;</span>
        <img id="lightbox-img" src="" alt="">
    </div>
    <div class="mobile-nav">
        <a href="{{ url_for('index') }}">🏠</a>
        <a href="{{ url_for('explore') }}">🔍</a>
        <a href="{{ url_for('trending_page') }}">🔥</a>
        <a href="{{ url_for('messages') }}">💬</a>
    </div>
    <script>
    const socket = io();
    function openLightbox(src) {
        document.getElementById('lightbox-img').src = src;
        document.getElementById('lightbox').style.display = 'flex';
    }
    {% if session.get('user_id') %}
    socket.emit('join_notifications', {user_id: {{ session.get('user_id') }}});
    socket.on('new_notification', function(data) {
        const badge = document.getElementById('notif-badge');
        if (badge) {
            let count = parseInt(badge.textContent || '0') + 1;
            badge.textContent = count;
            badge.style.display = 'inline-block';
        }
    });
    fetch('/notifications/json').then(r => r.json()).then(data => {
        if (data.length > 0) {
            const badge = document.getElementById('notif-badge');
            if (badge) {
                badge.textContent = data.length;
                badge.style.display = 'inline-block';
            }
        }
    });
    {% endif %}
    </script>
</body>
</html>
"""

INDEX_TEMPLATE = """
{% extends "base.html" %}
{% block content %}
{% if session.get('user_id') %}
<div class="stories-bar">
    <div style="text-align:center;flex-shrink:0;">
        <div class="story-ring" onclick="document.getElementById('story-upload-form').style.display=document.getElementById('story-upload-form').style.display=='none'?'block':'none'">
            <div class="story-inner" style="background:var(--primary);color:#fff;font-size:1.2rem;font-weight:bold;">+</div>
        </div>
        <span style="font-size:.65rem;color:var(--text-muted);">Add 24h</span>
    </div>
    {% for story in stories %}
    <div style="text-align:center;flex-shrink:0;" onclick="openLightbox('{{ url_for('static', filename='uploads/' + story.image_filename) }}')">
        <div class="story-ring">
            <div class="story-inner">
                <img src="{{ url_for('static', filename='uploads/' + story.image_filename) }}" alt="">
            </div>
        </div>
        <span style="font-size:.65rem;color:var(--text-muted);">@{{ story.username }}</span>
    </div>
    {% endfor %}
</div>

<div id="story-upload-form" class="card" style="display:none;background:#10151c;border-style:dashed;">
    <h3 style="font-size:.9rem;margin-bottom:8px;color:var(--warn);">⏱️ Post a 24-Hour Drop</h3>
    <form method="POST" action="{{ url_for('create_story') }}" enctype="multipart/form-data">
        <input type="hidden" name="csrf" value="{{ csrf_token }}">
        <input type="file" name="file" accept="image/*" required style="margin-bottom:8px;font-size:.8rem;">
        <div style="display:flex;justify-content:flex-end;">
            <button type="submit" class="btn" style="padding:4px 12px;font-size:.75rem;">Upload 24h Drop</button>
        </div>
    </form>
</div>

<div class="card">
    <form method="POST" action="{{ url_for('create_post') }}" enctype="multipart/form-data">
        <input type="hidden" name="csrf" value="{{ csrf_token }}">
        <textarea name="content" rows="3" placeholder="Take a stance. Use #tags and @names." style="resize:none;background:transparent;border:none;font-size:1rem;color:var(--text);outline:none;"></textarea>
        
        <div id="poll-creator" style="display:none;margin-top:10px;border-top:1px dashed var(--border);padding-top:10px;">
            <label style="font-size:.8rem;color:var(--warn);margin-bottom:4px;">Attach Poll Options</label>
            <input type="text" name="poll_opt1" placeholder="Option 1" style="margin-bottom:6px;font-size:.85rem;padding:8px;">
            <input type="text" name="poll_opt2" placeholder="Option 2" style="font-size:.85rem;padding:8px;">
        </div>

        <div class="row" style="margin-top:12px;border-top:1px solid var(--border);padding-top:10px;flex-wrap:wrap;">
            <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
                <label style="margin:0;cursor:pointer;background:#090d16;border:1px solid var(--border);padding:6px 12px;border-radius:10px;font-size:.8rem;color:var(--text-muted);">
                    📷 Upload Images
                    <input type="file" name="files" accept="image/*" multiple style="display:none;">
                </label>
                <button type="button" class="btn btn-outline" style="padding:5px 10px;font-size:.75rem;" onclick="let el=document.getElementById('poll-creator');el.style.display=el.style.display=='none'?'block':'none';">📊 Add Poll</button>
                <label style="margin:0;display:flex;gap:6px;align-items:center;color:var(--warn);font-size:.8rem;">
                    <input type="checkbox" name="is_stance" value="1" style="width:auto;"> Stance
                </label>
            </div>
            <button type="submit" class="btn">Broadcast</button>
        </div>
    </form>
</div>
{% endif %}

<div class="row" style="margin-bottom:12px;">
    <span style="font-weight:700;font-size:.95rem;color:var(--text-muted);">{{ page_title | default('Timeline') }}</span>
    <div class="feeds">
        {% if session.get('user_id') %}
        <a href="{{ url_for('index', feed='following') }}" class="{{ 'on' if feed_type=='following' }}">Following</a>
        {% endif %}
        <a href="{{ url_for('index', feed='global') }}" class="{{ 'on' if feed_type=='global' }}">Unfiltered</a>
        <a href="{{ url_for('index', feed='clash') }}" class="{{ 'on' if feed_type=='clash' }}">Clash</a>
    </div>
</div>

<div id="posts-container">
    {% for post in posts %}
        {% include "post_card.html" %}
    {% else %}
    <p style="color:var(--text-muted);text-align:center;padding:40px 0;">The network is quiet. Take a stance.</p>
    {% endfor %}
</div>

<div id="load-more-trigger" style="text-align:center;padding:20px;">
    <button id="load-more-btn" class="btn btn-outline" onclick="loadMorePosts()" style="font-size:.8rem;">Load More Older Posts</button>
</div>

<script>
let page = 1;
const feedType = "{{ feed_type }}";
async function loadMorePosts() {
    page++;
    const btn = document.getElementById('load-more-btn');
    btn.textContent = 'Loading...';
    try {
        const res = await fetch(`/feed/json?feed=${feedType}&page=${page}`);
        const data = await res.json();
        if (data.posts.length === 0) {
            document.getElementById('load-more-trigger').innerHTML = '<p style="color:var(--text-muted);font-size:.8rem;">No more posts to load.</p>';
            return;
        }
        const container = document.getElementById('posts-container');
        for (const post of data.posts) {
            const div = document.createElement('div');
            div.innerHTML = post.html_card;
            container.appendChild(div.firstElementChild);
        }
        btn.textContent = 'Load More Older Posts';
    } catch(err) {
        btn.textContent = 'Error loading posts';
    }
}
</script>
{% endblock %}
"""

POST_CARD_TEMPLATE = """
<div class="card">
    <div class="post-header">
        <div class="who">
            {% if post.avatar %}
                <img src="{{ url_for('static', filename='uploads/' + post.avatar) }}" class="av" alt="">
            {% else %}
                <div class="av" style="background:{{ post.username|avatar }}">{{ post.username[:1]|upper }}</div>
            {% endif %}
            <div>
                <a href="{{ url_for('profile', username=post.username) }}" class="username">@{{ post.username }}</a>
                {% if post.is_prompt %}<span class="badge badge-sys">PROMPT</span>{% endif %}
                {% if post.is_stance and not post.retracted %}<span class="badge">STANCE</span>{% endif %}
                {% if post.retracted %}<span class="badge badge-dead">RETRACTED</span>{% endif %}
            </div>
        </div>
        <span class="timestamp">{{ post.created_at|ago }}</span>
    </div>
    {% if post.retracted %}
        <div class="post-content" style="color:var(--text-muted);">This stance was retracted in public.</div>
    {% else %}
        {% if post.quoted_post %}
        <div class="quote-box">
            <div style="font-weight:700;font-size:.8rem;margin-bottom:4px;color:var(--text-muted);">@{{ post.quoted_post.username }}</div>
            <div>{{ post.quoted_post.formatted_content }}</div>
        </div>
        {% endif %}
        <div class="post-content">{{ post.formatted_content }}</div>
        
        {% if post.og_url %}
        <a href="{{ post.og_url }}" target="_blank" style="text-decoration:none;color:inherit;">
            <div style="border:1px solid var(--border);border-radius:8px;background:#090d16;overflow:hidden;margin-bottom:12px;display:flex;flex-direction:column;">
                {% if post.og_image %}
                <img src="{{ post.og_image }}" style="width:100%;height:150px;object-fit:cover;" alt="">
                {% endif %}
                <div style="padding:10px;">
                    <div style="font-weight:700;font-size:.85rem;margin-bottom:2px;color:var(--text);">{{ post.og_title or post.og_url }}</div>
                    <div style="font-size:.75rem;color:var(--text-muted);">{{ post.og_description or '' }}</div>
                </div>
            </div>
        </a>
        {% endif %}

        {% if post.poll %}
        <div class="poll-container">
            <div style="font-size:.8rem;font-weight:bold;margin-bottom:6px;color:var(--text-muted);">📊 Live Poll</div>
            {% set total_votes = post.poll.options | sum(attribute='votes') %}
            {% for opt in post.poll.options %}
                {% set pct = (opt.votes / total_votes * 100) | round | int if total_votes > 0 else 0 %}
                <form method="POST" action="{{ url_for('vote_poll', option_id=opt.id) }}" style="margin:0;">
                    <input type="hidden" name="csrf" value="{{ csrf_token }}">
                    <button type="submit" class="poll-option">
                        <div class="poll-bar" style="width: {{ pct }}%;"></div>
                        <div class="poll-text">
                            <span>{{ opt.text }}</span>
                            <span style="font-weight:bold;color:var(--primary);">{{ pct }}% ({{ opt.votes }})</span>
                        </div>
                    </button>
                </form>
            {% endfor %}
        </div>
        {% endif %}

        {% if post.image_filenames %}
            {% set imgs = post.image_filenames.split(',') %}
            <div class="image-grid">
                {% for img in imgs %}
                    <img src="{{ url_for('static', filename='uploads/' + img) }}" class="post-image {% if imgs|length == 1 %}single{% endif %}" onclick="openLightbox(this.src)" alt="">
                {% endfor %}
            </div>
        {% endif %}
    {% endif %}
    <div style="font-size:.75rem;color:var(--text-muted);margin-bottom:6px;">Score {{ post.score }} · ⚡ {{ post.resonates }} · ⚔ {{ post.breaks }}</div>
    <div class="post-actions">
        {% if session.get('user_id') and not post.retracted %}
        <form method="POST" action="{{ url_for('resonate_post', post_id=post.id) }}" style="display:inline;">
            <input type="hidden" name="csrf" value="{{ csrf_token }}">
            <button class="linkish" type="submit">⚡ Resonate</button>
        </form>
        <form method="POST" action="{{ url_for('bookmark_post', post_id=post.id) }}" style="display:inline;">
            <input type="hidden" name="csrf" value="{{ csrf_token }}">
            <button class="linkish" type="submit">🔖 Save</button>
        </form>
        {% endif %}
        <a href="{{ url_for('post_detail', post_id=post.id) }}">💬 {{ post.comments_count }} Replies</a>
        {% if session.get('user_id') and session.get('user_id') == post.user_id %}
        <form method="POST" action="{{ url_for('delete_post', post_id=post.id) }}" style="display:inline;margin-left:auto;" onsubmit="return confirm('Delete post?')">
            <input type="hidden" name="csrf" value="{{ csrf_token }}">
            <button class="linkish" style="color:var(--danger);" type="submit">Delete</button>
        </form>
        {% endif %}
    </div>
</div>
"""

POST_DETAIL_TEMPLATE = """
{% extends "base.html" %}
{% block content %}
<a href="{{ url_for('index') }}" style="color:var(--primary);text-decoration:none;font-size:.85rem;display:inline-block;margin-bottom:12px;">← Back to Timeline</a>
{% set post = post %}
{% include "post_card.html" %}

<div class="card" style="margin-top:16px;">
    <h3 style="font-size:.9rem;margin-bottom:10px;">Leave a Reply</h3>
    {% if session.get('user_id') %}
    <form method="POST" action="{{ url_for('add_comment', post_id=post.id) }}">
        <input type="hidden" name="csrf" value="{{ csrf_token }}">
        <textarea name="content" rows="2" placeholder="Write your response..." required style="resize:none;margin-bottom:8px;font-size:.9rem;"></textarea>
        <div style="display:flex;justify-content:flex-end;">
            <button type="submit" class="btn" style="padding:6px 14px;">Reply</button>
        </div>
    </form>
    {% else %}
    <p style="font-size:.85rem;color:var(--text-muted);"><a href="{{ url_for('login') }}" style="color:var(--primary);">Sign in</a> to reply.</p>
    {% endif %}
</div>

<h4 style="font-size:.9rem;color:var(--text-muted);margin:16px 0 10px 0;">Replies</h4>
{% for c in comments %}
<div class="card" style="padding:12px;background:#0f131a;">
    <div class="post-header" style="margin-bottom:6px;">
        <div class="who">
            {% if c.avatar %}
                <img src="{{ url_for('static', filename='uploads/' + c.avatar) }}" class="av" style="width:28px;height:28px;" alt="">
            {% else %}
                <div class="av" style="width:28px;height:28px;background:{{ c.username|avatar }}">{{ c.username[:1]|upper }}</div>
            {% endif %}
            <a href="{{ url_for('profile', username=c.username) }}" class="username" style="font-size:.85rem;">@{{ c.username }}</a>
        </div>
        <span class="timestamp">{{ c.created_at|ago }}</span>
    </div>
    <div style="font-size:.85rem;">{{ c.formatted_content }}</div>
</div>
{% else %}
<p style="color:var(--text-muted);font-size:.85rem;">No replies yet. Start the conversation.</p>
{% endfor %}
{% endblock %}
"""

EXPLORE_TEMPLATE = """
{% extends "base.html" %}
{% block content %}
<div class="card">
    <form method="GET" action="{{ url_for('explore') }}" style="display:flex;gap:8px;">
        <input type="text" name="q" value="{{ q }}" placeholder="Search posts or #tags..." style="margin:0;">
        <button type="submit" class="btn" style="padding:0 16px;">Search</button>
    </form>
</div>

<div class="card" style="background:#10151c;">
    <h3 style="font-size:.9rem;margin-bottom:10px;color:var(--warn);">🔥 Trending Hashtags</h3>
    <div style="display:flex;flex-wrap:wrap;gap:8px;">
        {% for tag, count in trending_tags %}
        <a href="{{ url_for('tag_view', tag=tag.replace('#','')) }}" class="btn btn-outline" style="padding:4px 10px;font-size:.75rem;">
            {{ tag }} <span style="color:var(--text-muted);">({{ count }})</span>
        </a>
        {% else %}
        <p style="color:var(--text-muted);font-size:.8rem;">No trending tags right now.</p>
        {% endfor %}
    </div>
</div>

{% if q %}
<h3 style="font-size:.9rem;color:var(--text-muted);margin-bottom:12px;">Results for {{ q }}</h3>
{% endif %}
<div id="posts-container">
    {% for post in posts %}
        {% include "post_card.html" %}
    {% else %}
    <p style="color:var(--text-muted);text-align:center;padding:30px 0;">No matching posts found.</p>
    {% endfor %}
</div>
{% endblock %}
"""

TRENDING_TEMPLATE = """
{% extends "base.html" %}
{% block content %}
<h3 style="font-size:1.1rem;font-weight:800;margin-bottom:14px;">🔥 High Resonance Feed</h3>
<div id="posts-container">
    {% for post in posts %}
        {% include "post_card.html" %}
    {% else %}
    <p style="color:var(--text-muted);text-align:center;padding:30px 0;">No trending discussions right now.</p>
    {% endfor %}
</div>
{% endblock %}
"""

BOOKMARKS_TEMPLATE = """
{% extends "base.html" %}
{% block content %}
<h3 style="font-size:1.1rem;font-weight:800;margin-bottom:14px;">🔖 Bookmarked Posts</h3>
<div id="posts-container">
    {% for post in posts %}
        {% include "post_card.html" %}
    {% else %}
    <p style="color:var(--text-muted);text-align:center;padding:30px 0;">You haven't bookmarked any posts yet.</p>
    {% endfor %}
</div>
{% endblock %}
"""

NOTIFICATIONS_TEMPLATE = """
{% extends "base.html" %}
{% block content %}
<h3 style="font-size:1.1rem;font-weight:800;margin-bottom:14px;">⚡ Notifications</h3>
<div>
    {% for n in notifs %}
    <div class="card" style="padding:12px;display:flex;align-items:center;justify-content:space-between;font-size:.85rem;">
        <div>
            <a href="{{ url_for('profile', username=n.username) }}" class="tag">@{{ n.username }}</a> 
            {% if n.type == 'resonate' %}resonated with your broadcast.
            {% elif n.type == 'reply' %}replied to your post.
            {% elif n.type == 'follow' %}started following you.
            {% elif n.type == 'message' %}sent you a direct message.
            {% else %}interacted with your profile.
            {% endif %}
        </div>
        <span class="timestamp">{{ n.created_at|ago }}</span>
    </div>
    {% else %}
    <p style="color:var(--text-muted);text-align:center;padding:30px 0;">No notifications yet.</p>
    {% endfor %}
</div>
{% endblock %}
"""

PROFILE_TEMPLATE = """
{% extends "base.html" %}
{% block content %}
<div class="card" style="text-align:center;padding:24px 18px;">
    {% if profile_user.avatar %}
        <img src="{{ url_for('static', filename='uploads/' + profile_user.avatar) }}" class="av" style="width:72px;height:72px;margin:0 auto 12px auto;" alt="">
    {% else %}
        <div class="av" style="width:72px;height:72px;font-size:1.5rem;margin:0 auto 12px auto;background:{{ profile_user.username|avatar }}">{{ profile_user.username[:1]|upper }}</div>
    {% endif %}
    
    <h2 style="font-size:1.2rem;margin-bottom:4px;">{% if profile_user.username == 'johnpaul' %}John Paul Ojwang Omondi{% else %}@{{ profile_user.username }}{% endif %}</h2>
    <p style="font-size:.8rem;color:var(--text-muted);margin-bottom:2px;">@{{ profile_user.username }}</p>
    {% if profile_user.bio %}<p style="font-size:.85rem;color:var(--text);margin:8px 0;">{{ profile_user.bio }}</p>{% endif %}
    <p style="font-size:.8rem;color:var(--text-muted);margin-bottom:12px;">Joined {{ profile_user.created_at[:10] }}</p>

    <div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-bottom:12px;">
        {% if session.get('user_id') and session.get('user_id') == profile_user.id %}
        <form method="POST" action="{{ url_for('upload_avatar') }}" enctype="multipart/form-data">
            <input type="hidden" name="csrf" value="{{ csrf_token }}">
            <input type="file" name="file" accept="image/*" required style="font-size: 0.75rem; margin-bottom: 6px;">
            <button type="submit" class="btn btn-outline" style="padding: 4px 12px; font-size: 0.75rem;">Change Photo</button>
        </form>
        {% endif %}

        {% if session.get('user_id') and session.get('user_id') != profile_user.id %}
        <form method="POST" action="{{ url_for('follow_user', user_id=profile_user.id) }}">
            <input type="hidden" name="csrf" value="{{ csrf_token }}">
            <button type="submit" class="btn {% if not is_following %}btn-outline{% endif %}" style="padding:6px 20px;">
                {{ 'Following' if is_following else 'Follow' }}
            </button>
        </form>
        <a href="{{ url_for('messages_chat', username=profile_user.username) }}" class="btn btn-outline" style="padding:6px 16px;">Direct Message</a>
        {% endif %}
    </div>
</div>

<h4 style="font-size:.9rem;color:var(--text-muted);margin:16px 0 10px 0;">Activity Log</h4>
<div id="posts-container">
    {% for post in posts %}
        {% include "post_card.html" %}
    {% else %}
    <p style="color:var(--text-muted);text-align:center;padding:30px 0;">No posts shared by this user yet.</p>
    {% endfor %}
</div>
{% endblock %}
"""

MESSAGES_TEMPLATE = """
{% extends "base.html" %}
{% block content %}
<h3 style="font-size:1.1rem;font-weight:800;margin-bottom:14px;">💬 Encrypted Inbox</h3>
<div class="card" style="padding:0;overflow:hidden;">
    {% for conv in conversations %}
    <a href="{{ url_for('messages_chat', username=conv.username) }}" style="display:flex;align-items:center;padding:12px 16px;border-bottom:1px solid var(--border);text-decoration:none;color:inherit;gap:12px;">
        {% if conv.avatar %}
            <img src="{{ url_for('static', filename='uploads/' + conv.avatar) }}" class="av" alt="">
        {% else %}
            <div class="av" style="background:{{ conv.username|avatar }}">{{ conv.username[:1]|upper }}</div>
        {% endif %}
        <div style="flex-grow:1;min-width:0;">
            <div style="font-weight:700;font-size:.9rem;color:var(--text);">@{{ conv.username }}</div>
            <div style="font-size:.8rem;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{{ conv.last_message }}</div>
        </div>
        <span class="timestamp">{{ conv.created_at|ago }}</span>
    </a>
    {% else %}
    <div style="padding:40px;text-align:center;color:var(--text-muted);">
        <p style="margin-bottom:8px;">No active message conversations yet.</p>
        <p style="font-size:.8rem;">Visit a user's profile to start a secure chat channel.</p>
    </div>
    {% endfor %}
</div>
{% endblock %}
"""

MESSAGES_CHAT_TEMPLATE = """
{% extends "base.html" %}
{% block content %}
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
    <a href="{{ url_for('messages') }}" style="color:var(--primary);text-decoration:none;font-size:.85rem;">← Back to Inbox</a>
    <h3 style="font-size:1rem;">Chat with @{{ chat_user.username }}</h3>
</div>

<div class="card" style="height:400px;overflow-y:auto;display:flex;flex-direction:column;gap:10px;padding:16px;" id="chat-messages-box">
    {% for m in messages %}
    <div style="max-width:75%; padding:10px 14px; border-radius:12px; font-size:.9rem; {{ 'background:var(--primary);color:#fff;align-self:flex-end;' if m.sender_id == session.get('user_id') else 'background:#090d16;border:1px solid var(--border);align-self:flex-start;' }}">
        <div>{{ m.content }}</div>
        <div style="font-size:0.65rem; opacity:0.7; margin-top:4px; text-align:right;">{{ m.created_at|ago }}</div>
    </div>
    {% else %}
    <p style="color:var(--text-muted);text-align:center;margin:auto;font-size:.85rem;">Send your first direct message to @{{ chat_user.username }}.</p>
    {% endfor %}
</div>

<div class="card" style="padding:12px;">
    <form id="chat-form" onsubmit="sendDirectMessage(event)" style="display:flex;gap:8px;">
        <input type="hidden" id="recipient_id" value="{{ chat_user.id }}">
        <input type="text" id="chat-input" placeholder="Type a message..." required autocomplete="off" style="margin:0;">
        <button type="submit" class="btn" style="padding:0 20px;">Send</button>
    </form>
</div>

<script>
const chatBox = document.getElementById('chat-messages-box');
chatBox.scrollTop = chatBox.scrollHeight;

socket.emit('join_chat', {user_id: {{ session.get('user_id') }}});

socket.on('new_direct_message', function(data) {
    if (data.sender_id === {{ chat_user.id }} || data.sender_id === {{ session.get('user_id') }}) {
        const div = document.createElement('div');
        const isMe = data.sender_id === {{ session.get('user_id') }};
        div.style.cssText = isMe ? 'max-width:75%; padding:10px 14px; border-radius:12px; font-size:.9rem; background:var(--primary); color:#fff; align-self:flex-end;' : 'max-width:75%; padding:10px 14px; border-radius:12px; font-size:.9rem; background:#090d16; border:1px solid var(--border); align-self:flex-start;';
        div.innerHTML = `<div>${data.content}</div><div style="font-size:0.65rem; opacity:0.7; margin-top:4px; text-align:right;">now</div>`;
        chatBox.appendChild(div);
        chatBox.scrollTop = chatBox.scrollHeight;
    }
});

function sendDirectMessage(e) {
    e.preventDefault();
    const input = document.getElementById('chat-input');
    const content = input.value.trim();
    const recipient_id = document.getElementById('recipient_id').value;
    if (!content) return;
    
    socket.emit('send_direct_message', {
        recipient_id: parseInt(recipient_id),
        content: content
    });
    input.value = '';
}
</script>
{% endblock %}
"""

LOGIN_TEMPLATE = """
{% extends "base.html" %}
{% block content %}
<div class="card" style="max-width:380px;margin:40px auto;padding:24px;">
    <h2 style="font-size:1.2rem;margin-bottom:16px;text-align:center;">Sign In to Krptova</h2>
    <form method="POST">
        <input type="hidden" name="csrf" value="{{ csrf_token }}">
        <div class="form-group">
            <label>Username</label>
            <input type="text" name="username" required autocomplete="username">
        </div>
        <div class="form-group">
            <label>Password</label>
            <input type="password" name="password" required autocomplete="current-password">
        </div>
        <button type="submit" class="btn" style="width:100%;margin-top:8px;">Log In</button>
    </form>
    <p style="text-align:center;font-size:.8rem;color:var(--text-muted);margin-top:14px;">
        Don't have an account? <a href="{{ url_for('register') }}" style="color:var(--primary);">Register</a>
    </p>
</div>
{% endblock %}
"""

REGISTER_TEMPLATE = """
{% extends "base.html" %}
{% block content %}
<div class="card" style="max-width:380px;margin:40px auto;padding:24px;">
    <h2 style="font-size:1.2rem;margin-bottom:16px;text-align:center;">Create an Account</h2>
    <form method="POST">
        <input type="hidden" name="csrf" value="{{ csrf_token }}">
        <div class="form-group">
            <label>Username</label>
            <input type="text" name="username" required autocomplete="username">
        </div>
        <div class="form-group">
            <label>Password</label>
            <input type="password" name="password" required autocomplete="new-password">
        </div>
        <button type="submit" class="btn" style="width:100%;margin-top:8px;">Register</button>
    </form>
    <p style="text-align:center;font-size:.8rem;color:var(--text-muted);margin-top:14px;">
        Already have an account? <a href="{{ url_for('login') }}" style="color:var(--primary);">Log In</a>
    </p>
</div>
{% endblock %}
"""

# --- DYNAMIC TEMPLATE LOADER ---

class DynamicTemplateLoader(DictLoader):
    def get_source(self, environment, template):
        templates = {
            "base.html": BASE_TEMPLATE,
            "index.html": INDEX_TEMPLATE,
            "post_card.html": POST_CARD_TEMPLATE,
            "post_detail.html": POST_DETAIL_TEMPLATE,
            "explore.html": EXPLORE_TEMPLATE,
            "trending.html": TRENDING_TEMPLATE,
            "bookmarks.html": BOOKMARKS_TEMPLATE,
            "notifications.html": NOTIFICATIONS_TEMPLATE,
            "profile.html": PROFILE_TEMPLATE,
            "messages.html": MESSAGES_TEMPLATE,
            "messages_chat.html": MESSAGES_CHAT_TEMPLATE,
            "login.html": LOGIN_TEMPLATE,
            "register.html": REGISTER_TEMPLATE
        }
        if template in templates:
            return templates[template], template, lambda: True
        return super().get_source(environment, template)

app.jinja_loader = DynamicTemplateLoader({})
app.jinja_env.filters['ago'] = timeago
app.jinja_env.filters['avatar'] = avatar_color
app.jinja_env.filters['pulse_format'] = format_pulse

@app.before_request
def csrf_protect():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    g.csrf_token = session["csrf_token"]
    if request.method == "POST":
        token = request.form.get("csrf") or request.headers.get("X-CSRF-Token")
        if not token or token != session.get("csrf_token"):
            abort(400, "CSRF token missing or incorrect.")

@app.context_processor
def inject_csrf():
    return dict(csrf_token=g.get("csrf_token", ""))

# --- HELPER ROUTINES ---

def get_trending_tags(limit=6):
    db = get_db()
    posts = db.execute("SELECT content FROM posts WHERE created_at >= datetime('now', '-7 days')").fetchall()
    counts = defaultdict(int)
    for p in posts:
        tags = re.findall(r"(#\w+)", p["content"] or "")
        for t in tags:
            counts[t.lower()] += 1
    sorted_tags = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return sorted_tags[:limit]

def ensure_daily_prompt():
    with app.app_context():
        db = get_db()
        sys_user = db.execute("SELECT id FROM users WHERE username = ?", (SYSTEM_USERNAME,)).fetchone()
        if not sys_user:
            return
        today_str = date.today().isoformat()
        prompt_text = DAILY_PROMPTS[date.today().toordinal() % len(DAILY_PROMPTS)]
        existing = db.execute("SELECT id FROM posts WHERE user_id = ? AND is_prompt = 1 AND date(created_at) = ?", (sys_user["id"], today_str)).fetchone()
        if not existing:
            db.execute("INSERT INTO posts (user_id, content, is_prompt) VALUES (?, ?, 1)", (sys_user["id"], prompt_text))
            db.commit()

def hydrate_post(post_row, current_user_id=None):
    if not post_row:
        return None
    d = dict(post_row)
    db = get_db()
    
    author = db.execute("SELECT username, avatar FROM users WHERE id = ?", (d["user_id"],)).fetchone()
    d["username"] = author["username"] if author else "unknown"
    d["avatar"] = author["avatar"] if author else None
    
    resonates = db.execute("SELECT COUNT(*) FROM resonates WHERE post_id = ?", (d["id"],)).fetchone()[0]
    replies = db.execute("SELECT COUNT(*) FROM comments WHERE post_id = ?", (d["id"],)).fetchone()[0]
    breaks = db.execute("SELECT COUNT(*) FROM breaks WHERE post_id = ?", (d["id"],)).fetchone()[0]
    
    d["resonates"] = resonates
    d["breaks"] = breaks
    d["comments_count"] = replies
    d["score"] = pulse_score(resonates, replies, breaks)
    d["formatted_content"] = format_pulse(d["content"])
    
    poll = db.execute("SELECT id FROM polls WHERE post_id = ?", (d["id"],)).fetchone()
    if poll:
        opts = db.execute("SELECT id, text FROM poll_options WHERE poll_id = ?", (poll["id"],)).fetchall()
        opt_list = []
        for opt in opts:
            v_count = db.execute("SELECT COUNT(*) FROM poll_votes WHERE option_id = ?", (opt["id"],)).fetchone()[0]
            opt_list.append({"id": opt["id"], "text": opt["text"], "votes": v_count})
        d["poll"] = {"id": poll["id"], "options": opt_list}
    else:
        d["poll"] = None
        
    return d

# --- SOCKETIO EVENTS FOR REAL-TIME DMs ---

@socketio.on('join_chat')
def handle_join_chat(data):
    user_id = data.get('user_id')
    if user_id:
        join_room(f"user_{user_id}")

@socketio.on('send_direct_message')
def handle_send_direct_message(data):
    sender_id = session.get('user_id')
    recipient_id = data.get('recipient_id')
    content = data.get('content', '').strip()
    
    if not sender_id or not recipient_id or not content:
        return
        
    db = get_db()
    db.execute("INSERT INTO messages (sender_id, recipient_id, content) VALUES (?, ?, ?)", (sender_id, recipient_id, content))
    db.execute("INSERT INTO notifications (user_id, sender_id, type) VALUES (?, ?, 'message')", (recipient_id, sender_id))
    db.commit()
    
    msg_data = {
        "sender_id": sender_id,
        "recipient_id": recipient_id,
        "content": content
    }
    emit('new_direct_message', msg_data, room=f"user_{sender_id}")
    emit('new_direct_message', msg_data, room=f"user_{recipient_id}")
    emit('new_notification', {"type": "message"}, room=f"user_{recipient_id}")

# --- ROUTES ---

@app.route("/")
def index():
    db = get_db()
    feed = request.args.get("feed", "global")
    user_id = session.get("user_id")
    
    if feed == "following" and user_id:
        followed = db.execute("SELECT followed_id FROM follows WHERE follower_id = ?", (user_id,)).fetchall()
        f_ids = [row[0] for row in followed] + [user_id]
        placeholders = ",".join(["?"] * len(f_ids))
        query = f"SELECT * FROM posts WHERE user_id IN ({placeholders}) ORDER BY created_at DESC LIMIT 15"
        posts_raw = db.execute(query, f_ids).fetchall()
        page_title = "Following Timeline"
    elif feed == "clash":
        query = "SELECT * FROM posts ORDER BY (SELECT COUNT(*) FROM breaks WHERE post_id = posts.id) DESC, created_at DESC LIMIT 15"
        posts_raw = db.execute(query).fetchall()
        page_title = "🔥 Clash Feed"
    else:
        feed = "global"
        query = "SELECT * FROM posts ORDER BY created_at DESC LIMIT 15"
        posts_raw = db.execute(query).fetchall()
        page_title = "Unfiltered Timeline"
        
    posts = [hydrate_post(p, user_id) for p in posts_raw]
    stories_raw = db.execute("SELECT s.*, u.username FROM stories s JOIN users u ON s.user_id = u.id WHERE s.created_at >= datetime('now', '-24 hours') ORDER BY s.created_at DESC").fetchall()
    
    return render_template("index.html", posts=posts, stories=stories_raw, feed_type=feed, page_title=page_title)

@app.route("/feed/json")
def feed_json():
    db = get_db()
    feed = request.args.get("feed", "global")
    page = int(request.args.get("page", 1))
    limit = 15
    offset = (page - 1) * limit
    user_id = session.get("user_id")
    
    if feed == "following" and user_id:
        followed = db.execute("SELECT followed_id FROM follows WHERE follower_id = ?", (user_id,)).fetchall()
        f_ids = [row[0] for row in followed] + [user_id]
        placeholders = ",".join(["?"] * len(f_ids))
        query = f"SELECT * FROM posts WHERE user_id IN ({placeholders}) ORDER BY created_at DESC LIMIT ? OFFSET ?"
        posts_raw = db.execute(query, f_ids + [limit, offset]).fetchall()
    elif feed == "clash":
        query = "SELECT * FROM posts ORDER BY (SELECT COUNT(*) FROM breaks WHERE post_id = posts.id) DESC, created_at DESC LIMIT ? OFFSET ?"
        posts_raw = db.execute(query, (limit, offset)).fetchall()
    else:
        query = "SELECT * FROM posts ORDER BY created_at DESC LIMIT ? OFFSET ?"
        posts_raw = db.execute(query, (limit, offset)).fetchall()
        
    posts = [hydrate_post(p, user_id) for p in posts_raw]
    rendered_posts = [{"html_card": render_template("post_card.html", post=post)} for post in posts]
    return jsonify({"posts": rendered_posts})

@app.route("/post/new", methods=["POST"])
@login_required
def create_post():
    if not rate_ok("create_post:" + str(session["user_id"]), n=10, window=60):
        flash("Posting too fast. Slow down.")
        return redirect(url_for("index"))
        
    content = request.form.get("content", "").strip()
    is_stance = 1 if request.form.get("is_stance") else 0
    
    if not content and not request.files.getlist("files"):
        flash("Post cannot be empty.")
        return redirect(url_for("index"))
        
    files = request.files.getlist("files")
    saved_imgs = []
    for f in files:
        if f and allowed_file(f.filename):
            fname = f"{uuid.uuid4().hex}.webp"
            path = os.path.join(app.config["UPLOAD_FOLDER"], fname)
            if optimize_image(f, path):
                saved_imgs.append(fname)
    img_str = ",".join(saved_imgs) if saved_imgs else None
    
    og_title, og_description, og_image, og_url = None, None, None, None
    for word in content.split():
        if word.startswith("http://") or word.startswith("https://"):
            og_url = word
            break
            
    db = get_db()
    cur = db.execute(
        "INSERT INTO posts (user_id, content, image_filenames, og_url, og_title, og_description, og_image, is_stance) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (session["user_id"], content, img_str, og_url, og_title, og_description, og_image, is_stance)
    )
    post_id = cur.lastrowid
    
    opt1 = request.form.get("poll_opt1", "").strip()
    opt2 = request.form.get("poll_opt2", "").strip()
    if opt1 and opt2:
        p_cur = db.execute("INSERT INTO polls (post_id) VALUES (?)", (post_id,))
        poll_id = p_cur.lastrowid
        db.execute("INSERT INTO poll_options (poll_id, text) VALUES (?, ?)", (poll_id, opt1))
        db.execute("INSERT INTO poll_options (poll_id, text) VALUES (?, ?)", (poll_id, opt2))
        
    db.commit()
    return redirect(url_for("index"))

@app.route("/post/<int:post_id>")
def post_detail(post_id):
    db = get_db()
    post_row = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not post_row:
        abort(404)
    post = hydrate_post(post_row, session.get("user_id"))
    
    comments_raw = db.execute("SELECT * FROM comments WHERE post_id = ? ORDER BY created_at ASC", (post_id,)).fetchall()
    comments = []
    for c in comments_raw:
        cd = dict(c)
        author = db.execute("SELECT username, avatar FROM users WHERE id = ?", (cd["user_id"],)).fetchone()
        cd["username"] = author["username"] if author else "unknown"
        cd["avatar"] = author["avatar"] if author else None
        cd["formatted_content"] = format_pulse(cd["content"])
        comments.append(cd)
        
    return render_template("post_detail.html", post=post, comments=comments)

@app.route("/post/<int:post_id>/comment", methods=["POST"])
@login_required
def add_comment(post_id):
    content = request.form.get("content", "").strip()
    if not content:
        return redirect(url_for("post_detail", post_id=post_id))
    db = get_db()
    db.execute("INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)", (post_id, session["user_id"], content))
    db.commit()
    
    post = db.execute("SELECT user_id FROM posts WHERE id = ?", (post_id,)).fetchone()
    if post and post["user_id"] != session["user_id"]:
        db.execute("INSERT INTO notifications (user_id, sender_id, type, post_id) VALUES (?, ?, 'reply', ?)", (post["user_id"], session["user_id"], post_id))
        db.commit()
        socketio.emit(f"notification_{post['user_id']}", {"type": "reply"})
        
    return redirect(url_for("post_detail", post_id=post_id))

@app.route("/post/<int:post_id>/resonate", methods=["POST"])
@login_required
def resonate_post(post_id):
    db = get_db()
    uid = session["user_id"]
    existing = db.execute("SELECT * FROM resonates WHERE user_id = ? AND post_id = ?", (uid, post_id)).fetchone()
    if existing:
        db.execute("DELETE FROM resonates WHERE user_id = ? AND post_id = ?", (uid, post_id))
    else:
        db.execute("INSERT INTO resonates (user_id, post_id) VALUES (?, ?)", (uid, post_id))
        post = db.execute("SELECT user_id FROM posts WHERE id = ?", (post_id,)).fetchone()
        if post and post["user_id"] != uid:
            db.execute("INSERT INTO notifications (user_id, sender_id, type, post_id) VALUES (?, ?, 'resonate', ?)", (post["user_id"], uid, post_id))
            socketio.emit(f"notification_{post['user_id']}", {"type": "resonate"})
    db.commit()
    return redirect(request.referrer or url_for("index"))

@app.route("/post/<int:post_id>/delete", methods=["POST"])
@login_required
def delete_post(post_id):
    db = get_db()
    post = db.execute("SELECT * FROM posts WHERE id = ? AND user_id = ?", (post_id, session["user_id"])).fetchone()
    if post:
        db.execute("DELETE FROM comments WHERE post_id = ?", (post_id,))
        db.execute("DELETE FROM resonates WHERE post_id = ?", (post_id,))
        db.execute("DELETE FROM breaks WHERE post_id = ?", (post_id,))
        db.execute("DELETE FROM bookmarks WHERE post_id = ?", (post_id,))
        poll = db.execute("SELECT id FROM polls WHERE post_id = ?", (post_id,)).fetchone()
        if poll:
            db.execute("DELETE FROM poll_options WHERE poll_id = ?", (poll["id"],))
            db.execute("DELETE FROM poll_votes WHERE poll_id = ?", (poll["id"],))
            db.execute("DELETE FROM polls WHERE id = ?", (poll["id"],))
        db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        db.commit()
    return redirect(url_for("index"))

@app.route("/poll/<int:option_id>/vote", methods=["POST"])
@login_required
def vote_poll(option_id):
    db = get_db()
    opt = db.execute("SELECT poll_id FROM poll_options WHERE id = ?", (option_id,)).fetchone()
    if not opt:
        abort(404)
    poll_id = opt["poll_id"]
    uid = session["user_id"]
    
    existing = db.execute("SELECT * FROM poll_votes WHERE user_id = ? AND poll_id = ?", (uid, poll_id)).fetchone()
    if not existing:
        db.execute("INSERT INTO poll_votes (user_id, poll_id, option_id) VALUES (?, ?, ?)", (uid, poll_id, option_id))
        db.commit()
    return redirect(request.referrer or url_for("index"))

@app.route("/story/new", methods=["POST"])
@login_required
def create_story():
    f = request.files.get("file")
    if f and allowed_file(f.filename):
        fname = f"{uuid.uuid4().hex}.webp"
        path = os.path.join(app.config["UPLOAD_FOLDER"], fname)
        if optimize_image(f, path):
            db = get_db()
            db.execute("INSERT INTO stories (user_id, image_filename) VALUES (?, ?)", (session["user_id"], fname))
            db.commit()
    return redirect(url_for("index"))

@app.route("/profile/upload-avatar", methods=["POST"])
@login_required
def upload_avatar():
    f = request.files.get("file")
    if f and allowed_file(f.filename):
        fname = f"avatar_{uuid.uuid4().hex}.webp"
        path = os.path.join(app.config["UPLOAD_FOLDER"], fname)
        if optimize_image(f, path):
            db = get_db()
            db.execute("UPDATE users SET avatar = ? WHERE id = ?", (fname, session["user_id"]))
            db.commit()
    return redirect(url_for('profile', username=session['username']))

@app.route("/explore")
def explore():
    db = get_db()
    q = request.args.get("q", "").strip()
    posts = []
    if q:
        posts_raw = db.execute("SELECT * FROM posts WHERE content LIKE ? ORDER BY created_at DESC LIMIT 20", (f"%{q}%",)).fetchall()
        posts = [hydrate_post(p, session.get("user_id")) for p in posts_raw]
    trending_tags = get_trending_tags()
    return render_template("explore.html", q=q, posts=posts, trending_tags=trending_tags)

@app.route("/tag/<tag>")
def tag_view(tag):
    db = get_db()
    tag_str = f"#{tag}"
    posts_raw = db.execute("SELECT * FROM posts WHERE content LIKE ? ORDER BY created_at DESC LIMIT 20", (f"%{tag_str}%",)).fetchall()
    posts = [hydrate_post(p, session.get("user_id")) for p in posts_raw]
    trending_tags = get_trending_tags()
    return render_template("explore.html", q=tag_str, posts=posts, trending_tags=trending_tags)

@app.route("/trending")
def trending_page():
    db = get_db()
    posts_raw = db.execute("SELECT * FROM posts ORDER BY (SELECT COUNT(*) FROM resonates WHERE post_id = posts.id) DESC, created_at DESC LIMIT 20").fetchall()
    posts = [hydrate_post(p, session.get("user_id")) for p in posts_raw]
    return render_template("trending.html", posts=posts)

@app.route("/bookmarks")
@login_required
def bookmarks_page():
    db = get_db()
    posts_raw = db.execute("SELECT p.* FROM posts p JOIN bookmarks b ON p.id = b.post_id WHERE b.user_id = ? ORDER BY b.post_id DESC", (session["user_id"],)).fetchall()
    posts = [hydrate_post(p, session["user_id"]) for p in posts_raw]
    return render_template("bookmarks.html", posts=posts)

@app.route("/post/<int:post_id>/bookmark", methods=["POST"])
@login_required
def bookmark_post(post_id):
    db = get_db()
    uid = session["user_id"]
    existing = db.execute("SELECT * FROM bookmarks WHERE user_id = ? AND post_id = ?", (uid, post_id)).fetchone()
    if existing:
        db.execute("DELETE FROM bookmarks WHERE user_id = ? AND post_id = ?", (uid, post_id))
    else:
        db.execute("INSERT INTO bookmarks (user_id, post_id) VALUES (?, ?)", (uid, post_id))
    db.commit()
    return redirect(request.referrer or url_for("index"))

@app.route("/notifications")
@login_required
def notifications():
    db = get_db()
    notifs = db.execute("SELECT n.*, u.username FROM notifications n JOIN users u ON n.sender_id = u.id WHERE n.user_id = ? ORDER BY n.created_at DESC LIMIT 30", (session["user_id"],)).fetchall()
    db.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (session["user_id"],))
    db.commit()
    return render_template("notifications.html", notifs=notifs)

@app.route("/notifications/json")
@login_required
def notifications_json():
    db = get_db()
    notifs = db.execute("SELECT id FROM notifications WHERE user_id = ? AND is_read = 0", (session["user_id"],)).fetchall()
    return jsonify([dict(n) for n in notifs])

@app.route("/profile/<username>")
def profile(username):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not user:
        abort(404)
    posts_raw = db.execute("SELECT * FROM posts WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)).fetchall()
    posts = [hydrate_post(p, session.get("user_id")) for p in posts_raw]
    
    is_following = False
    if session.get("user_id"):
        f = db.execute("SELECT * FROM follows WHERE follower_id = ? AND followed_id = ?", (session["user_id"], user["id"])).fetchone()
        if f:
            is_following = True
            
    return render_template("profile.html", profile_user=user, posts=posts, is_following=is_following)

@app.route("/user/<int:user_id>/follow", methods=["POST"])
@login_required
def follow_user(user_id):
    if user_id == session["user_id"]:
        return redirect(request.referrer or url_for("index"))
    db = get_db()
    existing = db.execute("SELECT * FROM follows WHERE follower_id = ? AND followed_id = ?", (session["user_id"], user_id)).fetchone()
    if existing:
        db.execute("DELETE FROM follows WHERE follower_id = ? AND followed_id = ?", (session["user_id"], user_id))
    else:
        db.execute("INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)", (session["user_id"], user_id))
        db.execute("INSERT INTO notifications (user_id, sender_id, type) VALUES (?, ?, 'follow')", (user_id, session["user_id"]))
        socketio.emit(f"notification_{user_id}", {"type": "follow"})
    db.commit()
    return redirect(request.referrer or url_for("index"))

@app.route("/messages")
@login_required
def messages():
    db = get_db()
    uid = session["user_id"]
    convs_raw = db.execute("""
        SELECT DISTINCT CASE WHEN sender_id = ? THEN recipient_id ELSE sender_id END as other_id
        FROM messages WHERE sender_id = ? OR recipient_id = ?
    """, (uid, uid, uid)).fetchall()
    
    conversations = []
    for row in convs_raw:
        other_id = row["other_id"]
        other_user = db.execute("SELECT username, avatar FROM users WHERE id = ?", (other_id,)).fetchone()
        last_msg = db.execute("""
            SELECT content, created_at FROM messages 
            WHERE (sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?) 
            ORDER BY created_at DESC LIMIT 1
        """, (uid, other_id, other_id, uid)).fetchone()
        
        if other_user and last_msg:
            conversations.append({
                "username": other_user["username"],
                "avatar": other_user["avatar"],
                "last_message": last_msg["content"],
                "created_at": last_msg["created_at"]
            })
            
    return render_template("messages.html", conversations=conversations)

@app.route("/messages/<username>")
@login_required
def messages_chat(username):
    db = get_db()
    chat_user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not chat_user or chat_user["id"] == session["user_id"]:
        abort(404)
        
    uid = session["user_id"]
    other_id = chat_user["id"]
    msgs_raw = db.execute("""
        SELECT * FROM messages 
        WHERE (sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?) 
        ORDER BY created_at ASC
    """, (uid, other_id, other_id, uid)).fetchall()
    
    return render_template("messages_chat.html", chat_user=chat_user, messages=msgs_raw)

@app.route("/auth/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        if not username or not password:
            flash("Fill in all fields.")
            return redirect(url_for("register"))
        db = get_db()
        try:
            db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", (username, generate_password_hash(password)))
            db.commit()
            flash("Account created. Log in.")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Username already taken.")
            return redirect(url_for("register"))
    return render_template("register.html")

@app.route("/auth/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("index"))
        flash("Invalid username or password.")
    return render_template("login.html")

@app.route("/auth/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/explore_page")
def explore_page():
    return redirect(url_for('explore'))

if __name__ == "__main__":
    init_db()
    ensure_daily_prompt()
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
