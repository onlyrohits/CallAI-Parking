import os
import sys
import json
from datetime import datetime, timedelta
import asyncio
import aiohttp
import pytz
import re
from urllib.parse import urlencode
from pipecat.frames.frames import TextFrame, EndFrame, LLMMessagesFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_response import (
    LLMAssistantResponseAggregator,
    LLMUserResponseAggregator,
)
from pipecat.services.cartesia import CartesiaTTSService
from pipecat.services.deepgram import DeepgramTTSService, DeepgramSTTService
from pipecat.services.openai import OpenAILLMService, OpenAILLMContext
from pipecat.transports.network.fastapi_websocket import (
    FastAPIWebsocketTransport,
    FastAPIWebsocketParams,
)
from pipecat.vad.silero import SileroVADAnalyzer
from pipecat.serializers.twilio import TwilioFrameSerializer

from openai.types.chat import ChatCompletionToolParam

from loguru import logger
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv(override=True)
logger.remove(0)
logger.add(sys.stderr, level="DEBUG")

# Airtable configuration
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_ARRIVALS_TABLE = os.getenv("AIRTABLE_ARRIVALS_TABLE")
AIRTABLE_DEPARTURES_TABLE = os.getenv("AIRTABLE_DEPARTURES_TABLE")


async def start_find_booking(function_name, llm, context):
    await llm.push_frame(TextFrame("Let me check that booking for you."))


async def transfer_call(function_name, tool_call_id, arguments, llm, context, result_callback):
    call_sid = arguments.get("call_sid")

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    client = Client(account_sid, auth_token)

    logger.debug(f"Transferring call {call_sid}")

    try:
        client.calls(call_sid).update(
            twiml=f'<Response><Dial>{os.getenv("TRANSFER_NUMBER")}</Dial></Response>'
        )
        result = "The call was transferred successfully, say goodbye to the customer."
        await result_callback(json.dumps({"success": result}))
    except Exception as error:
        logger.error(f"Error transferring call: {str(error)}")
        await result_callback(json.dumps({"error": str(error)}))


def get_current_time():
    # Get the current time in UTC
    utc_now = datetime.now(pytz.utc)

    # Convert to UK time (assuming that's the relevant timezone for Manchester Airport)
    uk_tz = pytz.timezone("Europe/London")
    uk_time = utc_now.astimezone(uk_tz)

    # Format the time as a string
    formatted_time = uk_time.strftime("%I:%M %p")  # e.g., "02:30 PM"

    return {"current_time": formatted_time, "timestamp": uk_time.isoformat()}


# Example of how to use in your main code:
async def handle_get_current_time(
    function_name, tool_call_id, arguments, llm, context, result_callback
):
    current_time_info = get_current_time()
    await result_callback(json.dumps(current_time_info))


async def whatsapp_message(function_name, tool_call_id, arguments, llm, context, result_callback):
    registration = arguments.get("registration")
    is_arrival = arguments.get("is_arrival", False)

    airtable_api_key = os.getenv("AIRTABLE_API_KEY")
    base_id = os.getenv("AIRTABLE_BASE_ID")
    table_name = (
        os.getenv("AIRTABLE_ARRIVALS_TABLE")
        if is_arrival
        else os.getenv("AIRTABLE_DEPARTURES_TABLE")
    )
    twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    twilio_whatsapp_number = os.getenv("TWILIO_WHATSAPP_NUMBER")
    manager_whatsapp_group = os.getenv("MANAGER_WHATSAPP_GROUP")

    formatted_registration = registration.replace(" ", "").upper()

    airtable_url = (
        f"https://api.airtable.com/v0/{base_id}/{table_name}?"
        f'filterByFormula=UPPER({{Registration}})=UPPER("{formatted_registration}")&'
        f"cellFormat=string&timeZone=Europe/London&userLocale=en-gb"
    )

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                airtable_url, headers={"Authorization": f"Bearer {airtable_api_key}"}
            ) as response:
                airtable_data = await response.json()

            if airtable_data["records"]:
                record = airtable_data["records"][0]["fields"]

                vehicle_make = record.get("Vehicle_Make", "N/A")
                name = record.get("Name", "N/A")
                contact_number = record.get("Contact_Number", "N/A")
                entry_date_time = record.get("Entry_Date_Time", "N/A")
                terminal = record.get("Terminal", "N/A")
                estimated_eta = record.get("Current_ETA", "N/A")

                booking_type = "Arrival (Pick-up)" if is_arrival else "Departure (Drop-off)"

                message = f"""
New {booking_type} Booking Requires Driver Assignment:
- Vehicle: {vehicle_make}
- Registration: {registration}
- Customer Name: {name}
- Contact Number: {contact_number}
- {"Arrival" if is_arrival else "Entry"} Date/Time: {entry_date_time}
- Estimated {"Landing Time" if is_arrival else "ETA"}: {estimated_eta}
- Terminal: {terminal}

Please assign a driver for this {"pick-up" if is_arrival else "drop-off"}.
"""

                logger.debug(f"WhatsApp message content: {message}")

                twilio_url = (
                    f"https://api.twilio.com/2010-04-01/Accounts/{twilio_account_sid}/Messages.json"
                )
                auth = aiohttp.BasicAuth(twilio_account_sid, twilio_auth_token)
                data = {
                    "From": f"whatsapp:{twilio_whatsapp_number}",
                    "To": f"whatsapp:{manager_whatsapp_group}",
                    "Body": message,
                }

                async with session.post(
                    twilio_url,
                    auth=auth,
                    data=urlencode(data),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                ) as twilio_response:
                    twilio_data = await twilio_response.json()

                logger.info(f'WhatsApp message sent successfully: {twilio_data["sid"]}')

                await result_callback(
                    json.dumps(
                        {
                            "success": "Manager notified successfully.",
                            "messageId": twilio_data["sid"],
                            "isArrival": is_arrival,
                        }
                    )
                )
            else:
                logger.warning(f"No booking found for registration: {formatted_registration}")
                await result_callback(
                    json.dumps({"error": "No booking found for this registration number."})
                )
        except Exception as error:
            logger.error(f"Error in whatsappMessage function: {str(error)}")
            await result_callback(
                json.dumps({"error": "Failed to process the request.", "details": str(error)})
            )


async def find_booking_by_phone(
    function_name, tool_call_id, arguments, llm, context, result_callback
):
    phone_number = arguments.get("phone_number")
    is_arrival = arguments.get("is_arrival", False)

    logger.debug(f"Finding booking for phone number: {phone_number}, isArrival: {is_arrival}")

    table_name = AIRTABLE_ARRIVALS_TABLE if is_arrival else AIRTABLE_DEPARTURES_TABLE

    # Remove any non-digit characters and ensure the number starts with '44' or '0'
    formatted_phone_number = re.sub(r"\D", "", phone_number)
    if formatted_phone_number.startswith("44"):
        formatted_phone_number = "0" + formatted_phone_number[2:]
    elif not formatted_phone_number.startswith("0"):
        formatted_phone_number = "0" + formatted_phone_number

    url = (
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_name}?"
        f'filterByFormula=OR(SEARCH("{formatted_phone_number}",{{Contact_Number}}),SEARCH("{formatted_phone_number.replace("^0", "44")}",{{Contact_Number}}))&'
        f"cellFormat=string&timeZone=Europe/London&userLocale=en-gb"
    )

    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as response:
                data = await response.json()

            if data["records"]:
                record = data["records"][0]["fields"]

                booking_time = datetime.strptime(record["Entry_Date_Time"], "%d/%m/%Y %H:%M")
                booking_time = pytz.timezone("Europe/London").localize(booking_time)
                formatted_booking_time = booking_time.strftime("%B %d at %I:%M %p")

                result = {
                    "found": True,
                    "customerName": record.get("Name", "Not provided"),
                    "terminal": record.get("Terminal"),
                    "bookingTime": formatted_booking_time,
                    "contactNumber": record.get("Contact_Number"),
                    "allocatedCarPark": record.get("Allocated_Car_Park"),
                    "registration": record.get("Registration"),
                    "isArrival": is_arrival,
                }
                await result_callback(json.dumps(result))
            else:
                await result_callback(
                    json.dumps({"found": False, "error": "No booking found for this phone number."})
                )
        except Exception as error:
            logger.error(f"Error finding booking by phone: {str(error)}")
            await result_callback(
                json.dumps(
                    {"found": False, "error": "Failed to find booking.", "details": str(error)}
                )
            )


async def update_terminal(function_name, tool_call_id, arguments, llm, context, result_callback):
    registration = arguments.get("registration")
    terminal = arguments.get("terminal")
    is_arrival = arguments.get("is_arrival", False)

    timezone = pytz.timezone("Europe/London")
    current_time = datetime.now(timezone)
    logger.debug(
        f"Updating terminal for registration: {registration}, new terminal: {terminal}, isArrival: {is_arrival}, currentTime: {current_time.strftime('%Y-%m-%d %H:%M:%S')}"
    )

    table_name = AIRTABLE_ARRIVALS_TABLE if is_arrival else AIRTABLE_DEPARTURES_TABLE
    formatted_registration = registration.replace(" ", "").upper()

    url = (
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_name}?"
        f'filterByFormula=UPPER({{Registration}})=UPPER("{formatted_registration}")&'
        f"cellFormat=string&timeZone=Europe/London&userLocale=en-gb"
    )

    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if not data["records"]:
                        logger.warning(
                            f"No booking found for registration: {formatted_registration}"
                        )
                        await result_callback(
                            json.dumps({"error": "No booking found for this registration number."})
                        )
                        return

                    record = data["records"][0]
                    record_id = record["id"]

                    patch_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_name}"
                    patch_data = {
                        "records": [{"id": record_id, "fields": {"Terminal": terminal}}],
                        "typecast": True,
                    }

                    async with session.patch(
                        patch_url, headers=headers, json=patch_data
                    ) as patch_response:
                        if patch_response.status == 200:
                            patch_data = await patch_response.json()
                            logger.info(f"Terminal updated successfully. New terminal: {terminal}")
                            await result_callback(
                                json.dumps(
                                    {
                                        "success": "Terminal updated successfully.",
                                        "updatedRecord": patch_data["records"][0],
                                        "updatedTerminal": terminal,
                                        "isArrival": is_arrival,
                                    }
                                )
                            )
                        else:
                            error_text = await patch_response.text()
                            logger.error(f"Error updating terminal: {error_text}")
                            await result_callback(
                                json.dumps(
                                    {"error": "Failed to update terminal.", "details": error_text}
                                )
                            )
                else:
                    error_text = await response.text()
                    logger.error(f"Error response from Airtable: {response.status}")
                    await result_callback(
                        json.dumps(
                            {
                                "error": f"Failed to find booking. Status: {response.status}",
                                "details": error_text,
                            }
                        )
                    )
        except Exception as error:
            logger.error(f"Error updating terminal: {str(error)}")
            await result_callback(
                json.dumps({"error": "Failed to update terminal.", "details": str(error)})
            )


async def update_eta(function_name, tool_call_id, arguments, llm, context, result_callback):
    customer_eta = arguments.get("customer_eta")
    registration = arguments.get("registration")
    is_arrival = arguments.get("is_arrival", False)

    timezone = pytz.timezone("Europe/London")
    current_time = datetime.now(timezone)
    logger.debug(
        f"Updating ETA for registration: {registration}, customerETA: {customer_eta}, isArrival: {is_arrival}, currentTime: {current_time.strftime('%Y-%m-%d %H:%M:%S')}"
    )

    table_name = AIRTABLE_ARRIVALS_TABLE if is_arrival else AIRTABLE_DEPARTURES_TABLE
    formatted_registration = registration.replace(" ", "").upper()

    url = (
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_name}?"
        f'filterByFormula=UPPER({{Registration}})=UPPER("{formatted_registration}")&'
        f"cellFormat=string&timeZone=Europe/London&userLocale=en-gb"
    )

    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if not data["records"]:
                        logger.warning(
                            f"No booking found for registration: {formatted_registration}"
                        )
                        await result_callback(
                            json.dumps({"error": "No booking found for this registration number."})
                        )
                        return

                    record = data["records"][0]
                    record_id = record["id"]

                    eta_time = parse_eta(customer_eta, current_time, timezone)
                    if not eta_time:
                        await result_callback(
                            json.dumps(
                                {
                                    "error": 'Invalid time format provided. Please use format like "30 minutes", "2 hours", or a specific time like "4:30 PM".'
                                }
                            )
                        )
                        return

                    patch_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_name}"
                    patch_data = {
                        "records": [
                            {
                                "id": record_id,
                                "fields": {
                                    "Current_ETA": eta_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                                },
                            }
                        ],
                        "typecast": True,
                    }

                    async with session.patch(
                        patch_url, headers=headers, json=patch_data
                    ) as patch_response:
                        if patch_response.status == 200:
                            patch_data = await patch_response.json()
                            logger.info(
                                f"ETA updated successfully. New ETA: {eta_time.strftime('%Y-%m-%d %H:%M:%S')}"
                            )
                            await result_callback(
                                json.dumps(
                                    {
                                        "success": "ETA updated successfully.",
                                        "updatedRecord": patch_data["records"][0],
                                        "formattedETA": eta_time.strftime("%B %d at %I:%M %p"),
                                        "isArrival": is_arrival,
                                    }
                                )
                            )
                        else:
                            error_text = await patch_response.text()
                            logger.error(f"Error updating ETA: {error_text}")
                            await result_callback(
                                json.dumps(
                                    {
                                        "error": "Failed to update ETA.",
                                        "details": error_text,
                                    }
                                )
                            )
                else:
                    error_text = await response.text()
                    logger.error(f"Error response from Airtable: {response.status}")
                    await result_callback(
                        json.dumps(
                            {
                                "error": f"Failed to find booking. Status: {response.status}",
                                "details": error_text,
                            }
                        )
                    )
        except Exception as error:
            logger.error(f"Error updating ETA: {str(error)}")
            await result_callback(
                json.dumps({"error": "Failed to update ETA.", "details": str(error)})
            )


def parse_eta(eta_string, current_time, timezone):
    # Try parsing as relative time
    relative_regex = r"(\d+)\s*(minutes?|hours?)"
    match = re.match(relative_regex, eta_string, re.IGNORECASE)
    if match:
        value = int(match.group(1))
        unit = "hours" if match.group(2).lower().startswith("hour") else "minutes"
        return current_time + timedelta(**{unit: value})

    # Try parsing as exact time
    try:
        parsed_time = datetime.strptime(eta_string, "%I:%M %p").time()
        eta_date = current_time.date()
        if parsed_time < current_time.time():
            eta_date += timedelta(days=1)
        return timezone.localize(datetime.combine(eta_date, parsed_time))
    except ValueError:
        pass

    # If both parsing methods fail, return None
    return None


async def update_registration(
    function_name, tool_call_id, arguments, llm, context, result_callback
):
    old_registration = arguments.get("old_registration")
    new_registration = arguments.get("new_registration")
    is_arrival = arguments.get("is_arrival", False)

    timezone = pytz.timezone("Europe/London")
    current_time = datetime.now(timezone)
    logger.debug(
        f"Updating registration from: {old_registration} to: {new_registration}, isArrival: {is_arrival}, currentTime: {current_time.strftime('%Y-%m-%d %H:%M:%S')}"
    )

    table_name = AIRTABLE_ARRIVALS_TABLE if is_arrival else AIRTABLE_DEPARTURES_TABLE
    formatted_old_registration = old_registration.replace(" ", "").upper()
    formatted_new_registration = new_registration.replace(" ", "").upper()

    url = (
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_name}?"
        f'filterByFormula=UPPER({{Registration}})=UPPER("{formatted_old_registration}")&'
        f"cellFormat=string&timeZone=Europe/London&userLocale=en-gb"
    )

    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if not data["records"]:
                        logger.warning(
                            f"No booking found for registration: {formatted_old_registration}"
                        )
                        await result_callback(
                            json.dumps({"error": "No booking found for this registration number."})
                        )
                        return

                    record = data["records"][0]
                    record_id = record["id"]

                    patch_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_name}"
                    patch_data = {
                        "records": [
                            {
                                "id": record_id,
                                "fields": {"Registration": formatted_new_registration},
                            }
                        ],
                        "typecast": True,
                    }

                    async with session.patch(
                        patch_url, headers=headers, json=patch_data
                    ) as patch_response:
                        if patch_response.status == 200:
                            patch_data = await patch_response.json()
                            logger.info(
                                f"Registration updated successfully. New registration: {formatted_new_registration}"
                            )
                            await result_callback(
                                json.dumps(
                                    {
                                        "success": "Registration updated successfully.",
                                        "updatedRecord": patch_data["records"][0],
                                        "oldRegistration": formatted_old_registration,
                                        "newRegistration": formatted_new_registration,
                                        "isArrival": is_arrival,
                                    }
                                )
                            )
                        else:
                            error_text = await patch_response.text()
                            logger.error(f"Error updating registration: {error_text}")
                            await result_callback(
                                json.dumps(
                                    {
                                        "error": "Failed to update registration.",
                                        "details": error_text,
                                    }
                                )
                            )
                else:
                    error_text = await response.text()
                    logger.error(f"Error response from Airtable: {response.status}")
                    await result_callback(
                        json.dumps(
                            {
                                "error": f"Failed to find booking. Status: {response.status}",
                                "details": error_text,
                            }
                        )
                    )
        except Exception as error:
            logger.error(f"Error updating registration: {str(error)}")
            await result_callback(
                json.dumps({"error": "Failed to update registration.", "details": str(error)})
            )


class DepartureFlow:
    def __init__(self):
        self.booking_found = False
        self.eta_updated = False
        self.instructions_given = False
        self.staff_notified = False

    def check_completion(self):
        return all(
            [self.booking_found, self.eta_updated, self.instructions_given, self.staff_notified]
        )


# In your main processing loop:
departure_flow = DepartureFlow()

# After finding booking:
departure_flow.booking_found = True

# After updating ETA:
departure_flow.eta_updated = True

# After giving instructions:
departure_flow.instructions_given = True

# After notifying staff:
departure_flow.staff_notified = True

# Before ending conversation:
if not departure_flow.check_completion():
    # Prompt AI to complete missing steps
    pass


async def update_phone_number(
    function_name, tool_call_id, arguments, llm, context, result_callback
):
    registration = arguments.get("registration")
    phone_number = arguments.get("phone_number")
    is_arrival = arguments.get("is_arrival", False)

    timezone = pytz.timezone("Europe/London")
    current_time = datetime.now(timezone)
    logger.debug(
        f"Updating phone number for registration: {registration}, new number: {phone_number}, isArrival: {is_arrival}, currentTime: {current_time.strftime('%Y-%m-%d %H:%M:%S')}"
    )

    table_name = AIRTABLE_ARRIVALS_TABLE if is_arrival else AIRTABLE_DEPARTURES_TABLE
    formatted_registration = registration.replace(" ", "").upper()

    url = (
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_name}?"
        f'filterByFormula=UPPER({{Registration}})=UPPER("{formatted_registration}")&'
        f"cellFormat=string&timeZone=Europe/London&userLocale=en-gb"
    )

    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if not data["records"]:
                        logger.warning(
                            f"No booking found for registration: {formatted_registration}"
                        )
                        await result_callback(
                            json.dumps({"error": "No booking found for this registration number."})
                        )
                        return

                    record = data["records"][0]
                    record_id = record["id"]

                    patch_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_name}"
                    patch_data = {
                        "records": [{"id": record_id, "fields": {"Contact_Number": phone_number}}],
                        "typecast": True,
                    }

                    async with session.patch(
                        patch_url, headers=headers, json=patch_data
                    ) as patch_response:
                        if patch_response.status == 200:
                            patch_data = await patch_response.json()
                            logger.info(
                                f"Phone number updated successfully. New number: {phone_number}"
                            )
                            await result_callback(
                                json.dumps(
                                    {
                                        "success": "Phone number updated successfully.",
                                        "updatedRecord": patch_data["records"][0],
                                        "updatedPhoneNumber": phone_number,
                                        "isArrival": is_arrival,
                                    }
                                )
                            )
                        else:
                            error_text = await patch_response.text()
                            logger.error(f"Error updating phone number: {error_text}")
                            await result_callback(
                                json.dumps(
                                    {
                                        "error": "Failed to update phone number.",
                                        "details": error_text,
                                    }
                                )
                            )
                else:
                    error_text = await response.text()
                    logger.error(f"Error response from Airtable: {response.status}")
                    await result_callback(
                        json.dumps(
                            {
                                "error": f"Failed to find booking. Status: {response.status}",
                                "details": error_text,
                            }
                        )
                    )
        except Exception as error:
            logger.error(f"Error updating phone number: {str(error)}")
            await result_callback(
                json.dumps({"error": "Failed to update phone number.", "details": str(error)})
            )


async def find_booking(function_name, tool_call_id, arguments, llm, context, result_callback):
    registration = arguments.get("registration", "")
    is_arrival = arguments.get("is_arrival", False)

    logger.debug(f"Raw input - registration: {registration}, isArrival: {is_arrival}")

    # Remove any non-alphanumeric characters and convert to uppercase
    formatted_registration = "".join(char for char in registration if char.isalnum()).upper()

    logger.debug(f"Formatted registration: {formatted_registration}")

    # Rest of the function remains the same...

    table_name = AIRTABLE_ARRIVALS_TABLE if is_arrival else AIRTABLE_DEPARTURES_TABLE

    url = (
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_name}?"
        + f'filterByFormula=UPPER({{Registration}})="{formatted_registration}"&'
        + "cellFormat=string&timeZone=Europe/London&userLocale=en-gb"
    )

    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if data["records"]:
                        record = data["records"][0]["fields"]

                        try:
                            booking_time = datetime.strptime(
                                record["Entry_Date_Time"], "%d/%m/%Y %H:%M"
                            )
                            booking_time = pytz.timezone("Europe/London").localize(booking_time)
                            formatted_booking_time = booking_time.strftime("%B %d at %I:%M %p")
                        except ValueError:
                            logger.error(f"Error parsing booking time: {record['Entry_Date_Time']}")
                            formatted_booking_time = "Date format error"

                        contact_number = record.get("Contact_Number", "Not provided")
                        if contact_number != "Not provided":
                            contact_number = " ".join(
                                [
                                    contact_number[i : i + 4]
                                    for i in range(0, len(contact_number), 4)
                                ]
                            )

                        result = {
                            "found": True,
                            "customerName": record.get("Name", "Not provided"),
                            "terminal": record.get("Terminal", "Not provided"),
                            "bookingTime": formatted_booking_time,
                            "contactNumber": contact_number,
                            "allocatedCarPark": record.get("Allocated_Car_Park", "Not provided"),
                            "registration": formatted_registration,
                        }

                        await result_callback(json.dumps(result))
                    else:
                        logger.warning(
                            f"No booking found for registration: {formatted_registration}"
                        )
                        await result_callback(
                            json.dumps(
                                {
                                    "found": False,
                                    "error": f"No booking found for registration {formatted_registration}.",
                                }
                            )
                        )
                else:
                    logger.error(f"Error response from Airtable: {response.status}")
                    await result_callback(
                        json.dumps(
                            {
                                "found": False,
                                "error": f"Failed to find booking. Status: {response.status}",
                            }
                        )
                    )
        except Exception as error:
            logger.error(f"Error finding booking: {str(error)}")
            await result_callback(
                json.dumps(
                    {"found": False, "error": f"Failed to find booking. Error: {str(error)}"}
                )
            )


async def run_bot(websocket_client, stream_sid):
    async with aiohttp.ClientSession() as session:
        try:
            transport = FastAPIWebsocketTransport(
                websocket=websocket_client,
                params=FastAPIWebsocketParams(
                    audio_out_enabled=True,
                    add_wav_header=False,
                    vad_enabled=True,
                    vad_analyzer=SileroVADAnalyzer(),
                    vad_audio_passthrough=True,
                    serializer=TwilioFrameSerializer(stream_sid),
                ),
            )

            llm = OpenAILLMService(
                api_key=os.getenv("GROQ_API_KEY"),
                base_url="https://api.groq.com/openai/v1",
                model="llama-3.1-70b-versatile",
            )
            llm.register_function("find_booking", find_booking)
            llm.register_function("update_terminal", update_terminal)
            llm.register_function("update_registration", update_registration)
            llm.register_function("update_phone_number", update_phone_number)
            llm.register_function("transfer_call", transfer_call)
            llm.register_function("whatsapp_message", whatsapp_message)
            llm.register_function("find_booking_by_phone", find_booking_by_phone)
            llm.register_function("update_eta", update_eta)
            llm.register_function("get_current_time", handle_get_current_time)

            stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))

            tts = DeepgramTTSService(
                aiohttp_session=session,
                api_key=os.getenv("DEEPGRAM_API_KEY"),
                voice="aura-helios-en",
                encoding="linear16",  # or "mulaw" or "alaw" for streaming
                sample_rate=16000,  # choose an appropriate sample rate
                container="none",  # This is the key change
            )

            # tts = CartesiaTTSService(
            #     api_key=os.getenv("CARTESIA_API_KEY"),
            #     voice_id="79a125e8-cd45-4c13-8a67-188112f4dd22",  # British Lady
            # )

            tools = [
                ChatCompletionToolParam(
                    type="function",
                    function={
                        "name": "find_booking",
                        "description": "Find booking information for Manchester Airport Parking",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "registration": {
                                    "type": "string",
                                    "description": "The vehicle registration number",
                                },
                                "is_arrival": {
                                    "type": "boolean",
                                    "description": "True if the customer is arriving, False if departing",
                                },
                            },
                            "required": ["registration", "is_arrival"],
                        },
                    },
                ),
                ChatCompletionToolParam(
                    type="function",
                    function={
                        "name": "update_terminal",
                        "description": "Update the terminal for a booking",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "registration": {
                                    "type": "string",
                                    "description": "The vehicle registration number",
                                },
                                "terminal": {
                                    "type": "string",
                                    "description": "The new terminal number",
                                },
                                "is_arrival": {
                                    "type": "boolean",
                                    "description": "True if the customer is arriving, False if departing",
                                },
                            },
                            "required": ["registration", "terminal", "is_arrival"],
                        },
                    },
                ),
                ChatCompletionToolParam(
                    type="function",
                    function={
                        "name": "update_registration",
                        "description": "Update the registration number for a booking",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "old_registration": {
                                    "type": "string",
                                    "description": "The current vehicle registration number",
                                },
                                "new_registration": {
                                    "type": "string",
                                    "description": "The new vehicle registration number",
                                },
                                "is_arrival": {
                                    "type": "boolean",
                                    "description": "True if the customer is arriving, False if departing",
                                },
                            },
                            "required": ["old_registration", "new_registration", "is_arrival"],
                        },
                    },
                ),
                ChatCompletionToolParam(
                    type="function",
                    function={
                        "name": "update_phone_number",
                        "description": "Update the phone number for a booking",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "registration": {
                                    "type": "string",
                                    "description": "The vehicle registration number",
                                },
                                "phone_number": {
                                    "type": "string",
                                    "description": "The new phone number",
                                },
                                "is_arrival": {
                                    "type": "boolean",
                                    "description": "True if the customer is arriving, False if departing",
                                },
                            },
                            "required": ["registration", "phone_number", "is_arrival"],
                        },
                    },
                ),
                ChatCompletionToolParam(
                    type="function",
                    function={
                        "name": "transfer_call",
                        "description": "Transfer the current call to a human agent",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "call_sid": {
                                    "type": "string",
                                    "description": "The unique identifier for the current call",
                                },
                            },
                            "required": ["call_sid"],
                        },
                    },
                ),
                ChatCompletionToolParam(
                    type="function",
                    function={
                        "name": "whatsapp_message",
                        "description": "Send a WhatsApp message to the manager about a booking",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "registration": {
                                    "type": "string",
                                    "description": "The vehicle registration number",
                                },
                                "is_arrival": {
                                    "type": "boolean",
                                    "description": "True if the customer is arriving, False if departing",
                                },
                            },
                            "required": ["registration", "is_arrival"],
                        },
                    },
                ),
                ChatCompletionToolParam(
                    type="function",
                    function={
                        "name": "find_booking_by_phone",
                        "description": "Find a booking using the customer's phone number",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "phone_number": {
                                    "type": "string",
                                    "description": "The customer's phone number",
                                },
                                "is_arrival": {
                                    "type": "boolean",
                                    "description": "True if the customer is arriving, False if departing",
                                },
                            },
                            "required": ["phone_number", "is_arrival"],
                        },
                    },
                ),
                ChatCompletionToolParam(
                    type="function",
                    function={
                        "name": "update_eta",
                        "description": "Update the estimated time of arrival (ETA) for a booking",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "registration": {
                                    "type": "string",
                                    "description": "The vehicle registration number",
                                },
                                "customer_eta": {
                                    "type": "string",
                                    "description": "The customer's estimated time of arrival. Can be a relative time (e.g., '30 minutes' or '2 hours') or an exact time (e.g., '4:30 PM')",
                                },
                                "is_arrival": {
                                    "type": "boolean",
                                    "description": "True if the customer is arriving, False if departing",
                                },
                            },
                            "required": ["registration", "customer_eta", "is_arrival"],
                        },
                    },
                ),
                ChatCompletionToolParam(
                    type="function",
                    function={
                        "name": "get_current_time",
                        "description": "Get the current time in UK timezone",
                        "parameters": {
                            "type": "object",
                            "properties": {},  # This function doesn't require any parameters
                            "required": [],
                        },
                    },
                ),
            ]

            messages = [
                {
                    "role": "system",
                    "content": """You are Josh, an AI assistant for Manchester Airport Parking. Your role is to handle customer inquiries about parking reservations for car drop-offs and pick-ups efficiently and professionally, following a specific conversation flow.

                                Main Objective:
                                Assist customers with Manchester Airport Parking reservations, focusing on car drop-offs and pick-ups.

                                General Guidelines:
                                - Maintain a professional and helpful tone throughout the conversation.
                                - Keep responses concise and under 500 characters.
                                - Always confirm details before proceeding, especially registration numbers.
                                - Format numbers for clear pronunciation (e.g., phone numbers with dashes: "0742-111-7301").
                                - Pronounce dates and times completely and slowly.
                                - Do not share raw function data with customers.
                                - Avoid using special characters to facilitate audio conversion.
                                - Do not disclose that you are AI or imply that you are human.
                                - Ask for clarification if unsure about any details.
                                - Do not guess or assume booking details or function parameter values.
                                - Avoid unnecessary repetition unless specifically requested by the customer.

                                Conversation Flow:
                                1. Greet the customer and determine their intent (Drop-off or Pick-up).
                                2. For Drop-offs (ensure is_arrival is False):
                                a. Ask for the car registration number and confirm by repeating it back.
                                b. Use find_booking function to locate the booking.
                                c. Confirm booking details one by one: Name, booking time, terminal number, contact phone number.
                                d. Ask for the customer's estimated arrival time, suggesting they check their navigation system. If they provide an estimated time, calculate what the time and confirm with them by repeating it back to them.
                                e. Only after confirming, Update the ETA using the update_eta function.
                                f. Provide specific instructions for drop-off location.
                                g. Notify staff using the whatsapp_message function.
                                h. Ask if there's anything else you can assist with.
                                i. Conclude the call with a polite farewell.

                                3. For Pick-ups:
                                (Follow a similar flow, adapted for pick-up scenarios)

                                Example Conversation for Drop-Off:
                                AI: "Hello! Welcome to Manchester Airport Parking. Are you calling to drop off a car for us to park or have you landed and want us to bring your car to the airport for collection?"
                                Customer: "Hi, yeah I'm calling about a drop-off."
                                AI: "Sure, could I get your car registration number, please?"
                                Customer: "Yeah, it's {Registration}."
                                AI: "Just to confirm, that's {Registration}"
                                Customer: "Yes, that's correct."
                                AI: "Okay, perfect. Give me a moment while I find your booking." 
                                (Use find_booking("find_booking", "test_id", {"registration": registration, "is_arrival": False}, mock_llm, mock_context, result_callback)) 
                                AI: "I've found your booking. The name we have is {Name}, is that correct?"
                                Customer: "Yes, that's correct."
                                AI: "Great. The booking time we have is {booking Time}, is that correct?"
                                Customer: "Yes, it's correct."
                                AI: "Your flight is from {Terminal}, is that right?"
                                Customer: "Yes."
                                AI: "And is {Phone number} still the best number to reach you at?"
                                Customer: "Yes."
                                AI: "Perfect. Could you tell me what time you expect to arrive at {Terminal}? For accuracy, could you check your navigation system?"
                                Customer: "It says I'll arrive at 3:36 PM."
                                AI: "Alright, perfect. I'll update that in our system."
                                (Use update_eta("update_eta", "test_id", {"registration": registration, "customer_eta": customer_eta, "is_arrival": False}, mock_llm, mock_context, result_callback))
                                AI: "When you arrive at Terminal 2, please head to Level 0 of the Multi-Storey Car Park. A driver will be waiting there and will call you when they're close by."
                                AI: "I'll notify our staff to be ready for your arrival."
                                (Use whatsapp_message("whatsapp_message", "test_id", {"registration": registration, "is_arrival": False}, mock_llm, mock_context, result_callback))
                                AI: "Is there anything else I can assist you with today?"
                                Customer: "No, that's all. Thank you."
                                AI: "You're welcome. Safe travels, and we'll see you soon!"

                                Function Usage:
                                - find_booking("find_booking", "test_id", {"registration": registration, "is_arrival": Boolean}, mock_llm, mock_context, result_callback)
                                - update_eta("update_eta", "test_id", {"registration": registration, "customer_eta": customer_eta, "is_arrival": Boolean}, mock_llm, mock_context, result_callback)
                                - update_terminal("update_terminal", "test_id", {"registration": registration, "terminal": terminal, "is_arrival": Boolean}, mock_llm, mock_context, result_callback)
                                - update_registration("update_registration", "test_id", {"old_registration": old_registration, "new_registration": new_registration, "is_arrival": Boolean}, mock_llm, mock_context, result_callback)
                                - update_phone_number("update_phone_number", "test_id", {"registration": registration, "phone_number": phone_number, "is_arrival": Boolean}, mock_llm, mock_context, result_callback)
                                - whatsapp_message("whatsapp_message", "test_id", {"registration": registration, "is_arrival": Boolean}, mock_llm, mock_context, result_callback)
                                - find_booking_by_phone("find_booking_by_phone", "test_id", {"phone_number": phone_number, "is_arrival": Boolean}, mock_llm, mock_context, result_callback)
                                - transfer_call("transfer_call", "test_id", {"call_sid": call_sid}, mock_llm, mock_context, result_callback)
                                - handle_get_current_time("get_current_time", "test_id", {}, mock_llm, mock_context, result_callback)

                                When using these functions:
                                - Use exact function names and parameters as listed.
                                - Do not proceed until each function call is complete.
                                - If no booking is found, ask for alternative registration or use find_booking_by_phone.
                                - Before using functions, inform the customer of the action (e.g., "Let me find your booking").

                                TTS Guidelines:
                                1. Add appropriate punctuation at the end of each statement.
                                2. Format dates as MM/DD/YYYY (e.g., 04/20/2023).
                                3. Insert "-" for pauses in phone numbers and registration numbers.
                                4. Use double question marks for emphasis (e.g., "Are you here??").
                                5. Avoid quotation marks unless referring to a direct quote.

                                Apply these TTS guidelines to all responses without explicitly mentioning them to the user.

                                If a customer requests a transfer:
                                Use transfer_call("transfer_call", "test_id", {"call_sid": call_sid}, mock_llm, mock_context, result_callback) and say, "I'll transfer you to a human agent who can assist you further."

                                Remember to adapt your responses based on the customer's needs and the specific situation while following these guidelines and the conversation flow.""",
                }
            ]

            context = OpenAILLMContext(messages, tools)
            context_aggregator = llm.create_context_aggregator(context)

            pipeline = Pipeline(
                [
                    transport.input(),  # Websocket input from client
                    stt,  # Speech-To-Text
                    context_aggregator.user(),
                    llm,  # LLM
                    tts,  # Text-To-Speech
                    transport.output(),  # Websocket output to client
                    context_aggregator.assistant(),
                ]
            )

            task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))

            @transport.event_handler("on_client_connected")
            async def on_client_connected(transport, client):
                # Kick off the conversation.
                await tts.say(
                    "Hello! Welcome to Manchester Airport Parking. Are you calling to drop off a car for us to park or have you landed and want us to bring your car to the airport for collection??"
                )

            @transport.event_handler("on_client_disconnected")
            async def on_client_disconnected(transport, client):
                await task.queue_frames([TextFrame("Goodbye!")])

            runner = PipelineRunner(handle_sigint=False)

            await runner.run(task)

        except Exception as e:
            logger.error(f"Error in run_bot: {str(e)}")
        finally:
            # Ensure all tasks are properly cancelled and resources are cleaned up
            if "runner" in locals():
                await runner.stop()
            if "pipeline" in locals():
                await pipeline.stop()


if __name__ == "__main__":
    print("This script should be imported and used by server.py, not run directly.")
