import os
import uuid
import datetime as dt
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.responses import RedirectResponse
from starlette.datastructures import Headers

from sqlalchemy import (
    create_engine, MetaData, Table, Column, LargeBinary, String, DateTime,
    BigInteger
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import select, insert, delete
from sqlalchemy.engine import Engine

# ---- Config ----
DATABASE_URL = os.environ.get("DATABASE_URL")  # e.g. postgresql+psycopg://user:pass@host:5432/db
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))  # basic guardrail
APP_TITLE = os.environ.get("APP_TITLE", "WebFileBox")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required. Example: postgresql+psycopg://user:pass@host:5432/db")

# ---- DB setup ----
engine: Engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
metadata = MetaData()

files = Table(
    "files",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
    Column("filename", String(512), nullable=False),
    Column("content_type", String(255), nullable=True),
    Column("size_bytes", BigInteger, nullable=False),
    Column("data", LargeBinary, nullable=False),
    Column("uploaded_at", DateTime(timezone=True), nullable=False),
)

# Create table if it doesn't exist (simple bootstrapping; for real use, run migrations)
with engine.begin() as conn:
    conn.run_callable(metadata.create_all)

app = FastAPI(title=APP_TITLE)

# ---- Minimal UI ----
INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{{title}}</title>
  <link href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css" rel="stylesheet">
  <style>
    body { max-width: 900px; margin: 2rem auto; }
    .muted { color: #777; font-size: .9rem; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    .nowrap { white-space: nowrap; }
  </style>
</head>
<body>
  <main>
    <h1>{{title}}</h1>
    <details open>
      <summary>Upload a file</summary>
      <form method="POST" action="/upload" enctype="multipart/form-data">
        <input type="file" name="file" required />
        <button type="submit">Upload</button>
        <p class="muted">Max size: {{max_mb}} MB</p>
      </form>
    </details>

    <h2>Files</h2>
    {% if files|length == 0 %}
      <p>No files yet.</p>
    {% else %}
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th class="nowrap">Size</th>
            <th>Type</th>
            <th>Uploaded</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {% for f in files %}
          <tr>
            <td class="mono">{{ f.filename }}</td>
            <td class="nowrap">{{ "{:,}".format(f.size_bytes) }} B</td>
            <td>{{ f.content_type or "n/a" }}</td>
            <td class="nowrap">{{ f.uploaded_at }}</td>
            <td>
              <a href="/download/{{ f.id }}">Download</a>
              <form method="POST" action="/delete/{{ f.id }}" style="display:inline" onsubmit="return confirm('Delete this file?')">
                <button type="submit" class="secondary">Delete</button>
              </form>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    {% endif %}
  </main>
</body>
</html>
"""

from jinja2 import Environment, BaseLoader, select_autoescape
jinja_env = Environment(
    loader=BaseLoader(),
    autoescape=select_autoescape(["html", "xml"])
)

def render_index(rows):
    template = jinja_env.from_string(INDEX_HTML)
    return template.render(title=APP_TITLE, files=rows, max_mb=MAX_UPLOAD_MB)

# ---- Routes ----
@app.get("/", response_class=HTMLResponse)
def index():
    with engine.begin() as conn:
        rows = conn.execute(select(files).order_by(files.c.uploaded_at.desc())).mappings().all()
    return HTMLResponse(render_index(rows))

@app.post("/upload")
async def upload(file: UploadFile = File(...), request: Request = None):
    # Size guardrail (best-effort; reverse proxies should also enforce)
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_UPLOAD_MB * 1024 * 1024:
                raise HTTPException(status_code=413, detail="File too large")
        except ValueError:
            pass

    data = await file.read()
    size_bytes = len(data)
    if size_bytes == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    if size_bytes > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large")

    safe_name = file.filename or "unnamed"
    # Trim overly long names (DB column limit 512)
    safe_name = safe_name[:512]

    rec_id = uuid.uuid4()
    now = dt.datetime.now(dt.timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            insert(files).values(
                id=rec_id,
                filename=safe_name,
                content_type=file.content_type,
                size_bytes=size_bytes,
                data=data,
                uploaded_at=now,
            )
        )

    return RedirectResponse(url="/", status_code=303)

@app.get("/download/{file_id}")
def download(file_id: uuid.UUID):
    with engine.begin() as conn:
        row = conn.execute(select(files).where(files.c.id == file_id)).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Not found")

    def iter_bytes():
        # In real-world apps storing big files, stream/chunk from DB.
        # Here we keep it simple (already loaded).
        yield row["data"]

    headers = {
        "Content-Disposition": f'attachment; filename="{row["filename"]}"'
    }
    return StreamingResponse(
        iter_bytes(),
        media_type=row["content_type"] or "application/octet-stream",
        headers=headers
    )

@app.post("/delete/{file_id}")
def delete_file(file_id: uuid.UUID):
    with engine.begin() as conn:
        result = conn.execute(delete(files).where(files.c.id == file_id))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Not found")
    return RedirectResponse(url="/", status_code=303)
