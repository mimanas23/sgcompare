import difflib
import io
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from reportlab.pdfgen import canvas
from PyPDF2 import PdfReader, PdfWriter

import click
import pymupdf
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from flask import Flask, flash, redirect, render_template, request, url_for
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_wtf import CSRFProtect, FlaskForm
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from wtforms import BooleanField, PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Length, Regexp


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".docx"}
TOKEN_PATTERN = re.compile(r"\s+|[^\W_]+(?:['’][^\W_]+)*|[^\w\s]", re.UNICODE)
USERNAME_PATTERN = r"^[a-zA-Z0-9_.@+-]+$"


def create_app():
    app = Flask(__name__)
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        raise RuntimeError("Set SECRET_KEY to a long, random value before starting the app.")

    default_db = Path(app.instance_path) / "app.sqlite3"
    app.config.update(
        SECRET_KEY=secret_key,
        DATABASE=os.environ.get("DATABASE_PATH", str(default_db)),
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production",
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production",
    )
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)

    CSRFProtect(app)
    login_manager = LoginManager(app)
    login_manager.login_view = "login"
    login_manager.login_message = "Please sign in to continue."

    @contextmanager
    def connect_db():
        connection = sqlite3.connect(app.config["DATABASE"])
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize_database():
        with connect_db() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1
                )
                """
            )

    class User(UserMixin):
        def __init__(self, row):
            self.id = row["id"]
            self.username = row["username"]
            self.is_admin = bool(row["is_admin"])
            self._is_active = bool(row["is_active"])

        @property
        def is_active(self):
            return self._is_active

    class LoginForm(FlaskForm):
        username = StringField("Username", validators=[DataRequired(), Length(max=80)])
        password = PasswordField("Password", validators=[DataRequired(), Length(max=128)])
        submit = SubmitField("Sign in")

    class CreateUserForm(FlaskForm):
        username = StringField(
            "Username",
            validators=[
                DataRequired(),
                Length(min=3, max=80),
                Regexp(USERNAME_PATTERN, message="Use letters, numbers, and . _ @ + - only."),
            ],
        )
        password = PasswordField(
            "Temporary password",
            validators=[DataRequired(), Length(min=12, max=128)],
        )
        is_admin = BooleanField("Administrator")
        submit = SubmitField("Create account")

    @login_manager.user_loader
    def load_user(user_id):
        with connect_db() as connection:
            row = connection.execute(
                "SELECT id, username, is_admin, is_active FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return User(row) if row else None

    def require_supported_file(upload):
        safe_name = secure_filename(upload.filename or "")
        suffix = Path(safe_name).suffix.lower()
        if not safe_name or suffix not in ALLOWED_EXTENSIONS:
            raise ValueError("Choose a PDF or modern Word (.docx) file for each document.")
        return safe_name, suffix

    def tokenize(text, style, output):
        output.extend(Token(match.group(0), style) for match in TOKEN_PATTERN.finditer(text))

    @dataclass(frozen=True)
    class Token:
        text: str
        style: tuple

    def style_label(style):
        return ", ".join(style) if style else "plain/default"

    def visible_whitespace(text):
        markers = {
            " ": "␠",
            "\t": "⇥",
            "\n": "↵",
            "\r": "␍",
            "\f": "␌",
            "\u00a0": "⍽",
        }
        return "".join(markers.get(character, character) for character in text)

    def token_locations(tokens, page_aware):
        locations = []
        line = column = page = 1
        for token in tokens:
            location = f"line {line}, column {column}"
            if page_aware:
                location = f"page {page}, {location}"
            locations.append(location)
            for character in token.text:
                if character == "\f":
                    page += 1
                    line = column = 1
                elif character == "\n":
                    line += 1
                    column = 1
                else:
                    column += 1
        return locations

    def token_context(tokens, index):
        if index is None:
            return ""
        start = max(0, index - 5)
        end = min(len(tokens), index + 6)
        excerpt = "".join(token.text for token in tokens[start:index])
        excerpt += f"⟦{tokens[index].text}⟧"
        excerpt += "".join(token.text for token in tokens[index + 1:end])
        return visible_whitespace(excerpt)

    def effective_run_property(run, property_name):
        value = getattr(run.font, property_name)
        if value is not None:
            return value
        for style in (run.style, run._parent.style):
            while style is not None:
                value = getattr(style.font, property_name)
                if value is not None:
                    return value
                style = style.base_style
        return None

    def docx_style(run):
        labels = []
        if effective_run_property(run, "bold") is True:
            labels.append("bold")
        if effective_run_property(run, "italic") is True:
            labels.append("italic")
        if effective_run_property(run, "underline") is True:
            labels.append("underline")
        font_name = effective_run_property(run, "name")
        font_size = effective_run_property(run, "size")
        if font_name:
            labels.append(f"font: {font_name}")
        if font_size:
            labels.append(f"size: {font_size.pt:g} pt")
        return tuple(labels)

    def extract_docx(stream):
        document = Document(stream)
        tokens = []

        def add_paragraph(paragraph):
            for run in paragraph.runs:
                tokenize(run.text, docx_style(run), tokens)
            tokenize("\n", (), tokens)

        def add_table(table):
            for row in table.rows:
                for cell in row.cells:
                    for block in cell._tc.iterchildren():
                        if block.tag.endswith("}p"):
                            add_paragraph(Paragraph(block, cell))
                        elif block.tag.endswith("}tbl"):
                            add_table(Table(block, cell))
                    tokenize("\n", (), tokens)

        for block in document.element.body.iterchildren():
            if block.tag.endswith("}p"):
                add_paragraph(Paragraph(block, document))
            elif block.tag.endswith("}tbl"):
                add_table(Table(block, document))
        return tokens

    def pdf_style(span):
        flags = span.get("flags", 0)
        labels = []
        if flags & 16:
            labels.append("bold")
        if flags & 2:
            labels.append("italic")
        if span.get("font"):
            labels.append(f"font: {span['font']}")
        if span.get("size"):
            labels.append(f"size: {span['size']:g} pt")
        return tuple(labels)

    def extract_pdf(stream):
        document = pymupdf.open(stream=stream, filetype="pdf")
        tokens = []
        try:
            if document.is_encrypted:
                raise ValueError("Encrypted PDFs are not supported.")
            for page_index, page in enumerate(document):
                if page_index:
                    tokenize("\f", (), tokens)
                page_data = page.get_text("dict", sort=True)
                for block in page_data["blocks"]:
                    if block.get("type") != 0:
                        continue
                    for line in block["lines"]:
                        for span in line["spans"]:
                            tokenize(span["text"], pdf_style(span), tokens)
                        tokenize("\n", (), tokens)
        finally:
            document.close()
        return tokens

    def extract_document(upload, suffix):
        data = upload.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("Each document must be 20 MB or smaller.")
        stream = io.BytesIO(data)
        try:
            if suffix == ".pdf":
                return extract_pdf(stream)
            return extract_docx(stream)
        except ValueError:
            raise
        except Exception as error:
            raise ValueError("The file could not be read. Check that it is a valid, unprotected PDF or DOCX.") from error

    def compare_documents(master, child, master_is_pdf=False, child_is_pdf=False):
        matcher = difflib.SequenceMatcher(
            a=[token.text for token in master],
            b=[token.text for token in child],
        )
        differences = []
        master_locations = token_locations(master, master_is_pdf)
        child_locations = token_locations(child, child_is_pdf)

        def add_difference(kind, master_index=None, child_index=None):
            master_token = master[master_index] if master_index is not None else None
            child_token = child[child_index] if child_index is not None else None
            master_text = master_token.text if master_token else ""
            child_text = child_token.text if child_token else ""
            differences.append(
                {
                    "kind": kind,
                    "master": visible_whitespace(master_text),
                    "child": visible_whitespace(child_text),
                    "master_style": style_label(master_token.style) if master_token else "",
                    "child_style": style_label(child_token.style) if child_token else "",
                    "master_location": master_locations[master_index] if master_index is not None else "",
                    "child_location": child_locations[child_index] if child_index is not None else "",
                    "master_context": token_context(master, master_index),
                    "child_context": token_context(child, child_index),
                }
            )

        for opcode, master_start, master_end, child_start, child_end in matcher.get_opcodes():
            if opcode == "equal":
                for master_index, child_index in zip(
                    range(master_start, master_end),
                    range(child_start, child_end),
                ):
                    master_token = master[master_index]
                    child_token = child[child_index]
                    if master_token.style != child_token.style:
                        add_difference("Formatting", master_index, child_index)
            elif opcode == "replace":
                paired = min(master_end - master_start, child_end - child_start)
                for index in range(paired):
                    master_index = master_start + index
                    child_index = child_start + index
                    old, new = master[master_index], child[child_index]
                    kind = (
                        "Whitespace changed"
                        if old.text.isspace() and new.text.isspace()
                        else "Text changed"
                    )
                    add_difference(kind, master_index, child_index)
                for master_index in range(master_start + paired, master_end):
                    token = master[master_index]
                    kind = "Whitespace removed" if token.text.isspace() else "Removed from child"
                    add_difference(kind, master_index)
                for child_index in range(child_start + paired, child_end):
                    token = child[child_index]
                    kind = "Whitespace added" if token.text.isspace() else "Added in child"
                    add_difference(kind, child_index=child_index)
            elif opcode == "delete":
                for master_index in range(master_start, master_end):
                    token = master[master_index]
                    kind = "Whitespace removed" if token.text.isspace() else "Removed from child"
                    add_difference(kind, master_index)
            elif opcode == "insert":
                for child_index in range(child_start, child_end):
                    token = child[child_index]
                    kind = "Whitespace added" if token.text.isspace() else "Added in child"
                    add_difference(kind, child_index=child_index)
        return differences

    # Function to highlight differences in the child PDF
    def highlight_pdf(input_pdf, output_pdf, differences):
        """Highlight differences in the child PDF."""
        reader = PdfReader(input_pdf)
        writer = PdfWriter()
        packet = io.BytesIO()
        can = canvas.Canvas(packet)

        for page_num, page in enumerate(reader.pages):
            for diff in differences:
                if diff.get("child_location") and diff.get("child_location").startswith(f"page {page_num + 1}"):
                    # Highlight text
                    can.setFillColor("red" if diff["kind"] == "Critical" else "yellow")
                    # Example coordinates for highlighting (adjust based on your PDF structure)
                    can.rect(100, 700, 200, 20, fill=1)
                    # Add comment box
                    can.drawString(100, 680, diff["kind"] + ": " + ["child_context"])

        can.save()
        packet.seek(0)
        overlay = PdfReader(packet)
        for page, overlay_page in zip(reader.pages, overlay.pages):
            page.merge_page(overlay_page)
            writer.add_page(page)

        with open(output_pdf, "wb") as f:
            writer.write(f)
        print(f"Highlighted PDF saved as {output_pdf}")
            
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("index"))
        form = LoginForm()
        if form.validate_on_submit():
            with connect_db() as connection:
                row = connection.execute(
                    "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                    (form.username.data.strip(),),
                ).fetchone()
            if row and row["is_active"] and check_password_hash(row["password_hash"], form.password.data):
                login_user(User(row))
                return redirect(url_for("index"))
            flash("Invalid username or password.", "error")
        return render_template("login.html", form=form)

    @app.post("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("login"))


    # Modify the `index` route to include PDF highlighting and download functionality
    @app.route("/", methods=["GET", "POST"])
    @login_required
    def index():
        differences = None
        master_name = child_name = None
        highlighted_pdf = None
        if request.method == "POST":
            master_upload = request.files.get("master_file")
            child_upload = request.files.get("child_file")
            try:
                if not master_upload or not child_upload:
                    raise ValueError("Select both a master and a child document.")
                master_name, master_suffix = require_supported_file(master_upload)
                child_name, child_suffix = require_supported_file(child_upload)
                master_tokens = extract_document(master_upload, master_suffix)
                child_tokens = extract_document(child_upload, child_suffix)
                differences = compare_documents(
                    master_tokens,
                    child_tokens,
                    master_is_pdf=master_suffix == ".pdf",
                    child_is_pdf=child_suffix == ".pdf",
                )
                # Highlight differences in the child PDF
                if child_suffix == ".pdf":
                    print(f"Highlighting differences in {child_name} and saving to highlighted_child.pdf")
                    highlighted_pdf = "highlighted_child.pdf"
                    highlight_pdf(child_upload, highlighted_pdf, differences)
            except ValueError as error:
                flash(str(error), "error")
        return render_template(
            "index.html",
            differences=differences,
            master_name=master_name,
            child_name=child_name,
            highlighted_pdf=highlighted_pdf,
        )

    @app.route("/admin", methods=["GET", "POST"])
    @login_required
    def admin():
        if not current_user.is_admin:
            return "Forbidden", 403
        form = CreateUserForm()
        if form.validate_on_submit():
            username = form.username.data.strip()
            try:
                with connect_db() as connection:
                    connection.execute(
                        "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
                        (
                            username,
                            generate_password_hash(form.password.data),
                            int(form.is_admin.data),
                        ),
                    )
                flash(f"Account created for {username}.", "success")
                return redirect(url_for("admin"))
            except sqlite3.IntegrityError:
                form.username.errors.append("That username already exists.")
        with connect_db() as connection:
            users = connection.execute(
                "SELECT username, is_admin, is_active FROM users ORDER BY username COLLATE NOCASE"
            ).fetchall()
        return render_template("admin.html", form=form, users=users)

    @app.route("/download/<filename>")
    @login_required
    def download_file(filename):
        file_path = os.path.join(app.instance_path, filename)
        if os.path.exists(file_path):
            return send_file(file_path, as_attachment=True)
        else:
            flash("File not found.", "error")
            return redirect(url_for("index"))
        
    @app.cli.command("create-user")
    @click.option("--admin", "is_admin", is_flag=True, help="Create an administrator account.")
    def create_user(is_admin):
        """Create a user account from the command line."""
        username = click.prompt("Username").strip()
        password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
        if len(username) < 3 or len(username) > 80 or not re.fullmatch(USERNAME_PATTERN, username):
            raise click.ClickException("Username must be 3-80 characters and use letters, numbers, . _ @ + - only.")
        if len(password) < 12 or len(password) > 128:
            raise click.ClickException("Password must be between 12 and 128 characters.")
        try:
            with connect_db() as connection:
                connection.execute(
                    "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
                    (username, generate_password_hash(password), int(is_admin)),
                )
        except sqlite3.IntegrityError as error:
            raise click.ClickException("That username already exists.") from error
        click.echo(f"Created {'administrator' if is_admin else 'user'} account: {username}")

    with app.app_context():
        initialize_database()

    return app


app = create_app()
