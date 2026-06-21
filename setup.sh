#!/bin/bash
# ==============================================================================
# dcBot Host Setup Script
# Automatically configures Python, Node, Deno, POT Server, and Supervisor.
# ==============================================================================

set -euo pipefail

# Ensure script is run as root or with sudo
if [ "$EUID" -ne 0 ]; then
  echo "Error: Please run this script as root or with sudo:"
  echo "sudo bash $0"
  exit 1
fi

# Detect current non-root user (who ran sudo)
USER_NAME="${SUDO_USER:-$USER}"
USER_HOME=$(eval echo "~$USER_NAME")
BOT_DIR=$(pwd)

echo "===================================================="
echo " Starting dcBot host environment setup"
echo " Host User: $USER_NAME"
echo " Bot Directory: $BOT_DIR"
echo "===================================================="

# 1. Update system packages
echo "--> Updating system packages..."
apt-get update -y && apt-get upgrade -y

# 2. Install core system dependencies
echo "--> Installing core dependencies (curl, git, ffmpeg, supervisor)..."
apt-get install -y curl git ffmpeg supervisor python3 python3-pip python3-venv build-essential lsof

# 3. Install Node.js (v20 LTS)
if ! command -v node &> /dev/null; then
  echo "--> Installing Node.js..."
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
else
  echo "--> Node.js already installed ($(node -v))"
fi

# 4. Install Deno (needed for solving YouTube JS challenges)
if ! command -v deno &> /dev/null; then
  echo "--> Installing Deno..."
  curl -fsSL https://deno.land/install.sh | sh -
  # Make deno globally accessible
  ln -sf "$USER_HOME/.deno/bin/deno" /usr/local/bin/deno
  echo "Deno installed at /usr/local/bin/deno"
else
  echo "--> Deno already installed ($(deno --version | head -n 1))"
fi

# 5. Set up Proof-of-Origin Token (POT) server backend
echo "--> Deploying POT Server (bgutil-ytdlp-pot-provider)..."
cd "$USER_HOME"
if [ ! -d "bgutil-ytdlp-pot-provider" ]; then
  git clone https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git
  chown -R "$USER_NAME:$USER_NAME" bgutil-ytdlp-pot-provider
fi

cd bgutil-ytdlp-pot-provider/server
sudo -u "$USER_NAME" npm install
sudo -u "$USER_NAME" npm run build

# 6. Set up Python virtual environment and install bot dependencies
echo "--> Setting up Python virtual environment in $BOT_DIR..."
cd "$BOT_DIR"
sudo -u "$USER_NAME" python3 -m venv .venv

echo "--> Installing Python dependencies..."
sudo -u "$USER_NAME" .venv/bin/pip install --upgrade pip setuptools wheel
sudo -u "$USER_NAME" .venv/bin/pip install \
  "discord.py[voice]>=2.7.1" \
  "python-dotenv>=0.9.9" \
  "httpx>=0.28.1" \
  "websockets>=12.0" \
  "pycryptodome>=3.23.0" \
  "pynacl>=1.5.0" \
  "paramiko>=5.0.0" \
  "yt-dlp>=2026.6.9" \
  "bgutil-ytdlp-pot-provider>=1.3.1"

# 7. Create Supervisor Configuration Files
echo "--> Generating Supervisor configuration files..."

# Supervisor config for POT Server
cat <<EOF > /etc/supervisor/conf.d/bgutil_pot.conf
[program:bgutil-pot]
command=node $USER_HOME/bgutil-ytdlp-pot-provider/server/build/main.js --port 4416
directory=$USER_HOME/bgutil-ytdlp-pot-provider/server
user=$USER_NAME
autostart=true
autorestart=true
stderr_logfile=$USER_HOME/bgutil-ytdlp-pot-provider/server/err.log
stdout_logfile=$USER_HOME/bgutil-ytdlp-pot-provider/server/out.log
EOF

# Supervisor config for Discord Bot
cat <<EOF > /etc/supervisor/conf.d/dcbot.conf
[program:dcbot]
command=$BOT_DIR/.venv/bin/python $BOT_DIR/dc.py
directory=$BOT_DIR
user=$USER_NAME
autostart=true
autorestart=true
environment=PYTHONMALLOC=malloc
stderr_logfile=$BOT_DIR/err.log
stdout_logfile=$BOT_DIR/out.log
EOF

# 8. Reload and Start services via Supervisor
echo "--> Restarting Supervisor services..."
supervisorctl reread
supervisorctl update
supervisorctl restart bgutil-pot
supervisorctl restart dcbot

echo "===================================================="
echo " Setup complete!"
echo " Next Steps for the User:"
echo " 1. Create a '.env' file in '$BOT_DIR' with your DISCORD_TOKEN."
echo " 2. Log into your dedicated YouTube account, export the cookies"
echo "    in Netscape format, save them as 'cookies.txt' in '$BOT_DIR'."
echo " 3. Restart the bot using: sudo supervisorctl restart dcbot"
echo "===================================================="
