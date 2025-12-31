import os
import asyncio
from telethon import TelegramClient, events, functions, types
from telethon.tl.types import MessageService, MessageActionTopicCreate
from dotenv import load_dotenv
from db import upsert_topic, upsert_bookmark, prune_bookmarks
from datetime import datetime

load_dotenv()

API_ID = os.getenv('TELEGRAM_API_ID')
API_HASH = os.getenv('TELEGRAM_API_HASH')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
# Store session in data/ directory for Docker volume persistence
SESSION_NAME = os.getenv('SESSION_NAME', 'data/bookmark_viewer')

client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)

async def get_client():
    if not client.is_connected():
        await client.connect()
    return client

async def download_media_async(message_id):
    async with client:
        try:
            chat = await client.get_entity(int(CHAT_ID))
            message = await client.get_messages(chat, ids=message_id)
            if message and (message.photo or message.video):
                ext = '.jpg' if message.photo else '.mp4'
                path = f"media/{message_id}{ext}"
                await client.download_media(message, path)
                return path
        except Exception as e:
            print(f"Download error: {e}")
    return None

async def sync_bookmarks_with_client(tg_client, chat, topic_id, download=False, photos_only=False):
    """Sync bookmarks for a topic using the provided client."""
    found_ids = []  # Track all messages we find in Telegram
    
    async for message in tg_client.iter_messages(chat, reply_to=topic_id, limit=500):
        if isinstance(message, MessageService):
            continue
        
        found_ids.append(message.id)
        text = message.text or ""
        media_path = None
        content_type = "text"
        
        if message.photo:
            content_type = "photo"
            path = f"media/{message.id}.jpg"
            if download and not os.path.exists(path):
                print(f"  Downloading photo {message.id}...")
                try:
                    # Telethon returns the actual path or None
                    result = await tg_client.download_media(message, path)
                    if result and os.path.exists(result) and os.path.getsize(result) > 0:
                        media_path = result
                    elif os.path.exists(path) and os.path.getsize(path) == 0:
                        os.remove(path)  # Remove empty files
                        print(f"    Failed: empty file removed")
                except Exception as e:
                    print(f"    Download error: {e}")
                    if os.path.exists(path) and os.path.getsize(path) == 0:
                        os.remove(path)
            elif os.path.exists(path) and os.path.getsize(path) > 0:
                media_path = path
        elif message.video:
            content_type = "video"
            # Download thumbnail for videos instead of full video
            thumb_path = f"media/{message.id}_thumb.jpg"
            video_path = f"media/{message.id}.mp4"
            
            # Check if we already have either thumbnail or full video
            if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
                media_path = thumb_path
            elif os.path.exists(video_path) and os.path.getsize(video_path) > 0:
                media_path = video_path
            elif download:
                # Download thumbnail (thumb=0 means first/largest thumbnail)
                print(f"  Downloading video thumbnail {message.id}...")
                try:
                    result = await tg_client.download_media(message, thumb_path, thumb=0)
                    if result and os.path.exists(result) and os.path.getsize(result) > 0:
                        media_path = result
                    elif os.path.exists(thumb_path) and os.path.getsize(thumb_path) == 0:
                        os.remove(thumb_path)
                        print(f"    Failed: empty thumbnail removed")
                except Exception as e:
                    print(f"    Thumbnail error: {e}")
                    if os.path.exists(thumb_path) and os.path.getsize(thumb_path) == 0:
                        os.remove(thumb_path)
        
        upsert_bookmark(
            message_id=message.id,
            topic_id=topic_id,
            text=text,
            media_path=media_path,
            content_type=content_type,
            date=message.date.isoformat()
        )
    
    # Prune any bookmarks that no longer exist in Telegram
    prune_bookmarks(topic_id, found_ids)
    print(f"  Pruned deleted messages (kept {len(found_ids)} bookmarks)")

# Store settings for run_sync to use
_sync_settings = {'download': False, 'photos_only': False}

async def run_sync(download=True, photos_only=True):
    """Sync topics and bookmarks from Telegram. 
    By default downloads photos only (for web UI - faster sync)."""
    print(f"[SYNC] Starting sync with download={download}, photos_only={photos_only}")
    
    # Create a fresh client for each sync to avoid event loop issues
    sync_client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
    
    try:
        print("[SYNC] Connecting to Telegram...")
        await sync_client.connect()
        
        # Ensure we're authorized
        if not await sync_client.is_user_authorized():
            print("[SYNC] ERROR: Not authorized. Please run telegram_client.py manually first.")
            return False
        
        print("[SYNC] Connected and authorized")
        chat = await sync_client.get_entity(int(CHAT_ID))
        print(f"[SYNC] Got chat entity: {chat.title if hasattr(chat, 'title') else chat}")
        
        result = await sync_client(functions.messages.GetForumTopicsRequest(
            peer=chat,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=100
        ))
        
        print(f"[SYNC] Found {len(result.topics)} topics")
        
        for topic in result.topics:
            upsert_topic(topic.id, topic.title)
            print(f"[SYNC] Syncing topic: {topic.title}")
            await sync_bookmarks_with_client(
                sync_client, chat, topic.id, 
                download=download, 
                photos_only=photos_only
            )
        
        print("[SYNC] Sync completed successfully")
        return True
    except Exception as e:
        print(f"[SYNC] Error during sync: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        print("[SYNC] Disconnecting...")
        await sync_client.disconnect()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Sync Telegram bookmarks')
    parser.add_argument('--download', '-d', action='store_true', 
                        help='Download media files during sync')
    parser.add_argument('--photos-only', '-p', action='store_true',
                        help='Only download photos, skip videos')
    args = parser.parse_args()
    
    _sync_settings['download'] = args.download
    _sync_settings['photos_only'] = args.photos_only
    
    if not os.path.exists('media'): os.makedirs('media')
    if not os.path.exists('data'): os.makedirs('data')
    
    print(f"Sync options: download={args.download}, photos_only={args.photos_only}")
    
    # Use client.start() for interactive login, then sync
    async def main():
        await client.start()  # This handles login interactively
        print("Logged in successfully!")
        await run_sync(download=args.download, photos_only=args.photos_only)
    
    asyncio.run(main())
