# Troubleshooting

Find what you see, then the likely cause and the fix. When a fix asks you to look at the log:
**Setup > Log**, and filter on the name you gave the hardware (for example "Home Connect").

## Signing in

### The sign-in page says "The given user is not assigned to this app"

Your Home Connect developer account uses a different email address than your Home Connect app
account. Home Connect only lets an application sign in to accounts with the same email.

Fix: register the application with a developer account that uses the email of your Home
Connect app account ([Installation, step 1](install.md#1-register-an-application-at-home-connect)),
put its Client ID in the hardware settings and press Update.

### The sign-in page mentions the terms of use, or the Home Connect device says "Action needed"

Home Connect wants you to accept (new) terms of use before it lets the application in.

Fix: log in on [developer.home-connect.com](https://developer.home-connect.com) and in the Home
Connect app, accept what they ask for, then choose **Sign in again** on the **Home Connect
actions** device.

### The Home Connect device says "Code expired"

The code was not approved in time. Choose **Sign in again** on the **Home Connect actions**
device, or press **Update** in the hardware settings, and approve the new code.

### The Home Connect device says "Sign-in lost"

Home Connect no longer accepts the plugin's sign-in: its access was removed in your Home
Connect account, or the plugin could not renew it for 60 days. Choose **Sign in again** and
approve the new code.

### I cannot find the Home Connect device

Once you are signed in it is hidden: Setup > Devices, filter on the Home Connect hardware.

## Devices

### No devices appear after signing in

- Check that **Accept new Hardware Devices** is on (Setup > Settings > System). While it is off,
  Domoticz refuses new devices and the log shows "Could not create" lines.
- Check that the appliances are in the Home Connect app and connected there.
- Choose **Refresh appliances** on the **Home Connect actions** device to read the appliance
  list again.

### State shows "(last known)"

The plugin has not received anything from Home Connect for more than 5 minutes, usually because
the internet connection or Home Connect itself was away. It reconnects by itself; the text goes
away with the next update. If it stays, look in the log for connection errors.

### An appliance shows Offline

Home Connect reports that the appliance is not connected: it is switched off at the mains, out
of Wi-Fi range, or its Wi-Fi module lost the network. Check it in the Home Connect app; when the
app sees it again, the plugin does too.

### A device I expected is missing

Settings, values the plugin has no name for, and diagnostics such as the interior light are
created hidden. Look under Setup > Devices
([Devices](devices.md#options-settings-and-other-values)).

### The dryer stays Running after the laundry is dry

With wrinkle guard (anti-crease) set, the dryer keeps turning the drum now and then after drying,
for the time you set. Home Connect reports that as Running until the wrinkle guard ends, so State
shows Running, Progress 100% and Remaining time 1 min all that time, and the Status code stays at
40 (Running).

Two devices tell the moments apart:

- **Drying finished** (Drogen klaar) comes on as soon as the laundry is dry and the wrinkle guard
  starts, and goes off when the program ends. Put your "laundry is dry" notification or script
  on this one.
- **Program finished** (Programma klaar) comes on when the whole program, wrinkle guard included,
  is over, and stays on until the dryer clears the message, for example when you open the door
  or it switches itself off. State then shows Finished, and the Status code goes to 70.

### The washing machine meters are not there yet

They appear when a program ends: that is when the washer reports its lifetime totals. Detergent
and softener only appear after a program that doses them.

### A scene selects the wrong program or time

Scenes reach selector levels up to 100 only (a Domoticz limit): the first 10 programs, or the
times 00:00 to 04:30. A scene stored with a higher level selects the level-100 choice instead.
Use a timer or a dzVents script for the others
([Control](control.md#scenes-timers-and-scripts)).

## A command is refused or fails

The plugin checks every command before sending it. When it refuses one, nothing is sent and the
log shows a `command_refused` line. Some problems only show after sending: Home Connect or the
appliance answers with an error (`command_failed`), the command could not be sent in time
(`command_expired`), or the appliance did not report the change (`command_unconfirmed`). In
every case the device goes back to what the appliance reports, and the log line carries the
reason. By default the same reason also appears briefly at the top of the screen
([On-screen messages](settings.md#on-screen-messages)). The table lists every reason, by what the
log says.

### I gave a command and see no message on screen

- Check **On-screen messages** in the hardware settings: it may be set to Off, and with
  **Problems** a command that worked shows nothing.
- It needs Domoticz 2026.1 or newer.
- The message shows for a few seconds only, and some themes place it partly behind their header.
  The Domoticz log always has the result: look for `command_refused` or `command_failed`.

**Control and sign-in**

| The log says | What to do |
|---|---|
| 'Allow control' is off in the hardware settings | Turn on [Allow control](settings.md#allow-control) if you want to operate appliances. |
| Not signed in to Home Connect; use the sign-in link ... | Sign in first ([Installation](install.md#4-sign-in)). |
| Sign-in changed before it was sent; give the command again | You signed in again while the command waited. Give it again. |
| Not allowed for this Home Connect app; check the application on the developer portal | Home Connect does not allow this request for your application. Check on [developer.home-connect.com](https://developer.home-connect.com) that it is a Device Flow application of the same account; if it keeps happening, open an issue with the log line. |
| Plugin stopping; command not sent | Domoticz or the plugin was restarting. Give the command again. |
| Appliance not discovered yet; try again shortly | The plugin is still reading the appliance after a start. Wait a minute. |
| This device only shows a value; it cannot be changed from Domoticz | The device is read-only (a reading, not a setting or option you can change). |
| Command ... is not supported here | The device does not take that kind of command (for example On/Off on a selector). |

**The appliance**

| The log says | What to do |
|---|---|
| ... is offline; check the power and Wi-Fi of the appliance | See [An appliance shows Offline](#an-appliance-shows-offline). |
| ... is switched off; switch it on first | Switch it on with **Power**, then choose the program again. |
| ... refused it without a reason; check that it is switched on, or try again in a few seconds | The appliance turned the command down without saying why. Check that it is on; otherwise it usually works when you give it again a few seconds later. If it keeps failing, look at the appliance (a steam oven, for example, needs its water tank). |
| Remote control is not enabled on ...; enable it once on the appliance | Switch on remote control on the appliance itself (once). |
| Remote start is not enabled on ...; press remote start on the appliance | Press remote start on the appliance, then Start again. |
| ... is being operated on its panel; try again in a few seconds | Wait until nobody uses the appliance's buttons. |
| ... is not ready to start (...); switch it on first | Switch the appliance on. |
| ... is not ready to start (...); open the door or acknowledge the end first | Open and close the door, or choose Acknowledge. |
| ... is not ready to start (...); stop the program waiting for its delayed start first | Stop it, then start again. |
| ... is not ready to start (...); wait until it is Ready | Wait until State shows Ready. |
| The door of ... is not closed | Close the door. |
| The front panel of ... is open | Close the front panel. |
| The meat probe is plugged into ...; remove it or use a probe program | Remove the probe, or choose a program that uses it. |
| The battery of ... is too low; let it charge first | Let it charge (cleaning robot). |
| ... has been lifted; put it back on the floor | Put the robot back on the floor. |
| The dust box of ... is not inserted | Insert the dust box. |
| ... is already at its charging station | Nothing to do: it is home already. |
| ... did not start; check the appliance (for example the water tank or the door) | The appliance accepted Start but did not start. Look at its display. |
| Not confirmed by ...; the device shows what ... reports | The appliance did not report the change. Check it on the appliance. |
| ... cannot be switched off remotely | The appliance has no remote off; normal for washers, dryers and fridges. |
| The power options of ... are not known yet; try again later | The plugin has not read the appliance's settings yet. Wait a minute. |

**Programs, options and actions**

| The log says | What to do |
|---|---|
| Choose a program; '-' selects nothing | Choose a program on Program choice. |
| Choose an action; '-' does nothing | Choose an action on Actions. |
| ... is not offered by ... | The appliance does not have that program or action. |
| A program is running on ...; stop it before choosing another | Stop the program first. |
| No program is selected on ...; choose one first | Choose a program on Program choice. |
| The program running on ... is not known yet; try again shortly | Wait a moment and try again. |
| No program is running on ... | Nothing to stop. |
| ... is not possible on ... now (...); it works only while ... | The action fits another state, for example Resume only while paused. |
| The program on ... cannot be stopped at this point | The appliance does not allow stopping in this phase. |
| ... is not offered by the selected program; select a program that offers it | Choose a program that has that option. |
| ... cannot be changed while the program runs; set it before the next start | Set it before the next start. |
| ... is not allowed for ... now; allowed: ... | Choose one of the values listed. |
| ... is outside the allowed range for ... (...) | Use a value inside the range named. |
| ...: ... rounded to ... | Not an error: the value was rounded to the option's step and sent. |
| ... cannot be changed from Domoticz | That setting is read-only. |
| That choice is not available | The selector level has no meaning (any more); choose another. |
| Not a number | Enter a number. |
| Selector levels unknown; restore a backup or delete the device | The plugin lost which level means what. Restore a Domoticz backup, or delete the device so it is created again. |
| No change (...); nothing sent | Not an error: the appliance already has that value. |

**Delayed start**

| The log says | What to do |
|---|---|
| The program offers no delayed start; choose '-' to start now | This program cannot start later. |
| ... is too early for this program; the earliest is ... | Choose that time or later. |
| ... is later than the appliance allows; using ... | Not an error: the latest possible time is used. |
| The time of the waiting program is not known yet; try again shortly | Wait a moment and try again. |
| The program waits for its delayed start; stop it and start again to start now | To start now, stop it and choose Start with "-". |

**Limits and Home Connect**

| The log says | What to do |
|---|---|
| Start or stop limit reached (5 per minute) | Home Connect allows 5 starts and 5 stops a minute; wait a minute. |
| Too many requests this minute | Wait the time named, or a minute. |
| Too many requests queued; try again in a minute | Wait a minute. |
| Daily API budget for commands used up; try again after ... | Wait until the time named ([How it works](internals.md#requests-and-the-daily-limit)). |
| Daily API budget used up; try again later | The plugin reached its daily limit; it recovers as the 24-hour window moves on. |
| Home Connect asked to wait ... / Home Connect asked to slow down | Home Connect answered "too many requests". Wait the time named. |
| Paused after repeated errors ... | The plugin pauses after several errors in a row. Wait the time named; look in the log for the errors before it. |
| Home Connect is not reachable; try again shortly | Check the internet connection of the Domoticz machine. |
| Not sent within ... s; dropped | The command could not be sent in time (for example during a reconnect). Give it again. |
| Not sent within ... s: the Start before it was not confirmed yet; choose Stop again | Choose Stop again. |
| Cancelled by Stop | Not an error: a waiting command was replaced by your Stop. |
| Start not sent: ... failed | A step before Start (program or option) failed; its own line says why. |
| Home Connect refused it (...) | Home Connect gave a reason the plugin has no text for. Search for the key in brackets, or open an issue. |

### The steam oven refuses a combination of steam and fast preheating

Some steam ovens do not allow added steam together with fast preheating. Switch one of them off
and try again.

## Still stuck?

Set **Debug level** to **Basic** ([Settings](settings.md#debug-level)), repeat what went wrong,
and open an issue on [GitHub](https://github.com/Rouzax/home-connect-domoticz-plugin/issues)
with the relevant log lines. Remove names and IDs you do not want to share.
