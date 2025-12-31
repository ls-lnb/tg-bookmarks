from fastlite import database
from datetime import datetime

db = database('data/bookmarks.sqlite')

# Define tables
topics = db.t.topics
if 'id' not in topics.c:
    topics.create(id=int, title=str, last_synced=str, pk='id')

# Store sync state (pts) for the channel
channel_state = db.t.channel_state
if 'id' not in channel_state.c:
    channel_state.create(id=int, pts=int, date=str, pk='id')

bookmarks = db.t.bookmarks
if 'id' not in bookmarks.c:
    bookmarks.create(
        id=int, 
        topic_id=int, 
        message_id=int,
        text=str, 
        media_path=str, 
        content_type=str, 
        date=str, 
        pk='id'
    )

def get_topics():
    return topics()

def get_bookmarks(topic_id, sort_order="DESC"):
    order = "date DESC" if sort_order == "DESC" else "date ASC"
    return bookmarks(where="topic_id = ?", where_args=[topic_id], order_by=order)

def search_bookmarks(q, sort_order="DESC"):
    order = "date DESC" if sort_order == "DESC" else "date ASC"
    return bookmarks(where="text LIKE ?", where_args=[f"%{q}%"], order_by=order)

def upsert_topic(topic_id, title):
    topics.upsert({'id': topic_id, 'title': title, 'last_synced': datetime.now().isoformat()})

def upsert_bookmark(message_id, topic_id, text, media_path, content_type, date):
    # Use a compound unique check or just use message_id if it's unique enough for this app
    # In Telegram, message_id is unique per chat.
    bookmarks.upsert({
        'id': message_id, # Use message_id as primary key
        'topic_id': topic_id,
        'message_id': message_id,
        'text': text,
        'media_path': media_path,
        'content_type': content_type,
        'date': date
    })

def upsert_bookmarks_batch(bookmarks_list):
    """Batch upsert a list of bookmarks dictionaries."""
    if not bookmarks_list:
        return
    # upsert_all requires a list of dictionaries and the pk
    bookmarks.upsert_all(bookmarks_list, pk='id')

def get_max_message_id(topic_id):
    """Get the maximum message_id for a given topic."""
    # fastlite/sqlite-utils doesn't have a direct max() helper, execute SQL
    result = list(db.execute("SELECT MAX(message_id) FROM bookmarks WHERE topic_id = ?", [topic_id]))
    if result and result[0][0] is not None:
        return result[0][0]
    return 0

def get_recent_message_ids(topic_id, limit=50):
    """Get the last 'limit' message IDs for a topic, ordered by ID desc."""
    rows = db.execute(f"SELECT id FROM bookmarks WHERE topic_id = ? ORDER BY id DESC LIMIT ?", [topic_id, limit])
    return [r[0] for r in rows]

def delete_bookmarks(topic_id, message_ids):
    """Delete specific bookmarks by ID."""
    if not message_ids:
        return
    placeholders = ",".join("?" for _ in message_ids)
    db.execute(f"DELETE FROM bookmarks WHERE topic_id = ? AND id IN ({placeholders})", [topic_id] + list(message_ids))

def delete_bookmarks_by_ids(message_ids):
    """Delete bookmarks across all topics by Message ID (globally unique in channel)."""
    if not message_ids:
        return
    placeholders = ",".join("?" for _ in message_ids)
    # Note: message_id is the primary key 'id' in our table
    db.execute(f"DELETE FROM bookmarks WHERE id IN ({placeholders})", list(message_ids))

def get_channel_pts():
    """Get the last known pts for the channel."""
    try:
        row = channel_state.get(1)
        return row['pts'] if row else None
    except:
        return None

def set_channel_pts(pts):
    """Update the channel pts."""
    channel_state.upsert({'id': 1, 'pts': pts, 'date': datetime.now().isoformat()})

def prune_bookmarks(topic_id, active_ids):
    """Delete bookmarks in a topic that are not in the active_ids list."""
    if not active_ids:
        # If no messages found, delete all for this topic
        db.execute("DELETE FROM bookmarks WHERE topic_id = ?", [topic_id])
        return
    
    placeholders = ",".join("?" for _ in active_ids)
    db.execute(f"DELETE FROM bookmarks WHERE topic_id = ? AND id NOT IN ({placeholders})", [topic_id] + list(active_ids))
