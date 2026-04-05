# 🎬 CineHub — Stage.in Telegram Download Bot

Automated video scraper & downloader for Stage.in, packaged as a Telegram bot.

## Features

- 🔐 Login once via phone + OTP — session persists
- 📥 Downloads any movie/episode from Stage.in
- 📊 Quality selection: 4K / 1080p / 720p / 480p / 360p
- 🔒 Signed HLS token forwarding (bypasses CloudFront auth)
- 🎵 Auto-merges video + audio with ffmpeg
- 📤 Uploads small files to Telegram, saves large files to disk
- 🖥️ VPS-ready — headless Chromium, no GUI needed

## Quick Start (Local)

```bash
# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Set bot token (get from @BotFather on Telegram)
# Windows:
set CINEHUB_TOKEN=your_token_here
# Linux:
export CINEHUB_TOKEN=your_token_here

# Run
python cinehub.py
```

## VPS Deployment (Ubuntu/Debian)

```bash
# 1. System dependencies
sudo apt update && sudo apt install -y python3 python3-pip ffmpeg

# 2. Clone/upload files
mkdir ~/cinehub && cd ~/cinehub
# Upload cinehub.py and requirements.txt

# 3. Install Python deps
pip3 install -r requirements.txt
playwright install chromium
playwright install-deps  # installs system libraries for headless Chrome

# 4. Set token
export CINEHUB_TOKEN="your_bot_token_here"

# 5. Run with screen/tmux (persists after SSH disconnect)
screen -S cinehub
python3 cinehub.py
# Press Ctrl+A, D to detach
```

### Run as systemd service (auto-start on boot)

```bash
sudo tee /etc/systemd/system/cinehub.service > /dev/null << 'EOF'
[Unit]
Description=CineHub Telegram Bot
After=network.target

[Service]
Type=simple
User=your_username
WorkingDirectory=/home/your_username/cinehub
Environment=CINEHUB_TOKEN=your_bot_token_here
ExecStart=/usr/bin/python3 /home/your_username/cinehub/cinehub.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable cinehub
sudo systemctl start cinehub
sudo systemctl status cinehub  # check status
sudo journalctl -u cinehub -f  # view logs
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome + status |
| `/login` | Login with phone + OTP |
| `/login_manual` | Import session from browser |
| `/downloads` | List downloaded files |
| `/help` | Usage guide |

## How It Works

1. **Login**: Headless Chromium navigates to Stage.in, handles phone+OTP login, saves cookies to `session.json`
2. **Scrape**: Opens the watch page with saved session, intercepts the signed `playlist.m3u8` URL from network traffic
3. **Download**: Parses the HLS manifest, downloads every segment individually with CloudFront auth tokens appended
4. **Merge**: Uses ffmpeg to combine video + audio into a single MP4
5. **Deliver**: Uploads to Telegram if <50MB, otherwise saves to `downloads/` folder

## Files

```
cinehub.py          # Main bot (all-in-one)
requirements.txt    # Python dependencies
session.json        # Saved login session (auto-created)
downloads/          # Downloaded videos (auto-created)
```

## Security

- Set `ALLOWED_USERS` in `cinehub.py` to restrict access to specific Telegram user IDs
- `session.json` contains your Stage.in login cookies — keep it private
- The bot runs on your machine/VPS — no data leaves your server

## Troubleshooting

| Issue | Fix |
|-------|-----|
| "No session" | Use `/login` to authenticate |
| "m3u8 not found" | Session expired → `/login` again |
| Download fails | Token expired mid-download, try again |
| 403 Forbidden | Signed URL expired — the bot auto-handles this |
| Bot not responding | Check `CINEHUB_TOKEN` is set correctly |
