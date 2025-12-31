import os
import asyncio
from telethon import TelegramClient, events, functions, types
from telethon.tl.types import MessageService, MessageActionTopicCreate
from dotenv import load_dotenv
from db import upsert_topic, upsert_bookmark, prune_bookmarks, get_max_message_id, upsert_bookmarks_batch, get_recent_message_ids, delete_bookmarks, get_channel_pts, set_channel_pts, delete_bookmarks_by_ids
from datetime import datetime
from telethon.tl.types import UpdateDeleteChannelMessages, UpdateEditMessage, UpdateNewChannelMessage
from telethon.tl.types.updates import ChannelDifference, ChannelDifferenceEmpty, ChannelDifferenceTooLong

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

async def process_message_object(message, topic_id, download=False):
    """Helper to process a single message object into bookmark dict."""
    if isinstance(message, MessageService):
        return None
    
    text = message.text or ""
    media_path = None
    content_type = "text"

    # If using difference, topic_id might not be known directly if not passed,
    # but here we pass it. If global diff, we need to extract topic from message.
    # Forum topic ID is usually in `reply_to.reply_to_msg_id` if it's a topic message?
    # Or `reply_to.forum_topic` is True?
    # In forum, `reply_to_msg_id` (top_msg_id) IS the topic ID.

    if message.reply_to and message.reply_to.reply_to_msg_id:
        msg_topic_id = message.reply_to.reply_to_msg_id
    else:
        # If it is the topic creation message itself, ID is the topic ID
        # But we skip MessageService (TopicCreate is Service).
        # Sometimes messages in General don't have reply_to?
        # For now, if topic_id is passed explicitly, use it.
        msg_topic_id = topic_id

    # If we are processing a specific topic sync, verify this message belongs to it.
    # If topic_id is None, we infer from message.
    if topic_id and msg_topic_id != topic_id:
        # This message belongs to another topic?
        # In iter_messages(reply_to=topic_id), we are guaranteed it is correct.
        pass

    if message.photo:
        content_type = "photo"
        path = f"media/{message.id}.jpg"
        if download and not os.path.exists(path):
            # We need client to download. But wait, client is not passed here?
            # We need to refactor to pass client or do download outside.
            # Let's return the need to download?
            # Or just assume client is available via closure? No.
            # Refactor: process_message takes client.
            pass
        elif os.path.exists(path) and os.path.getsize(path) > 0:
            media_path = path
    elif message.video:
        content_type = "video"
        thumb_path = f"media/{message.id}_thumb.jpg"
        video_path = f"media/{message.id}.mp4"
        
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            media_path = thumb_path
        elif os.path.exists(video_path) and os.path.getsize(video_path) > 0:
            media_path = video_path

    return {
        'id': message.id,
        'topic_id': msg_topic_id,
        'message_id': message.id,
        'text': text,
        'media_path': media_path, # Filled later if needed
        'content_type': content_type,
        'date': message.date.isoformat(),
        '_msg_obj': message # Keep ref for download
    }

async def download_bookmark_media(client, bookmark, download=False):
    """Handle media download for a bookmark dict."""
    message = bookmark.pop('_msg_obj', None)
    if not message or not download:
        return bookmark

    if bookmark['content_type'] == "photo":
        path = f"media/{bookmark['id']}.jpg"
        if not os.path.exists(path):
             try:
                print(f"  Downloading photo {bookmark['id']}...")
                result = await client.download_media(message, path)
                if result and os.path.exists(result):
                    bookmark['media_path'] = result
             except Exception as e:
                print(f"    Download error: {e}")
    elif bookmark['content_type'] == "video":
        thumb_path = f"media/{bookmark['id']}_thumb.jpg"
        if not os.path.exists(thumb_path):
            try:
                print(f"  Downloading video thumbnail {bookmark['id']}...")
                result = await client.download_media(message, thumb_path, thumb=0)
                if result and os.path.exists(result):
                    bookmark['media_path'] = result
            except Exception as e:
                 print(f"    Thumbnail error: {e}")

    return bookmark

async def sync_bookmarks_with_client(tg_client, chat, topic_id, download=False, photos_only=False, full_sync=False):
    """
    Classic sync: Iterates messages in a topic.
    Used for Full Sync (initial) or fallback.
    """
    found_ids = []
    bookmarks_buffer = []

    print(f"  Full sync for topic {topic_id}...")

    async for message in tg_client.iter_messages(chat, reply_to=topic_id, limit=None if full_sync else 500):
        bm = await process_message_object(message, topic_id)
        if not bm: continue
        
        bm = await download_bookmark_media(tg_client, bm, download=download)
        found_ids.append(message.id)
        bookmarks_buffer.append(bm)
    
    if bookmarks_buffer:
        upsert_bookmarks_batch(bookmarks_buffer)
        print(f"  Upserted {len(bookmarks_buffer)} bookmarks")

    # Prune always in this mode as it's explicit sync
    prune_bookmarks(topic_id, found_ids)
    print(f"  Pruned deleted messages (kept {len(found_ids)} bookmarks)")

async def sync_from_difference(tg_client, chat, download=False):
    """
    Sync using GetChannelDifference. Efficiently gets ALL updates (new, edits, deletes)
    since the last known state (pts).
    """
    pts = get_channel_pts()
    if not pts:
        print("[DIFF] No local state (pts), requiring full sync.")
        return False # Signal to run full sync

    print(f"[DIFF] Starting difference sync from pts={pts}...")

    while True:
        try:
            result = await tg_client(functions.updates.GetChannelDifferenceRequest(
                channel=chat,
                filter=types.ChannelMessagesFilterEmpty(),
                pts=pts,
                limit=100,
                force=True
            ))
        except Exception as e:
            print(f"[DIFF] Error getting difference: {e}")
            return False

        if isinstance(result, ChannelDifferenceEmpty):
            print("[DIFF] No new updates.")
            if result.pts > pts:
                 set_channel_pts(result.pts)
            return True

        if isinstance(result, ChannelDifferenceTooLong):
            print("[DIFF] Difference too long, falling back to full sync.")
            # We could use result.dialog.pts maybe? But safe to full sync.
            return False

        # Handle updates
        new_bookmarks = []
        deleted_ids = []

        # Process New Messages
        for message in result.new_messages:
            # We need to determine topic_id from message
            topic_id = None
            if message.reply_to and message.reply_to.reply_to_msg_id:
                topic_id = message.reply_to.reply_to_msg_id

            # If we can't determine topic (e.g. general chat message?), maybe skip or put in default?
            # For this app, let's assume valid topic_id is required or 1 (General) if not?
            # Actually, `db.upsert_bookmark` requires `topic_id`.
            # If `message.reply_to` is None, it might be in "General" (topic ID 1 usually if Forum enabled?)
            # Or if it IS a topic creation message (id=topic_id).
            if not topic_id:
                # Fallback: check if we have a topic with this ID?
                # Assume General or skip.
                # Let's try to handle: if it has no reply_to in a Forum, it's General (ID=1)?
                # We will skip messages we can't place.
                if message.id == 1: topic_id = 1 # ?
                else: continue

            bm = await process_message_object(message, topic_id)
            if bm:
                bm = await download_bookmark_media(tg_client, bm, download=download)
                new_bookmarks.append(bm)

        # Process Other Updates (Edits, Deletes)
        for update in result.other_updates:
            if isinstance(update, UpdateNewChannelMessage):
                # Handled in new_messages usually?
                # Sometimes appears here.
                pass
            elif isinstance(update, UpdateEditMessage):
                 # Edit is just a message object usually
                 msg = update.message
                 topic_id = None
                 if msg.reply_to and msg.reply_to.reply_to_msg_id:
                     topic_id = msg.reply_to.reply_to_msg_id

                 if topic_id:
                     bm = await process_message_object(msg, topic_id)
                     if bm:
                         bm = await download_bookmark_media(tg_client, bm, download=download)
                         new_bookmarks.append(bm)
            elif isinstance(update, UpdateDeleteChannelMessages):
                deleted_ids.extend(update.messages)

        # Apply changes
        if new_bookmarks:
            upsert_bookmarks_batch(new_bookmarks)
            print(f"  [DIFF] Upserted {len(new_bookmarks)} new/edited bookmarks")

        if deleted_ids:
            delete_bookmarks_by_ids(deleted_ids)
            print(f"  [DIFF] Deleted {len(deleted_ids)} bookmarks")

        # Update state
        pts = result.pts
        set_channel_pts(pts)

        if result.final:
            break

    return True

# Store settings for run_sync to use
_sync_settings = {'download': False, 'photos_only': False}

async def run_sync(download=True, photos_only=True, full_sync=False, client=None):
    """
    Sync topics and bookmarks from Telegram.
    By default downloads photos only (for web UI - faster sync).

    args:
        client: Optional existing TelegramClient instance. If provided, it is used instead of creating a new one.
    """
    print(f"[SYNC] Starting sync with download={download}, photos_only={photos_only}, full_sync={full_sync}")

    # Use existing client or create a fresh one
    if client:
        sync_client = client
        # If external client is passed, we assume we shouldn't disconnect it unless we connect it here.
        # But generally, if it's passed, it might be already connected.
        should_disconnect = False
    else:
        # Create a fresh client for each sync to avoid event loop issues (e.g. from web server)
        sync_client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
        should_disconnect = True
    
    try:
        if not sync_client.is_connected():
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
        
        # Update topics first
        for topic in result.topics:
            upsert_topic(topic.id, topic.title)

        # Try Differential Sync first (if not forced full)
        diff_success = False
        if not full_sync:
            print("[SYNC] Attempting differential sync...")
            diff_success = await sync_from_difference(sync_client, chat, download=download)
        
        if diff_success:
            print("[SYNC] Differential sync successful.")
        else:
            print("[SYNC] Performing Full/Fallback Sync...")
            # Do full per-topic sync
            for topic in result.topics:
                 print(f"[SYNC] Syncing topic: {topic.title}")
                 await sync_bookmarks_with_client(
                    sync_client, chat, topic.id,
                    download=download,
                    photos_only=photos_only,
                    full_sync=True # Force full fetch when diff fails or is requested
                )

            # After full sync, we must capture the current state (pts) so diff sync works next time.
            # GetFullChannelRequest or simple GetChannelDifference with pts=1 to get latest state?
            # Or just fetch latest updates?
            try:
                # Efficient way to get current PTS: get channel full info
                full_chat = await sync_client(functions.channels.GetFullChannelRequest(chat))
                print(f"[SYNC] Updating local state to pts={full_chat.full_chat.pts}")
                set_channel_pts(full_chat.full_chat.pts)
            except Exception as e:
                print(f"[SYNC] Failed to update state: {e}")

        print("[SYNC] Sync completed successfully")
        return True
    except Exception as e:
        print(f"[SYNC] Error during sync: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        if should_disconnect:
            print("[SYNC] Disconnecting...")
            await sync_client.disconnect()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Sync Telegram bookmarks')
    parser.add_argument('--download', '-d', action='store_true', 
                        help='Download media files during sync')
    parser.add_argument('--photos-only', '-p', action='store_true',
                        help='Only download photos, skip videos')
    parser.add_argument('--full', '-f', action='store_true',
                        help='Perform a full sync (re-fetch all messages and prune deleted)')
    args = parser.parse_args()
    
    _sync_settings['download'] = args.download
    _sync_settings['photos_only'] = args.photos_only
    
    if not os.path.exists('media'): os.makedirs('media')
    if not os.path.exists('data'): os.makedirs('data')
    
    print(f"Sync options: download={args.download}, photos_only={args.photos_only}, full={args.full}")
    
    # Use client.start() for interactive login, then sync
    async def main():
        await client.start()  # This handles login interactively
        print("Logged in successfully!")
        # Pass the already connected client to run_sync to avoid "database locked" errors
        await run_sync(download=args.download, photos_only=args.photos_only, full_sync=args.full, client=client)
    
    asyncio.run(main())
