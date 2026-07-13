"""
SoftwarePro - Production SaaS Starter Application
Entry point for the application server and CLI commands.

This module serves as the main entry point for:
- Development server startup
- Production server initialization
- Database migrations and seeding
- CLI administrative commands
- Health checks and monitoring setup
"""

import asyncio
import logging
import os
import signal
import sys
from typing import Optional, NoReturn
from pathlib import Path

import click
import uvicorn
from dotenv import load_dotenv

# Add project root to Python path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import settings
from app.core.database import engine, async_session_maker
from app.core.logger import setup_logging
from app.db.init_db import init_db, seed_development_data
from app.services.stripe_service import StripeService
from app.services.email_service import EmailService
from app.tasks.scheduler import start_scheduler, stop_scheduler

# Load environment variables
load_dotenv()

# Setup logging
logger = setup_logging()


class GracefulShutdown:
    """Handle graceful shutdown of the application."""
    
    def __init__(self) -> None:
        self.should_exit = False
        self.force_exit = False
        
    def handle_signal(self, sig: int, frame) -> None:
        """Handle shutdown signals."""
        if self.should_exit:
            logger.warning("Forcing immediate shutdown...")
            self.force_exit = True
            sys.exit(1)
        
        logger.info(f"Received signal {sig}, initiating graceful shutdown...")
        self.should_exit = True


shutdown_handler = GracefulShutdown()


async def startup_checks() -> bool:
    """
    Perform startup health checks before running the application.
    
    Returns:
        bool: True if all checks pass, False otherwise
    """
    logger.info("Running startup checks...")
    
    try:
        # Check database connection
        async with engine.begin() as conn:
            await conn.execute("SELECT 1")
        logger.info("✓ Database connection successful")
        
        # Check Stripe configuration
        if settings.STRIPE_SECRET_KEY:
            stripe_service = StripeService()
            if await stripe_service.verify_connection():
                logger.info("✓ Stripe API connection successful")
            else:
                logger.warning("⚠ Stripe API connection failed")
        else:
            logger.warning("⚠ Stripe not configured")
        
        # Check email service
        email_service = EmailService()
        if email_service.is_configured():
            logger.info("✓ Email service configured")
        else:
            logger.warning("⚠ Email service not configured")
        
        return True
        
    except Exception as e:
        logger.error(f"✗ Startup checks failed: {str(e)}")
        return False


async def shutdown_cleanup() -> None:
    """Cleanup resources during shutdown."""
    logger.info("Performing cleanup tasks...")
    
    try:
        # Stop background scheduler
        await stop_scheduler()
        logger.info("✓ Scheduler stopped")
        
        # Close database connections
        await engine.dispose()
        logger.info("✓ Database connections closed")
        
        logger.info("Cleanup completed successfully")
        
    except Exception as e:
        logger.error(f"Error during cleanup: {str(e)}")


@click.group()
def cli() -> None:
    """SoftwarePro - Production SaaS Starter CLI"""
    pass


@cli.command()
@click.option("--host", default="0.0.0.0", help="Host to bind to")
@click.option("--port", default=8000, help="Port to bind to")
@click.option("--reload", is_flag=True, help="Enable auto-reload for development")
@click.option("--workers", default=1, help="Number of worker processes")
def serve(host: str, port: int, reload: bool, workers: int) -> None:
    """Start the application server."""
    
    # Register signal handlers
    signal.signal(signal.SIGINT, shutdown_handler.handle_signal)
    signal.signal(signal.SIGTERM, shutdown_handler.handle_signal)
    
    logger.info(f"Starting SoftwarePro server on {host}:{port}")
    logger.info(f"Environment: {settings.ENVIRONMENT}")
    logger.info(f"Debug mode: {settings.DEBUG}")
    
    # Run startup checks in production
    if settings.ENVIRONMENT == "production":
        loop = asyncio.get_event_loop()
        checks_passed = loop.run_until_complete(startup_checks())
        
        if not checks_passed:
            logger.error("Startup checks failed. Exiting...")
            sys.exit(1)
    
    # Configure uvicorn
    config = uvicorn.Config(
        "app.main:app",
        host=host,
        port=port,
        reload=reload,
        workers=workers if not reload else 1,
        log_level=settings.LOG_LEVEL.lower(),
        access_log=True,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    
    server = uvicorn.Server(config)
    
    try:
        server.run()
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    finally:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(shutdown_cleanup())


@cli.command()
@click.option("--drop", is_flag=True, help="Drop all tables before creating")
async def init_database(drop: bool) -> None:
    """Initialize the database schema."""
    
    try:
        logger.info("Initializing database...")
        
        if drop:
            logger.warning("Dropping all existing tables...")
            async with engine.begin() as conn:
                from app.db.base import Base
                await conn.run_sync(Base.metadata.drop_all)
        
        await init_db()
        logger.info("✓ Database initialized successfully")
        
    except Exception as e:
        logger.error(f"Failed to initialize database: {str(e)}")
        sys.exit(1)


@cli.command()
async def seed_data() -> None:
    """Seed the database with development data."""
    
    if settings.ENVIRONMENT == "production":
        logger.error("Cannot seed data in production environment")
        sys.exit(1)
    
    try:
        logger.info("Seeding database with development data...")
        await seed_development_data()
        logger.info("✓ Database seeded successfully")
        
    except Exception as e:
        logger.error(f"Failed to seed database: {str(e)}")
        sys.exit(1)


@cli.command()
@click.argument("migration_message")
async def migrate(migration_message: str) -> None:
    """Create a new database migration."""
    
    try:
        from alembic import command
        from alembic.config import Config
        
        logger.info(f"Creating migration: {migration_message}")
        
        alembic_cfg = Config("alembic.ini")
        command.revision(alembic_cfg, message=migration_message, autogenerate=True)
        
        logger.info("✓ Migration created successfully")
        
    except Exception as e:
        logger.error(f"Failed to create migration: {str(e)}")
        sys.exit(1)


@cli.command()
async def upgrade() -> None:
    """Apply pending database migrations."""
    
    try:
        from alembic import command
        from alembic.config import Config
        
        logger.info("Applying database migrations...")
        
        alembic_cfg = Config("alembic.ini")
        command.upgrade(alembic_cfg, "head")
        
        logger.info("✓ Migrations applied successfully")
        
    except Exception as e:
        logger.error(f"Failed to apply migrations: {str(e)}")
        sys.exit(1)


@cli.command()
async def health_check() -> None:
    """Perform a health check of all services."""
    
    try:
        checks_passed = await startup_checks()
        
        if checks_passed:
            logger.info("✓ All health checks passed")
            sys.exit(0)
        else:
            logger.error("✗ Some health checks failed")
            sys.exit(1)
            
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        sys.exit(1)


@cli.command()
@click.option("--email", required=True, help="Admin email address")
@click.option("--password", required=True, help="Admin password")
async def create_admin(email: str, password: str) -> None:
    """Create an admin user."""
    
    try:
        from app.services.user_service import UserService
        from app.schemas.user import UserCreate
        
        async with async_session_maker() as session:
            user_service = UserService(session)
            
            user_data = UserCreate(
                email=email,
                password=password,
                full_name="Administrator",
                is_active=True,
                is_superuser=True,
            )
            
            user = await user_service.create_user(user_data)
            logger.info(f"✓ Admin user created: {user.email}")
            
    except Exception as e:
        logger.error(f"Failed to create admin user: {str(e)}")
        sys.exit(1)


@cli.command()
async def start_scheduler() -> None:
    """Start the background task scheduler."""
    
    try:
        logger.info("Starting background scheduler...")
        await start_scheduler()
        logger.info("✓ Scheduler started successfully")
        
        # Keep running until interrupted
        try:
            while not shutdown_handler.should_exit:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt")
        finally:
            await stop_scheduler()
            
    except Exception as e:
        logger.error(f"Scheduler failed: {str(e)}")
        sys.exit(1)


@cli.command()
def version() -> None:
    """Display version information."""
    click.echo(f"SoftwarePro v{settings.VERSION}")
    click.echo(f"Environment: {settings.ENVIRONMENT}")
    click.echo(f"Python: {sys.version}")


def main() -> NoReturn:
    """Main entry point."""
    try:
        cli()
    except Exception as e:
        logger.exception(f"Unhandled exception: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()