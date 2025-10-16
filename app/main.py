import os
import uuid
import datetime as dt
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
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
    Column("data", LargeBinary, nullable=True),
    Column("path", String(1024), nullable=True),
    Column("storage_type", String(16), nullable=False, default="db"),
    Column("uploaded_at", DateTime(timezone=True), nullable=False),
)

with engine.begin() as conn:
    metadata.create_all(conn)
    try:
        conn.execute(text("ALTER TABLE files ADD COLUMN IF NOT EXISTS path TEXT"))
        conn.execute(text("ALTER TABLE files ADD COLUMN IF NOT EXISTS storage_type VARCHAR(16) DEFAULT 'db'"))
    except Exception as e:
        print(f"Schema check warning: {e}")

# -----------------------
# FastAPI app
# -----------------------

app = FastAPI(title=APP_TITLE)
templates = Jinja2Templates(directory="app/templates")

# -----------------------
# Routes
# -----------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with engine.begin() as conn:
        rows = conn.execute(select(files).order_by(files.c.uploaded_at.desc())).mappings().all()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "title": APP_TITLE,
        "files": rows,
        "max_mb": MAX_UPLOAD_MB,
    })


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
def delete_file(file_id: uuid.UUID):
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

    # Redirect back to the index page
    return RedirectResponse(url="/", status_code=303)
