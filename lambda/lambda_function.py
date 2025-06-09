# VERSION 0.2 Limad44

CODE_VERS = 0.2
JEEDOM_URL = "https://xxxxxxxxxxxx.xx/"
APIKEY = ""

DEBUG = True
VERIFY_SSL = True
TOKEN = ""
QUESTION_URL = f"plugins/alexaapiv2/core/php/askQuestion.php?apikey={APIKEY}"
REPONSE_URL = f"plugins/alexaapiv2/core/php/alexa_push.php?apikey={APIKEY}&command=reponseASK"

import json
import logging
from typing import Union, Optional
import isodate
import urllib3
from ask_sdk_core.dispatch_components import AbstractExceptionHandler, AbstractRequestHandler, AbstractRequestInterceptor
from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.utils import (
    get_account_linking_access_token,
    is_request_type,
    is_intent_name,
    get_intent_name,
    get_slot,
    get_slot_value,
)
from ask_sdk_model import SessionEndedReason
from ask_sdk_model.slu.entityresolution import StatusCode
from urllib3 import HTTPResponse

import prompts
from schemas import JeeState, JeeStateError
from utils import get_logger
from const import (
    INPUT_TEXT_ENTITY,
    RESPONSE_YES,
    RESPONSE_NO,
    RESPONSE_NONE,
    RESPONSE_SELECT,
    RESPONSE_NUMERIC,
    RESPONSE_DURATION,
    RESPONSE_STRING,
    RESPONSE_DATE_TIME,
)

JEEDOM_URL = JEEDOM_URL.rstrip("/")
logger = get_logger(DEBUG)

def _handle_response(handler, speak_out: Optional[str]):
    """
    This function has the purpose of allowing the suspension of the default Okay response
    so the user can have Jeedom do a custom response or follow-up question.

    Fixed issue: #147

    :param handler:
    :param speak_out:
    :return:
    """
    if speak_out:
        return handler.response_builder.speak(speak_out).response
    return handler.response_builder.response

class Borg:
    """Borg MonoState Class for State Persistence."""
    _shared_state = {}
    def __init__(self):
        self.__dict__ = self._shared_state

def _init_http_pool():
    return urllib3.PoolManager(
        cert_reqs="CERT_REQUIRED" if VERIFY_SSL else "CERT_NONE",
        timeout=urllib3.Timeout(connect=10.0, read=10.0)
    )

def _string_to_bool(value: Optional[str], default: bool = False) -> bool:
    """
    Used because we need to convert boolean values passed in strings since
    entity states don't natively support json and are treated as strings.

    :param value:
    :param default:
    :return:
    """
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return default
    value = value.lower()
    if value == "true":
        return True
    if value == "false":
        return False
    return default

class Jeedom(Borg):
    jee_state: Optional[Union[JeeState, JeeStateError]]

    def __init__(self, handler_input=None):
        super().__init__()
        self.jee_state = None
        self.http = _init_http_pool()
        if handler_input:
            self.handler_input = handler_input
            self.language_strings = handler_input.attributes_manager.request_attributes["_"]
            self.token = self._fetch_token() if not TOKEN else TOKEN
            logger.debug(self.token)
            self.get_jee_question()

    def _fetch_token(self):
        return get_account_linking_access_token(self.handler_input)

    def _set_jee_error(self, prompt: str):
        """
        Sets the self.jee_state to the error prompt

        Used when a function fails and alexa should say the error message instead of the
        intended one

        :param prompt: Value obtained from prompts file
        :return:
        """
        self.jee_state = JeeStateError(text=self.language_strings[prompt])

    @staticmethod
    def _build_url(*_):
        """
        Builds the url from paths given

        :param path:
        :return:
        """
        logger.debug(f"Création url {JEEDOM_URL}/{QUESTION_URL}")
        return f"{JEEDOM_URL}/{QUESTION_URL}"

    @staticmethod
    def _build_url_post(*_):
        """
        Builds the url from paths given POST

        :param path:
        :return:
        """
        logger.debug(f"Création url POST : {JEEDOM_URL}/{REPONSE_URL}")
        return f"{JEEDOM_URL}/{REPONSE_URL}"

    def _get_headers(self):
        """
        Returns the request headers

        :return:
        """

        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def _check_response_errors(self, response: HTTPResponse) -> Union[bool, str]:
        if response.status == 401:
            logger.error("401 Error from Jeedom.")
            logger.debug(response.data)
            return f"Error 401 {self.language_strings[prompts.ERROR_401]}"
        if response.status == 404:
            logger.error("404 Error from Jeedom.")
            logger.debug(response.data)
            return f"Error 404 {self.language_strings[prompts.ERROR_404]}"
        if response.status >= 400:
            logger.error(f"{response.status} Error from Jeedom.")
            logger.debug(response.data)
            return f"Error {response.status}, {self.language_strings[prompts.ERROR_400]}"
        return False

    def _get(self, *path: str, extra_headers: Optional[dict] = None):
        """
        Performs a request

        :param path:
        :param headers:
        :param params:
        :return:
        """
        headers = self._get_headers()
        if extra_headers:
            headers.update(extra_headers)
        url = self._build_url(*path)
        response = self.http.request("GET", url, headers=headers)
        errors = self._check_response_errors(response)
        if errors:
            self.jee_state = JeeStateError(text=errors)
            logger.debug(self.jee_state)
            return None
        return response

    def _post(self, *path: str, body: dict, extra_headers: Optional[dict] = None):
        """
        Performs a request

        :param path:
        :param headers:
        :param params:
        :return:
        """
        headers = self._get_headers()
        if extra_headers:
            headers.update(extra_headers)
        url = self._build_url_post(*path)
        logger.debug(json.dumps(body))
        response = self.http.request("POST", url, headers=headers, body=json.dumps(body).encode("utf-8"))
        errors = self._check_response_errors(response)
        if errors:
            self.jee_state = JeeStateError(text=errors)
            logger.debug(self.jee_state)
            return None
        return response

    def _decode_response(self, response) -> Optional[dict]:
        """
        Decodes the response into a json object

        :param response:
        :return: Json object or None
        """
        
        decoded_response = json.loads(response.data.decode("utf-8")).get("state")
        logger.debug(f"Decoded response: {decoded_response}")
        if decoded_response:
            return json.loads(decoded_response)
        logger.error("No entity state provided by Jeedom.")
        self._set_jee_error(prompts.ERROR_CONFIG)
        logger.debug(self.jee_state)
        return

    def clear_state(self):
        """
        Clear the state of the local Jeedom object.
        """
        logger.debug("Clearing Jeedom local state")
        self.jee_state = None

    def get_jee_question(self):
        """
        Updates the local HA state with the servers state

        Used for getting the text to speak, event_id as well as other passable variables
        """
        logger.debug(f"INPUT_TEXT_ENTITY: {INPUT_TEXT_ENTITY}")
        response = self._get("api", "states", INPUT_TEXT_ENTITY)
        if not response:
            return
        response = self._decode_response(response)
        if not response:
            return
        self.jee_state = JeeState(
            event_id=response.get("event"),
            suppress_confirmation=_string_to_bool(response.get("suppress_confirmation")),
            text=response.get("text"),
            deviceSerialNumber=response.get("deviceSerialNumber"),
            textBrut=response.get("textBrut"),
        )
        logger.debug(self.jee_state)

    def post_to_jeedom(self, response: str, response_type: str, **kwargs) -> Optional[str]:
        """
        Posts an event to the Jeedom server.

        :param response: The response to send to the Jeedom server.
        :param response_type: The type of response to send to the Jeedom server.
        :param kwargs: Additional parameters to send to the Jeedom server.
        :return: The text to speak to the user.
        """
        body = {
            "event_id": self.jee_state.event_id,
            "event_response": response,
            "event_response_type": response_type,
            "deviceSerialNumber": self.jee_state.deviceSerialNumber,
            "textBrut": self.jee_state.textBrut,
            "code_version": CODE_VERS
        }
        body.update(kwargs)
        if getattr(self.handler_input.request_envelope.context.system, "person", None):
            body["event_person_id"] = self.handler_input.request_envelope.context.system.person.person_id
        resp = self._post("api", "events", "alexa_actionable_notification", body=body)
        if not resp:
            return self.jee_state.text
        if not self.jee_state.suppress_confirmation:
            self.clear_state()
            return self.language_strings[prompts.OKAY]
        self.clear_state()
        return ""

    def get_value_for_slot(self, slot_name):
        slot = get_slot(self.handler_input, slot_name=slot_name)
        if slot and slot.resolutions and slot.resolutions.resolutions_per_authority:
            for resolution in slot.resolutions.resolutions_per_authority:
                if resolution.status.code == StatusCode.ER_SUCCESS_MATCH:
                    for value in resolution.values:
                        if value.value and value.value.name:
                            return value.value.name

class LaunchRequestHandler(AbstractRequestHandler):
    """Handler for Skill Launch."""

    def can_handle(self, handler_input):
        return is_request_type("LaunchRequest")(handler_input)
    def handle(self, handler_input):
        jee_obj = Jeedom(handler_input)
        speak_output = jee_obj.jee_state.text
        event_id = jee_obj.jee_state.event_id
        handler = handler_input.response_builder.speak(speak_output)
        if event_id:
            handler.ask("")
        return handler.response

class YesIntentHandler(AbstractRequestHandler):
    """Handler for Yes Intent."""

    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.YesIntent")(handler_input)
    def handle(self, handler_input):
        logger.info("Yes Intent Handler triggered")
        jee_obj = Jeedom(handler_input)
        speak_output = jee_obj.post_to_jeedom(RESPONSE_YES, RESPONSE_YES)
        return _handle_response(handler_input, speak_output)

class NoIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.NoIntent")(handler_input)
    def handle(self, handler_input):
        logger.info("No Intent Handler triggered")
        jee_obj = Jeedom(handler_input)
        speak_output = jee_obj.post_to_jeedom(RESPONSE_NO, RESPONSE_NO)
        return _handle_response(handler_input, speak_output)

class NumericIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("Number")(handler_input)
    def handle(self, handler_input):
        logger.info("Numeric Intent Handler triggered")
        jee_obj = Jeedom(handler_input)
        number = get_slot_value(handler_input, "Numbers")
        logger.debug(f"Number: {number}")
        if number == "?":
            raise
        speak_output = jee_obj.post_to_jeedom(number, RESPONSE_NUMERIC)
        return _handle_response(handler_input, speak_output)

class StringIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("String")(handler_input)
    def handle(self, handler_input):
        logger.info("String Intent Handler triggered")
        jee_obj = Jeedom(handler_input)
        strings = get_slot_value(handler_input, "Strings")
        logger.debug(f"String: {strings}")
        speak_output = jee_obj.post_to_jeedom(strings, RESPONSE_STRING)
        return _handle_response(handler_input, speak_output)

class SelectIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("Select")(handler_input)
    def handle(self, handler_input):
        logger.info("Selection Intent Handler triggered")
        jee_obj = Jeedom(handler_input)
        selection = jee_obj.get_value_for_slot("Selections")
        logger.debug(f"Selection: {selection}")
        if not selection:
            raise
        jee_obj.post_to_jeedom(selection, RESPONSE_SELECT)
        data = handler_input.attributes_manager.request_attributes["_"]
        speak_output = data[prompts.SELECTED].format(selection)
        return _handle_response(handler_input, speak_output)

class DurationIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("Duration")(handler_input)
    def handle(self, handler_input):
        logger.info("Duration Intent Handler triggered")
        jee_obj = Jeedom(handler_input)
        duration = get_slot_value(handler_input, "Durations")
        logger.debug(f"Duration: {duration}")
        speak_output = jee_obj.post_to_jeedom(isodate.parse_duration(duration).total_seconds(), RESPONSE_DURATION)
        return _handle_response(handler_input, speak_output)

class DateTimeIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("Date")(handler_input)
    def handle(self, handler_input):
        logger.info("Date Intent Handler triggered")
        jee_obj = Jeedom(handler_input)
        date = get_slot_value(handler_input, "Dates")
        time = get_slot_value(handler_input, "Times")
        logger.debug(f"Dates: {date} of type {type(date)}")
        logger.debug(f"Times: {time} of type {type(time)}")
        if not date and not time:
            raise
        speak_output = jee_obj.post_to_jeedom(
            json.dumps({**self._parse_date(date), **self._parse_time(time)}), RESPONSE_DATE_TIME
        )
        return _handle_response(handler_input, speak_output)
    @staticmethod
    def _parse_date(date: str) -> dict:
        if not date:
            return {"day": None, "month": None, "year": None}
        parts = date.split("-")
        return {
            "day": parts[2] if len(parts) >= 3 else None,
            "month": parts[1] if len(parts) >= 2 else None,
            "year": parts[0] if len(parts) >= 1 else None,
        }
    @staticmethod
    def _parse_time(time: str) -> dict:
        if not time:
            return {"seconds": None, "minute": None, "hour": None}
        t = time.lower()
        if "s" in t:
            return {"seconds": t.replace("s", ""), "minute": None, "hour": None}
        if "m" in t:
            return {"seconds": None, "minute": t.replace("m", ""), "hour": None}
        if "h" in t:
            return {"seconds": None, "minute": None, "hour": t.replace("h", "")}
        parts = time.split(":")
        return {
            "seconds": parts[2] if len(parts) >= 3 else None,
            "minute": parts[1] if len(parts) >= 2 else None,
            "hour": parts[0] if len(parts) >= 1 else None,
        }

class CancelOrStopIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.CancelIntent")(handler_input) or is_intent_name("AMAZON.StopIntent")(handler_input)
    def handle(self, handler_input):
        logger.info("Cancel or Stop Intent Handler triggered")
        data = handler_input.attributes_manager.request_attributes["_"]
        speak_output = data[prompts.STOP_MESSAGE]
        return _handle_response(handler_input, speak_output)

class SessionEndedRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_request_type("SessionEndedRequest")(handler_input)
    def handle(self, handler_input):
        logger.info("Session Ended Request Handler triggered")
        jee_obj = Jeedom(handler_input)
        reason = handler_input.request_envelope.request.reason
        if reason in (SessionEndedReason.EXCEEDED_MAX_REPROMPTS, SessionEndedReason.USER_INITIATED):
            jee_obj.post_to_jeedom(RESPONSE_NONE, RESPONSE_NONE)
        return handler_input.response_builder.response

class IntentReflectorHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_request_type("IntentRequest")(handler_input)
    def handle(self, handler_input):
        logger.info("Reflector Intent triggered")
        intent_name = get_intent_name(handler_input)
        speak_output = f"Vous avez lancé {intent_name}."
        return handler_input.response_builder.speak(speak_output).response

class CatchAllExceptionHandler(AbstractExceptionHandler):
    def can_handle(self, handler_input, exception):
        return True
    def handle(self, handler_input, exception):
        logger.info("Catch All Exception triggered")
        logger.error(exception, exc_info=True)
        jee_obj = Jeedom()
        data = handler_input.attributes_manager.request_attributes["_"]
        if jee_obj.jee_state and jee_obj.jee_state.text:
            speak_output = data[prompts.ERROR_ACOUSTIC].format(jee_obj.jee_state.text)
            return handler_input.response_builder.speak(speak_output).ask("").response
        speak_output = data[prompts.ERROR_CONFIG].format(jee_obj.jee_state.text)
        return handler_input.response_builder.speak(speak_output).response

class LocalizationInterceptor(AbstractRequestInterceptor):
    def process(self, handler_input):
        locale = handler_input.request_envelope.request.locale
        logger.info(f"Locale is {locale[:2]}")
        with open("language_strings.json", encoding="utf-8") as language_prompts:
            language_data = json.load(language_prompts)
        data = language_data[locale[:2]]
        if locale in language_data:
            data.update(language_data[locale])
        handler_input.attributes_manager.request_attributes["_"] = data

sb = SkillBuilder()
sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(YesIntentHandler())
sb.add_request_handler(NoIntentHandler())
sb.add_request_handler(StringIntentHandler())
sb.add_request_handler(SelectIntentHandler())
sb.add_request_handler(NumericIntentHandler())
sb.add_request_handler(DurationIntentHandler())
sb.add_request_handler(DateTimeIntentHandler())
sb.add_request_handler(CancelOrStopIntentHandler())
sb.add_request_handler(SessionEndedRequestHandler())
sb.add_request_handler(IntentReflectorHandler())
sb.add_exception_handler(CatchAllExceptionHandler())
sb.add_global_request_interceptor(LocalizationInterceptor())
lambda_handler = sb.lambda_handler()