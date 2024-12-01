"""
bot_runner.py

HTTP service that listens for incoming calls from Twilio,
provisioning a Daily room and starting a Pipecat bot in response.
"""

import aiohttp
import os
import argparse
import subprocess

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse

from pipecat.transports.services.helpers.daily_rest import (
    DailyRESTHelper,
    DailyRoomObject,
    DailyRoomProperties,
    DailyRoomSipParams,
    DailyRoomParams,
)

from dotenv import load_dotenv

load_dotenv(override=True)

# ------------ Configuration ------------ #

MAX_SESSION_TIME = 5 * 60  # 5 minutes
REQUIRED_ENV_VARS = ["OPENAI_API_KEY", "DAILY_API_KEY"]

daily_helpers = {}

# ----------------- API ----------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    aiohttp_session = aiohttp.ClientSession()
    daily_helpers["rest"] = DailyRESTHelper(
        daily_api_key=os.getenv("DAILY_API_KEY", ""),
        daily_api_url=os.getenv("DAILY_API_URL", "https://api.daily.co/v1"),
        aiohttp_session=aiohttp_session,
    )
    yield
    await aiohttp_session.close()

app = FastAPI(lifespan=lifespan)

async def create_daily_room(callId):
    """Create a fresh Daily room for each call"""
    params = DailyRoomParams(
        properties=DailyRoomProperties(
            sip=DailyRoomSipParams(
                display_name="dialin-user",
                video=False,
                sip_mode="dial-in",
                num_endpoints=1
            )
        )
    )

    print(f"Creating new room...")
    room: DailyRoomObject = await daily_helpers["rest"].create_room(params=params)
    print(f"Daily room: {room.url} {room.config.sip_endpoint}")

    # Get token for the bot
    token = await daily_helpers["rest"].get_token(room.url, MAX_SESSION_TIME)

    if not room or not token:
        raise HTTPException(status_code=500, detail=f"Failed to get room or token")

    # Start the bot process
    bot_proc = f"python3 -m bot_twilio -u {room.url} -t {token} -i {callId} -s {room.config.sip_endpoint}"
    try:
        subprocess.Popen(
            [bot_proc], shell=True, bufsize=1, cwd=os.path.dirname(os.path.abspath(__file__))
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start subprocess: {e}")

    return room

@app.post("/twilio_start_bot", response_class=PlainTextResponse)
async def twilio_start_bot(request: Request):
    print(f"POST /twilio_start_bot")

    try:
        form_data = await request.form()
        data = dict(form_data)
    except Exception:
        data = {}

    callId = data.get("CallSid")
    if not callId:
        raise HTTPException(status_code=500, detail="Missing 'CallSid' in request")

    print("CallId: %s" % callId)

    # Create a fresh room for this call
    room: DailyRoomObject = await create_daily_room(callId)

    print(f"Put Twilio on hold...")
    # Put the call on hold until the bot is ready
    resp = VoiceResponse()
    resp.play(
        url="http://com.twilio.sounds.music.s3.amazonaws.com/MARKOVICHAMP-Borghestral.mp3",
        loop=10
    )
    return str(resp)

# ----------------- Main ----------------- #

if __name__ == "__main__":
    # Check environment variables
    for env_var in REQUIRED_ENV_VARS:
        if env_var not in os.environ:
            raise Exception(f"Missing environment variable: {env_var}.")

    parser = argparse.ArgumentParser(description="Pipecat Bot Runner")
    parser.add_argument(
        "--host", type=str, default=os.getenv("HOST", "0.0.0.0"), help="Host address"
    )
    parser.add_argument("--port", type=int, default=os.getenv("PORT", 7860), help="Port number")
    parser.add_argument("--reload", action="store_true", default=True, help="Reload code on change")

    config = parser.parse_args()

    try:
        import uvicorn
        uvicorn.run("bot_runner:app", host=config.host, port=config.port, reload=config.reload)
    except KeyboardInterrupt:
        print("Bot runner shutting down...")
