#!/bin/bash
# ==============================================================================
# dcBot Host Setup Script
# Automatically configures Python, Node, Deno, POT Server, Cloudflare WARP,
# the YouTube resolver, the automatic cookie refresher, and Supervisor.
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

# 3. Install Node.js 22 LTS. The YouTube resolver routes its traffic through
#    YOUTUBE_PROXY with NODE_USE_ENV_PROXY, which needs Node >= 22.21; on older
#    Node the proxy is silently ignored and YouTube blocks the server again.
NODE_OK=0
if command -v node &> /dev/null; then
  node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||(a===22&&b>=21)?0:1)' && NODE_OK=1
fi
if [ "$NODE_OK" -eq 0 ]; then
  echo "--> Installing Node.js 22..."
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
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
sudo -u "$USER_NAME" .venv/bin/pip install -r requirements.txt

# 7. Install the local YouTube resolver (youtubei.js, primary YouTube path) and
#    the headless Chromium used by the automatic cookie refresher.
echo "--> Installing YouTube resolver + cookie refresher dependencies (ytjs)..."
cd "$BOT_DIR/ytjs"
sudo -u "$USER_NAME" npm install
sudo -u "$USER_NAME" npx playwright install chromium
npx playwright install-deps chromium
cd "$BOT_DIR"

# 8. Cloudflare WARP in proxy mode (127.0.0.1:40000). YouTube blocks most
#    datacenter IPs; routing only YouTube traffic through WARP fixes that
#    without any cookies. Proxy mode leaves SSH and all other traffic alone.
echo "--> Installing Cloudflare WARP (proxy mode)..."
if ! command -v warp-cli &> /dev/null; then
  curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" > /etc/apt/sources.list.d/cloudflare-client.list
  apt-get update -y && apt-get install -y cloudflare-warp
fi
warp-cli --accept-tos registration show &> /dev/null || warp-cli --accept-tos registration new
warp-cli --accept-tos mode proxy
warp-cli --accept-tos proxy port 40000
warp-cli --accept-tos connect

# Make sure .env exists and points YouTube traffic at WARP.
if [ ! -f "$BOT_DIR/.env" ]; then
  sudo -u "$USER_NAME" cp "$BOT_DIR/.env.example" "$BOT_DIR/.env"
fi
grep -q '^YOUTUBE_PROXY=' "$BOT_DIR/.env" || echo 'YOUTUBE_PROXY=http://127.0.0.1:40000' >> "$BOT_DIR/.env"

# 9. Automatic cookie refresher (systemd, low priority). Every few hours a
#    headless Chromium opens YouTube through WARP and atomically replaces
#    cookies.txt -- nobody has to export cookies by hand.
echo "--> Installing the automatic cookie refresher service..."
cat <<EOF > /etc/systemd/system/dcbot-cookies.service
[Unit]
Description=dcBot YouTube cookie refresher
After=network-online.target warp-svc.service
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$BOT_DIR/ytjs
ExecStart=$(command -v node) $BOT_DIR/ytjs/cookie_refresher.mjs
Restart=always
RestartSec=30
Nice=19
IOSchedulingClass=idle
CPUQuota=50%
MemoryMax=450M
StandardOutput=append:$BOT_DIR/ytjs/cookies.out.log
StandardError=append:$BOT_DIR/ytjs/cookies.err.log

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now dcbot-cookies

# 10. Create Supervisor Configuration Files
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

# Supervisor config for the local YouTube resolver. Binds to 127.0.0.1 only.
cat <<EOF > /etc/supervisor/conf.d/dcbot_ytjs.conf
[program:dcbot-ytjs]
command=node $BOT_DIR/ytjs/server.mjs
directory=$BOT_DIR/ytjs
user=$USER_NAME
autostart=true
autorestart=true
stderr_logfile=$BOT_DIR/ytjs/err.log
stdout_logfile=$BOT_DIR/ytjs/out.log
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

# 11. Reload and Start services via Supervisor
echo "--> Restarting Supervisor services..."
supervisorctl reread
supervisorctl update
supervisorctl restart bgutil-pot
supervisorctl restart dcbot-ytjs
supervisorctl restart dcbot

echo "===================================================="
echo " Setup complete!"
echo " Next Steps for the User:"
echo " 1. Put your DISCORD_TOKEN in '$BOT_DIR/.env'."
echo " 2. Restart the bot using: sudo supervisorctl restart dcbot"
echo " No cookies needed: WARP + the automatic cookie refresher handle YouTube."
echo "===================================================="
