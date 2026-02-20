import asyncio
import json
import os
from http import HTTPStatus
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from stage_complete import StageIdentityEngine, format_stage_message

# Get bot token from environment variable
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

# Initialize engine
engine = StageIdentityEngine()

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    welcome_message = """
🎬 **Stage Identity Bot Pro** 

🔥 *Welcome to the ultimate Stage content extractor!*

I can instantly extract comprehensive information from any Stage URL with professional accuracy.

⚡ **Quick Start:**
• `/stage <url>` - Extract complete metadata
• `/help` - View all commands

📽️ **Example:**
`/stage https://www.stage.in/en/haryanvi/movie/kayantar-14145`

🚀 **Pro Features:**
✅ Smart Movie/Series detection
✅ Auto poster extraction (Landscape + Portrait)
✅ Episode count for series
✅ Complete metadata extraction
✅ Duration format conversion
✅ Multi-language support

🎯 *Just paste any Stage URL and watch the magic happen!*
"""
    await update.message.reply_text(welcome_message, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_message = """
🎬 **Stage Identity Bot Help**

**Commands:**
• `/stage <url>` - Extract information from Stage URL
• `/start` - Welcome message
• `/help` - Show this help

**Usage:**
1. Copy any Stage URL
2. Send: `/stage <paste-url>`
3. Get detailed information with poster!

**Supported URLs:**
• Movies: `https://www.stage.in/.../movie/...`
• Series: `https://www.stage.in/.../series/...`
• All language content

**Extracted Information:**
• Title & Description
• Type (Movie/Series)
• Release Date
• Duration
• Genre
• Languages
• Episode Count (for series)
• Stage Internal ID
• Landscape & Portrait Posters

**Troubleshooting:**
• Make sure URL is from stage.in
• Check if URL is accessible
• Try again if timeout occurs
"""
    await update.message.reply_text(help_message, parse_mode='Markdown')

async def stage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stage command"""
    # Check if URL is provided
    if len(context.args) < 1:
        await update.message.reply_text(
            "❌ Please provide a Stage URL\n\n"
            "Usage: `/stage <url>`\n\n"
            "Example: `/stage https://www.stage.in/en/haryanvi/movie/kayantar-14145`",
            parse_mode='Markdown'
        )
        return
    
    url = context.args[0]
    
    # Validate URL
    if not validate_stage_url(url):
        await update.message.reply_text(
            "❌ Invalid Stage URL\n\n"
            "Please provide a valid Stage URL:\n"
            "• Must be from stage.in domain\n"
            "• Should contain movie or series content",
            parse_mode='Markdown'
        )
        return
    
    # Send processing message
    processing_msg = await update.message.reply_text("🔄 Processing Stage URL...")
    
    try:
        # Extract data
        result = engine.get_stage_identity(url)
        
        if not result.get("success"):
            error_msg = f"❌ Failed to extract data\n\n"
            if result.get("error"):
                error_msg += f"Error: {result['error']}"
            else:
                error_msg += "Could not extract information from the provided URL"
            
            await processing_msg.edit_text(error_msg)
            return
        
        # Format message
        message = format_stage_message(result)
        
        # Try to send with poster
        poster_url = result.get("landscape_poster") or result.get("portrait_poster")
        
        if poster_url:
            try:
                await update.message.reply_photo(
                    photo=poster_url,
                    caption=message,
                    parse_mode='Markdown'
                )
                await processing_msg.delete()
            except Exception as photo_error:
                print(f"Photo sending failed: {photo_error}")
                # Fallback to text message
                await processing_msg.edit_text(message, parse_mode='Markdown')
        else:
            # No poster available, send text message
            await processing_msg.edit_text(message, parse_mode='Markdown')
    
    except Exception as e:
        error_message = f"❌ An error occurred\n\nError: {str(e)}"
        await processing_msg.edit_text(error_message)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages (auto-detect Stage URLs)"""
    text = update.message.text
    
    # Check if message contains a Stage URL
    if "stage.in" in text:
        urls = extract_urls_from_text(text)
        stage_urls = [url for url in urls if validate_stage_url(url)]
        
        if stage_urls:
            await update.message.reply_text(
                "🎬 Detected Stage URL! Use `/stage <url>` to extract information.",
                parse_mode='Markdown'
            )

def validate_stage_url(url: str) -> bool:
    """Validate if URL is a valid Stage URL"""
    try:
        return "stage.in" in url.lower() and ("movie" in url.lower() or "series" in url.lower() or "-" in url)
    except:
        return False

def extract_urls_from_text(text: str):
    """Extract URLs from text"""
    import re
    url_pattern = r'https?://[^\s<>"{}|\\^`[\]]+'
    return re.findall(url_pattern, text)

# Vercel handler function
async def handler(event, context):
    """Vercel serverless function handler"""
    try:
        # Parse the request
        body = json.loads(event['body'])
        
        # Create bot instance
        bot = Bot(token=BOT_TOKEN)
        
        # Create update object
        update = Update.de_json(body, bot)
        
        # Create application
        application = Application.builder().token(BOT_TOKEN).build()
        
        # Add handlers
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("stage", stage_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
        
        # Process update
        await application.initialize()
        await application.process_update(update)
        await application.shutdown()
        
        return {
            'statusCode': 200,
            'body': json.dumps({'message': 'OK'})
        }
        
    except Exception as e:
        print(f"Error processing update: {e}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }

# For local testing
if __name__ == "__main__":
    print("This bot is designed for Vercel deployment with webhooks")
    print("Set TELEGRAM_BOT_TOKEN environment variable")
    print("Use /setwebhook command with your Vercel URL")