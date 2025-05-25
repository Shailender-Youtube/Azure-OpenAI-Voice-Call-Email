import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream
from dotenv import load_dotenv
import re
import requests
import msal

# Load environment
load_dotenv()

# Configuration
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")
OPENAI_API_ENDPOINT = os.getenv("OPENAI_API_ENDPOINT")
PORT                = int(os.getenv('PORT', 5050))

# Graph credentials
CLIENT_ID     = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
TENANT_ID     = os.getenv("TENANT_ID")
SENDER_UPN    = os.getenv("SENDER_UPN")  
RECIPIENT     = os.getenv("RECIPIENT")

SYSTEM_MESSAGE = (
    "You are an AI assistant acting as a hotel customer-service agent. "
    "First, greet the caller saying welcome to Sunshine Hotels Special Request Department and ask for their booking ID. "
    "Once they provide the booking ID, ask if they have any special requests, "
    "for example: cake for birthday or anniversary, spa booking, lunch or dinner planning. "
    "Collect exactly two pieces of information: booking ID and special request. "
    "When you have both, respond with valid JSON and nothing else in this format:\n"
    "{\n"
    "  \"booking_id\": \"the booking ID\",\n"
    "  \"special_request\": \"the guest’s request\"\n"
    "}\n"
)
VOICE = 'alloy'
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated',
    'response.done', 'input_audio_buffer.committed',
    'input_audio_buffer.speech_stopped', 'input_audio_buffer.speech_started',
    'session.created'
]
SHOW_TIMING_MATH = False

app = FastAPI()

if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

# MSAL helper to get Graph access token
def acquire_app_token():
    authority = f"https://login.microsoftonline.com/{TENANT_ID}"
    app = msal.ConfidentialClientApplication(
        client_id=CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=authority
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(f"MSAL token error: {result.get('error_description')}")
    token = result["access_token"]
    return token

# Helper to resolve mailbox object ID
def get_user_object_id(token: str, upn: str) -> str:
    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/users/{upn}",
        headers={"Authorization": f"Bearer {token}"}
    )
    resp.raise_for_status()
    return resp.json()["id"]

# Resolve the mailbox's GUID at startup
_INITIAL_TOKEN = acquire_app_token()
SENDER_ID      = get_user_object_id(_INITIAL_TOKEN, SENDER_UPN)
print(">>> Resolved sender object ID:", SENDER_ID)

# Function to send email via Graph
def send_booking_email(booking_id: str, special_request: str, recipient: str):
    token = acquire_app_token()
    url   = f"https://graph.microsoft.com/v1.0/users/{SENDER_ID}/sendMail"
    payload = {
        "message": {
            "subject": f"New special request for booking {booking_id}",
            "body": {
                "contentType": "Text",
                "content": (
                    f"Booking ID: {booking_id}\n"
                    f"Special request: {special_request}"
                )
            },
            "toRecipients": [
                {"emailAddress": {"address": recipient}}
            ]
        },
        "saveToSentItems": "true"
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }


    resp = requests.post(url, headers=headers, json=payload)

    resp.raise_for_status()

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Application is running!"}

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    response = VoiceResponse()
    response.say("Please wait while we connect you to our hotel service.")
    response.pause(length=1)
    response.say("You are now Connected! Wait for the Agent.")
    host = request.url.hostname
    connect = Connect()
    connect.stream(url=f'wss://{host}/media-stream')
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    print("Client connected")
    await websocket.accept()

    async with websockets.connect(
        OPENAI_API_ENDPOINT,
        additional_headers={"api-key": OPENAI_API_KEY}
    ) as openai_ws:
        await initialize_session(openai_ws)

        stream_sid                     = None
        latest_media_timestamp         = 0
        last_assistant_item            = None
        mark_queue                     = []
        response_start_timestamp_twilio= None

        async def receive_from_twilio():
            nonlocal stream_sid, latest_media_timestamp
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data.get("type") == "transcript" and data.get("text"):
                        print(f"User: {data['text']}")
                    if data['event'] == 'media':
                        latest_media_timestamp = int(data['media']['timestamp'])
                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }
                        await openai_ws.send(json.dumps(audio_append))
                    elif data['event'] == 'start':
                        stream_sid = data['start']['streamSid']
                        response_start_timestamp_twilio = None
                        latest_media_timestamp         = 0
                        last_assistant_item            = None
                    elif data['event'] == 'mark':
                        if mark_queue:
                            mark_queue.pop(0)
            except WebSocketDisconnect:
                print("Client disconnected.")
                if openai_ws.open:
                    await openai_ws.close()

        async def send_to_twilio():
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)

                    if response.get("type") == "response.done":
                        for item in response["response"]["output"]:
                            for chunk in item["content"]:
                                if chunk.get("type") == "audio" and chunk.get("transcript"):
                                    text = chunk["transcript"]
                                    print(f"AI:   {text}")
                                    match = re.search(r"\{[^}]+\}", text, re.S)
                                    if match:
                                        data = json.loads(match.group())
                                        send_booking_email(
                                            booking_id=data["booking_id"],
                                            special_request=data["special_request"],
                                            recipient=data["RECIPIENT"]
                                        )

                    if response.get('type') == 'response.audio.delta' and 'delta' in response:
                        audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                        audio_delta = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": { "payload": audio_payload }
                        }
                        await websocket.send_json(audio_delta)

                        if response_start_timestamp_twilio is None:
                            response_start_timestamp_twilio = latest_media_timestamp
                        if response.get('item_id'):
                            last_assistant_item = response['item_id']

                        await send_mark(websocket, stream_sid)

                    if response.get('type') == 'input_audio_buffer.speech_started':
                        print("Speech started detected.")
                        if last_assistant_item:
                            print(f"Interrupting response with id: {last_assistant_item}")
                            await handle_speech_started_event()
            except Exception as e:
                print(f"Error in send_to_twilio: {e}")

        async def handle_speech_started_event():
            nonlocal response_start_timestamp_twilio, last_assistant_item
            print("Handling speech started event.")
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                if SHOW_TIMING_MATH:
                    print(f"Calculating elapsed time for truncation: {elapsed_time}ms")

                if last_assistant_item:
                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": elapsed_time
                    }
                    await openai_ws.send(json.dumps(truncate_event))

                await websocket.send_json({"event": "clear", "streamSid": stream_sid})
                mark_queue.clear()
                last_assistant_item = None
                response_start_timestamp_twilio = None

        async def send_mark(connection, stream_sid):
            if stream_sid:
                mark_event = {"event": "mark", "streamSid": stream_sid, "mark": {"name": "responsePart"}}
                await connection.send_json(mark_event)
                mark_queue.append('responsePart')

        await asyncio.gather(receive_from_twilio(), send_to_twilio())

async def send_initial_conversation_item(openai_ws):
    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Hello! Welcome to Sunshine Hotel Special Request Customer Service Department. Before we proceed further can you please share your booking ID?"}
            ]
        }
    }
    await openai_ws.send(json.dumps(initial_conversation_item))
    await openai_ws.send(json.dumps({"type": "response.create"}))

async def initialize_session(openai_ws):
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": SYSTEM_MESSAGE,
            "modalities": ["text", "audio"],
            "temperature": 0.8,
        }
    }
    await openai_ws.send(json.dumps({"type": "conversation.reset"}))
    print('Sending session update:', json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))

    await send_initial_conversation_item(openai_ws)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
