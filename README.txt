Control your ecobee thermostat from Homey — including its comfort profiles.

Most ecobee apps let you change the temperature, but not the comfort profile. This one does. Switch your thermostat between Home, Away, and Sleep (and any custom profile you created in the ecobee app) with a tap or a flow, and it applies that profile's own setpoints and fan settings automatically.

No ecobee developer account or API key is needed. You sign in with the same email and password you already use for the ecobee app, and two-step verification is fully supported during setup.


WHAT YOU CAN CONTROL

Thermostat
- Comfort profile: Home, Away, Sleep, or resume the normal schedule
- Mode: Off, Heat, Cool, Auto
- Target temperature (whole degrees; in Auto it follows the side that's actually in play — cooling on a warm afternoon, heating on a cold night)
- Fan: Auto or continuously On
- Indoor temperature and humidity
- Hold status: following the schedule, holding, or on vacation

Room sensors
- Temperature and motion from every ecobee room sensor, plus the thermostat's built-in sensor
- Read through your thermostat, so they add no extra load on ecobee and need no separate sign-in


FLOWS

Actions: set a comfort profile (with a choice of holding indefinitely or until the next scheduled change), set a comfort profile by name for custom profiles, resume the schedule, set mode, set fan, set temperature.
Condition: the comfort profile is / is not a given profile.
Triggers: the comfort profile changed, the indoor temperature changed, the mode changed — plus Homey's built-in motion and temperature cards on the room sensors.

Example: "When everyone has left home, set the comfort profile to Away." One card, and the thermostat takes care of the rest.


GETTING STARTED

1. Install the app.
2. Add device -> ecobee Comfort -> ecobee Thermostat.
3. Sign in with your ecobee email and password, and enter the verification code if your account uses two-step sign-in.
4. Pick the thermostat you want to add. Repeat to add more.
5. To add room sensors, wait a few minutes for the thermostat's first update, then Add device -> ecobee Comfort -> ecobee Room Sensor. Your sensors appear automatically — no sign-in needed.

Then open the Comfort Profile control on your thermostat and try switching to Away, then back to Schedule.


GOOD TO KNOW

- Your password is used only at sign-in and is never stored on your Homey.
- ecobee limits how often a thermostat can be read, so changes made on the thermostat itself can take a few minutes to show in Homey. Changes you make from Homey are sent right away.
- Comfort-profile holds stay until you change them or resume the schedule. You can choose "until the next scheduled change" in the flow cards instead.
- If you change your ecobee password, just remove and re-add the thermostat once.


ABOUT

This is an unofficial, community-built, open-source app. It is not affiliated with, endorsed by, or supported by ecobee. It signs in the same way the ecobee website does; if ecobee changes how sign-in works, the app may need an update.

Source code, issues, and ways to support the project: https://github.com/johndellarciprete/ecobeecomfort
