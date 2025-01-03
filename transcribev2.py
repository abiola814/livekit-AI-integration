from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from typing import Dict, Optional, List, Set
from redis import asyncio as aioredis
import aio_pika
from dataclasses import dataclass
import json
import asyncio
import time
import numpy as np
from dataclasses import dataclass
from datetime import datetime
import logging
from livekit import rtc, api
import traceback
from concurrent.futures import ThreadPoolExecutor
from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent
from fastapi.middleware.cors import CORSMiddleware
from socketio import AsyncServer, ASGIApp
import socketio

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


class SocketConnection:
    def __init__(self, socket_id: str, sio: AsyncServer):
        self.socket_id = socket_id
        self.sio = sio
        self.is_connected = True
        self.lock = asyncio.Lock()

    async def send_json(self, message: dict) -> bool:
        async with self.lock:
            if not self.is_connected:
                return False
            try:
                await self.sio.emit("message", message, room=self.socket_id)
                return True
            except Exception as e:
                logger.error(f"Error sending Socket.IO message: {e}")
                self.is_connected = False
                return False

    async def close(self):
        self.is_connected = False


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
    sockets: Set[str]
    cleanup_task: Optional[asyncio.Task] = None


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
        socket_connection: SocketConnection,
        room_id: str,
    ):
        super().__init__(output_stream)
        self.active_speaker_name = active_speaker_name
        self.socket_connection = socket_connection  # Changed from websocket_connection
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
                        pass
                    else:
                        if message["text"]:
                            await self.socket_connection.send_json(message)

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
        self.queue = asyncio.Queue(
            maxsize=100
        )  # Added maxsize to prevent memory issues
        self._lock = asyncio.Lock()

    async def extend(self, data: bytes):
        try:
            if len(data) > 0:
                await self.queue.put(
                    bytes(data)
                )  # Convert to bytes to ensure consistency
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


class TranscriptionEventHandler(TranscriptResultStreamHandler):
    def __init__(
        self,
        output_stream,
        speaker_name: str,
        room_id: str,
        sio: AsyncServer,
    ):
        super().__init__(output_stream)
        self.speaker_name = speaker_name
        self.room_id = room_id
        self.sio = sio
        self.last_partial = ""

    async def handle_transcript_event(self, transcript_event: TranscriptEvent):
        try:
            results = transcript_event.transcript.results
            for result in results:
                for alt in result.alternatives:
                    message = {
                        "status": "transcription",
                        "speaker": self.speaker_name,
                        "type": "partial" if result.is_partial else "final",
                        "text": alt.transcript.strip(),
                        "room_id": self.room_id,
                        "timestamp": datetime.utcnow().isoformat(),
                    }

                    if result.is_partial:
                        if message["text"] and message["text"] != self.last_partial:
                            # Broadcast to room
                            pass
                            # await self.sio.emit("message", message, room=self.room_id)
                            # self.last_partial = message["text"]
                    else:
                        if message["text"]:
                            # Broadcast to room
                            await self.sio.emit("message", message, room=self.room_id)

        except Exception as e:
            logger.error(f"Error in handle_transcript_event: {e}")
            logger.error(traceback.format_exc())


class OptimizedAudioHandler:
    def __init__(
        self,
        speaker_name: str,
        participant_id: str,
        room_id: str,
        socket_connection: Optional[SocketConnection] = None,  # Made optional
        sio: Optional[AsyncServer] = None,  # Added sio parameter
        redis_manager: RedisManager = None,
        language_code: str = "en-US",
        config: dict = None,
    ):
        self.speaker_name = speaker_name
        self.participant_id = participant_id
        self.room_id = room_id
        self.socket_connection = socket_connection
        self.sio = sio
        self.redis_manager = redis_manager
        self.language_code = language_code
        self.config = config or AUDIO_CONFIG.copy()

        # Audio configuration
        self.sample_rate = self.config["sample_rate"]
        self.channels = self.config["channels"]
        self.chunk_size = self.config["chunk_size"]
        self.delay_seconds = self.config.get("delay_seconds", 0.2)
        self.buffer_size = self.config.get("buffer_size", 32768)

        # Initialize audio buffer
        self.audio_buffer = DelayedAudioBuffer(
            sample_rate=self.sample_rate,
            num_channels=self.channels,
            delay_seconds=self.delay_seconds,
            buffer_size=self.buffer_size,
        )

        # State management
        self.is_streaming = False
        self._stop_processing = asyncio.Event()
        self._cleanup_lock = asyncio.Lock()
        self._is_stopping = False

        # Components
        self.transcribe_client = None
        self.stream = None
        self.handler = None
        self._processing_task = None
        self._handler_task = None
        self._tasks = set()

    async def _heartbeat(self):
        """Monitor stream health and reconnect if needed"""
        while not self._stop_processing.is_set():
            try:
                current_time = time.time()
                if (current_time - self._last_activity) > self.ACTIVITY_TIMEOUT:
                    logger.warning(
                        f"No activity detected for {self.speaker_name}, attempting reconnection"
                    )
                    await self._attempt_reconnection()
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Error in heartbeat monitoring: {e}")

    async def _attempt_reconnection(self):
        """Attempt to reconnect the transcription stream"""
        if self._reconnect_attempt < self.MAX_RECONNECT_ATTEMPTS:
            self._reconnect_attempt += 1
            logger.info(
                f"Attempting reconnection {self._reconnect_attempt}/{self.MAX_RECONNECT_ATTEMPTS}"
            )

            try:
                await self.stop()
                await asyncio.sleep(1)  # Brief delay before reconnecting
                await self.start()
                self._reconnect_attempt = 0
                logger.info("Successfully reconnected")
            except Exception as e:
                logger.error(f"Reconnection attempt failed: {e}")
        else:
            logger.error("Max reconnection attempts reached")
            await self.stop()

    async def broadcast_message(self, message: dict):
        """Send message to all clients in the room"""
        try:
            if self.sio:
                await self.sio.emit("message", message, room=self.room_id)
            elif self.socket_connection:
                await self.socket_connection.send_json(message)
        except Exception as e:
            logger.error(f"Error broadcasting message: {e}")

    async def start(self):
        if self.is_streaming:
            return

        try:
            logger.info(f"Starting audio handler for {self.speaker_name}")

            self.transcribe_client = TranscribeStreamingClient(region="us-east-2")
            self.stream = await self.transcribe_client.start_stream_transcription(
                language_code=self.language_code,
                media_sample_rate_hz=self.sample_rate,
                media_encoding="pcm",
            )

            if not self.stream or not self.stream.input_stream:
                raise Exception("Failed to initialize transcription stream")

            self.handler = TranscriptionEventHandler(
                self.stream.output_stream,
                self.speaker_name,
                self.room_id,
                self.sio if self.sio else None,
            )

            self.is_streaming = True
            self._stop_processing.clear()
            self._is_stopping = False

            self._processing_task = self._create_tracked_task(self._process_audio())
            self._handler_task = self._create_tracked_task(self.handler.handle_events())

            logger.info(f"Successfully started audio handler for {self.speaker_name}")

        except Exception as e:
            logger.error(f"Failed to start audio handler: {e}")
            await self.stop()
            raise

    async def handle_frame(self, frame_data: bytes):
        if not self.is_streaming or self._is_stopping:
            return

        try:
            self._last_activity = time.time()
            await self.audio_buffer.extend(frame_data)
        except Exception as e:
            logger.error(f"Error handling frame: {e}")
            if not self._is_stopping:
                await self._attempt_reconnection()

    async def _process_audio(self):
        batch_size = self.chunk_size
        sleep_time = batch_size / (self.sample_rate * self.channels * 2)
        backoff_time = sleep_time

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
                        backoff_time = sleep_time  # Reset backoff on success
                        await asyncio.sleep(sleep_time)
                    except Exception as e:
                        if "Rate exceeded" in str(e):
                            backoff_time = min(
                                backoff_time * 2, 1.0
                            )  # Max 1 second backoff
                            await asyncio.sleep(backoff_time)
                            continue
                        else:
                            logger.error(f"Error processing audio chunk: {e}")
                            await self._attempt_reconnection()
                            break
                else:
                    await asyncio.sleep(sleep_time / 2)

        except asyncio.CancelledError:
            logger.info(f"Audio processing task cancelled for {self.speaker_name}")
        except Exception as e:
            logger.error(f"Error in audio processing: {e}")
            if not self._is_stopping:
                await self._attempt_reconnection()

    def _create_tracked_task(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def stop(self):
        async with self._cleanup_lock:
            if self._is_stopping:
                return

            self._is_stopping = True
            logger.info(f"Stopping audio handler for {self.speaker_name}")

            # Cancel all tasks
            self._stop_processing.set()
            self.is_streaming = False

            for task in list(self._tasks):
                if not task.done():
                    task.cancel()

            try:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            except Exception as e:
                logger.error(f"Error during task cleanup: {e}")

            # Close stream
            if self.stream and hasattr(self.stream, "input_stream"):
                try:
                    await self.stream.input_stream.end_stream()
                except Exception as e:
                    logger.debug(f"Error ending stream: {e}")

            # Clear state
            self.stream = None
            self.transcribe_client = None
            self._processing_task = None
            self._handler_task = None
            self._heartbeat_task = None
            self._is_stopping = False
            self._tasks.clear()

            logger.info(f"Successfully stopped audio handler for {self.speaker_name}")


class SocketIOTranscriptionManager:
    def __init__(self, sio: AsyncServer):
        self.sio = sio
        self.redis_manager = RedisManager(REDIS_URL)
        self.message_queue = MessageQueue()
        self.audio_handlers: Dict[str, Dict[str, OptimizedAudioHandler]] = {}
        self.active_rooms: Dict[str, RoomConnection] = {}
        self.lock = asyncio.Lock()
        self.cleanup_tasks: Dict[str, asyncio.Task] = {}

    async def connect_to_room(
        self,
        room_id: str,
        socket_id: str,
        participant_id: Optional[str] = None,
        language: str = "en-US",
    ) -> RoomConnection:
        try:
            async with self.lock:
                # Join Socket.IO room
                await self.sio.enter_room(socket_id, room_id)
                logger.info(f"Socket {socket_id} joined room {room_id}")

                if participant_id:
                    await self.redis_manager.set_participant_language(
                        room_id, participant_id, language
                    )

                if room_id in self.active_rooms:
                    room_conn = self.active_rooms[room_id]
                    room_conn.sockets.add(socket_id)
                    return room_conn

                # Create new room connection
                room = rtc.Room()
                room_conn = RoomConnection(room=room, sockets={socket_id})

                # Set up event handlers
                async def handle_track_subscribed(track, publication, participant):
                    if track.kind == rtc.TrackKind.KIND_AUDIO:
                        await self._handle_audio_track_safe(
                            track=track,
                            participant_name=participant.name,
                            room_id=room_id,
                            participant_id=participant_id,
                            sio=self.sio,
                        )

                room.on("track_subscribed")(
                    lambda *args: asyncio.create_task(handle_track_subscribed(*args))
                )

                # Connect to LiveKit
                token = self._generate_token(room_id)
                await room.connect(
                    LIVEKIT_URL, token, options=rtc.RoomOptions(auto_subscribe=True)
                )

                self.active_rooms[room_id] = room_conn
                return room_conn

        except Exception as e:
            logger.error(f"Error connecting to room: {e}")
            await self.cleanup_socket(socket_id, room_id)
            raise

    async def _handle_audio_track_safe(
        self,
        track,
        participant_name: str,
        room_id: str,
        participant_id: str,
        sio: AsyncServer,
    ):
        track_id = f"{room_id}_{participant_name}_{track.sid}"

        try:
            if room_id not in self.audio_handlers:
                self.audio_handlers[room_id] = {}

            if track_id not in self.audio_handlers[room_id]:
                language = await self.redis_manager.get_participant_language(
                    room_id, participant_id
                )

                # Updated to use correct parameters
                handler = OptimizedAudioHandler(
                    speaker_name=participant_name,
                    participant_id=participant_id,
                    room_id=room_id,
                    sio=sio,
                    redis_manager=self.redis_manager,
                    language_code=language,
                    config=AUDIO_CONFIG,
                )

                await handler.start()
                self.audio_handlers[room_id][track_id] = handler

                audio_stream = rtc.AudioStream(
                    track=track, sample_rate=16000, num_channels=1, capacity=100
                )

                try:
                    async for frame_event in audio_stream:
                        if not handler.is_streaming:
                            break
                        await handler.handle_frame(frame_event.frame.data)
                except asyncio.CancelledError:
                    logger.info(f"Audio stream cancelled for {participant_name}")
                finally:
                    await handler.stop()
                    if room_id in self.audio_handlers:
                        self.audio_handlers[room_id].pop(track_id, None)

        except Exception as e:
            logger.error(f"Error in handle_audio_track: {e}")
            logger.error(traceback.format_exc())

    async def cleanup_socket(self, socket_id: str, room_id: str):
        try:
            await self.sio.leave_room(socket_id, room_id)
            if room_id in self.active_rooms:
                room_conn = self.active_rooms[room_id]
                room_conn.sockets.discard(socket_id)

                # Only cleanup room if no sockets left
                if not room_conn.sockets:
                    await self._cleanup_room(room_id)

        except Exception as e:
            logger.error(f"Error in cleanup_socket: {e}")

    async def _cleanup_room(self, room_id: str):
        try:
            if room_id in self.audio_handlers:
                for handler in self.audio_handlers[room_id].values():
                    await handler.stop()
                del self.audio_handlers[room_id]

            if room_id in self.active_rooms:
                room_conn = self.active_rooms[room_id]
                await room_conn.room.disconnect()
                del self.active_rooms[room_id]

        except Exception as e:
            logger.error(f"Error in _cleanup_room: {e}")

    async def initialize(self):
        await self.redis_manager.connect()
        await self.message_queue.connect(RABBITMQ_URL)

    async def _cleanup_room_for_socket(self, room_id: str, socket_id: str):
        """Safely cleanup room resources for a specific socket"""
        try:
            room_conn = self.active_rooms.get(room_id)
            if not room_conn:
                return

            # Remove socket from room
            room_conn.sockets.discard(socket_id)

            # Clean up audio handlers for this socket
            if room_id in self.audio_handlers:
                handlers_to_remove = []
                for track_id, handler in self.audio_handlers[room_id].items():
                    if handler.socket_connection.socket_id == socket_id:
                        try:
                            await handler.stop()
                        except Exception as e:
                            logger.error(f"Error stopping audio handler: {e}")
                        handlers_to_remove.append(track_id)

                # Remove stopped handlers
                for track_id in handlers_to_remove:
                    self.audio_handlers[room_id].pop(track_id, None)

                # Clean up empty room handlers
                if not self.audio_handlers[room_id]:
                    del self.audio_handlers[room_id]

            # Clean up empty rooms
            if not room_conn.sockets:
                await self._cleanup_empty_room(room_id)

        except Exception as e:
            logger.error(f"Error cleaning up room for socket: {e}")

    async def _cleanup_empty_room(self, room_id: str):
        """Safely cleanup an empty room"""
        try:
            if room_id not in self.active_rooms:
                return

            room_conn = self.active_rooms[room_id]

            # Disconnect from LiveKit
            try:
                await room_conn.room.disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting from LiveKit: {e}")

            # Clean up Redis data
            try:
                await self.redis_manager.cleanup_room_data(room_id)
            except Exception as e:
                logger.error(f"Error cleaning up Redis data: {e}")

            # Remove room
            del self.active_rooms[room_id]

            # Cancel cleanup task if it exists
            if room_id in self.cleanup_tasks:
                task = self.cleanup_tasks.pop(room_id)
                if not task.done():
                    task.cancel()

        except Exception as e:
            logger.error(f"Error cleaning up empty room: {e}")

    def _generate_token(self, room_id: str) -> str:
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
                )
            )
            .to_jwt()
        )


# Initialize transcription manager

app = FastAPI()
sio = AsyncServer(
    async_mode="asgi", cors_allowed_origins=["https://staging2.urcalls.com"]
)
socket_app = ASGIApp(sio)
transcription_manager = SocketIOTranscriptionManager(sio)


# Socket.IO event handlers
@sio.on("connect")
async def connect(sid, environ):
    logger.info(f"Client connected: {sid}")


@sio.on("join_room")
async def join_room(sid, data):
    try:
        room_id = data["room_id"]
        participant_id = data.get("participant_id")
        language = data.get("language", "en-US")

        await transcription_manager.connect_to_room(
            room_id=room_id,
            socket_id=sid,
            participant_id=participant_id,
            language=language,
        )

        await sio.emit(
            "connection_status",
            {
                "status": "connected",
                "room_id": room_id,
                "participant_id": participant_id,
            },
            room=sid,
        )

    except Exception as e:
        logger.error(f"Error joining room: {e}")
        await sio.emit("error", {"error": str(e)}, room=sid)


@sio.on("update_language")
async def update_language(sid, data):
    room_id = data.get("room_id")
    participant_id = data.get("participant_id")
    new_language = data.get("language", "en-US")

    if all([room_id, participant_id]):
        await transcription_manager.set_participant_language(
            room_id, participant_id, new_language
        )
        await sio.emit(
            "language_updated",
            {
                "participant_id": participant_id,
                "language": new_language,
            },
            room=sid,
        )


@sio.on("disconnect")
async def disconnect(sid):
    logger.info(f"Client disconnected: {sid}")
    # Handle cleanup as needed


app.mount("/", socket_app)


@app.on_event("startup")
async def startup():
    await transcription_manager.initialize()
