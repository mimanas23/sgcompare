
# sgcompare
Web app for SG
=======
# Document Compare

A Flask application for authenticated PDF and modern Word (`.docx`) comparison. It reports text changes, whitespace changes, and detectable formatting changes (bold, italic, underline, font name, and font size). Whitespace is rendered with visible markers in the results table alongside line/column and surrounding context. PDF text and formatting are extracted from PDF text spans. Scanned/image-only PDFs require OCR and are not supported.

Passwords are stored as Werkzeug one-way password hashes in SQLite. Uploaded documents are processed for the request and are not retained as application files. Users are provisioned by an administrator through the protected Admin page or the Flask command line. The SQLite database is never exposed as a download or end-user route.

## Run locally

Use Python 3.11 or later. In PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:SECRET_KEY = (py -c "import secrets; print(secrets.token_hex(32))")
flask --app app create-user --admin
flask --app app run
```

Open `http://127.0.0.1:5000`. The first command prompts for the initial administrator username and password. From the Admin page, create user accounts and optional additional administrators.

## Deploy to Azure App Service (Linux)

This deployment uses the App Service built-in Python runtime and its persistent `/home` storage for SQLite. Keep the app at **one instance**: SQLite is not suitable for multiple scaled-out instances. For higher availability or horizontal scaling, migrate the user store to Azure SQL or another shared database.

1. Create an Azure resource group and App Service plan, then create a Linux web app configured for Python 3.12. For example, using Azure CLI (replace the names and region):

   ```powershell
   az login
   az group create --name doc-compare-rg --location eastus
   az appservice plan create --name doc-compare-plan --resource-group doc-compare-rg --is-linux --sku B1
   az webapp create --resource-group doc-compare-rg --plan doc-compare-plan --name <globally-unique-app-name> --runtime "PYTHON:3.12"
   ```

2. In the Azure portal, open the Web App's **Configuration → Application settings** and add:
   * `SECRET_KEY`: a randomly generated long secret (for example, generate with `python -c "import secrets; print(secrets.token_hex(32))"`). Do not commit it to source control.
   * `DATABASE_PATH`: `/home/data/users.sqlite3`
   * `FLASK_ENV`: `production`
   * `SCM_DO_BUILD_DURING_DEPLOYMENT`: `true`

   Save and restart the Web App. App Service provides persistent `/home` storage on Linux; the app creates `/home/data` and the database file on first start. Keep App Service **Always On** enabled where the plan supports it.

3. Set the App Service startup command in **Configuration → General settings → Startup Command** to:

   ```text
   gunicorn --bind=0.0.0.0:8000 --timeout 600 app:app
   ```

4. Deploy this folder from the repository or with the deployment method your team uses (for example, GitHub Actions or `az webapp up`). Include `app.py`, `requirements.txt`, `templates/`, and `static/`. Confirm deployment logs show the Python dependencies installed and Gunicorn started.

5. Create the initial administrator on the running service. In the Azure portal, open **Development Tools → SSH** for the web app and run:

   ```sh
   cd /home/site/wwwroot
   source antenv/bin/activate
   flask --app app create-user --admin
   ```

   The CLI prompts for the administrator name and password. The command uses the app's configured `DATABASE_PATH` and `SECRET_KEY`. Sign in at the app's HTTPS URL, then use **Admin** to create end-user accounts. Never place account passwords in deployment settings or source control.

6. Test HTTPS sign-in, admin account creation, and comparisons using representative PDF and DOCX files. Ensure the Web App's **HTTPS Only** setting is enabled. Configure monitoring/backups according to your organization's retention policy; the SQLite file contains password hashes and should be treated as sensitive.

## Scope and security notes

- The app accepts `.pdf` and `.docx` (not legacy binary `.doc`) files, up to 20 MB each.
- Differences are token-based and report inserted, removed, replaced, and formatting-changed tokens. Word formatting includes inherited bold/italic/underline, font name, and size. PDF comparison uses text-span metadata (bold/italic/font/size); underline is not reliably represented in PDF text metadata. PDF extraction depends on embedded text, so scanned/image-only PDFs require OCR.
- Word body paragraphs and tables are read; headers, footers, comments, and tracked-change history are not included.
- Flask-WTF CSRF protection, HttpOnly/SameSite cookies, secure cookies in production, Werkzeug password hashing, parameterized SQLite queries, and upload size limits are enabled. Azure must terminate requests over HTTPS.
- The SQLite file is accessible to the application identity and Azure operators with filesystem access; it is not directly accessible to authenticated end users through this app. Admin access is limited to account provisioning, not viewing stored password hashes.

