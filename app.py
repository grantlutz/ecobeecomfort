"""ecobee Comfort app entry point."""

from homey.app import App


class EcobeeApp(App):
    async def on_init(self) -> None:
        self.log("ecobee Comfort app has been initialized")


homey_export = EcobeeApp
