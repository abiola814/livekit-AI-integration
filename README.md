# LiveKit Real-Time Transcription Service

A robust real-time speech transcription service designed to seamlessly integrate with LiveKit video streaming applications. This service provides multi-language transcription support using Amazon Transcribe, with efficient state management through Redis and message queuing via RabbitMQ.

## Features

- Real-time speech-to-text transcription for LiveKit audio streams
- Multi-language support with dynamic language switching
- Scalable architecture using Redis for state management
- Message queuing with RabbitMQ for reliable message delivery
- Socket.IO integration for real-time updates
- Automatic error recovery and reconnection handling
- Configurable audio processing parameters
- Comprehensive logging and monitoring

## Prerequisites

Before you begin, ensure you have the following:

- Python 3.8 or higher
- Docker and Docker Compose
- AWS account with Amazon Transcribe access
- LiveKit server setup
- Redis server
- RabbitMQ server

## Installation

1. Clone the repository:
```bash
git clone https://github.com/abiola814/livekit-AI-integration
cd livekit-transcription
```

2. Create a `.env` file in the project root:
```env
LIVEKIT_URL=wss://your-livekit-server.com
REDIS_URL=redis://redis:6379
RABBITMQ_URL=amqp://guest:guest@rabbitmq:5672/
AWS_ACCESS_KEY_ID=your_aws_access_key
AWS_SECRET_ACCESS_KEY=your_aws_secret_key
AWS_REGION=us-east-2
```

3. Start the services using Docker Compose:
```bash
docker-compose up -d
```

## Configuration

The service can be configured through environment variables or by modifying the `AUDIO_CONFIG` in the code:

```python
AUDIO_CONFIG = {
    "chunk_size": 8000,
    "sample_rate": 16000,
    "channels": 1,
    "delay_seconds": 0.2,
    "buffer_size": 32768,
}
```

## Client-Side Integration

Add the Socket.IO client to your frontend application:

```javascript
const socket = io('your-transcription-server-url', {
  transports: ['websocket'],
  autoConnect: true
});

// Join transcription room
socket.emit('join_room', {
  room_id: 'your-room-id',
  participant_id: 'participant-id',
  language: 'en-US'
});

// Listen for transcriptions
socket.on('message', (data) => {
  if (data.status === 'transcription') {
    console.log(`${data.speaker}: ${data.text}`);
    // Update UI with transcription
  }
});
```

## API Reference

### Socket.IO Events

#### Client to Server

- `join_room`: Join a transcription room
  ```javascript
  {
    room_id: string,
    participant_id: string,
    language: string  // e.g., "en-US"
  }
  ```

- `update_language`: Change transcription language
  ```javascript
  {
    room_id: string,
    participant_id: string,
    language: string
  }
  ```

#### Server to Client

- `message`: Transcription updates
  ```javascript
  {
    status: "transcription",
    speaker: string,
    type: "partial" | "final",
    text: string,
    room_id: string,
    timestamp: string
  }
  ```

- `connection_status`: Room connection status
  ```javascript
  {
    status: "connected",
    room_id: string,
    participant_id: string
  }
  ```

## Architecture

The service consists of several key components:

1. **AudioHandler**: Manages audio stream processing and transcription
2. **TranscriptionManager**: Coordinates room connections and participant management
3. **RedisManager**: Handles state persistence and participant data
4. **MessageQueue**: Manages message distribution using RabbitMQ

## Error Handling

The service includes comprehensive error handling:

- Automatic reconnection for failed streams
- Graceful degradation during high load
- Rate limiting with exponential backoff
- Resource cleanup on disconnection

## Monitoring

The service uses structured logging for monitoring:

```python
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
```

Monitor logs through Docker:
```bash
docker-compose logs -f transcription
```

## Development

To run the service in development mode:

1. Create a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Run the service:
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

## Contributing

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- LiveKit team for their excellent video streaming platform
- Amazon Web Services for their Transcribe service
- The FastAPI team for their high-performance framework
- The Socket.IO team for their real-time communication framework

## Support

For support, please open an issue in the GitHub repository or contact the maintainers.

---
