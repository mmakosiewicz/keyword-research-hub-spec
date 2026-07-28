from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; APP=(ROOT/'app.py').read_text(); WORKER=(ROOT/'worker.py').read_text(); TEMPLATE=(ROOT/'templates'/'index.html').read_text()
def test_fail_closed_auth_and_secret():
    assert 'required_env("SECRET_KEY")' in APP and 'APP_PASSWORD_HASH' in APP and 'auth_required' in APP
def test_safe_runtime_defaults():
    assert 'os.getenv("HOST","127.0.0.1")' in APP and 'debug=False' in APP and 'MAX_CONTENT_LENGTH' in APP
def test_csrf_and_upload_limits():
    assert 'CSRFProtect(app)' in APP and 'X-CSRFToken' in TEMPLATE and '100000' in APP and 'utf-8-sig' in APP
def test_no_user_supplied_url_fetching():
    assert 'requests.get' not in APP and 'requests.get' not in WORKER and 'allow_redirects=False' in WORKER
def test_run_id_path_guard():
    assert 'run_id.isalnum()' in APP and 'rid.isalnum()' in WORKER
def test_no_known_private_identifiers():
    combined=APP+WORKER+TEMPLATE+(ROOT/'README.md').read_text()
    for token in ['/home/agent','/home/console','mateusz','ahrefs.com','2278974','github_main','ahrefs_oauth','127.0.0.1:18080','127.0.0.1:18081']:
        assert token.lower() not in combined.lower()
def test_template_escapes_imported_text():
    assert 'const esc=' in TEMPLATE and 'esc(r.keyword)' in TEMPLATE and 'esc(r.cluster' in TEMPLATE
