import csv
import logging
import os
import shutil
import uuid
import click
import sqlite3
from dataclasses import asdict
from pathlib import Path
from contextlib import contextmanager
from wtforms import BooleanField, PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Length, Regexp


from flask import Flask, abort, render_template_string, request, send_file, url_for
from flask import flash, redirect, render_template
from flask_wtf import CSRFProtect, FlaskForm
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from library.pdf_compare import compare_pdfs


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
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
        RESULTS_FOLDER=os.path.join(app.instance_path, "results")
    )
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    Path(app.config["RESULTS_FOLDER"]).parent.mkdir(parents=True, exist_ok=True)

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


    def _render_page(error=None, download_url=None, difference_count=None):
        return render_template(
            "index.html",
            error=error,
            download_url=download_url,
            difference_count=difference_count,
        )

    @app.get("/")
    @login_required
    def index():
        return _render_page()
    
    # Modify the `index` route to include PDF highlighting and download functionality
    @app.post("/compare")
    @login_required
    def compare():

        master = request.files.get("master")
        child = request.files.get("child")            
        if not master or not child or not master.filename or not child.filename:
            return _render_page(error="Select both a master PDF and a child PDF."), 400
        if not master.filename.lower().endswith(".pdf") or not child.filename.lower().endswith(".pdf"):
            return _render_page(error="Both uploaded files must be PDFs."), 400

        run_id = uuid.uuid4().hex
        run_dir = os.path.join(app.config["RESULTS_FOLDER"], run_id)
        os.makedirs(run_dir, exist_ok=True)
        output_path = os.path.join(run_dir, "child_highlighted.pdf")

        try:
            result = compare_pdfs(master.stream, child.stream, output_path)
            #report_path = os.path.join(run_dir, "diffs.csv")
            #rows = [asdict(difference) for difference in result.differences]
            #with open(report_path, "w", newline="", encoding="utf-8") as report_file:
            #    writer = csv.DictWriter(
            #        report_file,
            #        fieldnames=list(rows[0]) if rows else ["id"],
            #    )
            #    writer.writeheader()
            #   writer.writerows(rows)
        except Exception:
            logging.exception("PDF comparison failed")
            shutil.rmtree(run_dir, ignore_errors=True)
            return _render_page(error="Comparison failed. Check that both files are valid PDFs and try again."), 400

        #return _render_page(
        #    download_url=url_for("download_pdf", run_id=run_id),
        #    difference_count=len(result.differences),
	    #)
        return _render_page(
		    download_url=url_for("download_pdf", run_id=run_id),
		    difference_count=len(result.differences),
	        )

    @app.get("/download/<run_id>")
    @login_required
    def download_pdf(run_id):
        try:
            run_id = uuid.UUID(run_id).hex
        except ValueError:
            abort(404)
        output_path = os.path.join(app.config["RESULTS_FOLDER"], run_id, "child_highlighted.pdf")
        if not os.path.isfile(output_path):
            abort(404)
        response = send_file(output_path, as_attachment=True, download_name="child_highlighted.pdf")

        def remove_result_folder():
            try:
                shutil.rmtree(os.path.dirname(output_path))
            except OSError:
                app.logger.exception("Failed to remove completed result folder for run %s", run_id)

        response.call_on_close(remove_result_folder)
        return response


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
