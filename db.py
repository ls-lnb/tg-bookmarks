from fastlite import database
from datetime import datetime

db = database('data/bookmarks.sqlite')

# Define tables
topics = db.t.topics
if 'id' not in topics.c:
    topics.create(id=int, title=str, last_synced=str, pk='id')

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

def prune_bookmarks(topic_id, active_ids):
    """Delete bookmarks in a topic that are not in the active_ids list."""
    if not active_ids:
        # If no messages found, delete all for this topic
        db.execute("DELETE FROM bookmarks WHERE topic_id = ?", [topic_id])
        return
    
    placeholders = ",".join("?" for _ in active_ids)
    db.execute(f"DELETE FROM bookmarks WHERE topic_id = ? AND id NOT IN ({placeholders})", [topic_id] + list(active_ids))
