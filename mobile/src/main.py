import flet as ft

from boothpulse_app.ui import BoothPulseApp


def main(page: ft.Page):
    BoothPulseApp(page).start()


if __name__ == "__main__":
    ft.run(main)
