"""App updates over the air, from the server's side.

    manage.py app_update status                    what everyone and the test phones get now
    manage.py app_update publish <dir|.tgz> [--commit SHA]   a new app code (GitHub does this on every push)
    manage.py app_update promote                   the newest test version goes to everyone
    manage.py app_update rollback [--everyone]     take the newest version back
    manage.py app_update devices                   the phones that asked for updates, and whose they are
    manage.py app_update tester <phone id> [--off] make a phone a test phone (it gets new versions first)
    manage.py app_update apk <file.apk>            offer a new APK (for native changes; its version is read from it)
    manage.py app_update keygen [--group jarvis]   make the signing key (once per server)
"""
import re
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core import appupdates as au
from core.models import AppApk, AppDevice, AppUpdate, Channel


class Command(BaseCommand):
    help = "Over-the-air app updates: publish, promote, roll back, test phones, APKs, signing key."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        sub.add_parser("status")
        p = sub.add_parser("publish")
        p.add_argument("source")
        p.add_argument("--commit", default="")
        p.add_argument("--message", default="")
        sub.add_parser("promote")
        r = sub.add_parser("rollback")
        r.add_argument("--everyone", action="store_true", help="Roll back what everyone has, even if a test version is newer.")
        r.add_argument("--apk", action="store_true", help="Withdraw the newest APK offer instead.")
        sub.add_parser("devices")
        t = sub.add_parser("tester")
        t.add_argument("device")
        t.add_argument("--off", action="store_true")
        a = sub.add_parser("apk")
        a.add_argument("file")
        a.add_argument("--version", default="", help="Optional: must match the version inside the APK (it is read from the file).")
        a.add_argument("--notes", default="")
        a.add_argument("--replace", action="store_true")
        k = sub.add_parser("keygen")
        k.add_argument("--force", action="store_true")
        k.add_argument("--group", default="", help="Let this group read the key too (the VPS runs JARVIS as user jarvis).")

    def handle(self, *args, **opts):
        try:
            getattr(self, "do_" + opts["action"])(opts)
        except au.UpdateError as exc:
            raise CommandError(str(exc)) from exc

    def say(self, text, style=None):
        self.stdout.write(style(text) if style else text)

    # ------------------------------------------------------------------------------------------------ actions
    def do_status(self, opts):
        signing = au.can_sign()
        self.say(f"Signing key: {'present' if signing else 'MISSING (run: manage.py app_update keygen)'}",
                 None if signing else self.style.ERROR)
        try:
            self.say(f"Certificate SHA-256: {au.certificate_fingerprint(au.certificate_file().read_bytes())}")
        except OSError:
            pass
        self.say(f"Public address for phones: {settings.JARVIS_PUBLIC_URL or '(not set: taken from each request)'}")
        runtimes = sorted(set(AppUpdate.objects.filter(withdrawn=False).values_list("runtime_version", flat=True)))
        if not runtimes:
            self.say("No app update published yet: every phone runs its APK's own code.")
        for rt in runtimes:
            live = au.current_for(rt, tester=False)
            test = au.current_for(rt, tester=True)
            self.say(self.style.MIGRATE_HEADING(f"\nApp version {rt}"))
            self.say(f"  Everyone:    {au.label(live)}")
            newer = test and test != live and (live is None or test.fingerprint != live.fingerprint)
            self.say(f"  Test phones: {au.label(test)}" + ("   <- not sent to everyone yet (promote)" if newer else "   (same code as everyone)" if test != live else ""))
        apk = AppApk.objects.filter(withdrawn=False).first()
        if apk:
            self.say(f"\nAPK offered: {apk.version} ({'everyone' if apk.channel == Channel.PRODUCTION else 'test phones only'})")
        testers = AppDevice.objects.filter(tester=True).count()
        self.say(f"\nTest phones: {testers}" + ("" if testers else "  (none: mark yours with `app_update tester <phone id>`)"))

    def do_publish(self, opts):
        message = opts["message"]
        if not message and opts["commit"]:
            message = self._commit_subject(opts["commit"])
        update, created = au.publish(Path(opts["source"]), commit=opts["commit"], message=message)
        if created:
            self.say(f"Published to the test phones: {au.label(update)}", self.style.SUCCESS)
            self.say("When it looks right on a test phone: manage.py app_update promote")
        else:
            self.say(f"No change in the app since {au.label(update)}: nothing published.")
        src = Path(opts["source"])
        if src.is_file() and src.parent.name == "incoming":
            src.unlink(missing_ok=True)  # GitHub's upload is not needed once the files are in the store

    def do_promote(self, opts):
        for line in au.promote():
            self.say(line, self.style.SUCCESS)

    def do_rollback(self, opts):
        if opts["apk"]:
            apk = AppApk.objects.filter(withdrawn=False).first()
            if not apk:
                raise CommandError("No APK is being offered.")
            apk.withdrawn = True
            apk.save(update_fields=["withdrawn"])
            self.say(f"APK {apk.version} is no longer offered.", self.style.SUCCESS)
            return
        for line in au.rollback(everyone=opts["everyone"]):
            self.say(line, self.style.SUCCESS)

    def do_devices(self, opts):
        rows = list(AppDevice.objects.select_related("user")[:50])
        if not rows:
            self.say("No phone has asked for updates yet (open JARVIS on the phone once).")
            return
        self.say(f"{'Phone id':10} {'Test':5} {'Person':18} {'Phone':22} {'Version':10} {'Running':10} Last seen")
        for d in rows:
            who = (d.user.display_name or d.user.username) if d.user else "-"
            running = d.update_id[:8] if d.update_id else "APK's own"
            seen = timezone.localtime(d.last_seen).strftime("%d %b %H:%M")
            self.say(f"{d.device_id[:8]:10} {'yes' if d.tester else '':5} {who[:18]:18} {d.model[:22]:22} "
                     f"{d.runtime_version[:10]:10} {running:10} {seen}")

    def do_tester(self, opts):
        prefix = opts["device"].strip()
        if len(prefix) < 6:
            raise CommandError("Give at least the first 6 characters of the phone id (see: app_update devices).")
        found = list(AppDevice.objects.filter(device_id__startswith=prefix)[:2])
        if not found and not opts["off"] and au.DEVICE_ID.match(prefix) and len(prefix) >= 16:
            # The FULL id of a phone that has not asked yet (e.g. from another server): ready before its first check.
            AppDevice.objects.create(device_id=prefix, tester=True)
            self.say(f"Phone {prefix[:8]} has not asked for updates yet; it is a test phone from its first check.",
                     self.style.SUCCESS)
            return
        if not found:
            raise CommandError(f"No phone with an id starting {prefix} (see: app_update devices).")
        if len(found) > 1:
            raise CommandError("More than one phone starts with that; give more characters.")
        dev = found[0]
        dev.tester = not opts["off"]
        dev.save(update_fields=["tester"])
        who = dev.user.username if dev.user else "unknown user"
        self.say(f"Phone {dev.device_id[:8]} ({who}, {dev.model or 'phone'}) is {'now a test phone' if dev.tester else 'no longer a test phone'}.",
                 self.style.SUCCESS)

    def do_apk(self, opts):
        apk = au.add_apk(Path(opts["file"]), opts["version"], opts["notes"], opts["replace"])
        self.say(f"APK {apk.version} ({apk.size / 1048576:.1f} MB) is offered to the test phones. "
                 "When it installs fine there: manage.py app_update promote", self.style.SUCCESS)

    def do_keygen(self, opts):
        try:
            cert, fingerprint = au.keygen(force=opts["force"], group=opts["group"])
        except KeyError as exc:
            raise CommandError(f"No group called {opts['group']} on this server.") from exc
        who = f"this account and the {opts['group']} group" if opts["group"] else "this account only"
        self.say(f"Signing key made: {au.key_file()} (readable by {who}; never copy it anywhere).", self.style.SUCCESS)
        self.say(f"Certificate (public, goes into the app): {cert}")
        self.say(f"Certificate SHA-256 fingerprint: {fingerprint}")

    @staticmethod
    def _commit_subject(commit: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{7,40}", commit or ""):
            return ""
        repo = Path(settings.BASE_DIR).parent
        try:
            out = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%s", commit], capture_output=True, text=True, timeout=10)
            return out.stdout.strip()[:200] if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""
