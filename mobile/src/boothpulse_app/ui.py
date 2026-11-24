"""BoothPulse mobile UI (Flet).

Screens:
  Login          phone number -> OTP
  Booth home     live booth stats, search, election-day lists, sync status
  Voter list     search + status filter, one-tap "Done"
  Voter detail   status, assistance, contact (with consent), notes, household
  Dashboard      coordinators / candidate / control room: booths, alerts, turnout
"""

import os

import flet as ft

from .api import Api, ApiError, AuthError, OfflineError
from .demo import DEMO_USERS, DemoApi, seed_demo
from .store import Store
from .sync import SyncEngine, minutes_since

NAVY, TEAL, AMBER, RED, GREEN, GREY, BLUE = (
    "#0B1F3A", "#0F766E", "#D97706", "#DC2626", "#16A34A", "#6B7280", "#2563EB",
)
STATUS = {
    "not_contacted": ("Not contacted", GREY),
    "contacted": ("Contacted", BLUE),
    "pending": ("Pending", AMBER),
    "vote_done": ("Vote done", GREEN),
    "needs_assistance": ("Needs assistance", RED),
    "not_available": ("Not available", "#9CA3AF"),
}
DEFAULT_SERVER = os.environ.get("BOOTHPULSE_SERVER", "http://127.0.0.1:8000")
SYNC_INTERVAL = 30
DASHBOARD_REFRESH = 20


def label(text, size=13, color=GREY, weight=None):
    return ft.Text(text, size=size, color=color, weight=weight)


def stat_tile(value, caption, color, on_click=None):
    return ft.Container(
        expand=True, padding=ft.Padding.symmetric(vertical=10, horizontal=6),
        bgcolor="#F3F5F8", border_radius=ft.BorderRadius.all(10), on_click=on_click,
        content=ft.Column(
            [ft.Text(str(value), size=24, weight=ft.FontWeight.BOLD, color=color),
             label(caption, 12)],
            spacing=2, horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )


def status_pill(status):
    text, color = STATUS.get(status, (status, GREY))
    return ft.Container(
        padding=ft.Padding.symmetric(vertical=2, horizontal=8), bgcolor=color,
        border_radius=ft.BorderRadius.all(10), content=ft.Text(text, size=11, color="white"),
    )


class BoothPulseApp:
    def __init__(self, page: ft.Page, store: Store | None = None):
        self.page = page
        self.store = store or Store()
        self.api = Api(
            self.store.get("server_url", DEFAULT_SERVER),
            self.store.get("token", ""),
            self.store.device_id(),
        )
        self.user = self.store.get("user")
        self.demo = bool(self.store.get("demo"))
        if self.demo and self.user:
            self.api = DemoApi(self.store, self.user)
        self.sync = SyncEngine(self.store, self.api)
        self.sync.listeners.append(self._on_sync_result)
        self.active_booths = set(self.store.get("active_booths", []))
        self.refreshers = {}  # route -> callable that redraws that screen
        self.dashboard_visible = False

        page.title = "BoothPulse"
        page.theme_mode = ft.ThemeMode.LIGHT
        page.theme = ft.Theme(color_scheme_seed=TEAL)
        page.on_view_pop = lambda e: self.pop()

    # ------------------------------------------------------------------ helpers

    def start(self):
        if self.api.token and self.user:
            self.go_home()
        else:
            self.show_login()

    def reset(self, view):
        self.page.views.clear()
        self.page.views.append(view)
        self.page.update()

    def push(self, view):
        self.page.views.append(view)
        self.page.update()

    def pop(self):
        if len(self.page.views) > 1:
            self.page.views.pop()
            self.refresh_top()
            self.page.update()

    def refresh_top(self):
        if self.page.views:
            fn = self.refreshers.get(self.page.views[-1].route)
            if fn:
                fn()

    def toast(self, message, error=False):
        self.page.show_dialog(ft.SnackBar(content=ft.Text(message), bgcolor=RED if error else NAVY))

    def bg(self, fn, *args):
        self.page.run_thread(fn, *args)

    def can_edit(self):
        return bool(self.user and self.user.get("can_edit"))

    def _remember_booth(self, booth_id):
        self.active_booths.add(booth_id)
        self.store.set("active_booths", sorted(self.active_booths))

    def _on_sync_result(self, result):
        if result.auth_failed:
            self.sign_out(expired=True)
            return
        if result.rejected:
            self.toast(f"{len(result.rejected)} change(s) were refused by the server", error=True)
        try:
            self.refresh_top()
            self.page.update()
        except Exception:
            pass  # page closed while syncing

    def sync_status_text(self):
        waiting = self.store.outbox_count()
        mins = minutes_since(self.store.get("last_sync_ok"))
        when = "never" if mins is None else ("just now" if mins < 1 else f"{mins} min ago")
        return f"Synced {when}" + (f" · {waiting} waiting to send" if waiting else "")

    # ------------------------------------------------------------------ login

    def show_login(self):
        server = ft.TextField(label="Server address", value=self.api.base_url or DEFAULT_SERVER,
                              keyboard_type=ft.KeyboardType.URL, dense=True)
        phone = ft.TextField(label="Mobile number", keyboard_type=ft.KeyboardType.PHONE,
                             max_length=10, prefix="+91 ", autofocus=True)
        code = ft.TextField(label="OTP", keyboard_type=ft.KeyboardType.NUMBER, max_length=6, visible=False)
        hint = label("", 12)
        error = ft.Text("", color=RED, size=13)
        button = ft.FilledButton(content="Send OTP", height=48, expand=True)
        busy = ft.ProgressRing(width=20, height=20, visible=False)

        def submit(e=None):
            error.value = ""
            busy.visible, button.disabled = True, True
            self.page.update()
            self.bg(do_submit)

        def do_submit():
            try:
                self.api.base_url = server.value.strip().rstrip("/")
                self.store.set("server_url", self.api.base_url)
                if not code.visible:
                    r = self.api.request_otp(phone.value)
                    code.visible = True
                    button.content = "Verify and sign in"
                    hint.value = f"Development code: {r['dev_code']}" if r.get("dev_code") else r.get("detail", "")
                else:
                    r = self.api.verify_otp(phone.value, code.value)
                    self.api.token = r["token"]
                    self.user = r["user"]
                    self.store.set("token", r["token"])
                    self.store.set("user", r["user"])
                    self.go_home()
                    return
            except OfflineError:
                error.value = "Cannot reach the server. Check the address and your internet."
            except ApiError as ex:
                error.value = ex.message
            busy.visible, button.disabled = False, False
            self.page.update()

        def demo(role):
            def handler(e):
                self.start_demo(role)
            return handler

        demo_box = ft.Container(
            bgcolor="#F3F5F8", border_radius=ft.BorderRadius.all(12), padding=14,
            content=ft.Column([
                ft.Text("Try the demo", size=15, weight=ft.FontWeight.BOLD, color=NAVY),
                label("Sample data on this phone. No server or login needed.", 12),
                ft.Row([
                    ft.OutlinedButton(content="Booth agent", icon=ft.Icons.PERSON, expand=True,
                                      on_click=demo("agent")),
                    ft.OutlinedButton(content="Dashboard", icon=ft.Icons.DASHBOARD, expand=True,
                                      on_click=demo("coordinator")),
                ]),
            ], spacing=8),
        )

        button.on_click = submit
        phone.on_submit = submit
        code.on_submit = submit

        self.reset(ft.View(
            route="/login", padding=24, scroll=ft.ScrollMode.AUTO,
            controls=[ft.SafeArea(content=ft.Column([
                ft.Container(height=24),
                ft.Container(width=48, height=5, bgcolor=AMBER, border_radius=ft.BorderRadius.all(3)),
                ft.Text("BoothPulse", size=34, weight=ft.FontWeight.BOLD, color=NAVY),
                label("Booth management and live election-day tracking", 14),
                ft.Container(height=16),
                phone, code, ft.Row([button, busy]), error, hint,
                ft.Container(height=8),
                demo_box,
                ft.ExpansionTile(title=ft.Text("Server settings", size=13), controls=[server]),
            ], spacing=8, horizontal_alignment=ft.CrossAxisAlignment.STRETCH))],
        ))

    def start_demo(self, role):
        self.sync.stop()
        self.store.clear_session(wipe_data=True)
        seed_demo(self.store)
        self.user = DEMO_USERS[role]
        self.demo = True
        self.store.set("demo", True)
        self.store.set("user", self.user)
        self.store.set("token", "demo")
        self.api = DemoApi(self.store, self.user)
        self.sync.api = self.api
        self.active_booths = set()
        self.go_home()

    def sign_out(self, expired=False):
        self.sync.stop()
        if self.demo:
            self.store.clear_session(wipe_data=True)
            self.demo, self.user, self.active_booths = False, None, set()
            self.api = Api(self.store.get("server_url", DEFAULT_SERVER), "", self.store.device_id())
            self.sync.api = self.api
            self.dashboard_visible = False
            self.show_login()
            return
        self.dashboard_visible = False
        unsent = self.store.outbox_count()

        def finish(e=None):
            try:
                if not expired:
                    self.api.logout()
            except ApiError:
                pass
            # Keep unsent changes if the session merely expired; they go out
            # after the agent signs in again.
            self.store.clear_session(wipe_data=not expired)
            self.api.token, self.user, self.active_booths = "", None, set()
            self.show_login()
            if expired:
                self.toast("Your session ended. Please sign in again.", error=True)

        if unsent and not expired:
            def confirm(e):
                self.page.pop_dialog()
                finish()

            self.page.show_dialog(ft.AlertDialog(
                title=ft.Text("Unsent changes"),
                content=ft.Text(f"{unsent} change(s) have not reached the server yet. "
                                "Signing out now will lose them. Sync first if you can."),
                actions=[ft.TextButton(content="Cancel", on_click=lambda e: self.page.pop_dialog()),
                         ft.TextButton(content="Sign out anyway", on_click=confirm)],
            ))
            return
        finish()

    # ------------------------------------------------------------------ home

    def go_home(self):
        role = (self.user or {}).get("role")
        if role == "agent" and self.user.get("booth"):
            booth = self.user["booth"]
            self._remember_booth(booth["id"])
            self.reset(self.booth_home_view(booth, root=True))
            self.sync.start(lambda: sorted(self.active_booths), SYNC_INTERVAL)
            self.bg(self.sync.sync_once, [booth["id"]])
        elif role == "agent":
            self.reset(ft.View(route="/none", padding=24, controls=[
                ft.Text("No booth assigned", size=22, weight=ft.FontWeight.BOLD),
                label("Ask the control room to assign you to a booth, then sign in again."),
                ft.OutlinedButton(content="Sign out", on_click=lambda e: self.sign_out()),
            ]))
        else:
            self.reset(self.dashboard_view())
            self.sync.start(lambda: sorted(self.active_booths), SYNC_INTERVAL)

    def appbar(self, title, root=False):
        actions = [ft.IconButton(icon=ft.Icons.SYNC, tooltip="Sync now", on_click=lambda e: self.manual_sync())]
        if root:
            actions.append(ft.PopupMenuButton(items=[
                ft.PopupMenuItem(content=f"{self.user.get('name')} · {self.user.get('role_label')}"),
                ft.PopupMenuItem(content="Sign out", icon=ft.Icons.LOGOUT, on_click=lambda e: self.sign_out()),
            ]))
        title_control = ft.Text(title, size=18)
        if self.demo:
            title_control = ft.Row([title_control, ft.Container(
                bgcolor=AMBER, border_radius=ft.BorderRadius.all(6),
                padding=ft.Padding.symmetric(vertical=2, horizontal=6),
                content=ft.Text("DEMO", size=11, color="white", weight=ft.FontWeight.BOLD))], spacing=8)
        return ft.AppBar(title=title_control, bgcolor=NAVY, color="white", actions=actions)

    def manual_sync(self):
        self.toast("Syncing...")

        def run():
            r = self.sync.sync_once(sorted(self.active_booths))
            if r.offline:
                self.toast("No connection. Changes are saved on the phone and will be sent later.", error=True)
            elif r.error:
                self.toast(r.error, error=True)
            elif r.ok:
                self.toast(f"Synced. Sent {r.pushed}, received {r.pulled}.")
            if self.dashboard_visible:
                self.load_dashboard()

        self.bg(run)

    def booth_home_view(self, booth, root=False):
        bid = booth["id"]
        route = f"/booth/{bid}"
        header = ft.Text("", size=20, weight=ft.FontWeight.BOLD, color="white")
        progress = ft.ProgressBar(value=0, color=AMBER, bgcolor="#28406A", bar_height=8)
        tiles = ft.Row(spacing=8)
        sync_line = label("")
        search = ft.TextField(hint_text="Search name / EPIC / house no. / serial no.",
                              prefix_icon=ft.Icons.SEARCH, dense=True, border_radius=ft.BorderRadius.all(24))
        search.on_submit = lambda e: self.push(self.voter_list_view(booth, "Search results", q=search.value))

        def open_list(title, **kw):
            return lambda e: self.push(self.voter_list_view(booth, title, **kw))

        def refresh():
            c = self.store.counts(bid)
            total, voted = c.get("total", 0), c.get("vote_done", 0)
            header.value = f"Voted {voted:,} of {total:,}" if total else "Downloading voter list..."
            progress.value = (voted / total) if total else None
            tiles.controls = [
                stat_tile(c.get("pending", 0), "Pending", AMBER, open_list("Pending voters", status="pending")),
                stat_tile(c.get("assistance_open", 0), "Assist", RED, open_list("Need assistance", assistance=True)),
                stat_tile(c.get("not_contacted", 0), "Not contacted", GREY,
                          open_list("Not contacted", status="not_contacted")),
            ]
            sync_line.value = self.sync_status_text()

        self.refreshers[route] = refresh
        refresh()

        def menu_button(text, icon, color, handler):
            return ft.Container(
                bgcolor="#F3F5F8", border_radius=ft.BorderRadius.all(10), on_click=handler,
                padding=ft.Padding.symmetric(vertical=14, horizontal=16),
                content=ft.Row([ft.Icon(icon, color=color), ft.Text(text, size=15, weight=ft.FontWeight.W_500, expand=True),
                                ft.Icon(ft.Icons.CHEVRON_RIGHT, color=GREY)]),
            )

        title = f"Booth {booth['number']} - {booth['name']}"
        return ft.View(
            route=route, padding=0, scroll=ft.ScrollMode.AUTO,
            appbar=self.appbar(f"Booth {booth['number']}", root=root),
            controls=[
                ft.Container(bgcolor=TEAL, padding=16, content=ft.Column(
                    [ft.Text(title, size=14, color="white"), header, progress], spacing=6)),
                ft.Container(padding=16, content=ft.Column([
                    tiles,
                    search,
                    menu_button("Election-day list (pending, by serial no.)", ft.Icons.HOW_TO_VOTE, AMBER,
                                open_list("Election day", status=["pending", "contacted", "not_contacted"])),
                    menu_button("All voters", ft.Icons.PEOPLE, TEAL, open_list("All voters")),
                    menu_button("Assistance requests", ft.Icons.ACCESSIBLE, RED,
                                open_list("Need assistance", assistance=True)),
                    menu_button("Already voted", ft.Icons.CHECK_CIRCLE, GREEN, open_list("Voted", status="vote_done")),
                    ft.Row([ft.Icon(ft.Icons.CLOUD_SYNC, size=16, color=GREY), sync_line]),
                    label("The app records only whether a person has voted, never whom they voted for.", 11),
                ], spacing=12, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)),
            ],
        )

    # ------------------------------------------------------------------ voter list

    def voter_list_view(self, booth, title, q="", status=None, assistance=False):
        bid = booth["id"]
        route = f"/booth/{bid}/list/{title}"
        state = {"q": q, "status": status, "assistance": assistance}
        results = ft.ListView(expand=True, spacing=4)
        footer = label("", 12)
        search = ft.TextField(value=q, hint_text="Name / EPIC / house / serial", prefix_icon=ft.Icons.SEARCH,
                              dense=True, border_radius=ft.BorderRadius.all(24))

        def mark_done(voter):
            def handler(e):
                self.store.record_change(voter["id"], {"status": "vote_done"})
                self.toast(f"{voter['name']} marked as voted")
                refresh()
                self.page.update()
                self.bg(self.sync.sync_once, [bid])
            return handler

        def tile(v):
            text, color = STATUS.get(v["status"], (v["status"], GREY))
            trailing = None
            if self.can_edit() and v["status"] != "vote_done":
                trailing = ft.FilledButton(content="Done", bgcolor=GREEN, height=34, on_click=mark_done(v))
            elif v["status"] == "vote_done":
                trailing = ft.Icon(ft.Icons.CHECK_CIRCLE, color=GREEN)
            sub = f"S.No {v['serial_no']} · H.{v['house_no'] or '-'} · {v['age'] or '-'} {v['gender'] or ''} · {text}"
            if v["needs_assistance"] and v["status"] != "vote_done":
                sub += " · needs help"
            return ft.ListTile(
                leading=ft.CircleAvatar(content=ft.Text(str(v["serial_no"]), size=11), bgcolor=color, color="white"),
                title=ft.Text(v["name"], weight=ft.FontWeight.W_500),
                subtitle=ft.Text(sub, size=12), trailing=trailing, bgcolor="#F8FAFC",
                on_click=lambda e, vid=v["id"]: self.push(self.voter_detail_view(booth, vid)),
            )

        def refresh():
            rows = self.store.search(bid, state["q"], state["status"], state["assistance"], limit=300)
            results.controls = [tile(v) for v in rows]
            if not rows:
                c = self.store.counts(bid)
                results.controls = [ft.Container(padding=24, content=label(
                    "Voter list is still downloading. Pull to sync from the home screen."
                    if not c.get("total") else "No voters match."))]
            footer.value = f"{len(rows)} shown" + (" (first 300, refine the search)" if len(rows) == 300 else "")

        def on_search(e):
            state["q"] = search.value
            refresh()
            self.page.update()

        search.on_submit = on_search
        search.on_change = on_search
        self.refreshers[route] = refresh
        refresh()
        return ft.View(
            route=route, padding=12, appbar=self.appbar(title),
            controls=[ft.Column([search, footer, results], expand=True, spacing=6)],
        )

    # ------------------------------------------------------------------ voter detail

    def voter_detail_view(self, booth, voter_id):
        bid = booth["id"]
        route = f"/voter/{voter_id}"
        v = self.store.voter(voter_id)
        if v is None:
            return ft.View(route=route, appbar=self.appbar("Voter"), controls=[label("Voter not found on this phone.")])
        editable = self.can_edit()
        features = (self.user or {}).get("features", {})

        status_row = ft.Row(wrap=True, spacing=8, run_spacing=8)
        assist = ft.Switch(label="Needs assistance on polling day", value=v["needs_assistance"], disabled=not editable)
        assist_note = ft.TextField(label="Assistance note (e.g. wheelchair at 10 AM)", value=v["assistance_note"] or "",
                                   max_length=200, dense=True, disabled=not editable)
        consent = ft.Checkbox(label="Voter agreed to share their phone number", value=v["contact_consent"],
                              disabled=not editable)
        phone = ft.TextField(label="Phone", value=v.get("phone") or "", keyboard_type=ft.KeyboardType.PHONE,
                             max_length=10, dense=True, disabled=not (editable and v["contact_consent"]))
        notes = ft.TextField(label="Notes", value=v.get("notes") or "", multiline=True, min_lines=2, max_lines=4,
                             max_length=500, disabled=not editable)
        inclination = ft.SegmentedButton(
            visible=bool(features.get("inclination")), disabled=not editable,
            selected=[v.get("inclination") or "unknown"],
            segments=[ft.Segment(value=k, label=ft.Text(t)) for k, t in
                      [("supporter", "Supporter"), ("neutral", "Neutral"), ("opposed", "Opposed"), ("unknown", "Unknown")]],
        )

        def set_status(new):
            def handler(e):
                self.store.record_change(voter_id, {"status": new})
                draw_status(new)
                self.toast(f"Status: {STATUS[new][0]}")
                self.page.update()
                self.bg(self.sync.sync_once, [bid])
            return handler

        def draw_status(current):
            status_row.controls = []
            for key, (text, color) in STATUS.items():
                selected = key == current
                status_row.controls.append(ft.Button(
                    content=text, disabled=not editable, on_click=set_status(key),
                    bgcolor=color if selected else "white", color="white" if selected else color,
                    style=ft.ButtonStyle(side=ft.BorderSide(1.5, color)),
                ))

        def consent_changed(e):
            phone.disabled = not consent.value
            if not consent.value:
                phone.value = ""
            self.page.update()

        consent.on_change = consent_changed

        def save(e):
            fields = {
                "needs_assistance": bool(assist.value),
                "assistance_note": assist_note.value or "",
                "contact_consent": bool(consent.value),
                "phone": (phone.value or "") if consent.value else "",
                "notes": notes.value or "",
            }
            if inclination.visible:
                fields["inclination"] = (inclination.selected or ["unknown"])[0]
            if fields["phone"] and len("".join(ch for ch in fields["phone"] if ch.isdigit())) != 10:
                self.toast("Enter a 10-digit phone number", error=True)
                return
            self.store.record_change(voter_id, fields)
            self.toast("Saved. It will sync automatically.")
            self.bg(self.sync.sync_once, [bid])
            self.pop()

        draw_status(v["status"])
        family = self.store.household(bid, v["house_no"], exclude_id=voter_id)
        family_tiles = [
            ft.ListTile(dense=True, title=ft.Text(f["name"]),
                        subtitle=ft.Text(f"S.No {f['serial_no']} · {f['age'] or '-'} {f['gender'] or ''}", size=12),
                        trailing=status_pill(f["status"]),
                        on_click=lambda e, fid=f["id"]: self.push(self.voter_detail_view(booth, fid)))
            for f in family
        ]

        body = [
            ft.Text(v["name"], size=22, weight=ft.FontWeight.BOLD, color=NAVY),
            label(f"{'Relative: ' + v['relative_name'] if v['relative_name'] else ''}"),
            label(f"EPIC {v['epic']}  |  S.No {v['serial_no']}  |  Booth {v['booth_number']}  |  "
                  f"House {v['house_no'] or '-'}  |  {v['age'] or '-'} {v['gender'] or ''}"),
            ft.Divider(),
            label("STATUS", 12, weight=ft.FontWeight.BOLD),
            status_row,
            ft.Divider(),
            assist, assist_note,
            label("CONTACT", 12, weight=ft.FontWeight.BOLD),
            consent, phone,
        ]
        if inclination.visible:
            body += [label("INCLINATION (visible to campaign only)", 12, weight=ft.FontWeight.BOLD), inclination]
        body += [notes]
        if editable:
            body.append(ft.FilledButton(content="Save and sync", bgcolor=NAVY, height=48, on_click=save))
        else:
            body.append(label("Read-only access", 12))
        if family_tiles:
            body += [ft.Divider(), label(f"SAME HOUSE ({len(family_tiles)})", 12, weight=ft.FontWeight.BOLD), *family_tiles]

        return ft.View(route=route, padding=16, scroll=ft.ScrollMode.AUTO, appbar=self.appbar("Voter details"),
                       controls=[ft.Column(body, spacing=8, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)])

    # ------------------------------------------------------------------ dashboard

    def dashboard_view(self):
        route = "/dashboard"
        self.dash_tiles = ft.Row(spacing=8)
        self.dash_status = label("Loading...", 12)
        self.dash_alerts = ft.Column(spacing=4)
        self.dash_booths = ft.Column(spacing=4)
        self.dash_title = ft.Text("", size=16, weight=ft.FontWeight.BOLD, color=NAVY)
        self.refreshers[route] = self.resume_dashboard
        self.resume_dashboard()
        scope = self.user.get("cluster") or self.user.get("constituency") or "All booths"
        self.dash_title.value = f"{self.user.get('role_label')} · {scope}"
        return ft.View(
            route=route, padding=16, scroll=ft.ScrollMode.AUTO, appbar=self.appbar("Live dashboard", root=True),
            controls=[ft.Column([
                self.dash_title, self.dash_status, self.dash_tiles,
                label("PRIORITY ALERTS", 12, weight=ft.FontWeight.BOLD), self.dash_alerts,
                label("BOOTHS (tap to open)", 12, weight=ft.FontWeight.BOLD), self.dash_booths,
            ], spacing=10)],
        )

    def resume_dashboard(self):
        """Called when the dashboard is shown again: reload now, keep refreshing."""
        self.dashboard_visible = True
        self.bg(self.load_dashboard)
        if not getattr(self, "_dash_loop_running", False):
            self._dash_loop_running = True
            self.bg(self._dashboard_loop)

    def _dashboard_loop(self):
        import time
        try:
            while self.dashboard_visible and self.user:
                time.sleep(DASHBOARD_REFRESH)
                if self.dashboard_visible and self.page.views and self.page.views[-1].route == "/dashboard":
                    self.load_dashboard()
        finally:
            self._dash_loop_running = False

    def load_dashboard(self):
        try:
            d = self.api.dashboard()
        except AuthError:
            self.sign_out(expired=True)
            return
        except ApiError as e:
            self.dash_status.value = f"Offline: showing last data. ({e.message})"
            self.page.update()
            return
        t = d["totals"]
        self.dash_status.value = f"Live · {t['booths']} booths · {t['voters']:,} voters · refreshes every {DASHBOARD_REFRESH}s"
        self.dash_tiles.controls = [
            stat_tile(f"{t['turnout_pct']}%", "Turnout", TEAL),
            stat_tile(f"{t['voted']:,}", "Voted", GREEN),
            stat_tile(f"{t['pending']:,}", "Pending", AMBER),
            stat_tile(t["assistance_open"], "Assist", RED),
        ]
        colors = {"danger": RED, "warning": AMBER, "muted": GREY}
        booths_by_id = {b["id"]: b for b in d["booths"]}
        self.dash_alerts.controls = [
            ft.ListTile(dense=True, leading=ft.Icon(ft.Icons.CIRCLE, size=12, color=colors[a["level"]]),
                        title=ft.Text(f"Booth {a['booth']}", weight=ft.FontWeight.W_500),
                        subtitle=ft.Text(a["message"], size=12),
                        on_click=lambda e, b=booths_by_id.get(a["booth_id"]): self.open_booth(b))
            for a in d["alerts"][:10]
        ] or [label("No alerts.")]
        self.dash_booths.controls = [self._booth_row(b) for b in d["booths"]]
        self.page.update()

    def _booth_row(self, b):
        pct = b["turnout_pct"]
        color = AMBER if pct < 45 else TEAL
        mins = minutes_since(b["last_sync"])
        synced = "agent never synced" if mins is None else f"synced {mins} min ago"
        return ft.Container(
            bgcolor="#F8FAFC", border_radius=ft.BorderRadius.all(8), padding=10,
            on_click=lambda e: self.open_booth(b),
            content=ft.Column([
                ft.Row([ft.Text(f"{b['number']} · {b['name']}", weight=ft.FontWeight.W_500, expand=True),
                        ft.Text(f"{pct}%", color=color, weight=ft.FontWeight.BOLD)]),
                ft.ProgressBar(value=min(pct, 100) / 100, color=color, bgcolor="#E5E7EB"),
                label(f"{b['voted']:,}/{b['total']:,} voted · {b['pending']} pending · "
                      f"{b['assistance_open']} assist · {synced}", 11),
            ], spacing=4),
        )

    def open_booth(self, b):
        if not b:
            return
        booth = {"id": b["id"], "number": b["number"], "name": b["name"]}
        self._remember_booth(b["id"])
        self.dashboard_visible = False
        self.push(self.booth_home_view(booth))
        self.bg(self.sync.sync_once, [b["id"]])
