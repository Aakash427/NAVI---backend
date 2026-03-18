# Navi Backend

Navi is an intelligent AI agent that automates browser-based tasks across any web portal. The backend provides a conversational API for credential management, task execution, and result interpretation.

## Features

- **Universal Portal Automation**: Execute tasks on any web portal using TinyFish browser automation
- **Intelligent Routing**: Smart message routing with follow-up reasoning and execution guards
- **Credential Management**: Secure encrypted credential storage with Fernet encryption
- **Live Preview**: Real-time browser preview streaming during execution
- **Result Memory**: Conversational follow-up questions about previous results without re-execution
- **Execution Blueprints**: Learning from successful runs for faster, more reliable repeat executions

## Local Development

### Prerequisites

- Python 3.9+
- pip

### Setup

1. Clone the repository:
```bash
git clone <your-repo-url>
cd backend
```

2. Create a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Create `.env` file from example:
```bash
cp .env.example .env
```

5. Configure environment variables in `.env`:
```
GEMINI_API_KEY=your_gemini_api_key
TINYFISH_API_KEY=your_tinyfish_api_key
ENCRYPTION_KEY=your_encryption_key
PORT=5000
```

To generate an encryption key:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

6. Run the development server:
```bash
python app.py
```

The server will start at `http://localhost:5000`

### Health Check

```bash
curl http://localhost:5000/health
```

Expected response:
```json
{"ok": true}
```

## API Endpoints

### Core Routes

- `POST /api/chat` - Main conversational chat endpoint
- `GET /api/session/<session_id>/status` - Poll session status for live preview
- `GET /api/nodes` - Get saved portal nodes
- `DELETE /api/nodes/<node_id>` - Delete a saved node
- `GET /health` - Health check endpoint

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `GEMINI_API_KEY` | Google Gemini API key for AI reasoning | Yes |
| `TINYFISH_API_KEY` | TinyFish API key for browser automation | Yes |
| `ENCRYPTION_KEY` | Fernet encryption key for credential storage | Yes |
| `PORT` | Server port (default: 5000) | No |

## Deployment

### Render / Railway Deployment

1. **Build Command**:
```bash
pip install -r requirements.txt
```

2. **Start Command**:
```bash
gunicorn app:app
```

3. **Environment Variables**:
Set the following in your deployment platform:
- `GEMINI_API_KEY`
- `TINYFISH_API_KEY`
- `ENCRYPTION_KEY`
- `PORT` (automatically set by Render/Railway)

4. **Database**:
The SQLite database (`navi.db`) will be created automatically on first run. Note that ephemeral filesystems (like Render free tier) will reset the database on each deployment.

### Client Configuration

Both web and mobile clients should point to your deployed backend URL:

```javascript
// Example frontend configuration
const API_BASE_URL = 'https://your-navi-backend.onrender.com';
```

## Architecture

- **Router**: Intelligent message routing (new_task, repeat_run, followup_reasoning, general_chat)
- **Session Manager**: Credential collection and execution state management
- **Result Handler**: Universal result normalization and Gemini interpretation
- **Execution Blueprints**: Navigation intelligence stored from successful runs
- **Live Status Polling**: Real-time execution status and streaming preview URLs

## Database

SQLite database (`navi.db`) stores:
- **Nodes**: Saved portal connections with encrypted credentials
- **Sessions**: Execution sessions with state and results (in-memory, persisted to DB)

## Security

- Credentials encrypted with Fernet symmetric encryption
- API keys stored in environment variables
- `.env` file excluded from git
- No hardcoded secrets in codebase

## Development Notes

- Database path is deployment-safe using relative paths
- App binds to `0.0.0.0` for external access
- Port reads from `PORT` environment variable
- CORS enabled for web client access
- Health endpoint for deployment monitoring

## License

Proprietary
