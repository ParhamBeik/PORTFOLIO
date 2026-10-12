"""Send one message through the alert bot and say plainly whether it arrived.

Run after setting ALERT_TELEGRAM_BOT_TOKEN / ALERT_TELEGRAM_CHAT_ID. Every alert
failed with a bare "status=404" for days because nothing checked the bot once:
on Bale a 404 means the token is not a Bale bot (a Telegram token, say), and a
400 means the chat id is wrong or the bot was never started in that chat.
"""
from django.core.management.base import BaseCommand, CommandError

from config.observability import _send_text, _telegram_api_base, _telegram_target

HINTS = {
    401: "token rejected: copy it again from @BotFather in Bale",
    404: "no such bot on this server: the token is not a Bale bot (a Telegram token?)",
    400: "chat not found: send /start to the bot from that chat, then check the chat id",
    403: "the bot cannot post in that chat: add it to the group or start it",
}


class Command(BaseCommand):
    help = "Send a test message through the alert bot."

    def handle(self, *args, **options):
        target = _telegram_target()
        if target is None:
            raise CommandError("ALERT_TELEGRAM_BOT_TOKEN and ALERT_TELEGRAM_CHAT_ID are not both set.")
        try:
            _send_text(*target, "✅ Test alert\nIf you can read this, alerts reach you.")
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            hint = HINTS.get(status, type(exc).__name__)
            raise CommandError(f"{_telegram_api_base()} answered {status}: {hint}") from None
        self.stdout.write(self.style.SUCCESS(f"Delivered via {_telegram_api_base()}."))
