from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from typing import Dict, Optional, List
from redis import asyncio as aioredis
import aio_pika
from dataclasses import dataclass
import json
import asyncio
import numpy as np
from dataclasses import dataclass
import logging
from livekit import rtc, api
import traceback
from concurrent.futures import ThreadPoolExecutor
from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent
from fastapi.middleware.cors import CORSMiddleware

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Configuration
REDIS_URL = "redis://redis:6379"  # Changed from localhost to service name
RABBITMQ_URL = "amqp://guest:guest@rabbitmq:5672/"
LIVEKIT_URL = "wss://live-kit.urcalls.com"

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@dataclass
class AudioConfig:
    channels: int = 1
    sample_width: int = 2
    sample_rate: int = 16000
    chunk_size: int = 4000


AUDIO_CONFIG = {
    "chunk_size": 8000,  # Increased from 4000 for better performance
    "sample_rate": 16000,
    "channels": 1,
    "delay_seconds": 0.2,
    "buffer_size": 32768,  # 32KB buffer
}


@dataclass
class RoomConnection:
    room: rtc.Room
    websockets: set[WebSocket]
    cleanup_task: asyncio.Task = None


class WebSocketConnection:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.is_connected = True
        self.lock = asyncio.Lock()
        self.close_sent = False

    async def send_json(self, message: dict) -> bool:
        async with self.lock:
            if not self.is_connected or self.close_sent:
                return False
            try:
                await self.websocket.send_json(message)
                return True
            except Exception as e:
                logger.error(f"Error sending WebSocket message: {e}")
                self.is_connected = False
                return False

    async def close(self):
        async with self.lock:
            if not self.close_sent and self.is_connected:
                try:
                    self.close_sent = True
                    self.is_connected = False
                    await self.websocket.close()
                except Exception as e:
                    logger.debug(f"Error closing WebSocket (may already be closed): {e}")



class RedisManager:
    def __init__(self, url: str):
        self.url = url
        self.redis = aioredis.from_url(
            self.url,
            decode_responses=True,
            retry_on_timeout=True,
            socket_connect_timeout=30,
            socket_keepalive=True,
        )

    async def connect(self, max_retries: int = 5, retry_delay: int = 5):
        """Connect to Redis with retries"""
        for attempt in range(max_retries):
            try:
                logger.info(
                    f"Attempting to connect to Redis at {self.url} (attempt {attempt + 1}/{max_retries})"
                )
                # Test connection
                await self.redis.ping()
                logger.info("Successfully connected to Redis")
                return
            except Exception as e:
                logger.error(f"Failed to connect to Redis: {e}")
                if attempt < max_retries - 1:
                    logger.info(f"Retrying in {retry_delay} seconds...")
                    await asyncio.sleep(retry_delay)
                else:
                    raise

    async def get_participant_language(self, room_id: str, participant_id: str) -> str:
        key = f"language:{room_id}:{participant_id}"
        language = await self.redis.get(key)
        return language or "en-US"

    async def set_participant_language(
        self, room_id: str, participant_id: str, language: str
    ):
        key = f"language:{room_id}:{participant_id}"
        await self.redis.set(key, language)

    async def store_participant_id(
        self, room_id: str, participant_name: str, participant_id: str
    ):
        key = f"participant:{room_id}:{participant_name}"
        await self.redis.set(key, participant_id)

    async def get_participant_ids(self, room_id: str) -> dict:
        pattern = f"participant:{room_id}:*"
        keys = await self.redis.keys(pattern)
        if not keys:
            return {}
        values = await self.redis.mget(keys)
        return {k.split(":")[-1]: v for k, v in zip(keys, values)}

    async def store_room_state(self, room_id: str, state: dict):
        key = f"room:{room_id}"
        await self.redis.set(key, json.dumps(state))

    async def get_room_state(self, room_id: str) -> dict:
        key = f"room:{room_id}"
        state = await self.redis.get(key)
        return json.loads(state) if state else {}

    async def add_websocket_to_room(self, room_id: str, websocket_id: str):
        key = f"websockets:{room_id}"
        await self.redis.sadd(key, websocket_id)

    async def remove_websocket_from_room(self, room_id: str, websocket_id: str):
        key = f"websockets:{room_id}"
        await self.redis.srem(key, websocket_id)

    async def get_room_websockets(self, room_id: str) -> List[str]:
        key = f"websockets:{room_id}"
        return await self.redis.smembers(key)


class MessageQueue:
    def __init__(self):
        self.connection = None
        self.channel = None
        self.exchange = None

    async def connect(self, url: str, max_retries: int = 5, retry_delay: int = 5):
        """Connect to RabbitMQ with retries"""
        for attempt in range(max_retries):
            try:
                logger.info(
                    f"Attempting to connect to RabbitMQ at {url} (attempt {attempt + 1}/{max_retries})"
                )
                self.connection = await aio_pika.connect_robust(
                    url, timeout=30, retry_delay=2
                )
                self.channel = await self.connection.channel()
                self.exchange = await self.channel.declare_exchange(
                    "transcription", aio_pika.ExchangeType.TOPIC
                )
                logger.info("Successfully connected to RabbitMQ")
                return
            except Exception as e:
                logger.error(f"Failed to connect to RabbitMQ: {e}")
                if attempt < max_retries - 1:
                    logger.info(f"Retrying in {retry_delay} seconds...")
                    await asyncio.sleep(retry_delay)
                else:
                    raise

    async def publish_transcript(self, room_id: str, message: dict):
        routing_key = f"transcript.{room_id}"
        await self.exchange.publish(
            aio_pika.Message(body=json.dumps(message).encode()), routing_key=routing_key
        )

    async def subscribe_to_transcripts(self, room_id: str, callback):
        queue = await self.channel.declare_queue(f"transcript_queue_{room_id}")
        await queue.bind(self.exchange, f"transcript.{room_id}")

        async def process_message(message):
            async with message.process():
                data = json.loads(message.body.decode())
                await callback(data)

        await queue.consume(process_message)

    async def cleanup(self):
        if self.connection:
            await self.connection.close()


class OptimizedTranscribeEventHandler(TranscriptResultStreamHandler):
    def __init__(
        self,
        output_stream,
        active_speaker_name: str,
        websocket_connection: WebSocketConnection,
        room_id: str,
    ):
        super().__init__(output_stream)
        self.active_speaker_name = active_speaker_name
        self.websocket_connection = websocket_connection
        self.room_id = room_id
        self.last_partial = ""

    async def handle_transcript_event(self, transcript_event: TranscriptEvent):
        try:
            results = transcript_event.transcript.results
            for result in results:
                for alt in result.alternatives:
                    message = {
                        "speaker": self.active_speaker_name,
                        "type": "partial" if result.is_partial else "final",
                        "text": alt.transcript.strip(),
                        "room_id": self.room_id,
                    }

                    if result.is_partial:
                        if message["text"] and message["text"] != self.last_partial:
                            self.last_partial = message["text"]
                            if await self.websocket_connection.send_json(message):
                                logger.debug(
                                    f"Sent partial transcript for {self.active_speaker_name}"
                                )
                            else:
                                return  # Stop processing if connection is closed
                    else:
                        if message["text"]:
                            if await self.websocket_connection.send_json(message):
                                logger.info(
                                    f"Sent final transcript for {self.active_speaker_name}: {message['text']}"
                                )
                            else:
                                return  # Stop processing if connection is closed

        except Exception as e:
            logger.error(f"Error in handle_transcript_event: {e}")
            logger.error(traceback.format_exc())

    async def send_message(self, message: dict):
        try:
            await self.websocket.send_json(message)
        except Exception as e:
            logger.error(f"Error sending WebSocket message: {e}")


class DelayedAudioBuffer:
    def __init__(
        self,
        sample_rate: int,
        num_channels: int,
        delay_seconds: float = 0.2,
        buffer_size: int = 32768,
    ):
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.delay_seconds = delay_seconds
        self.buffer_size = buffer_size
        self.buffer = []  # Changed to list-based buffer
        self.queue = asyncio.Queue(maxsize=100)  # Added maxsize to prevent memory issues
        self._lock = asyncio.Lock()

    async def extend(self, data: bytes):
        try:
            if len(data) > 0:
                await self.queue.put(bytes(data))  # Convert to bytes to ensure consistency
        except asyncio.QueueFull:
            logger.warning("Audio buffer queue is full, dropping frame")
        except Exception as e:
            logger.error(f"Error adding data to buffer: {e}")

    async def get_chunk(self, chunk_size: int) -> Optional[bytes]:
        try:
            async with self._lock:
                # Fill buffer from queue
                while len(self.buffer) < chunk_size:
                    try:
                        data = await asyncio.wait_for(self.queue.get(), timeout=0.1)
                        self.buffer.extend(data)
                        self.queue.task_done()
                    except asyncio.TimeoutError:
                        break
                    except Exception as e:
                        logger.error(f"Error getting data from queue: {e}")
                        break

                # Return chunk if we have enough data
                if len(self.buffer) >= chunk_size:
                    chunk = bytes(self.buffer[:chunk_size])
                    self.buffer = self.buffer[chunk_size:]
                    return chunk

        except Exception as e:
            logger.error(f"Error in get_chunk: {e}")
        return None


class OptimizedAudioHandler:
    def __init__(
        self,
        speaker_name: str,
        participant_id: str,
        room_id: str,
        websocket_connection: WebSocketConnection,
        redis_manager: RedisManager,
        language_code: str = "en-US",
        config: dict = None,
    ):
        self.speaker_name = speaker_name
        self.participant_id = participant_id
        self.room_id = room_id
        self.websocket_connection = websocket_connection
        self.redis_manager = redis_manager
        self.language_code = language_code
        self.config = config or AUDIO_CONFIG.copy()

        # Extract config values
        self.sample_rate = self.config["sample_rate"]
        self.channels = self.config["channels"]
        self.chunk_size = self.config["chunk_size"]
        self.delay_seconds = self.config["delay_seconds"]
        self.buffer_size = self.config["buffer_size"]

        # Initialize audio buffer
        self.audio_buffer = DelayedAudioBuffer(
            sample_rate=self.sample_rate,
            num_channels=self.channels,
            delay_seconds=self.delay_seconds,
            buffer_size=self.buffer_size,
        )

        self.is_streaming = False
        self._stop_processing = asyncio.Event()
        self.transcribe_client = None
        self.stream = None
        self.handler = None
        self._processing_task = None
        self._handler_task = None
        self._cleanup_lock = asyncio.Lock()
        self._is_stopping = False

    async def handle_frame(self, frame_data: bytes):
        if not self.is_streaming or self._is_stopping:
            return

        try:
            await self.audio_buffer.extend(frame_data)
        except Exception as e:
            logger.error(f"Error handling frame: {e}")
            if not self._is_stopping:
                await self.stop()

    async def stop(self):
        async with self._cleanup_lock:
            if self._is_stopping:
                return

            self._is_stopping = True
            logger.info(f"Stopping audio handler for {self.speaker_name}")

            # Set stop flag first
            self._stop_processing.set()
            self.is_streaming = False

            # Cancel tasks
            tasks_to_cancel = []
            if self._processing_task and not self._processing_task.done():
                tasks_to_cancel.append(self._processing_task)
            if self._handler_task and not self._handler_task.done():
                tasks_to_cancel.append(self._handler_task)

            # Cancel all tasks at once
            if tasks_to_cancel:
                for task in tasks_to_cancel:
                    task.cancel()
                try:
                    await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
                except Exception as e:
                    logger.error(f"Error canceling tasks: {e}")

            # Close stream
            if self.stream and hasattr(self.stream, 'input_stream'):
                try:
                    await self.stream.input_stream.end_stream()
                except Exception as e:
                    logger.debug(f"Error ending stream (may already be closed): {e}")

            self.stream = None
            self.transcribe_client = None
            self._processing_task = None
            self._handler_task = None
            self._is_stopping = False

            logger.info(f"Successfully stopped audio handler for {self.speaker_name}")

    async def start(self):
        if self.is_streaming:
            return

        try:
            logger.info(
                f"Starting audio handler for {self.speaker_name} in room {self.room_id}"
            )

            self.transcribe_client = TranscribeStreamingClient(region="us-east-2")

            self.stream = await self.transcribe_client.start_stream_transcription(
                language_code=self.language_code,
                media_sample_rate_hz=self.sample_rate,
                media_encoding="pcm",
            )

            if not self.stream or not self.stream.input_stream:
                raise Exception("Failed to initialize transcription stream")

            self.handler = OptimizedTranscribeEventHandler(
                self.stream.output_stream,
                self.speaker_name,
                self.websocket_connection,
                self.room_id,
            )

            self.is_streaming = True
            self._stop_processing.clear()
            self._is_stopping = False

            self._processing_task = asyncio.create_task(self._process_audio())
            self._handler_task = asyncio.create_task(self.handler.handle_events())

            logger.info(f"Successfully started audio handler for {self.speaker_name}")

        except Exception as e:
            logger.error(f"Failed to start audio handler: {e}")
            await self.stop()
            raise

    async def _process_audio(self):
        batch_size = self.chunk_size
        sleep_time = batch_size / (self.sample_rate * self.channels * 2)

        try:
            while not self._stop_processing.is_set():
                if not self.stream or not self.stream.input_stream:
                    break

                chunk = await self.audio_buffer.get_chunk(batch_size)
                if chunk:
                    try:
                        await self.stream.input_stream.send_audio_event(
                            audio_chunk=chunk
                        )
                        await asyncio.sleep(sleep_time)
                    except Exception as e:
                        if "Rate exceeded" in str(e):
                            await asyncio.sleep(sleep_time * 2)
                            continue
                        raise
                else:
                    await asyncio.sleep(sleep_time / 2)

        except asyncio.CancelledError:
            logger.info(f"Audio processing task cancelled for {self.speaker_name}")
        except Exception as e:
            logger.error(f"Error in audio processing: {e}")
        finally:
            if not self._is_stopping:
                await self.stop()

    async def _process_audio(self):
        batch_size = self.config["chunk_size"]
        sleep_time = batch_size / (
            self.config["sample_rate"] * self.config["channels"] * 2
        )

        try:
            while not self._stop_processing.is_set():
                if not self.stream or not self.stream.input_stream:
                    break

                chunk = await self.audio_buffer.get_chunk(batch_size)
                if chunk:
                    try:
                        await self.stream.input_stream.send_audio_event(
                            audio_chunk=chunk
                        )
                        await asyncio.sleep(sleep_time)
                    except Exception as e:
                        if "Rate exceeded" in str(e):
                            await asyncio.sleep(sleep_time * 2)
                            continue
                        raise
                else:
                    await asyncio.sleep(sleep_time / 2)

        except asyncio.CancelledError:
            logger.info(f"Audio processing task cancelled for {self.speaker_name}")
        except Exception as e:
            logger.error(f"Error in audio processing: {e}")
        finally:
            if not self._is_stopping:
                await self.stop()


class OptimizedTranscriptionManager:
    def __init__(self):
        self.redis_manager = RedisManager(REDIS_URL)
        self.message_queue = MessageQueue()
        self.audio_handlers: Dict[str, Dict[str, OptimizedAudioHandler]] = {}
        self.active_rooms: Dict[str, RoomConnection] = {}
        self.lock = asyncio.Lock()
        self.token_refresh_tasks: Dict[str, asyncio.Task] = {}
        self.websocket_connections = {}

    async def initialize(self):
        """Initialize connections with retries"""
        try:
            logger.info("Initializing services...")

            # Connect to Redis first
            await self.redis_manager.connect()
            logger.info("Redis connection established")

            # Then connect to RabbitMQ
            await self.message_queue.connect(RABBITMQ_URL)
            logger.info("RabbitMQ connection established")

            logger.info("All services initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize services: {e}")
            raise

    async def connect_to_room(
        self,
        room_id: str,
        websocket: WebSocket,
        participant_id: Optional[str] = None,
        language: str = "en-US",
    ) -> RoomConnection:
        async with self.lock:
            websocket_id = str(id(websocket))
            await self.redis_manager.add_websocket_to_room(room_id, websocket_id)

            if participant_id:
                await self.redis_manager.set_participant_language(
                    room_id, participant_id, language
                )

            if room_id in self.active_rooms:
                room_conn = self.active_rooms[room_id]
                room_conn.websockets.add(websocket)
                return room_conn

            room = rtc.Room()
            room_conn = RoomConnection(room=room, websockets={websocket})

            # Create wrapped event handlers
            async def handle_track_subscribed(track, publication, participant):
                try:
                    if track.kind == rtc.TrackKind.KIND_AUDIO:
                        stored_participant_ids = (
                            await self.redis_manager.get_participant_ids(room_id)
                        )
                        current_participant_id = stored_participant_ids.get(
                            participant.name
                        )

                        if current_participant_id is None:
                            current_participant_id = f"{participant.name}_{track.sid}"
                            await self.redis_manager.store_participant_id(
                                room_id, participant.name, current_participant_id
                            )

                        participant_language = (
                            await self.redis_manager.get_participant_language(
                                room_id, current_participant_id
                            )
                        )

                        # Pass websocket to handle_audio_track
                        await self.handle_audio_track(
                            track=track,
                            participant_name=participant.name,
                            room_id=room_id,
                            participant_id=current_participant_id,
                            websocket=websocket,  # Pass the websocket here
                        )

                except Exception as e:
                    logger.error(f"Error in track_subscribed handler: {e}")
                    logger.error(traceback.format_exc())

            async def handle_track_unsubscribed(track, publication, participant):
                try:
                    if track.kind == rtc.TrackKind.KIND_AUDIO:
                        track_id = f"{room_id}_{participant.name}_{track.sid}"
                        if (
                            room_id in self.audio_handlers
                            and track_id in self.audio_handlers[room_id]
                        ):
                            handler = self.audio_handlers[room_id][track_id]
                            await handler.stop()
                            del self.audio_handlers[room_id][track_id]

                            if not self.audio_handlers[room_id]:
                                del self.audio_handlers[room_id]
                except Exception as e:
                    logger.error(f"Error in track_unsubscribed handler: {e}")

            async def handle_disconnected():
                logger.info(f"Room {room_id} disconnected, attempting reconnection")
                asyncio.create_task(self.handle_room_reconnection(room_id, room))

            # Create wrapper functions for event handlers
            def track_subscribed_wrapper(*args):
                asyncio.create_task(handle_track_subscribed(*args))

            def track_unsubscribed_wrapper(*args):
                asyncio.create_task(handle_track_unsubscribed(*args))

            def disconnected_wrapper():
                asyncio.create_task(handle_disconnected())

            # Attach event handlers
            room.on("track_subscribed")(track_subscribed_wrapper)
            room.on("track_unsubscribed")(track_unsubscribed_wrapper)
            room.on("disconnected")(disconnected_wrapper)

            # Connect to LiveKit
            token = self.generate_token(room_id)
            try:
                await room.connect(
                    LIVEKIT_URL, token, options=rtc.RoomOptions(auto_subscribe=True)
                )
                logger.info(f"Successfully connected to room {room_id}")

                # Start token refresh task
                self.token_refresh_tasks[room_id] = asyncio.create_task(
                    self.refresh_token_periodically(room_id)
                )

                self.active_rooms[room_id] = room_conn
                return room_conn

            except Exception as e:
                logger.error(f"Failed to connect to room {room_id}: {e}")
                await self.cleanup_room(room_id)
                raise

    async def handle_room_reconnection(self, room_id: str, room: rtc.Room):
        """Handle room reconnection logic"""
        max_retries = 3
        retry_delay = 5

        for attempt in range(max_retries):
            try:
                token = self.generate_token(room_id)
                await room.disconnect()
                await room.connect(
                    LIVEKIT_URL, token, options=rtc.RoomOptions(auto_subscribe=True)
                )
                logger.info(f"Successfully reconnected to room {room_id}")
                return
            except Exception as e:
                logger.error(f"Reconnection attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2

        # All retries failed
        await self.handle_connection_failure(room_id)

    async def handle_connection_failure(self, room_id: str):
        """Handle permanent connection failure"""
        if room_id not in self.active_rooms:
            return

        room_conn = self.active_rooms[room_id]
        error_message = {
            "type": "connection_error",
            "message": "Failed to maintain connection to the room",
        }

        # Notify all connected websockets
        for ws in room_conn.websockets:
            try:
                await ws.send_json(error_message)
            except Exception:
                pass

        # Clean up the room
        await self.cleanup_room(room_id)

    async def refresh_token_periodically(self, room_id: str):
        """Periodically refresh the room token"""
        try:
            while room_id in self.active_rooms:
                await asyncio.sleep(3300)  # 55 minutes
                if room_id in self.active_rooms:
                    room_conn = self.active_rooms[room_id]
                    new_token = self.generate_token(room_id)
                    try:
                        await room_conn.room.disconnect()
                        await room_conn.room.connect(
                            LIVEKIT_URL,
                            new_token,
                            options=rtc.RoomOptions(auto_subscribe=True),
                        )
                        logger.info(f"Successfully refreshed token for room {room_id}")
                    except Exception as e:
                        logger.error(
                            f"Failed to refresh connection for room {room_id}: {e}"
                        )
                        await self.handle_reconnection(room_id)
        except Exception as e:
            logger.error(f"Error in token refresh task for room {room_id}: {e}")

    async def cleanup_room(self, room_id: str):
        """Clean up all resources associated with a room"""
        if room_id in self.active_rooms:
            room_conn = self.active_rooms[room_id]

            # Stop token refresh task
            if room_id in self.token_refresh_tasks:
                self.token_refresh_tasks[room_id].cancel()
                del self.token_refresh_tasks[room_id]

            # Stop all audio handlers
            if room_id in self.audio_handlers:
                for handler in self.audio_handlers[room_id].values():
                    await handler.stop()
                del self.audio_handlers[room_id]

            # Disconnect room
            try:
                await room_conn.room.disconnect()
            except Exception:
                pass

            del self.active_rooms[room_id]

    async def disconnect_from_room(
        self, room_id: str, websocket: WebSocket, participant_id: Optional[str] = None
    ):
        """Handle disconnection from a room"""
        async with self.lock:
            websocket_id = str(id(websocket))
            await self.redis_manager.remove_websocket_from_room(room_id, websocket_id)

            if room_id in self.active_rooms:
                room_conn = self.active_rooms[room_id]
                room_conn.websockets.discard(websocket)

                remaining_websockets = await self.redis_manager.get_room_websockets(
                    room_id
                )
                if not remaining_websockets:
                    await self.cleanup_room(room_id)

    async def handle_audio_track(
        self,
        track,
        participant_name: str,
        room_id: str,
        participant_id: str,
        websocket: WebSocket,
    ):
        websocket_id = str(id(websocket))
        websocket_connection = WebSocketConnection(websocket)
        self.websocket_connections[websocket_id] = websocket_connection

        track_id = f"{room_id}_{participant_name}_{track.sid}"

        try:
            if room_id not in self.audio_handlers:
                self.audio_handlers[room_id] = {}

            if track_id not in self.audio_handlers[room_id]:
                language = await self.redis_manager.get_participant_language(
                    room_id, participant_id
                )

                handler = OptimizedAudioHandler(
                    speaker_name=participant_name,
                    participant_id=participant_id,
                    room_id=room_id,
                    websocket_connection=websocket_connection,
                    redis_manager=self.redis_manager,
                    language_code=language,
                    config=AUDIO_CONFIG,
                )

                await handler.start()
                self.audio_handlers[room_id][track_id] = handler

                async for frame_event in rtc.AudioStream(
                    track=track, sample_rate=16000, num_channels=1, capacity=100
                ):
                    if (
                        not handler.is_streaming
                        or not websocket_connection.is_connected
                    ):
                        break
                    await handler.handle_frame(frame_event.frame.data)

                # Cleanup
                await handler.stop()
                async with self.lock:
                    if room_id in self.audio_handlers:
                        self.audio_handlers[room_id].pop(track_id, None)
                        if not self.audio_handlers[room_id]:
                            del self.audio_handlers[room_id]

        except Exception as e:
            logger.error(f"Error in handle_audio_track: {e}")
            logger.error(traceback.format_exc())
        finally:
            # Cleanup WebSocket connection
            if websocket_id in self.websocket_connections:
                await self.websocket_connections[websocket_id].close()
                del self.websocket_connections[websocket_id]

    async def set_participant_language(
        self, room_id: str, participant_id: str, language: str
    ):
        """Update participant language and restart their audio handler if needed"""
        async with self.lock:
            await self.redis_manager.set_participant_language(
                room_id, participant_id, language
            )

            # Restart handlers with new language
            if room_id in self.audio_handlers:
                handlers_to_restart = [
                    (track_id, handler)
                    for track_id, handler in self.audio_handlers[room_id].items()
                    if handler.participant_id == participant_id
                ]

                for track_id, handler in handlers_to_restart:
                    await handler.stop()
                    new_handler = OptimizedAudioHandler(
                        speaker_name=handler.speaker_name,
                        participant_id=participant_id,
                        room_id=room_id,
                        message_queue=self.message_queue,
                        redis_manager=self.redis_manager,
                        language_code=language,
                    )
                    await new_handler.start()
                    self.audio_handlers[room_id][track_id] = new_handler

    def generate_token(self, room_id: str) -> str:
        """Generate LiveKit room token"""
        return (
            api.AccessToken()
            .with_identity("python-bot")
            .with_name("Python Bot")
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room_id,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=True,
                    room_record=True,
                )
            )
            .to_jwt()
        )


# Initialize transcription manager
transcription_manager = OptimizedTranscriptionManager()


@app.on_event("startup")
async def startup():
    """Initialize the application"""
    await transcription_manager.initialize()


@app.websocket("/ws/{room_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    room_id: str,
    participant_id: str = Query(None),
    language: str = Query("en-US"),
):
    await websocket.accept()
    logger.info(f"New WebSocket connection for room {room_id}")

    try:
        room_conn = await transcription_manager.connect_to_room(
            room_id, websocket, participant_id=participant_id, language=language
        )

        await websocket.send_json(
            {
                "type": "connection_status",
                "status": "connected",
                "room_id": room_id,
                "participant_id": participant_id or "",
                "language": language,
            }
        )

        while True:
            try:
                data = await websocket.receive_text()
                message = json.loads(data)

                if message.get("type") == "update_language":
                    new_language = message.get("language", "en-US")
                    await transcription_manager.set_participant_language(
                        room_id, participant_id, new_language
                    )
                    await websocket.send_json(
                        {
                            "type": "language_updated",
                            "participant_id": participant_id,
                            "language": new_language,
                        }
                    )

            except WebSocketDisconnect:
                logger.info(f"WebSocket disconnected for room {room_id}")
                break
            except json.JSONDecodeError:
                logger.error("Invalid JSON message received")
                continue

    except Exception as e:
        logger.error(f"Error in websocket connection: {e}")

    finally:
        await transcription_manager.disconnect_from_room(
            room_id, websocket, participant_id
        )
