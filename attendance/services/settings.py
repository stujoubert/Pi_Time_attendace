import os
from pathlib import Path
from db import get_conn, get_setting, set_setting
from PIL import Image

# Always use absolute path relative to this file's location
_BASE_DIR  = Path(__file__).resolve().parent.parent
UPLOAD_DIR = _BASE_DIR / "static" / "uploads"
LOGO_NAME  = "company_logo.png"

def get_company_settings():
    conn = get_conn()
    rows = conn.execute("SELECT key,value FROM settings WHERE key LIKE 'company_%'").fetchall()
    conn.close()
    data = {r["key"]: r["value"] for r in rows}
    logo_path = UPLOAD_DIR / LOGO_NAME
    return {
        "name":     data.get("company_name", ""),
        "rfc":      data.get("company_rfc", ""),
        "logo_url": f"/static/uploads/{LOGO_NAME}" if logo_path.exists() else None,
    }

def save_company_settings(name, rfc, logo_file=None):
    set_setting("company_name", name)
    set_setting("company_rfc", rfc)
    if logo_file and logo_file.filename:
        _save_logo(logo_file)

def _save_logo(file_storage):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    img = Image.open(file_storage.stream).convert("RGBA")
    if img.height > 80:
        ratio = 80 / img.height
        img = img.resize((int(img.width * ratio), 80), Image.LANCZOS)
    img.save(UPLOAD_DIR / LOGO_NAME, format="PNG", optimize=True)
