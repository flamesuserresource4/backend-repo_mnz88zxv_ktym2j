import os
import json
import uuid
from typing import Dict, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------
# In-memory matchmaking state
# ------------------------------
waiting_queue: list[WebSocket] = []
partner_map: Dict[WebSocket, WebSocket] = {}
room_map: Dict[WebSocket, str] = {}

async def pair_clients():
    """If at least two clients are waiting, pair them into a room."""
    while len(waiting_queue) >= 2:
        a = waiting_queue.pop(0)
        b = waiting_queue.pop(0)
        room_id = str(uuid.uuid4())
        partner_map[a] = b
        partner_map[b] = a
        room_map[a] = room_id
        room_map[b] = room_id
        # Inform both sides
        try:
            await a.send_json({"type": "matched", "roomId": room_id})
        except Exception:
            # if sending fails, clean up and requeue b
            partner_map.pop(a, None)
            partner_map.pop(b, None)
            room_map.pop(a, None)
            room_map.pop(b, None)
            waiting_queue.insert(0, b)
            continue
        try:
            await b.send_json({"type": "matched", "roomId": room_id})
        except Exception:
            # if sending fails to b, notify a and requeue a
            try:
                await a.send_json({"type": "partner_disconnect"})
            except Exception:
                pass
            partner_map.pop(a, None)
            partner_map.pop(b, None)
            room_map.pop(a, None)
            room_map.pop(b, None)
            waiting_queue.insert(0, a)

async def safe_send(ws: WebSocket, data: dict):
    try:
        await ws.send_json(data)
    except Exception:
        pass

async def disconnect(ws: WebSocket):
    # Remove from waiting queue if present
    if ws in waiting_queue:
        try:
            waiting_queue.remove(ws)
        except ValueError:
            pass
    # Notify partner if paired
    partner: Optional[WebSocket] = partner_map.pop(ws, None)
    room_id = room_map.pop(ws, None)
    if partner is not None:
        partner_map.pop(partner, None)
        room_map.pop(partner, None)
        await safe_send(partner, {"type": "partner_disconnect", "roomId": room_id})
        # Requeue partner to find a new match automatically
        try:
            waiting_queue.append(partner)
            await safe_send(partner, {"type": "searching"})
            await pair_clients()
        except Exception:
            pass

@app.get("/")
def read_root():
    return {"message": "Anonymous Chat Backend is running"}

@app.get("/api/hello")
def hello():
    return {"message": "Hello from the backend API!"}

@app.get("/test")
def test_database():
    """Test endpoint to check if database is available and accessible"""
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    
    try:
        # Try to import database module
        from database import db
        
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            
            # Try to list collections to verify connectivity
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]  # Show first 10 collections
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
            
    except ImportError:
        response["database"] = "❌ Database module not found (run enable-database first)"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"
    
    # Check environment variables
    import os
    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"
    
    return response

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    # Mark as searching immediately
    await safe_send(websocket, {"type": "searching"})
    # Add to waiting queue and attempt to pair
    waiting_queue.append(websocket)
    await pair_clients()

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                msg = {"type": "chat", "text": raw}
            msg_type = msg.get("type")

            if msg_type == "next":
                # Leave current partner and requeue
                partner = partner_map.pop(websocket, None)
                room_id = room_map.pop(websocket, None)
                if partner is not None:
                    partner_map.pop(partner, None)
                    room_map.pop(partner, None)
                    await safe_send(partner, {"type": "partner_disconnect", "roomId": room_id})
                    waiting_queue.append(partner)
                    await safe_send(partner, {"type": "searching"})
                waiting_queue.append(websocket)
                await safe_send(websocket, {"type": "searching"})
                await pair_clients()
                continue

            if msg_type == "typing":
                partner = partner_map.get(websocket)
                if partner:
                    await safe_send(partner, {"type": "typing"})
                continue

            if msg_type == "chat":
                text = msg.get("text", "")
                partner = partner_map.get(websocket)
                if partner:
                    await safe_send(partner, {"type": "chat", "text": text})
                else:
                    await safe_send(websocket, {"type": "system", "text": "Waiting for a partner..."})
                continue

            # Unknown types are ignored
    except WebSocketDisconnect:
        await disconnect(websocket)
    except Exception:
        await disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
