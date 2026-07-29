"""
Notification HTTP Server

Simple HTTP server that receives JSON notifications and forwards them
to Telegram. Replaces openclaw message send in the event system.
"""

import json
import logging
from typing import Callable, Optional
from aiohttp import web, ClientSession
import asyncio

logger = logging.getLogger(__name__)

class NotificationServer:
    """HTTP server for receiving event notifications."""

    def __init__(self, port: int, telegram_callback: Callable[[str], None],
                 inject_callback: Optional[Callable[[str], None]] = None):
        self.port = port
        self.telegram_callback = telegram_callback
        # /notify reaches the HUMAN. /inject reaches the AGENT.
        #
        # These are genuinely different destinations and conflating them cost us a real catch:
        # the commitment hook's async audit detected an unfulfilled commitment, POSTed it to
        # /notify, and the agent never saw it -- only the human did. A self-correcting check
        # has to land in the agent's input stream, not in a chat window it cannot read.
        self.inject_callback = inject_callback
        self.app = web.Application()
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None

        # Setup routes
        self.app.router.add_post('/notify', self.handle_notification)
        self.app.router.add_post('/', self.handle_notification)  # Alternative endpoint
        self.app.router.add_post('/inject', self.handle_inject)
        self.app.router.add_get('/health', self.handle_health)

        logger.info(f"NotificationServer initialized on port {port}")

    async def handle_notification(self, request: web.Request) -> web.Response:
        """Handle incoming notification POST requests."""
        try:
            # Get client IP for logging
            client_ip = request.remote

            # Parse JSON body
            data = await request.json()

            # Extract message
            message = data.get('message')
            if not message:
                logger.warning(f"Missing 'message' field in notification from {client_ip}")
                return web.json_response({
                    'error': 'Missing message field'
                }, status=400)

            # Log the notification
            logger.info(f"Notification from {client_ip}: {len(message)} chars")

            # Send to Telegram via callback
            if self.telegram_callback:
                self.telegram_callback(message)

            return web.json_response({
                'status': 'success',
                'message': 'Notification forwarded to Telegram'
            })

        except json.JSONDecodeError:
            logger.error(f"Invalid JSON from {request.remote}")
            return web.json_response({
                'error': 'Invalid JSON format'
            }, status=400)

        except Exception as e:
            logger.error(f"Error handling notification: {e}")
            return web.json_response({
                'error': f'Server error: {str(e)}'
            }, status=500)

    async def handle_inject(self, request: web.Request) -> web.Response:
        """Feed a message into the agent's session as if it were user input.

        Used by the commitment hook's detached audit so a finding reaches the agent and can be
        acted on, rather than only alerting the human. Falls back to the Telegram callback if
        no inject callback is wired, so a caller never silently loses the message.
        """
        try:
            data = await request.json()
            message = data.get('message')
            if not message:
                return web.json_response({'error': 'Missing message field'}, status=400)

            if self.inject_callback:
                logger.info(f"Inject from {request.remote}: {len(message)} chars")
                result = self.inject_callback(message)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
                return web.json_response({'status': 'success', 'delivered': 'session'})

            logger.warning("No inject_callback wired; falling back to Telegram")
            if self.telegram_callback:
                self.telegram_callback(message)
            return web.json_response({'status': 'success', 'delivered': 'telegram-fallback'})

        except json.JSONDecodeError:
            return web.json_response({'error': 'Invalid JSON format'}, status=400)
        except Exception as e:
            logger.error(f"Error handling inject: {e}")
            return web.json_response({'error': f'Server error: {str(e)}'}, status=500)

    async def handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({
            'status': 'healthy',
            'port': self.port,
            'service': 'ares-telegram-bridge-notify'
        })

    async def start(self):
        """Start the HTTP server."""
        try:
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()

            self.site = web.TCPSite(
                self.runner,
                'localhost',  # Only bind to localhost for security
                self.port
            )

            await self.site.start()
            logger.info(f"Notification server started on http://localhost:{self.port}")

        except Exception as e:
            logger.error(f"Failed to start notification server: {e}")
            raise

    async def stop(self):
        """Stop the HTTP server."""
        try:
            if self.site:
                await self.site.stop()
                logger.info("Notification server stopped")

            if self.runner:
                await self.runner.cleanup()

        except Exception as e:
            logger.error(f"Error stopping notification server: {e}")

    def get_status(self) -> dict:
        """Get server status."""
        return {
            'running': self.site is not None,
            'port': self.port,
            'endpoints': ['/notify', '/inject', '/', '/health']
        }


async def test_notification_server(port: int = 9998):
    """Test function for the notification server."""

    def test_callback(message: str):
        print(f"Test callback received: {message}")

    server = NotificationServer(port, test_callback)

    try:
        await server.start()
        print(f"Test server running on port {port}")
        print("Test with: curl -X POST localhost:9998/notify -H 'Content-Type: application/json' -d '{\"message\":\"test\"}'")

        # Keep server running for manual testing
        while True:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        print("\nShutting down test server...")
    finally:
        await server.stop()


if __name__ == "__main__":
    # Run test server if executed directly
    logging.basicConfig(level=logging.INFO)
    asyncio.run(test_notification_server())