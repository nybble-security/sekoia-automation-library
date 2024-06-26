import orjson
from functools import cached_property

from datetime import datetime, timedelta, timezone
from enum import Enum
import time

from google.oauth2 import service_account
from googleapiclient.discovery import build
from google_module.base import GoogleTrigger
from google_module.metrics import FORWARD_EVENTS_DURATION, INCOMING_MESSAGES, OUTCOMING_EVENTS

from sekoia_automation.storage import PersistentJSON
from sekoia_automation.connector import DefaultConnectorConfiguration

from requests.exceptions import HTTPError
from urllib3.exceptions import HTTPError as BaseHTTPError
from google.auth.exceptions import TransportError
from httplib2.error import ServerNotFoundError


class ApplicationName(str, Enum):
    ACCESS_TRANSPARENCY = "access_transparency"
    ADMIN = "admin"
    CALENDAR = "calendar"
    CHAT = "chat"
    DRIVE = "drive"
    GCP = "gcp"
    GPLUS = "gplus"
    GROUPS = "groups"
    GROUPS_ENTERPRISE = "groups_enterprise"
    JAMBOARD = "jamboard"
    LOGIN = "login"
    MEET = "meet"
    MOBILE = "mobile"
    RULES = "rules"
    SAML = "saml"
    TOKEN = "token"
    USER_ACCOUNTS = "user_accounts"
    CONTEXT_AWARE_ACCESS = "context_aware_access"
    CHROME = "chrome"
    DATA_STUDIO = "data_studio"
    KEEP = "keep"


class GoogleReportsConfig(DefaultConnectorConfiguration):
    admin_mail: str
    frequency: int = 20
    application_name: ApplicationName = ApplicationName.DRIVE
    chunk_size: int = 1000


class GoogleReports(GoogleTrigger):
    """
    Connect to Google Reports API and return the results

    Good to know : This's the parent class for all other Google reports applications classes
    """

    configuration: GoogleReportsConfig

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.context = PersistentJSON("context.json", self._data_path)
        self.service_account_path = self.CREDENTIALS_PATH
        self.scopes = [
            "https://www.googleapis.com/auth/admin.reports.audit.readonly",
            "https://www.googleapis.com/auth/admin.reports.usage.readonly",
        ]
        self.events_sum = 0
        self.from_date = ""

    @property
    def most_recent_date_seen(self):
        now = datetime.now(timezone.utc)

        with self.context as cache:
            app_key_name_in_cache = "most_recent_date_seen_" + self.configuration.application_name.value
            most_recent_date_seen_str = cache.get(app_key_name_in_cache) or cache.get("most_recent_date_seen")

            # if undefined, retrieve events from the last day
            if most_recent_date_seen_str is None:
                before_one_day = now - timedelta(days=1)
                return before_one_day.strftime("%Y-%m-%dT%H:%M:%SZ")

            # parse the most recent date seen
            most_recent_date_seen = datetime.strptime(most_recent_date_seen_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )

            # We don't retrieve messages older than almost 6 months
            six_months_ago = now - timedelta(days=180)
            if most_recent_date_seen < six_months_ago:
                most_recent_date_seen = six_months_ago

            return most_recent_date_seen.strftime("%Y-%m-%dT%H:%M:%SZ")

    @most_recent_date_seen.setter
    def most_recent_date_seen(self, recent_date):
        add_one_seconde = datetime.strptime(recent_date, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        ) + timedelta(seconds=1)
        self.from_date = add_one_seconde.strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.context as cache:
            app_key_name_in_cache = "most_recent_date_seen_" + self.configuration.application_name.value
            cache[app_key_name_in_cache] = self.from_date

    @cached_property
    def pagination_limit(self):
        return max(self.configuration.chunk_size, 1000)

    def run(self):
        self.log(
            message=f"Starting Google Reports api for {self.configuration.application_name.value} application at {self.most_recent_date_seen}",
            level="info",
        )

        try:
            while self.running:
                start = time.time()

                try:
                    self.get_reports_events()

                except (HTTPError, BaseHTTPError) as ex:
                    self.log_exception(ex, message="Failed to get next batch of events")
                except Exception as ex:
                    self.log(
                        message=f"An unknown exception occurred : {str(ex)}",
                        level="error",
                    )
                    raise

                # compute the duration of the last events fetching
                duration = int(time.time() - start)
                FORWARD_EVENTS_DURATION.labels(intake_key=self.configuration.intake_key).observe(duration)

                # Compute the remaining sleeping time
                delta_sleep = self.configuration.frequency - duration
                # if greater than 0, sleep
                if delta_sleep > 0:
                    time.sleep(delta_sleep)

        finally:
            self.log(
                message="Failed to forward events from Google Reports API",
                level="info",
            )

    def get_build_object(self):
        credentials = service_account.Credentials.from_service_account_file(
            self.service_account_path, scopes=self.scopes
        )
        delegated_credentials = credentials.with_subject(self.configuration.admin_mail)

        reports_service = build("admin", "reports_v1", credentials=delegated_credentials)

        return reports_service

    def get_activities(self):
        reports_service = self.get_build_object()

        self.log(message=f"Start requesting the Google reports with credential object created", level="info")

        try:
            activities = (
                reports_service.activities()
                .list(
                    userKey="all",
                    applicationName=self.configuration.application_name.value,
                    maxResults=self.pagination_limit,
                    startTime=self.most_recent_date_seen,
                )
                .execute()
            )

            return activities

        except (TransportError, ServerNotFoundError) as ex:
            self.log(message=f"Can't reach the google api server", level="warning")

    def get_next_activities(self, next_key):
        reports_service = self.get_build_object()

        self.log(
            message=f"Start requesting the Google reports with credential object created and also next key",
            level="info",
        )

        try:
            activities = (
                reports_service.activities()
                .list(
                    userKey="all",
                    applicationName=self.configuration.application_name.value,
                    maxResults=self.pagination_limit,
                    pageToken=next_key,
                    startTime=self.most_recent_date_seen,
                )
                .execute()
            )

            return activities

        except (TransportError, ServerNotFoundError) as ex:
            self.log(message=f"Can't reach the google api server", level="warning")

    def get_next_activities_with_next_key(self, next_key):
        const_next_key = next_key
        self.log(
            message=f"Start looping for all next activities with the first next key {const_next_key}",
            level="info",
        )
        while const_next_key:
            next_messages = []
            response_next_page = self.get_next_activities(next_key) or {}

            next_page_items = response_next_page.get("items", [])
            if next_page_items:
                recent_date = next_page_items[0].get("id").get("time")
                next_messages.extend(next_page_items)
                self.log(message=f"Sending other batches of {len(next_messages)} messages", level="info")
                OUTCOMING_EVENTS.labels(intake_key=self.configuration.intake_key).inc(len(next_messages))
                self.push_events_to_intakes(events=next_messages)
                self.most_recent_date_seen = recent_date
                self.log(
                    message=f"Updated recent date (get_next_activities) to  {self.most_recent_date_seen}",
                    level="info",
                )
                self.events_sum += len(next_messages)
                const_next_key = response_next_page.get("nextPageToken")
                self.log(
                    message=f"Updated nextKey to the new value: {const_next_key}",
                    level="info",
                )
            else:
                const_next_key = ""
                self.log(message=f"There's no items even if there's a next key!!", level="info")

    def get_reports_events(self):
        self.log(message=f"Creating a google credentials objects", level="info")

        activities = self.get_activities() or {}
        items, next_key = activities.get("items", []), activities.get("nextPageToken")

        self.log(message=f"Getting activities with {len(items)} elements", level="info")
        INCOMING_MESSAGES.labels(intake_key=self.configuration.intake_key).inc(len(items))

        if len(items) > 0:
            recent_date = items[0].get("id").get("time")
            messages = [orjson.dumps(message).decode("utf-8") for message in items]
            self.log(message=f"Sending the first batch of {len(messages)} messages", level="info")
            OUTCOMING_EVENTS.labels(intake_key=self.configuration.intake_key).inc(len(items))
            self.push_events_to_intakes(events=messages)
            self.most_recent_date_seen = recent_date
            self.log(
                message=f"Changing recent date in get reports events to  {self.most_recent_date_seen}", level="info"
            )
            self.events_sum += len(messages)

            if next_key:
                self.get_next_activities_with_next_key(next_key)

        else:
            self.log(message="No messages to forward", level="info")
