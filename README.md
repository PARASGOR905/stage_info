# 🎬 Stage Identity Engine

A comprehensive tool for extracting metadata from Stage.in URLs with Telegram bot integration.

## 🚀 Features

- ✅ **Smart Content Detection**: Automatically detects Movies vs Series
- ✅ **Complete Metadata Extraction**: Title, description, duration, genre, languages
- ✅ **Poster Detection**: Extracts both landscape and portrait posters
- ✅ **Episode Count**: For series content
- ✅ **Telegram Bot Integration**: `/stage <url>` command
- ✅ **Multiple Input Methods**: Interactive mode, test suite, and bot mode
- ✅ **Dual-layer Parsing**: Next.js data + JSON-LD structured data

## 📋 Requirements

```bash
pip install -r requirements.txt
```

## 🛠️ Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/PARASGOR905/stage_info.git
   cd stage_info
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Set up Telegram bot (optional):
   - Create a bot with @BotFather on Telegram
   - Get your BOT_TOKEN
   - Set environment variable: `TELEGRAM_BOT_TOKEN=your_token_here`

## 🎯 Usage

### Command Line Modes:

```bash
# Test mode - Run predefined tests
python stage_complete.py test

# Interactive mode - Test URLs manually
python stage_complete.py interactive

# Telegram bot mode
python stage_complete.py bot

# Help information
python stage_complete.py help
```

### Telegram Bot Commands:

- `/start` - Welcome message
- `/help` - Show help information
- `/stage <url>` - Extract information from Stage URL

### Example Usage:

```python
from stage_complete import StageIdentityEngine

engine = StageIdentityEngine()
result = engine.get_stage_identity("https://www.stage.in/en/gujarati/movie/nasoor-15995")
print(result)
```

## 📁 File Structure

```
stage_info/
├── stage_complete.py     # Main engine with Telegram bot
├── requirements.txt      # Python dependencies
├── README.md            # This file
└── .gitignore          # Git ignore rules
```

## 🎯 Supported Platforms

- Stage.in movies and series
- All language content (Hindi, Gujarati, Haryanvi, etc.)
- Automatic poster extraction
- Metadata parsing from multiple sources

## 🤖 Telegram Bot Setup

1. Create a new bot with @BotFather
2. Get your bot token
3. Run the bot:
   ```bash
   python stage_complete.py bot YOUR_BOT_TOKEN
   ```
   Or set `TELEGRAM_BOT_TOKEN` environment variable

## 📊 Output Format

```json
{
  "stage_id": "15995",
  "type": "Movie",
  "title": "Nasoor (Gujarati)",
  "description": "Harshvardhan, at the peak of his achievements...",
  "release_date": "2026",
  "duration": "1h 53m",
  "genre": null,
  "languages": "Gujarati",
  "episode_count": null,
  "landscape_poster": "https://media.stage.in/...",
  "portrait_poster": "https://media.stage.in/...",
  "url": "https://www.stage.in/en/gujarati/movie/nasoor-15995",
  "success": true
}
```

## 🛡️ Error Handling

- Automatic fallback between parsing methods
- Graceful handling of network errors
- Validation of input URLs
- Detailed error messages

## 📝 License

MIT License

## 🙏 Acknowledgments

- Built for Stage.in content extraction
- Uses advanced web scraping techniques
- Telegram bot integration powered by python-telegram-bot