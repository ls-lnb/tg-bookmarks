from fasthtml.common import *
from db import get_topics, get_bookmarks, search_bookmarks
import os
import re

def slugify(name):
    """Convert topic name to URL-friendly slug: spaces -> underscores, remove special chars/emojis, lowercase."""
    # Remove emojis and special characters, keep alphanumeric and spaces
    slug = re.sub(r'[^\w\s-]', '', name, flags=re.UNICODE)
    # Replace whitespace with underscores
    slug = re.sub(r'\s+', '_', slug.strip())
    return slug.lower()

def unslugify_to_topic(slug, topics):
    """Find topic by slug, returns topic dict or None."""
    for t in topics:
        if slugify(t['title']) == slug:
            return t
    return None

def parse_text_with_links(text):
    """
    Convert markdown-style links [text](url) and plain URLs to HTML.
    Returns a Safe string that won't be escaped.
    """
    if not text:
        return ""
    
    # First, convert markdown links [text](url) to HTML
    # Pattern: [anchor text](url)
    md_link_pattern = r'\[([^\]]+)\]\(([^)]+)\)'
    
    def md_to_html(match):
        anchor_text = match.group(1)
        url = match.group(2)
        return f'<a href="{url}" target="_blank" rel="noopener">{anchor_text}</a>'
    
    result = re.sub(md_link_pattern, md_to_html, text)
    
    # Then, convert remaining plain URLs to links (but not those already in href="...")
    # Simple URL pattern
    url_pattern = r'(?<!href=")(?<!src=")(https?://[^\s<>"]+)'
    
    def url_to_html(match):
        url = match.group(1)
        # Truncate very long URLs for display
        display_url = url if len(url) < 50 else url[:47] + "..."
        return f'<a href="{url}" target="_blank" rel="noopener">{display_url}</a>'
    
    result = re.sub(url_pattern, url_to_html, result)
    
    # Convert newlines to <br>
    result = result.replace('\n', '<br>')
    
    # Return as Safe so it won't be escaped
    return Safe(result)

app, rt = fast_app(
    hdrs=(
        Link(rel='stylesheet', href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap'),
        Link(rel='stylesheet', href='style.css'),
        Script(src="https://unpkg.com/htmx.org@1.9.12"),
        # Feather icons or similar for premium feel
        Script(src="https://unpkg.com/feather-icons"),
    )
)

def render_bookmark_grid(items):
    cards = []
    for item in items:
        content = []
        if item['text']:
            # Parse markdown links and URLs
            parsed_text = parse_text_with_links(item['text'])
            content.append(Div(parsed_text, cls="card-text"))
        
        if item['media_path'] or item['content_type'] in ["photo", "video"]:
            media_url = f"/media/{item['id']}"
            thumb_url = f"/thumb/{item['id']}"
            if item['content_type'] == "photo":
                content.append(Img(src=media_url, cls="card-media", loading="lazy"))
            elif item['content_type'] == "video":
                # Show thumbnail with play overlay, click to play video
                content.append(
                    Div(
                        Img(src=thumb_url, cls="card-media", loading="lazy"),
                        Div(I(data_feather="play-circle"), cls="play-overlay"),
                        A(href=media_url, target="_blank", cls="video-link"),
                        cls="video-thumb-container"
                    )
                )
        
        cards.append(Div(*content, cls="bookmark-card"))
    return Div(*cards, cls="grid"), Script("feather.replace();")

@rt("/search")
def post_search(q: str):
    if not q:
        return Div("Enter a search term.", cls="empty-state")
    
    # Simple search across all bookmarks
    items = search_bookmarks(q)
    if not items:
        return Div(f"No results found for '{q}'", cls="empty-state")
    
    return render_bookmark_grid(items)

from db import get_topics as db_get_topics

def get_topic_title(topic_id):
    topics = db_get_topics()
    for t in topics:
        if t['id'] == topic_id:
            return t['title']
    return "Bookmarks"

@rt("/topic/{topic_id}")
def get_topic_content(topic_id: int, sort: str = "DESC"):
    items = get_bookmarks(topic_id, sort)
    topic_title = get_topic_title(topic_id)
    
    # Out-of-band swap to update header
    header_update = H2(topic_title, id="topic-title", hx_swap_oob="true")
    
    # Sort toggle button - flip the sort order on click
    next_sort = "ASC" if sort == "DESC" else "DESC"
    sort_label = "Recent First" if sort == "DESC" else "Oldest First"
    sort_icon = "arrow-down" if sort == "DESC" else "arrow-up"
    sort_btn = Button(
        I(data_feather=sort_icon), f" {sort_label}",
        id="sort-toggle",
        cls="btn-sort",
        hx_get=f"/topic/{topic_id}?sort={next_sort}",
        hx_target="#bookmark-grid",
        hx_swap_oob="true"
    )
    
    # Refresh feather icons
    icon_script = Script("feather.replace();")
    
    if not items:
        return header_update, sort_btn, icon_script, Div("No bookmarks found for this topic.", cls="empty-state")
    
    grid = render_bookmark_grid(items)
    return header_update, sort_btn, icon_script, grid

from telegram_client import download_media_async

@rt("/media/{message_id}")
async def get_media(message_id: int):
    # Try JPG, video thumbnail, and MP4
    for suffix in ['.jpg', '_thumb.jpg', '.mp4']:
        file_path = f"media/{message_id}{suffix}"
        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            return FileResponse(file_path)
    
    # Fallback: On-demand download
    path = await download_media_async(message_id)
    if path and os.path.exists(path):
        return FileResponse(path)
        
    return "Media not found.", 404

@rt("/thumb/{message_id}")
async def get_thumb(message_id: int):
    # Serve only thumbnail images (for video preview)
    for suffix in ['_thumb.jpg', '.jpg']:
        file_path = f"media/{message_id}{suffix}"
        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            return FileResponse(file_path)
    
    # Return a placeholder or 404
    return "Thumbnail not found.", 404

from telegram_client import run_sync
import asyncio

# Sync lock to prevent concurrent syncs
_sync_in_progress = False

@rt("/api/sync")
def post_sync():
    global _sync_in_progress
    
    if _sync_in_progress:
        return Span("Sync already in progress...", cls="status-msg warning")
    
    _sync_in_progress = True
    print("Starting sync...", flush=True)
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success = loop.run_until_complete(run_sync())
        loop.close()
    except Exception as e:
        print(f"Sync error: {e}", flush=True)
        _sync_in_progress = False
        return Span("Sync failed. Check terminal.", cls="status-msg error")
    finally:
        _sync_in_progress = False
    
    if success:
        return Div(
            Span("Sync complete!", cls="status-msg success"),
            Script("setTimeout(() => window.location.reload(), 1500);")
        )
    return Span("Sync failed. Check credentials.", cls="status-msg error")

@rt("/")
def get():
    all_topics = get_topics()
    
    # Sidebar with Topics
    topic_items = [
        Li(A(
            Span(topic['title']),
            href=f"/{slugify(topic['title'])}", 
            hx_get=f"/topic/{topic['id']}", 
            hx_target="#bookmark-grid",
            hx_push_url=f"/{slugify(topic['title'])}",
            cls="topic-link",
            id=f"topic-{topic['id']}"
        )) for topic in all_topics
    ]
    
    sidebar = Aside(
        Div(
            H1("TG Markers", cls="brand"),
            Button(
                I(data_feather="refresh-cw"), 
                " Sync", 
                hx_post="/api/sync", 
                hx_target="#sync-status",
                hx_indicator="#sync-spinner",
                hx_disabled_elt="this",
                cls="btn-sync"
            ),
            Span(I(data_feather="loader"), id="sync-spinner", cls="htmx-indicator sync-spinner"),
            id="sidebar-header"
        ),
        Nav(Ul(*topic_items), id="sidebar-nav"),
        Div(id="sync-status"),
        id="sidebar"
    )
    
    main_content = Main(
        Header(
            H2("Bookmarks", id="topic-title"),
            Div(
                # Sort toggle button (hidden initially, shown when topic selected)
                Button(
                    I(data_feather="arrow-down"), " Recent First",
                    id="sort-toggle",
                    cls="btn-sort",
                    style="display: none;"
                ),
                Input(
                    type="text", 
                    name="q",
                    placeholder="Search bookmarks...", 
                    id="search-bar",
                    hx_post="/search",
                    hx_target="#bookmark-grid",
                    hx_trigger="keyup changed delay:500ms, search"
                ),
                cls="header-controls"
            ),
            cls="main-header"
        ),
        Div(
            Div("Select a topic from the sidebar to start browsing your saved items.", cls="empty-state"),
            id="bookmark-grid"
        ),
        id="main-content"
    )
    
    # Script for feather icons and topic selection
    script = Script("""
        feather.replace();
        document.getElementById('sidebar-nav').addEventListener('click', function(e) {
            var link = e.target.closest('.topic-link');
            if (link) {
                document.querySelectorAll('.topic-link').forEach(el => el.classList.remove('active'));
                link.classList.add('active');
            }
        });
    """)
    
    return Title("Telegram Bookmarks"), Div(sidebar, main_content, id="app-container"), script

# Route for direct topic URL access (e.g., /General)
@rt("/{topic_slug}")
def get_by_slug(topic_slug: str):
    # Skip if it looks like a file or known route
    if '.' in topic_slug or topic_slug in ['api', 'search', 'topic', 'media', 'thumb', 'style.css']:
        return "Not found", 404
    
    all_topics = get_topics()
    topic = unslugify_to_topic(topic_slug, all_topics)
    
    if not topic:
        return "Topic not found", 404
    
    # Render the full page with this topic pre-selected
    topic_items = [
        Li(A(
            Span(t['title']),
            href=f"/{slugify(t['title'])}", 
            hx_get=f"/topic/{t['id']}", 
            hx_target="#bookmark-grid",
            hx_push_url=f"/{slugify(t['title'])}",
            cls="topic-link" + (" active" if t['id'] == topic['id'] else ""),
            id=f"topic-{t['id']}"
        )) for t in all_topics
    ]
    
    sidebar = Aside(
        Div(
            H1("TG Markers", cls="brand"),
            Button(
                I(data_feather="refresh-cw"), 
                " Sync", 
                hx_post="/api/sync", 
                hx_target="#sync-status",
                hx_indicator="#sync-spinner",
                hx_disabled_elt="this",
                cls="btn-sync"
            ),
            Span(I(data_feather="loader"), id="sync-spinner", cls="htmx-indicator sync-spinner"),
            id="sidebar-header"
        ),
        Nav(Ul(*topic_items), id="sidebar-nav"),
        Div(id="sync-status"),
        id="sidebar"
    )
    
    # Get bookmarks for selected topic
    items = get_bookmarks(topic['id'])
    if items:
        grid_content, scroll_script = render_bookmark_grid(items)
    else:
        grid_content = Div("No bookmarks found for this topic.", cls="empty-state")
        scroll_script = Script("")
    
    # Sort button
    sort_btn = Button(
        I(data_feather="arrow-down"), " Recent First",
        id="sort-toggle",
        cls="btn-sort",
        hx_get=f"/topic/{topic['id']}?sort=ASC",
        hx_target="#bookmark-grid"
    )
    
    main_content = Main(
        Header(
            H2(topic['title'], id="topic-title"),
            Div(
                sort_btn,
                Input(
                    type="text", 
                    name="q",
                    placeholder="Search bookmarks...", 
                    id="search-bar",
                    hx_post="/search",
                    hx_target="#bookmark-grid",
                    hx_trigger="keyup changed delay:500ms, search"
                ),
                cls="header-controls"
            ),
            cls="main-header"
        ),
        Div(grid_content, id="bookmark-grid"),
        id="main-content"
    )
    
    script = Script("""
        feather.replace();
        document.getElementById('sidebar-nav').addEventListener('click', function(e) {
            var link = e.target.closest('.topic-link');
            if (link) {
                document.querySelectorAll('.topic-link').forEach(el => el.classList.remove('active'));
                link.classList.add('active');
            }
        });
    """)
    
    return Title(f"{topic['title']} - Telegram Bookmarks"), Div(sidebar, main_content, id="app-container"), script, scroll_script

serve()
