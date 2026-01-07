"""FastAPI backend for LLM Council."""

from fastapi import FastAPI, HTTPException, Request, Response, Depends, Cookie
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import os
import uuid
import json
import asyncio
import hashlib
import secrets

from . import storage
from .council import run_full_council, generate_conversation_title, stage1_collect_responses, stage2_collect_rankings, stage3_synthesize_final, calculate_aggregate_rankings

app = FastAPI(title="LLM Council API")

# Password from environment variable
COUNCIL_PASSWORD = os.getenv("COUNCIL_PASSWORD", "changeme")
SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))

# Store valid session tokens (in production, use Redis or database)
valid_sessions = set()

# Enable CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "https://*.up.railway.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def verify_session(session_token: Optional[str] = Cookie(None)):
    """Check if the session token is valid."""
    if session_token and session_token in valid_sessions:
        return True
    return False


LOGIN_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>LLM Council - Login</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
            background: #f5f5f5;
        }
        .login-box {
            background: white;
            padding: 40px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            text-align: center;
        }
        h1 { margin: 0 0 20px 0; color: #333; }
        input[type="password"] {
            width: 100%;
            padding: 12px;
            margin: 10px 0;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 16px;
            box-sizing: border-box;
        }
        button {
            width: 100%;
            padding: 12px;
            background: #4a90d9;
            color: white;
            border: none;
            border-radius: 4px;
            font-size: 16px;
            cursor: pointer;
        }
        button:hover { background: #357abd; }
        .error { color: red; margin-top: 10px; }
    </style>
</head>
<body>
    <div class="login-box">
        <h1>🏛️ LLM Council</h1>
        <form method="POST" action="/login">
            <input type="password" name="password" placeholder="Enter password" required>
            <button type="submit">Enter</button>
        </form>
        {error}
    </div>
</body>
</html>
"""


@app.get("/login")
async def login_page(error: str = ""):
    """Show login page."""
    error_html = f'<p class="error">{error}</p>' if error else ""
    return HTMLResponse(LOGIN_PAGE.replace("{error}", error_html))


@app.post("/login")
async def login(request: Request):
    """Process login."""
    form = await request.form()
    password = form.get("password", "")
    
    if password == COUNCIL_PASSWORD:
        session_token = secrets.token_hex(32)
        valid_sessions.add(session_token)
        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie(
            key="session_token",
            value=session_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=60*60*24*30  # 30 days
        )
        return response
    else:
        return RedirectResponse(url="/login?error=Invalid+password", status_code=302)


@app.get("/logout")
async def logout(session_token: Optional[str] = Cookie(None)):
    """Log out and clear session."""
    if session_token in valid_sessions:
        valid_sessions.discard(session_token)
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session_token")
    return response


class CreateConversationRequest(BaseModel):
    """Request to create a new conversation."""
    pass


class SendMessageRequest(BaseModel):
    """Request to send a message in a conversation."""
    content: str


class ConversationMetadata(BaseModel):
    """Conversation metadata for list view."""
    id: str
    created_at: str
    title: str
    message_count: int


class Conversation(BaseModel):
    """Full conversation with all messages."""
    id: str
    created_at: str
    title: str
    messages: List[Dict[str, Any]]


def require_auth(session_token: Optional[str] = Cookie(None)):
    """Dependency to require authentication."""
    if not session_token or session_token not in valid_sessions:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


@app.get("/api/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "LLM Council API"}


@app.get("/api/conversations", response_model=List[ConversationMetadata])
async def list_conversations(auth: bool = Depends(require_auth)):
    """List all conversations (metadata only)."""
    return storage.list_conversations()


@app.post("/api/conversations", response_model=Conversation)
async def create_conversation(request: CreateConversationRequest, auth: bool = Depends(require_auth)):
    """Create a new conversation."""
    conversation_id = str(uuid.uuid4())
    conversation = storage.create_conversation(conversation_id)
    return conversation


@app.get("/api/conversations/{conversation_id}", response_model=Conversation)
async def get_conversation(conversation_id: str, auth: bool = Depends(require_auth)):
    """Get a specific conversation with all its messages."""
    conversation = storage.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@app.post("/api/conversations/{conversation_id}/message")
async def send_message(conversation_id: str, request: SendMessageRequest, auth: bool = Depends(require_auth)):
    """
    Send a message and run the 3-stage council process.
    Returns the complete response with all stages.
    """
    conversation = storage.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    is_first_message = len(conversation["messages"]) == 0
    storage.add_user_message(conversation_id, request.content)

    if is_first_message:
        title = await generate_conversation_title(request.content)
        storage.update_conversation_title(conversation_id, title)

    stage1_results, stage2_results, stage3_result, metadata = await run_full_council(
        request.content
    )

    storage.add_assistant_message(
        conversation_id,
        stage1_results,
        stage2_results,
        stage3_result
    )

    return {
        "stage1": stage1_results,
        "stage2": stage2_results,
        "stage3": stage3_result,
        "metadata": metadata
    }


@app.post("/api/conversations/{conversation_id}/message/stream")
async def send_message_stream(conversation_id: str, request: SendMessageRequest, auth: bool = Depends(require_auth)):
    """
    Send a message and stream the 3-stage council process.
    Returns Server-Sent Events as each stage completes.
    """
    conversation = storage.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    is_first_message = len(conversation["messages"]) == 0

    async def event_generator():
        try:
            storage.add_user_message(conversation_id, request.content)

            title_task = None
            if is_first_message:
                title_task = asyncio.create_task(generate_conversation_title(request.content))

            yield f"data: {json.dumps({'type': 'stage1_start'})}\n\n"
            stage1_results = await stage1_collect_responses(request.content)
            yield f"data: {json.dumps({'type': 'stage1_complete', 'data': stage1_results})}\n\n"

            yield f"data: {json.dumps({'type': 'stage2_start'})}\n\n"
            stage2_results, label_to_model = await stage2_collect_rankings(request.content, stage1_results)
            aggregate_rankings = calculate_aggregate_rankings(stage2_results, label_to_model)
            yield f"data: {json.dumps({'type': 'stage2_complete', 'data': stage2_results, 'metadata': {'label_to_model': label_to_model, 'aggregate_rankings': aggregate_rankings}})}\n\n"

            yield f"data: {json.dumps({'type': 'stage3_start'})}\n\n"
            stage3_result = await stage3_synthesize_final(request.content, stage1_results, stage2_results)
            yield f"data: {json.dumps({'type': 'stage3_complete', 'data': stage3_result})}\n\n"

            if title_task:
                title = await title_task
                storage.update_conversation_title(conversation_id, title)
                yield f"data: {json.dumps({'type': 'title_complete', 'data': {'title': title}})}\n\n"

            storage.add_assistant_message(
                conversation_id,
                stage1_results,
                stage2_results,
                stage3_result
            )

            yield f"data: {json.dumps({'type': 'complete'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )


# === STATIC FILE SERVING (MUST BE LAST) ===
static_dir = "/app/frontend/dist"
if os.path.exists(static_dir):
    @app.get("/")
    async def serve_index(session_token: Optional[str] = Cookie(None)):
        if not session_token or session_token not in valid_sessions:
            return RedirectResponse(url="/login", status_code=302)
        return FileResponse(f"{static_dir}/index.html")
    
    app.mount("/assets", StaticFiles(directory=f"{static_dir}/assets"), name="static")
    
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str, session_token: Optional[str] = Cookie(None)):
        # Allow login page without auth
        if full_path.startswith("login"):
            return RedirectResponse(url="/login", status_code=302)
        # Require auth for everything else
        if not session_token or session_token not in valid_sessions:
            return RedirectResponse(url="/login", status_code=302)
        file_path = f"{static_dir}/{full_path}"
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        return FileResponse(f"{static_dir}/index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
