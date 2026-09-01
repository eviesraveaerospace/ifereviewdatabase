#!/bin/bash
set -e

# Derive the account and location instead of assuming ubuntu:/home/ubuntu.
APP_USER="$(id -un)"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# requirements-lite.txt skips openai-whisper (and so needs no ffmpeg/torch).
# Pass --with-whisper only if caption coverage proves inadequate.
REQS="requirements-lite.txt"
if [ "$1" == "--with-whisper" ]; then
    REQS="requirements.txt"
    echo "==> Including openai-whisper (needs ffmpeg on PATH)"
fi

echo "==> Setting up Python environment in $APP_DIR"
cd "$APP_DIR"
python3 -m venv .venv
source .venv/bin/activate
pip install --quiet -r "$REQS"
pip install --quiet gunicorn

if [ ! -f .env ]; then
    echo "!! No .env found. Create one with YOUTUBE_API_KEY and ANTHROPIC_API_KEY"
    echo "   before starting the service, or gunicorn will fail to launch."
fi

echo "==> Scheduling daily crawl at 3 AM"
CRON_CMD="0 3 * * * $APP_DIR/.venv/bin/python $APP_DIR/daily_crawl.py >> $APP_DIR/crawl_log.txt 2>&1"
(crontab -l 2>/dev/null | grep -v daily_crawl; echo "$CRON_CMD") | crontab -

# Everything below needs root. Skip it with --no-root to run unprivileged
# via  .venv/bin/gunicorn --bind 0.0.0.0:5000 app:app  instead.
if [ "$1" == "--no-root" ] || [ "$2" == "--no-root" ]; then
    echo ""
    echo "Skipping systemd/nginx (--no-root). Start the app manually with:"
    echo "  $APP_DIR/.venv/bin/gunicorn --workers 2 --bind 0.0.0.0:5000 app:app"
    exit 0
fi

echo "==> Installing Flask app as a systemd service"
sed -e "s|__APP_USER__|$APP_USER|g" -e "s|__APP_DIR__|$APP_DIR|g" \
    deploy/ife-app.service | sudo tee /etc/systemd/system/ife-app.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable ife-app
sudo systemctl restart ife-app

echo "==> Configuring nginx"
sudo cp deploy/nginx.conf /etc/nginx/sites-available/ife-app
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf /etc/nginx/sites-available/ife-app /etc/nginx/sites-enabled/ife-app
sudo nginx -t
sudo systemctl restart nginx

echo ""
echo "Done! App is served by nginx on port 80 of this host."
echo "Daily crawl is scheduled for 3 AM server time."
echo ""
echo "To check app status:  sudo systemctl status ife-app"
echo "To view logs:         sudo journalctl -u ife-app -f"
echo "To view crawl log:    tail -f $APP_DIR/crawl_log.txt"
