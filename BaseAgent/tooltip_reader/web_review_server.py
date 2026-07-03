"""Local web server for reviewing captured UI elements before saving.

Runs on ``localhost:8765``. Serves a single-page application where
the user can:

- See all pending elements captured via CTRL+H
- View the captured screenshot
- Edit the recognized text, name, and tags
- Save to the database or discard each element

No external dependencies — uses only Python's built-in ``http.server``.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

from src.core.logger import get_logger

logger = get_logger(__name__)

# Serve static file from the same directory as this module.
_MODULE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# HTML page (served at /)
# ---------------------------------------------------------------------------

# The entire frontend is a single HTML file embedded here. No CDN deps.
_REVIEW_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UI Element Review — DungeonCrusher</title>
<style>
  :root {
    --bg: #1a1a2e; --card-bg: #16213e; --accent: #e94560;
    --text: #eee; --text-dim: #8899aa; --input-bg: #0f3460;
    --btn-save: #2ecc71; --btn-discard: #e74c3c; --btn-edit: #3498db;
    --radius: 8px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg);
         color: var(--text); min-height: 100vh; }
  header { background: var(--card-bg); padding: 16px 24px;
           border-bottom: 2px solid var(--accent);
           display: flex; justify-content: space-between; align-items: center;
           position: sticky; top: 0; z-index: 10; }
  header h1 { font-size: 1.3rem; }
  header .count { font-size: 0.9rem; color: var(--text-dim); }
  .container { max-width: 960px; margin: 0 auto; padding: 20px; }
  .empty-state { text-align: center; padding: 80px 20px; color: var(--text-dim); }
  .empty-state .icon { font-size: 3rem; margin-bottom: 16px; }
  .empty-state p { font-size: 1rem; }
  .empty-state kbd { background: #333; padding: 2px 8px; border-radius: 4px;
                     border: 1px solid #555; font-family: monospace; }
  .card { background: var(--card-bg); border-radius: var(--radius);
          margin-bottom: 16px; overflow: hidden;
          border: 1px solid rgba(255,255,255,0.06); }
  .card-header { display: flex; justify-content: space-between; align-items: center;
                 padding: 12px 16px; background: rgba(0,0,0,0.2);
                 border-bottom: 1px solid rgba(255,255,255,0.05); }
  .card-header .id { font-family: monospace; font-size: 0.8rem; color: var(--text-dim); }
  .card-header .time { font-size: 0.8rem; color: var(--text-dim); }
  .card-body { display: flex; gap: 16px; padding: 16px; }
  .card-body .preview { flex: 0 0 auto; }
  .card-body .preview img { max-width: 400px; max-height: 200px;
                            border-radius: 4px; border: 1px solid rgba(255,255,255,0.1);
                            image-rendering: auto; display: block; }
  .card-body .preview .dims { font-size: 0.7rem; color: var(--text-dim);
                              text-align: center; margin-top: 4px; }
  .card-body .form { flex: 1; display: flex; flex-direction: column; gap: 10px; }
  .form-row { display: flex; gap: 10px; align-items: center; }
  .form-row label { min-width: 70px; font-size: 0.85rem; color: var(--text-dim);
                    text-align: right; }
  .form-row input, .form-row textarea { flex: 1; background: var(--input-bg);
    border: 1px solid rgba(255,255,255,0.1); color: var(--text);
    padding: 6px 10px; border-radius: 4px; font-family: inherit;
    font-size: 0.9rem; }
  .form-row textarea { resize: vertical; min-height: 28px; }
  .form-row input:focus, .form-row textarea:focus { outline: none;
    border-color: var(--accent); }
  .form-row .pos { font-size: 0.8rem; color: var(--text-dim); }
  .card-actions { display: flex; gap: 8px; padding: 0 16px 16px 16px;
                  justify-content: flex-end; }
  .btn { padding: 8px 20px; border: none; border-radius: 4px; cursor: pointer;
         font-size: 0.9rem; font-weight: 600; transition: opacity 0.15s; }
  .btn:hover { opacity: 0.85; }
  .btn-save { background: var(--btn-save); color: #fff; }
  .btn-discard { background: var(--btn-discard); color: #fff; }
  .toast { position: fixed; bottom: 20px; right: 20px; padding: 12px 24px;
           border-radius: 4px; color: #fff; font-weight: 600;
           animation: fadeIn 0.3s; z-index: 100; }
  .toast.success { background: var(--btn-save); }
  .toast.error { background: var(--btn-discard); }
  @keyframes fadeIn { from{opacity:0;transform:translateY(10px);}
                       to{opacity:1;transform:translateY(0);} }
  .saved-badge { display: inline-block; background: var(--btn-save);
                 color: #fff; font-size: 0.7rem; padding: 2px 8px;
                 border-radius: 10px; margin-left: 8px; }
</style>
</head>
<body>

<header>
  <h1>&#128736; UI Element Review</h1>
  <span class="count" id="counter">Loading...</span>
  <button class="btn btn-save" onclick="fetchPending()" style="margin-left:16px;">&#8635; Refresh</button>
</header>

<div class="container" id="app">
  <div class="empty-state">
    <div class="icon">&#128269;</div>
    <p>No pending elements.</p>
    <p>Press <kbd>Ctrl+H</kbd> in-game to capture a tooltip.</p>
  </div>
</div>

<script>
const API = '/api/pending';
let toastTimer = null;

// ---- Toast ----
function showToast(msg, type) {
  const old = document.getElementById('toast');
  if (old) old.remove();
  if (toastTimer) clearTimeout(toastTimer);
  const el = document.createElement('div');
  el.id = 'toast';
  el.className = 'toast ' + type;
  el.textContent = msg;
  document.body.appendChild(el);
  toastTimer = setTimeout(() => el.remove(), 2500);
}

// ---- Render ----
function render(items) {
  const app = document.getElementById('app');
  document.getElementById('counter').textContent =
    items.length + ' pending element' + (items.length !== 1 ? 's' : '');

  if (items.length === 0) {
    app.innerHTML = `<div class="empty-state">
      <div class="icon">&#128269;</div>
      <p>No pending elements.</p>
      <p>Press <kbd>Ctrl+H</kbd> in-game to capture a tooltip.</p>
    </div>`;
    return;
  }

  let html = '';
  items.forEach(el => {
    const imgSrc = el.image_base64
      ? 'data:image/png;base64,' + el.image_base64
      : '';
    const hasImage = !!imgSrc;

    html += `<div class="card" id="card-${el.id}">
      <div class="card-header">
        <span class="id">#${el.id}</span>
        <span class="time">${el.created_at || ''}</span>
      </div>
      <div class="card-body">
        <div class="preview">
          ${hasImage
            ? `<img src="${imgSrc}" alt="captured region" id="img-${el.id}" title="${el.region_width}x${el.region_height}px">`
            : '<div style="width:320px;height:60px;background:#111;border-radius:4px;display:flex;align-items:center;justify-content:center;color:#555;">No image</div>'}
          <div class="dims">${el.region_width}&times;${el.region_height}px
            &middot; pos (${el.window_x}, ${el.window_y})</div>
        </div>
        <div class="form">
          <div class="form-row">
            <label>Raw OCR</label>
            <span class="pos" style="color:var(--text);background:var(--input-bg);padding:6px 10px;border-radius:4px;flex:1;font-family:monospace;">${esc(el.raw_text || '(no text recognized)')}</span>
          </div>
          <div class="form-row">
            <label>Correct</label>
            <textarea id="text-${el.id}" rows="2"
                      placeholder="Your corrected version of the text..."
                      oninput="autoName('${el.id}')">${esc(el.edited_text || '')}</textarea>
          </div>
          <div class="form-row">
            <label>Name</label>
            <input type="text" id="name-${el.id}" value="${esc(el.name || '')}"
                   placeholder="Auto-generated from correct text...">
          </div>
          <div class="form-row">
            <label>Tags</label>
            <input type="text" id="tags-${el.id}"
                   value="${esc((el.tags || []).join(', '))}"
                   placeholder="combat, resource, button...">
          </div>
        </div>
      </div>
      <div class="card-actions">
        <button class="btn btn-discard" onclick="discard('${el.id}')">&#10005; Discard</button>
        <button class="btn btn-save" onclick="save('${el.id}')">&#10003; Save to DB</button>
      </div>
    </div>`;
  });
  app.innerHTML = html;
}

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ---- API calls ----
async function fetchPending() {
  try {
    const resp = await fetch(API + '?images=1');
    const data = await resp.json();
    render(data.items || []);
  } catch (e) {
    console.error('fetch failed:', e);
  }
}

async function save(id) {
  const name = document.getElementById('name-' + id)?.value || '';
  const edited_text = document.getElementById('text-' + id)?.value || '';
  const tagsStr = document.getElementById('tags-' + id)?.value || '';
  const tags = tagsStr.split(',').map(t => t.trim()).filter(Boolean);

  try {
    const resp = await fetch(API + '/' + id, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, edited_text, tags, action: 'save'}),
    });
    const data = await resp.json();
    if (data.ok) {
      showToast('Saved: ' + (data.name || id), 'success');
    } else {
      showToast('Error: ' + (data.error || 'unknown'), 'error');
    }
    fetchPending();
  } catch (e) {
    showToast('Failed to save: ' + e.message, 'error');
  }
}

async function discard(id) {
  try {
    const resp = await fetch(API + '/' + id, {method: 'DELETE'});
    const data = await resp.json();
    if (data.ok) {
      showToast('Discarded', 'success');
    }
    fetchPending();
  } catch (e) {
    showToast('Failed to discard', 'error');
  }
}

function autoName(id) {
  const textEl = document.getElementById('text-' + id);
  const nameEl = document.getElementById('name-' + id);
  if (textEl && nameEl) {
    const textVal = textEl.value.trim();
    // Only auto-fill if name hasn't been manually edited.
    if (!nameEl.dataset.manual || nameEl.dataset.manual === '0') {
      nameEl.value = textVal ? textVal.slice(0, 60) : '';
      nameEl.dataset.manual = '0';
    }
  }
}
// Mark name as manually edited when user types in it.
document.addEventListener('input', function(e) {
  if (e.target.id && e.target.id.startsWith('name-')) {
    e.target.dataset.manual = '1';
  }
});

// ---- Init ----
fetchPending();
// Poll every 3 seconds, but skip when the user is editing a field.
setInterval(function() {
  var active = document.activeElement;
  if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA')) {
    return;  // User is typing — don't disrupt them.
  }
  fetchPending();
}, 3000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------


class _ReviewHandler(BaseHTTPRequestHandler):
    """Handles requests for the UI review interface.

    Routes:
        GET  /              → HTML review page.
        GET  /api/pending   → List all pending elements (JSON).
        GET  /api/pending/<id> → Get one element (JSON).
        POST /api/pending/<id> → Update + optionally save to DB.
        DELETE /api/pending/<id> → Discard (remove).
    """

    # Reference to the shared store (set by WebReviewServer).
    store: "PendingElementStore" = None  # type: ignore[assignment]
    # Reference to the UI element database (set by WebReviewServer).
    db = None  # type: ignore[assignment]
    # Reference to the template matcher — if set, newly saved elements
    # are registered at runtime without a full reload.
    matcher = None  # type: ignore[assignment]

    def log_message(self, format, *args):  # noqa: A002
        """Suppress default HTTP request logging."""
        pass

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "" or path == "/":
            self._serve_html()
        elif path == "/api/pending":
            self._handle_list_pending(parsed.query)
        elif path.startswith("/api/pending/"):
            element_id = path.split("/")[-1]
            self._handle_get_pending(element_id)
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/pending/"):
            element_id = path.split("/")[-1]
            body = self._read_body()
            self._handle_update(element_id, body)
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/pending/"):
            element_id = path.split("/")[-1]
            self._handle_discard(element_id)
        else:
            self._send_json({"error": "Not found"}, 404)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _serve_html(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(_REVIEW_PAGE.encode("utf-8"))

    def _handle_list_pending(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        include_images = params.get("images", ["0"])[0] == "1"
        items = self.store.get_all(include_image=include_images)
        self._send_json({"items": items, "count": len(items)})

    def _handle_get_pending(self, element_id: str) -> None:
        item = self.store.get(element_id, include_image=True)
        if item is None:
            self._send_json({"error": "Element not found"}, 404)
            return
        self._send_json(item)

    def _handle_update(self, element_id: str, body: dict) -> None:
        action = body.get("action", "update")

        # Update editable fields.
        name = body.get("name")
        edited_text = body.get("edited_text")
        tags = body.get("tags")

        if not self.store.update(element_id, name=name, edited_text=edited_text, tags=tags):
            self._send_json({"ok": False, "error": "Element not found"}, 404)
            return

        if action == "save":
            # Pop from pending and save to database.
            element = self.store.pop(element_id)
            if element is None:
                self._send_json({"ok": False, "error": "Element already removed"}, 404)
                return

            if self.db is not None and element.image is not None:
                try:
                    record = self.db.add_element(
                        text=element.edited_text or element.raw_text,
                        region_image=element.image,
                        window_x=element.window_x,
                        window_y=element.window_y,
                        region_width=element.region_width,
                        region_height=element.region_height,
                        name=element.name,
                        tags=element.tags,
                    )
                    # Also register the template with the matcher at runtime.
                    if self.matcher is not None:
                        self.matcher.add_template(record.id, element.image)
                    logger.info(
                        f"[WebReview] Element '{element.id}' saved as "
                        f"'{record.id}' to database"
                    )
                    self._send_json({"ok": True, "name": record.name, "id": record.id})
                except Exception as exc:
                    logger.exception("[WebReview] Failed to save to database")
                    # Put it back in pending so the user can retry.
                    self.store._items[element.id] = element  # noqa: SLF001
                    self._send_json({"ok": False, "error": str(exc)}, 500)
            else:
                self._send_json({"ok": False, "error": "No database or image"}, 500)
        else:
            self._send_json({"ok": True, "message": "Updated"})

    def _handle_discard(self, element_id: str) -> None:
        removed = self.store.remove(element_id)
        if removed:
            self._send_json({"ok": True})
        else:
            self._send_json({"ok": False, "error": "Element not found"}, 404)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}


# ---------------------------------------------------------------------------
# Server wrapper
# ---------------------------------------------------------------------------


class WebReviewServer:
    """Local HTTP server for reviewing UI elements.

    Usage::

        store = PendingElementStore()
        db = UIElementDB("resources/ui_elements_db.json")
        server = WebReviewServer(store, db, port=8765)
        server.start()   # Runs in a background thread.
        # ... capture elements with CTRL+H ...
        # Open http://localhost:8765 in a browser.
        server.stop()
    """

    def __init__(
        self,
        store: "PendingElementStore",
        db: "UIElementDB",
        matcher=None,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        """
        Args:
            store: Shared pending-element store.
            db: UI element database for persistence.
            matcher: Optional :class:`TemplateMatcher` — if provided,
                     newly saved elements are registered at runtime.
            host: Bind address.
            port: Bind port.
        """
        self._store = store
        self._db = db
        self._matcher = matcher
        self._host = host
        self._port = port
        self._httpd: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the HTTP server in a background daemon thread."""
        # Inject dependencies into the handler class.
        _ReviewHandler.store = self._store
        _ReviewHandler.db = self._db
        _ReviewHandler.matcher = self._matcher

        self._httpd = HTTPServer((self._host, self._port), _ReviewHandler)

        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="web-review-server",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            f"UI Review server started at http://{self._host}:{self._port}"
        )

    def stop(self) -> None:
        """Shut down the HTTP server."""
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd = None
            logger.info("UI Review server stopped")

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    @property
    def is_running(self) -> bool:
        return self._httpd is not None and self._thread is not None and self._thread.is_alive()
