# neurons/validator_utils/logging_config.py

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from datetime import datetime
import sys


def setup_validator_logging(log_level: str = "INFO"):
    """
    Configure validator logging with daily rotation
    All logs saved to file, all logs shown in console/nohup output
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
    """
    # Create logs directory if it doesn't exist
    log_dir = Path(os.getenv("CHIPFORGE_DATA_DIR", ".")).expanduser() / "logs"
    log_dir.mkdir(exist_ok=True, parents=True)
    
    # Convert log level string to logging constant
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    root_logger.handlers.clear()
    
    # Console handler - shows all logs (for nohup output)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # Try to set up file logging with daily rotation
    try:
        # Use base log filename (TimedRotatingFileHandler will add date suffix)
        log_file = log_dir / "validator.log"
        
        # File handler with daily rotation at midnight
        file_handler = TimedRotatingFileHandler(
            filename=str(log_file),
            when='midnight',  # Rotate at midnight
            interval=1,  # Every 1 day
            backupCount=30,  # Keep 30 days of logs
            encoding='utf-8',
            utc=False  # Use local time (Pakistan time)
        )
        # Date format for rotated files: validator.log.2025-11-01
        file_handler.suffix = "%Y-%m-%d"
        
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        
        logger = logging.getLogger(__name__)
        logger.info(f"📝 Validator logging configured - Level: {log_level}")
        logger.info(f"📂 Log file: {log_file.absolute()}")
        logger.info(f"📋 Console output: enabled (captured by nohup)")
        logger.info(f"🕐 Log rotation: daily at midnight (local time)")
        logger.info(f"📅 Current time: {datetime.now()}")
        
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"❌ Error setting up file logging: {e}")
        logger.warning("📝 Using console logging only")
    
    # Suppress noisy third-party loggers
    logging.getLogger('async_substrate_interface').setLevel(logging.WARNING)
    logging.getLogger('websockets').setLevel(logging.WARNING)
    logging.getLogger('substrate_interface').setLevel(logging.WARNING)
    logging.getLogger('scalecodec').setLevel(logging.WARNING)
    logging.getLogger('bittensor').setLevel(logging.INFO)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('aiohttp').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)
    
    return root_logger