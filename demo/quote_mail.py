"""Single-profile Gmail integration for the local Oasis demo."""

import base64
import os
import smtplib
import ssl
from email.utils import parseaddr

import requests

PROFILE_EMAIL = "sahitha.sc@gmail.com"

# First name, last name, email. Replace sample addresses with demo inboxes.
CONTRACTORS = (
    ("Sahitha", "Chunduru", "ssc2236@columbia.edu"),
    ("Jordan", "Rivera", "bids@gulfcoast-construction.example.com"),
    ("Morgan", "Patel", "estimating@lonestar-mep.example.com"),
    ("Casey", "Brooks", "projects@heights-interiors.example.com"),
)

OAUTH_KEYS = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
sent_threads = {}


def is_sample(email):
    domain = email.rsplit("@", 1)[-1].lower()
    return domain == "example.com" or domain.endswith(".example.com")


def gmail_ready():
    return all(os.getenv(key) for key in OAUTH_KEYS)


class GmailError(Exception):
    pass


def smtp_password():
    return "".join(os.getenv("GMAIL_APP_PASSWORD", "").split())


def send_smtp(message):
    connection = None
    try:
        connection = smtplib.SMTP_SSL(
            "smtp.gmail.com", 465, timeout=15, context=ssl.create_default_context()
        )
        connection.login(PROFILE_EMAIL, smtp_password())
        connection.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise GmailError("Gmail rejected the app password. Check GMAIL_APP_PASSWORD.") from exc
    except OSError as exc:
        raise GmailError("Gmail SMTP could not confirm the send. Check Gmail Sent before retrying.") from exc
    finally:
        if connection is not None:
            connection.close()


class Gmail:
    def __init__(self, sender):
        if not gmail_ready():
            raise GmailError("Add Google OAuth credentials in .env and restart Oasis.")
        try:
            response = requests.post(
                "https://oauth2.googleapis.com/token",
                data=dict(zip(("client_id", "client_secret", "refresh_token"),
                              (os.environ[key] for key in OAUTH_KEYS)),
                          grant_type="refresh_token"),
                timeout=10,
            )
            response.raise_for_status()
            self.token = response.json()["access_token"]
        except (requests.RequestException, ValueError, KeyError) as exc:
            raise GmailError("Google authorization failed. Check your OAuth credentials.") from exc
        if self.call("GET", "profile").get("emailAddress", "").lower() != sender.lower():
            raise GmailError(f"Authorize Gmail as {sender} to match the Oasis profile.")

    def call(self, method, path, **kwargs):
        try:
            response = requests.request(
                method, f"https://gmail.googleapis.com/gmail/v1/users/me/{path}",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=15, **kwargs,
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise GmailError("Gmail could not complete the request. Check Gmail before retrying.") from exc

    def deliver(self, msg, save_draft):
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        if save_draft:
            self.call("POST", "drafts", json={"message": {"raw": raw}})
        else:
            sent = self.call("POST", "messages/send", json={"raw": raw})
            if sent.get("threadId"):
                sent_threads[sent["threadId"]] = (sent["id"], str(msg["To"]).lower())

    def replies(self):
        replies = []
        for thread_id, (sent_id, recipient) in sent_threads.copy().items():
            thread = self.call("GET", f"threads/{thread_id}", params={
                "format": "metadata", "metadataHeaders": "From",
            })
            for msg in thread.get("messages", []):
                if msg["id"] == sent_id or {"SENT", "DRAFT", "TRASH"} & set(msg.get("labelIds", [])):
                    continue
                headers = msg.get("payload", {}).get("headers", [])
                sender = next((h["value"] for h in headers if h["name"].lower() == "from"), "")
                if parseaddr(sender)[1].lower() == recipient:
                    replies.append({"id": msg["id"], "thread_id": thread_id, "sender": recipient})
        return replies
