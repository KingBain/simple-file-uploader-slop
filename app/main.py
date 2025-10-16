import os
import uuid
import datetime as dt
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from jinja2 import Environment, BaseLoader, select_autoescape
from sqlalchemy import (
    create_engine, MetaData, Table, Column, LargeBinary, String, DateTime, BigInteger, text
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import select, insert, delete
from sqlalchemy.engine import Engine

# -----------------------
# Config
# -----------------------

def getenv_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw)
    except (TypeError, ValueError):
        print(f"Warning: env {name} has invalid value {raw!r}; using default {default}")
        return default

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required. Example: postgresql+psycopg://user:pass@host:5432/db?sslmode=require")

MAX_UPLOAD_MB = getenv_int("MAX_UPLOAD_MB", 100)
APP_TITLE = os.environ.get("APP_TITLE", "Local File Uploader")

UPLOAD_DIR = "/tmp/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# -----------------------
# Database setup
# -----------------------

engine: Engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
metadata = MetaData()

files = Table(
    "files",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
    Column("filename", String(512), nullable=False),
    Column("content_type", String(255), nullable=True),
    Column("size_bytes", BigInteger, nullable=False),
    Column("data", LargeBinary, nullable=True),    # nullable to allow local storage
    Column("path", String(1024), nullable=True),   # local file path
    Column("storage_type", String(16), nullable=False, default="db"),
    Column("uploaded_at", DateTime(timezone=True), nullable=False),
)

with engine.begin() as conn:
    metadata.create_all(conn)
    # add missing columns if running against an old table
    try:
        conn.execute(text("ALTER TABLE files ADD COLUMN IF NOT EXISTS path TEXT"))
        conn.execute(text("ALTER TABLE files ADD COLUMN IF NOT EXISTS storage_type VARCHAR(16) DEFAULT 'db'"))
        # If original schema had NOT NULL on data, you can drop it manually:
        # conn.execute(text("ALTER TABLE files ALTER COLUMN data DROP NOT NULL"))
    except Exception as e:
        print(f"Schema check warning: {e}")

# -----------------------
# FastAPI app
# -----------------------

app = FastAPI(title=APP_TITLE)

# -----------------------
# Template (Jinja2)
# -----------------------

INDEX_HTML = """<!DOCTYPE html>
<html dir="ltr" lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />

    <meta
      name="description"
      content="Upload, list, download, and delete files — stored either in the database or in local container storage."
    />

    <title>Local File Uploader</title>

    <!-- GC Design System CSS Shortcuts -->
    <link
      rel="stylesheet"
      href="https://cdn.design-system.alpha.canada.ca/@gcds-core/css-shortcuts@1.0.0/dist/gcds-css-shortcuts.min.css"
    />

    <!-- GC Design System Components -->
    <link
      rel="stylesheet"
      href="https://cdn.design-system.alpha.canada.ca/@cdssnc/gcds-components@0.43.1/dist/gcds/gcds.css"
    />
    <script
      type="module"
      src="https://cdn.design-system.alpha.canada.ca/@cdssnc/gcds-components@0.43.1/dist/gcds/gcds.esm.js"
    ></script>

    <!-- Custom styles -->
    <style>
      gcds-container[main-container] {
        margin-block: 2rem;
      }

      form {
        margin-block: 1rem 2rem;
      }

      table {
        width: 100%;
        border-collapse: collapse;
      }

      th,
      td {
        padding: 0.5rem 0.75rem;
        border-bottom: 1px solid var(--gcds-border__default);
      }

      th {
        text-align: left;
      }

      .inline-form {
        display: inline;
      }

      .muted {
        color: var(--gcds-text__muted);
      }

      details summary {
        cursor: pointer;
        font-weight: 600;
      }

      details[open] summary {
        margin-bottom: 0.75rem;
      }

      select {
        margin-inline: 0.5rem;
        padding: 0.35rem 0.5rem;
      }

      input[type="file"] {
        display: block;
        margin-block: 0.5rem 1rem;
      }
    </style>
  </head>

  <body>
    <!-- GC Header -->
    <gcds-header lang-href="#" skip-to-href="#main-content">
      <gcds-breadcrumbs slot="breadcrumb">
        <gcds-breadcrumbs-item href="#">Canada.ca</gcds-breadcrumbs-item>
        <gcds-breadcrumbs-item href="#">Home</gcds-breadcrumbs-item>
        <gcds-breadcrumbs-item href="#" current>File uploader</gcds-breadcrumbs-item>
      </gcds-breadcrumbs>
      <gcds-search slot="search" label="Search Canada.ca"></gcds-search>
    </gcds-header>

    <!-- Main content -->
    <gcds-container id="main-content" main-container size="xl" centered tag="main">
      <section>
        <gcds-heading tag="h1">Local File Uploader</gcds-heading>
        <gcds-text class="muted">Max size per file: 100 MB</gcds-text>
      </section>

      <!-- Upload section -->
      <section>
          <summary><gcds-heading tag="h2" size="h5">Upload a file</gcds-heading></summary>
          <form method="POST" action="upload" enctype="multipart/form-data">
            <!-- Standard file selector restored -->
            <gcds-file-uploader
  id="file"
  name="file"
  label="Choose a file"
  required
></gcds-file-uploader>

            <label for="storage">Storage location:</label>
            <select id="storage" name="storage">
              <option value="db">Database</option>
              <option value="local">Local (container disk)</option>
            </select>

            <gcds-button type="submit" button-style="primary" size="default">
  Upload
</gcds-button>
            <p class="muted">Max size: 100 MB</p>
          </form>
      </section>

      <!-- File list section -->
      <section>
        <gcds-heading tag="h2" size="h5">Files</gcds-heading>

        <!-- Jinja placeholder for when files exist -->
        {% if files|length == 0 %}
        <p>No files yet.</p>
        {% else %}
        <gcds-table caption="Uploaded files">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Size</th>
                <th>Type</th>
                <th>Stored</th>
                <th>Uploaded</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {% for f in files %}
              <tr>
                <td>{{ f.filename }}</td>
                <td>{{ "{:,}".format(f.size_bytes) }} B</td>
                <td>{{ f.content_type or "n/a" }}</td>
                <td>{{ f.storage_type }}</td>
                <td>{{ f.uploaded_at }}</td>
                <td>
                  <a href="download/{{ f.id }}">Download</a>
                  <form
                    method="POST"
                    action="delete/{{ f.id }}"
                    class="inline-form"
                    onsubmit="return confirm('Delete this file?')"
                  >
                    <gcds-button
                      type="submit"
                      button-style="secondary"
                      size="small"
                      >Delete</gcds-button
                    >
                  </form>
                </td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
        </gcds-table>
        {% endif %}
      </section>
    </gcds-container>

    <!-- GC Footer -->
    <gcds-footer
      display="full"
      contextual-heading="Canadian Digital Service"
      contextual-links='{ "Why GC Notify": "#", "Features": "#", "Activity on GC Notify": "#" }'
    ></gcds-footer>
  </body>
</html>
"""

jinja_env = Environment(loader=BaseLoader(), autoescape=select_autoescape(["html", "xml"]))

def render_index(rows):
    template = jinja_env.from_string(INDEX_HTML)
    return template.render(
        title=APP_TITLE,
        files=rows,
        max_mb=MAX_UPLOAD_MB,
    )

# -----------------------
# Routes
# -----------------------

@app.get("/", response_class=HTMLResponse)
def index():
    with engine.begin() as conn:
        rows = conn.execute(select(files).order_by(files.c.uploaded_at.desc())).mappings().all()
    return HTMLResponse(render_index(rows))

@app.post("/upload")
async def upload(file: UploadFile = File(...), request: Request = None):
    form = await request.form()
    storage_choice = form.get("storage", "db")

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large")

    data = await file.read()
    size_bytes = len(data)
    if size_bytes == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    safe_name = (file.filename or "unnamed")[:512]
    rec_id = uuid.uuid4()
    now = dt.datetime.now(dt.timezone.utc)
    path = None
    storage_type = "db"

    if storage_choice == "local":
        path = os.path.join(UPLOAD_DIR, f"{rec_id}_{safe_name}")
        with open(path, "wb") as f:
            f.write(data)
        # if your DB still has NOT NULL on 'data', keep b""; otherwise you can set None
        data = b""
        storage_type = "local"

    with engine.begin() as conn:
        conn.execute(
            insert(files).values(
                id=rec_id,
                filename=safe_name,
                content_type=file.content_type,
                size_bytes=size_bytes,
                data=data,
                path=path,
                storage_type=storage_type,
                uploaded_at=now,
            )
        )

    return RedirectResponse(url=".", status_code=303)

@app.get("/download/{file_id}")
def download(file_id: uuid.UUID):
    with engine.begin() as conn:
        row = conn.execute(select(files).where(files.c.id == file_id)).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Not found")

    if row["storage_type"] == "local":
        file_path = row["path"]
        if not file_path or not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="File missing on disk")
        return StreamingResponse(
            open(file_path, "rb"),
            media_type=row["content_type"] or "application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{row["filename"]}"'}
        )

    return StreamingResponse(
        iter([row["data"]]),
        media_type=row["content_type"] or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{row["filename"]}"'}
    )

@app.post("/delete/{file_id}")
def delete_file(file_id: uuid.UUID, request: Request):
    with engine.begin() as conn:
        row = conn.execute(select(files).where(files.c.id == file_id)).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Not found")

        if row["storage_type"] == "local" and row["path"] and os.path.exists(row["path"]):
            try:
                os.remove(row["path"])
            except Exception as e:
                print(f"Warning: failed to delete local file {row['path']}: {e}")

        conn.execute(delete(files).where(files.c.id == file_id))

    return RedirectResponse(url=".", status_code=303)
