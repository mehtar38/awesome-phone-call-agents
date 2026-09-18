"""
signcall -- an ASL accessibility layer over CALL-E.

Credentials are loaded here, at the package root, rather than inside
calle/__init__.py alone. They used to live only there, which meant importing
workflow/ WITHOUT importing calle/ (e.g. calling clinic_lookup.find_clinics()
directly) left APIFY_API_TOKEN unset and silently degraded a live clinic
search to the synthetic fallback. Loading once here means any entry point into
this package -- the API server, the text harness, a bare import -- sees the
same environment.

Explicit path, not a bare load_dotenv(): this package is always run from
apps/ (see README's naming-collision note), so the CWD is apps/, not
apps/signcall/ where .env actually lives.
"""

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
