import paramiko
import os
import subprocess
from pathlib import Path

WORKSPACE = Path(r"E:\git-project\dcBot")
REMOTE_DIR = "/home/titanium/dcBot"

# Deployment is intentionally source-only. These files are local notes,
# credentials, generated state, or private media and must never be uploaded.
DEPLOY_EXCLUDES = {
    ".env",
    "server_detail.txt",
    "server_settings.json",
    "CHANGELOG.md",
    "README.md",
    "CLAUDE.md",
    "deploy.py",
    "deploy.bat",
    "check_logs.py",
    "check_deploy.py",
    "fix_pot.bat",
    "iptv_lang_cache.json",
}
DEPLOY_EXCLUDE_DIRS = {
    ".git", ".freebuff", ".venv", "venv", "__pycache__", "audio", "node_modules"
}

def parse_server_details():
    details = {}
    path = "E:\\git-project\\dcBot\\server_detail.txt"
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if ":" in line:
                key, val = line.split(":", 1)
                details[key.strip().lower()] = val.strip()
    return details

def should_ignore(rel_path):
    parts = rel_path.replace("\\", "/").split("/")
    if any(part in DEPLOY_EXCLUDE_DIRS for part in parts[:-1]):
        return True
    # Ignore git, venv, pycache, and IDE configs
    if any(p in parts for p in [".git", ".venv", "venv", "__pycache__", ".idea", ".vscode", ".freebuff", "node_modules"]):
        return True
    
    base = parts[-1]
    # Ignore log files and lock files
    if base in DEPLOY_EXCLUDES or base.endswith(".log") or base.endswith(".lock") or base == "dc.lock":
        return True
        
    # Ignore local credentials, settings, and the deploy scripts themselves
    if base in ["server_detail.txt", "server_settings.json", "deploy.py", "deploy.bat", "check_logs.py"]:
        return True

    # Ignore any temp *_logs.py debug scripts
    if base.endswith("_logs.py"):
        return True

        
    # Ignore private audio mp3s
    if len(parts) >= 2 and parts[-2] == "audio" and base.endswith(".mp3"):
        return True
        
    return False

def get_all_files():
    """Return only changed files eligible for an incremental deployment."""
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        check=True,
    )
    files = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        rel_path = line[3:]
        # Rename entries use: old path -> new path. Upload the new path only.
        if " -> " in rel_path:
            rel_path = rel_path.rsplit(" -> ", 1)[1]
        rel_path = rel_path.strip().replace("\\", "/")
        if status == "!!" or should_ignore(rel_path):
            continue
        files.append(rel_path)
    return sorted(set(files))

def make_remote_dirs(sftp, remote_filepath):
    # Ensure remote directory structure exists
    dir_path = os.path.dirname(remote_filepath).replace("\\", "/")
    parts = dir_path.split('/')
    current = ""
    for part in parts:
        if not part:
            continue
        current += "/" + part
        try:
            sftp.stat(current)
        except FileNotFoundError:
            print(f"Creating remote directory: {current}")
            sftp.mkdir(current)

def run_remote(client, command, description=None):
    """Run a command on the VPS and wait for it to finish.

    Returns (exit_status, stdout, stderr). Unlike a bare exec_command this
    blocks on the exit status, so a failing step cannot be silently skipped
    while the deploy reports success.
    """
    if description:
        print(f"--> {description}")
    stdin, stdout, stderr = client.exec_command(command)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    status = stdout.channel.recv_exit_status()
    return status, out, err


def run_remote_sudo(client, command, password, description=None):
    """Run a sudo command on the VPS, feeding the sudo password to the tty."""
    if description:
        print(f"--> {description}")
    stdin, stdout, stderr = client.exec_command(command, get_pty=True)
    stdin.write(password + "\n")
    stdin.flush()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    status = stdout.channel.recv_exit_status()
    return status, out, err


def deploy():
    details = parse_server_details()
    host = details.get("host")
    username = details.get("username")
    password = details.get("password")
    
    if not host or not username or not password:
        print("Error: Missing host, username, or password in server_detail.txt")
        return
        
    files_to_upload = get_all_files()
    if not files_to_upload:
        print("No eligible changed files found. Nothing to deploy.")
        return
        
    print(f"Found {len(files_to_upload)} files to upload to VPS.")
    
    print(f"Connecting to SSH {host}...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        client.connect(host, username=username, password=password, timeout=20)
        print("SSH Connection successful!")
        
        local_dir = str(WORKSPACE)
        remote_dir = REMOTE_DIR
        
        sftp = client.open_sftp()
        for rel_path in files_to_upload:
            local_path = os.path.join(local_dir, rel_path.replace("/", "\\"))
            remote_path = remote_dir + "/" + rel_path
            
            # Ensure parent directories exist on remote
            make_remote_dirs(sftp, remote_path)
            
            print(f"Uploading {local_path} to {remote_path}...")
            sftp.put(local_path, remote_path)
            
        sftp.close()
        print("All files uploaded successfully!")
        
        # Clean remote python cache (__pycache__ and .pyc files)
        clean_command = (
            f"find {remote_dir} -name '__pycache__' -type d -exec rm -rf {{}} +; "
            f"find {remote_dir} -name '*.pyc' -delete"
        )
        run_remote(client, clean_command, "Cleaning remote python cache on VPS...")
        print("Python cache cleaned successfully!")

        # Install only when the Python PO-token plugin is missing. Reinstalling
        # it on every deployment can replace working dependencies mid-service.
        status, out, err = run_remote(
            client,
            f"cd {remote_dir} && .venv/bin/python -c 'import yt_dlp_plugins.extractor'",
            "Checking PO Token provider plugin...",
        )
        if status == 0:
            print("    OK: PO Token provider already installed; skipping pip install.")
        else:
            print("    MISSING: installing PO Token provider plugin...")
            status, out, err = run_remote(
                client,
                f"cd {remote_dir} && .venv/bin/pip install 'bgutil-ytdlp-pot-provider>=1.3.1'",
                "Installing PO Token provider plugin on VPS...",
            )
            if status != 0:
                print("WARNING: dependency install failed. Deploy continues, but "
                      "YouTube playback may stay unreliable.")
            if err:
                print(err[-1500:])

        # Make sure the POT server is up without issuing `start` against an
        # already-running process, which only produces a misleading error.
        status, out, err = run_remote_sudo(
            client, "sudo supervisorctl status bgutil-pot", password,
            "Checking bgutil-pot (POT server)...",
        )
        if "RUNNING" in out:
            print("    OK: bgutil-pot is already running; skipping start.")
        else:
            run_remote_sudo(
                client, "sudo supervisorctl start bgutil-pot", password,
                "Starting bgutil-pot (POT server)...",
            )

        # Install the local resolver's Node dependency only when it is missing or
        # not on the version required by server.mjs. node_modules is never
        # uploaded because it is platform-specific and large.
        status, out, err = run_remote(
            client,
            f"cd {remote_dir}/ytjs && node -e \"const v=require('./node_modules/youtubei.js/package.json').version; process.exit(v.startsWith('18.') ? 0 : 1)\"",
            "Checking local YouTube resolver dependency...",
        )
        if status == 0:
            print("    OK: youtubei.js 18.x already installed; skipping npm install.")
        else:
            status, out, err = run_remote(
                client,
                f"cd {remote_dir}/ytjs && npm install --omit=dev --no-audit --no-fund",
                "Installing local YouTube resolver dependency on VPS...",
            )
            if status != 0:
                print("WARNING: ytjs npm install failed. YouTube playback will have no "
                      "fallback if yt-dlp fails.")
                if err:
                    print(err[-1500:])

        # Make sure supervisor knows about the resolver. A first deploy onto a
        # server set up before ytjs existed has no such program, so write the
        # config rather than silently leaving the resolver unmanaged.
        status, out, err = run_remote_sudo(
            client, "sudo supervisorctl status dcbot-ytjs", password,
            "Checking dcbot-ytjs (local YouTube resolver)...",
        )
        if "RUNNING" not in out or "no such process" in (out + err).lower():
            print("    dcbot-ytjs is unknown to supervisor; creating its config...")
            conf = (
                "[program:dcbot-ytjs]\n"
                f"command=node {remote_dir}/ytjs/server.mjs\n"
                f"directory={remote_dir}/ytjs\n"
                f"user={username}\n"
                "autostart=true\n"
                "autorestart=true\n"
                f"stderr_logfile={remote_dir}/ytjs/err.log\n"
                f"stdout_logfile={remote_dir}/ytjs/out.log\n"
            )
            run_remote_sudo(
                client,
                "sudo tee /etc/supervisor/conf.d/dcbot_ytjs.conf > /dev/null <<'EOF'\n"
                + conf + "EOF",
                password,
                "Writing supervisor config for the local YouTube resolver...",
            )
            run_remote_sudo(client, "sudo supervisorctl reread && sudo supervisorctl update",
                            password, "Reloading supervisor...")
            status, out, err = run_remote_sudo(
                client, "sudo supervisorctl start dcbot-ytjs", password,
                "Starting dcbot-ytjs...",
            )
        else:
            # Pick up changed server.mjs without attempting a redundant start.
            run_remote_sudo(client, "sudo supervisorctl restart dcbot-ytjs", password,
                            "Restarting dcbot-ytjs to pick up the new resolver...")
        if out:
            print(f"    {out}")

        # Restart the bot last, so it starts against a synced venv and a live
        # POT server.
        status, out, err = run_remote_sudo(
            client, "sudo supervisorctl restart dcbot", password,
            "Restarting dcbot supervisor process...",
        )
        if out:
            print("STDOUT:")
            print(out)
        if err:
            print("STDERR:")
            print(err)

        status, out, err = run_remote_sudo(
            client, "sudo supervisorctl status dcbot bgutil-pot dcbot-ytjs", password,
            "Final service status:",
        )
        if out:
            print(out)

        client.close()
        print("Deployment finished successfully!")
    except Exception as e:
        print(f"Deployment error: {e}")

if __name__ == "__main__":
    deploy()
